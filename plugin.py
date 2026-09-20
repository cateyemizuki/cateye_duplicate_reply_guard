"""cateye_duplicate_reply_guard —— 自回复与重复回复硬拦截。

解决两个由 Planner（决策模型）自主决策引发、而宿主默认**没有任何硬拦截**的问题：

1. **回复自己（self_reply）**：Planner 对 *bot 自己发出的消息* 调用 ``reply``。
   用户行为风格里通常写着"不回复自己的消息"，但那只是提示词约束，模型偶尔会违反。
2. **重复回复（duplicate_reply）**：Planner 在**同一个内部轮次的后继回合**里，对
   **同一条目标消息**再次调用 ``reply``。群里表现为"这条消息被回了两次"（实测
   两条内容不同、都带引用，所以不是"一条回复被重复下发"）。

为什么宿主既有机制拦不住（源码已核对）：

- ``reply`` 工具**不返回** ``pause_execution``（全项目仅 ``wait.py`` 会设置），
  所以回复成功后 ``_handle_planner_response_actions`` 落到
  ``CycleEnd("tool_continue")``（``src/maisaka/reasoning_engine.py:698``），
  ``round_index += 1`` 进入下一回合；下一回合上下文里已经有自己刚发的回复，
  但**没有任何"这条消息已经回过了"的状态**。
- ``reply.py:_find_recent_reply_to_target`` 只在 prompt 里加一句
  "你现在想再次回复这条消息，进行补充"——语义上**允许**补一条，不阻止发送。
- ``_should_replace_reasoning`` 相似度阈值 >0.9，实测不触发。
- ``MAX_INTERNAL_ROUNDS = 10`` 上限很宽，拦不到第 2 条。

本插件的拦截矩阵（两个功能 × 两个阶段，各自独立开关）：

============  ==========================================  ==========================================
阶段          触发点                                        动作
============  ==========================================  ==========================================
生成前        ``maisaka.planner.after_response``(BLOCKING)  从 ``output_items`` 里剔除命中的
                                                            ``reply`` 工具调用 → 该回合不产生回复，
                                                            也不消耗回复生成的 token
发送前兜底    ``send_service.before_send``(BLOCKING)        ``abort`` 本次下发 → 保证群里看不到
============  ==========================================  ==========================================

> 自回复拦截默认**只作用于已确认的群聊**（私聊豁免，见下方"会话类型"一条）；
> 重复回复拦截不受此限制，两个功能开关独立。

判定依据（都不需要任何 ctx 能力，全部零 RPC）：

- **自己的消息 ID**：① ``maisaka.planner.before_request`` 里扫描模型上下文，
  取出渲染为 ``is_self_message="true"`` 的 ``msg_id``（覆盖插件启动前的历史；
  这里拿到的正是模型**有可能选中**的那批 ID）；② ``send_service.after_send``
  观察本进程发出的消息，宿主已把平台消息 ID 回填进 ``message.message_id``
  （``src/services/send_service.py:691-711``），据此累积自身消息台账。
- **已回复过的目标**：``maisaka.reply.before_post_process`` 每次回复生成**恰好触发一次**
  （``reply.py:396-402``，生成成功且非空才会触发），用它记录本轮回复目标，
  并以"轮标记"把判定传给发送阶段——这样同一条回复被拆成多个分段时不会被误判成重复。
- **延时补充放行（默认开，阈值 30 秒）**：重复回复拦截只拦"短时间内的重复"。对**非自己**的目标消息，
  距上次回复已**达到或超过** ``late_repeat_after_seconds``（默认 30 秒，开关与时长均可配）时，
  本次回复按**补充说明**放行——与宿主 ``reply.py:_find_recent_reply_to_target``
  "你现在想再次回复这条消息，进行补充"的设计对齐。本项**只放宽、不会收紧**：阈值大于去重窗口时
  以去重窗口为准。自回复拦截不受影响（自己的消息恒拦）。
- **会话类型（私聊豁免）**：出站消息构建时**只有群聊才会填 ``message_info.group_info``**
  （``send_service.py:550-582``），私聊恒为 ``None``；该字段会随 Hook 载荷传过来，
  所以从 ``send_service.before_send`` / ``after_send`` 就能零 RPC 学到会话类型。
  自回复拦截**默认只作用于已确认的群聊**：宿主明确支持"补充说明你自己发送的消息"
  （``maisaka_generator_base.py:182-189``），而恋人插件的主动私聊正好是这种场景——
  那一轮没有用户消息可锚，回复目标自然落在 bot 自己上一条发言；群里回自己才是刷屏噪声。
  可用 ``self_reply_guard.exempt_private_chat = false`` 关闭豁免。重复回复拦截不受此影响。

可靠性说明：planner 层剔除后若该响应不再包含任何工具调用，宿主会走"无工具"分支
**立即结束本轮**（``_handle_planner_no_tool_retry`` 直接返回 ``should_end_after_no_tool=True``），
不存在重试风暴；而返回的 ``output_items`` 万一无法反序列化，宿主只记 warning 并忽略
（fail-open），不会把 bot 弄坏。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterator, List, Literal, Optional, Set, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF, ErrorPolicy, HookMode, HookOrder

from .intercept_log import InterceptLogger

SUPPORTED_CONFIG_VERSION = "1.1.0"

#: 被守护的内置工具名（只有 reply 会产出可见回复）。
GUARDED_TOOL_NAME = "reply"

#: 判定"轮标记"的有效时长：只用于把生成阶段的判定传递给紧随其后的发送阶段。
_BLOCKED_ROUND_TTL_SECONDS = 120.0

#: 单会话"已回复目标"记录上限（防长期驻留会话无限增长）。
_MAX_REPLIED_TARGETS_PER_SESSION = 500

#: 事件与阶段常量（同时是独立日志里的取值）。
FEATURE_SELF = "self_reply"
FEATURE_DUPLICATE = "duplicate_reply"
STAGE_PLANNER_STRIP = "planner_strip"
STAGE_ROUND_BLOCKED = "round_blocked"
STAGE_SEND_ABORT = "send_abort"

_MESSAGE_TAG_RE = re.compile(r"<message\b[^>]*>", re.IGNORECASE)
_MSG_ID_ATTR_RE = re.compile(r'msg_id="(-?\d+)"')
_SELF_ATTR_RE = re.compile(r'is_self_message="(?:true|1)"', re.IGNORECASE)


def _ui_i18n(zh_label: str, zh_hint: str, en_label: str, en_hint: str) -> Dict[str, Any]:
    """构造字段级 WebUI i18n 元数据（并入 ``json_schema_extra``）。

    WebUI 按界面语言读 ``field.i18n[locale]['label'/'hint']``，未命中时回退到字段
    的中文 ``label`` / ``description``。
    """

    return {
        "i18n": {
            "zh-CN": {"label": zh_label, "hint": zh_hint},
            "en": {"label": en_label, "hint": en_hint},
        }
    }


def _section_i18n(zh_title: str, zh_description: str, en_title: str, en_description: str) -> Dict[str, Any]:
    """构造配置节级 i18n 元数据（供 ``__ui_i18n__`` 使用）。"""

    return {
        "zh-CN": {"title": zh_title, "description": zh_description},
        "en": {"title": en_title, "description": en_description},
    }


def _iter_text_fragments(node: Any, depth: int = 0) -> Iterator[str]:
    """递归收集任意嵌套结构里的字符串（限深，容忍 Item schema 形态变化）。"""

    if depth > 8:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_text_fragments(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_text_fragments(value, depth + 1)


def _extract_self_message_ids(node: Any) -> Set[str]:
    """从模型上下文里取出渲染为 ``is_self_message="true"`` 的 msg_id 集合。

    宿主把 bot 自己的消息渲染成
    ``<message msg_id="100000001" quote="123456789" time="16:59:55" user="MyBot" is_self_message="true">``，
    这里按"标签内同时出现 msg_id 与 is_self_message"判定，不依赖属性顺序。
    """

    found: Set[str] = set()
    for fragment in _iter_text_fragments(node):
        if "is_self_message" not in fragment:
            continue
        for tag in _MESSAGE_TAG_RE.findall(fragment):
            if not _SELF_ATTR_RE.search(tag):
                continue
            matched = _MSG_ID_ATTR_RE.search(tag)
            if matched:
                found.add(matched.group(1))
    return found


# ----------------------------------------------------------------------
# 配置模型
# ----------------------------------------------------------------------


class PluginSectionConfig(PluginConfigBase):
    """插件总开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = _section_i18n(
        "插件", "插件总开关与配置版本。", "Plugin", "Plugin master switch and config version."
    )

    enabled: bool = Field(
        default=True,
        description="是否启用插件（总开关，关闭后所有拦截均不生效，仅保留配置）",
        json_schema_extra={
            "label": "启用插件",
            **_ui_i18n("启用插件", "总开关，关闭后所有拦截均不生效。", "Enabled", "Master switch; when off no interception happens."),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class SelfReplyGuardSectionConfig(PluginConfigBase):
    """回复自己（回复 bot 自身消息）的拦截配置。"""

    __ui_label__ = "自回复拦截"
    __ui_icon__ = "user-x"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = _section_i18n(
        "自回复拦截",
        "阻止 Planner 对 bot 自己发出的消息调用 reply。",
        "Self-reply guard",
        "Block the Planner from calling reply on the bot's own messages.",
    )

    enabled: bool = Field(
        default=True,
        description="是否启用自回复拦截",
        json_schema_extra={
            "label": "启用自回复拦截",
            **_ui_i18n("启用自回复拦截", "关闭后本功能完全不生效。", "Enable self-reply guard", "When off this guard is fully disabled."),
        },
    )
    strip_at_planner: bool = Field(
        default=True,
        description="生成前拦截：planner 响应里命中自身消息的 reply 调用直接从 output_items 中剔除（不消耗回复生成 token）",
        json_schema_extra={
            "label": "生成前剔除",
            **_ui_i18n(
                "生成前剔除",
                "推荐开启。命中的 reply 工具调用不会被生成，零 token 开销、无失败反馈。",
                "Strip before generation",
                "Recommended. The offending reply call is removed before generation: no tokens burned, no failure feedback.",
            ),
        },
    )
    abort_at_send: bool = Field(
        default=True,
        description="发送前兜底：真正下发前若目标仍是自身消息则中止本次发送",
        json_schema_extra={
            "label": "发送前兜底",
            **_ui_i18n(
                "发送前兜底",
                "推荐开启。可拦住生成前剔除没覆盖到的路径；代价是已生成的回复仍会消耗 token。",
                "Abort before send",
                "Recommended backstop for paths the planner stage misses; the reply is already generated, so tokens are spent.",
            ),
        },
    )
    scan_planner_context: bool = Field(
        default=True,
        description="从 planner 上下文解析自身消息 ID（覆盖插件启动前的历史消息，模型只能选上下文里出现过的 ID）",
        json_schema_extra={
            "label": "扫描上下文获取自身消息 ID",
            **_ui_i18n(
                "扫描上下文获取自身消息 ID",
                "每次 planner 请求解析一次上下文，覆盖插件启动前的历史。关掉后只依赖出站台账。",
                "Scan context for own message IDs",
                "Parses the planner context on every request so pre-startup history is covered. Off means outbound-ledger only.",
            ),
        },
    )
    track_outbound_ids: bool = Field(
        default=True,
        description="记录自身已发出消息的平台消息 ID（宿主在 after_send 时已回填）",
        json_schema_extra={
            "label": "记录自身出站消息 ID",
            **_ui_i18n(
                "记录自身出站消息 ID",
                "精确且零 RPC；可覆盖其它插件以 bot 身份发出的消息。",
                "Track own outbound message IDs",
                "Exact and RPC-free; also covers messages sent as the bot by other plugins.",
            ),
        },
    )
    exempt_private_chat: bool = Field(
        default=True,
        description="私聊豁免：私聊（如恋人插件的主动私聊）里不拦“回复自己”，只拦确认的群聊",
        json_schema_extra={
            "label": "私聊豁免",
            **_ui_i18n(
                "私聊豁免",
                "推荐开启。宿主明确支持“补充说明你自己发送的消息”，私聊里主动续话正是这个场景；"
                "群里回自己才是刷屏噪声。关闭后私聊也会拦。",
                "Exempt private chats",
                "Recommended. The host explicitly supports supplementing your own message, which is exactly the "
                "private-chat continuation pattern; only confirmed group chats are guarded when this is on.",
            ),
        },
    )
    id_ttl_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="自身消息 ID 的记录保留时长（秒）",
        json_schema_extra={
            "label": "自身消息 ID 保留时长（秒）",
            **_ui_i18n("自身消息 ID 保留时长（秒）", "过短会漏判较早的自身消息。", "Own message ID TTL (seconds)", "Too short may miss older own messages."),
        },
    )
    max_tracked_ids: int = Field(
        default=1000,
        ge=50,
        le=20000,
        description="单个会话最多记录多少条自身消息 ID",
        json_schema_extra={
            "label": "单会话记录上限",
            **_ui_i18n("单会话记录上限", "超出后淘汰最旧记录。", "Max tracked IDs per session", "Oldest entries are evicted beyond this limit."),
        },
    )


class DuplicateReplyGuardSectionConfig(PluginConfigBase):
    """重复回复（同一目标消息被回多次）的拦截配置。"""

    __ui_label__ = "重复回复拦截"
    __ui_icon__ = "copy-slash"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = _section_i18n(
        "重复回复拦截",
        "阻止在时间窗内对同一条目标消息重复调用 reply。",
        "Duplicate-reply guard",
        "Block repeated reply calls on the same target message within a time window.",
    )

    enabled: bool = Field(
        default=True,
        description="是否启用重复回复拦截",
        json_schema_extra={
            "label": "启用重复回复拦截",
            **_ui_i18n("启用重复回复拦截", "关闭后本功能完全不生效。", "Enable duplicate-reply guard", "When off this guard is fully disabled."),
        },
    )
    strip_at_planner: bool = Field(
        default=True,
        description="生成前拦截：planner 响应里重复指向同一目标的 reply 调用直接从 output_items 中剔除",
        json_schema_extra={
            "label": "生成前剔除",
            **_ui_i18n(
                "生成前剔除",
                "推荐开启。这是消除“同一条被回两次”的主手段，零 token 开销。",
                "Strip before generation",
                "Recommended primary fix for double replies; no tokens burned.",
            ),
        },
    )
    abort_at_send: bool = Field(
        default=True,
        description="发送前兜底：本轮被判定为重复回复时中止其下发",
        json_schema_extra={
            "label": "发送前兜底",
            **_ui_i18n(
                "发送前兜底",
                "建议开启；但若同时关掉“生成前剔除”，可能出现重复生成（见 README 风险章节）。",
                "Abort before send",
                "Keep on, but if strip-before-generation is off, repeated generation may occur (see README risks).",
            ),
        },
    )
    dedupe_window_seconds: int = Field(
        default=180,
        ge=10,
        le=3600,
        description="去重时间窗：同一条目标消息在该窗口内只允许回复一次（秒）",
        json_schema_extra={
            "label": "去重时间窗（秒）",
            **_ui_i18n(
                "去重时间窗（秒）",
                "窗口内同一目标只回一次；用户在你回复后又发新消息时目标是新消息，不受影响。",
                "Dedupe window (seconds)",
                "One reply per target inside the window. A new user message has a new ID and is unaffected.",
            ),
        },
    )
    allow_late_repeat: bool = Field(
        default=True,
        description="延时补充放行：对非自己的目标消息，距上次回复超过阈值后放行本次回复（视为补充说明）",
        json_schema_extra={
            "label": "允许延时补充",
            **_ui_i18n(
                "允许延时补充",
                "推荐开启。只拦短时间内的重复；隔了一会儿的「再补一句」放行，与宿主的补充回复设计一致。"
                "关闭后：去重窗口内一律不补。",
                "Allow late supplement",
                "Recommended. Only rapid repeats are blocked; a later follow-up is allowed, matching the host's "
                "supplement design. When off, nothing is allowed inside the dedupe window.",
            ),
        },
    )
    late_repeat_after_seconds: int = Field(
        default=30,
        ge=5,
        le=3600,
        description="延时补充阈值（秒）：距上次回复达到或超过该时长后，允许对同一非自身目标再次回复",
        json_schema_extra={
            "label": "延时补充阈值（秒）",
            **_ui_i18n(
                "延时补充阈值（秒）",
                "仅在开启「允许延时补充」时生效。窗口内、但距上次回复已超过该秒数 → 放行本次（按补充说明）。"
                "设得比去重窗口还大时以去重窗口为准（本项不会收紧拦截）。",
                "Late supplement threshold (seconds)",
                "Only used when late supplement is on. Inside the dedupe window but past this many seconds since the "
                "last reply -> this one is allowed as a supplement. Values above the dedupe window are clamped by the "
                "window itself; this option never tightens blocking.",
            ),
        },
    )


class InterceptLogSectionConfig(PluginConfigBase):
    """拦截行为独立日志配置。"""

    __ui_label__ = "拦截日志"
    __ui_icon__ = "file-text"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = _section_i18n(
        "拦截日志",
        "拦截行为写入插件 data_dir 下的独立日志文件，与宿主主日志分离。",
        "Intercept log",
        "Interception events go to a dedicated file under the plugin data_dir, separate from the host log.",
    )

    enabled: bool = Field(
        default=True,
        description="是否记录拦截行为到独立日志文件",
        json_schema_extra={
            "label": "启用独立日志",
            **_ui_i18n("启用独立日志", "关闭后拦截仍生效，只是不再写独立文件。", "Enable dedicated log", "When off, interception still works but nothing is written to the file."),
        },
    )
    file_name: str = Field(
        default="intercept.jsonl",
        description="日志文件名（位于 data/plugins/<插件ID>/ 下）",
        json_schema_extra={
            "label": "日志文件名",
            **_ui_i18n("日志文件名", "JSON Lines 格式，一行一条拦截记录。", "Log file name", "JSON Lines format, one interception record per line."),
        },
    )
    max_file_size_kb: int = Field(
        default=1024,
        ge=16,
        le=102400,
        description="单个日志文件大小上限（KB），超过后轮转",
        json_schema_extra={
            "label": "单文件大小上限（KB）",
            **_ui_i18n("单文件大小上限（KB）", "超过后轮转为 .1、.2 …。", "Max file size (KB)", "Rotates to .1, .2 ... once exceeded."),
        },
    )
    backup_count: int = Field(
        default=3,
        ge=0,
        le=20,
        description="轮转保留的历史文件份数（0 = 直接截断）",
        json_schema_extra={
            "label": "历史文件份数",
            **_ui_i18n("历史文件份数", "0 表示不保留历史，直接截断当前文件。", "Backup files", "0 truncates the current file instead of keeping history."),
        },
    )
    echo_to_main_log: bool = Field(
        default=False,
        description="是否同时把拦截记录打到宿主主日志",
        json_schema_extra={
            "label": "同时输出到主日志",
            **_ui_i18n("同时输出到主日志", "默认关闭；开启便于在主日志里直接看到拦截。", "Echo to main log", "Off by default; enable to see interceptions in the host log."),
        },
    )
    echo_level: Literal["debug", "info", "warning"] = Field(
        default="info",
        description="输出到主日志时使用的级别",
        json_schema_extra={
            "label": "主日志级别",
            **_ui_i18n("主日志级别", "仅在开启「同时输出到主日志」时生效。", "Echo level", "Only used when echo-to-main-log is on."),
        },
    )


class DuplicateReplyGuardConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    self_reply_guard: SelfReplyGuardSectionConfig = Field(default_factory=SelfReplyGuardSectionConfig)
    duplicate_reply_guard: DuplicateReplyGuardSectionConfig = Field(default_factory=DuplicateReplyGuardSectionConfig)
    intercept_log: InterceptLogSectionConfig = Field(default_factory=InterceptLogSectionConfig)


# ----------------------------------------------------------------------
# 插件
# ----------------------------------------------------------------------


class DuplicateReplyGuardPlugin(MaiBotPlugin):
    """自回复与重复回复拦截插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = DuplicateReplyGuardConfig

    def __init__(self) -> None:
        # 必须先初始化 SDK 基类状态（_ctx / 配置实例等），否则激活时 get_components() 会崩。
        super().__init__()
        # 自身消息 ID 台账：session_id -> {msg_id: 过期单调时刻}。
        self._self_ids: Dict[str, Dict[str, float]] = {}
        # 已回复目标台账：session_id -> {target_msg_id: 最近一次判定时刻}。
        self._replied_targets: Dict[str, Dict[str, float]] = {}
        # 会话类型台账：session_id -> 是否群聊（从出站载荷的 message_info.group_info 学到，零 RPC）。
        self._session_is_group: Dict[str, bool] = {}
        # 轮标记（生成阶段判定 → 发送阶段消费）：session_id -> {target: (过期时刻, 功能)}。
        self._blocked_rounds: Dict[str, Dict[str, Tuple[float, str]]] = {}
        # 拦截独立日志写入器。
        self._intercept_log: Optional[InterceptLogger] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild_intercept_log()
        log_path = self._intercept_log.path if self._intercept_log is not None else "未启用"
        self.ctx.logger.info(
            "插件已加载。总开关=%s；自回复拦截=%s（生成前剔除=%s / 发送前兜底=%s / 上下文扫描=%s / 出站台账=%s）；"
            "重复回复拦截=%s（生成前剔除=%s / 发送前兜底=%s / 去重窗口=%s 秒 / 延时补充=%s%s）；拦截独立日志=%s",
            self.config.plugin.enabled,
            self.config.self_reply_guard.enabled,
            self.config.self_reply_guard.strip_at_planner,
            self.config.self_reply_guard.abort_at_send,
            self.config.self_reply_guard.scan_planner_context,
            self.config.self_reply_guard.track_outbound_ids,
            self.config.duplicate_reply_guard.enabled,
            self.config.duplicate_reply_guard.strip_at_planner,
            self.config.duplicate_reply_guard.abort_at_send,
            self.config.duplicate_reply_guard.dedupe_window_seconds,
            "开" if self.config.duplicate_reply_guard.allow_late_repeat else "关",
            f"（阈值 {self.config.duplicate_reply_guard.late_repeat_after_seconds} 秒）"
            if self.config.duplicate_reply_guard.allow_late_repeat
            else "",
            log_path,
        )

    async def on_unload(self) -> None:
        if self._intercept_log is not None:
            self._intercept_log.close()
            self._intercept_log = None
        self.ctx.logger.info("插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self._rebuild_intercept_log()
        self.ctx.logger.info(
            "插件配置已更新：自回复拦截=%s，重复回复拦截=%s（去重窗口=%s 秒，延时补充=%s%s）",
            self.config.self_reply_guard.enabled,
            self.config.duplicate_reply_guard.enabled,
            self.config.duplicate_reply_guard.dedupe_window_seconds,
            "开" if self.config.duplicate_reply_guard.allow_late_repeat else "关",
            f"（阈值 {self.config.duplicate_reply_guard.late_repeat_after_seconds} 秒）"
            if self.config.duplicate_reply_guard.allow_late_repeat
            else "",
        )

    # ------------------------------------------------------------------
    # Hook 1：记录本会话里"哪些消息是 bot 自己发的"（生成前判定依据）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="self_message_id_scanner",
        description="扫描 planner 上下文，记录渲染为 is_self_message 的 msg_id（模型可能选中的自身消息全集）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> None:
        """只读扫描，不改写任何 kwargs。"""

        try:
            if not self._self_guard_enabled() or not self.config.self_reply_guard.scan_planner_context:
                return
            session_id = self._session_id_of(kwargs)
            if not session_id:
                return
            items = kwargs.get("items")
            if items is None:
                return
            found = _extract_self_message_ids(items)
            if not found:
                return
            self._remember_ids(session_id, found)
        except Exception as exc:  # noqa: BLE001 - 自身消息扫描绝不能影响 planner
            self.ctx.logger.warning("自身消息 ID 扫描失败，已跳过本轮: %s", exc)

    # ------------------------------------------------------------------
    # Hook 2：生成前剔除（主拦截）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.after_response",
        name="planner_reply_stripper",
        description="planner 响应里命中自回复/重复回复的 reply 工具调用直接从 output_items 中剔除（生成前拦截）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_after_response(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """剔除违规的 reply 工具调用；无改动时返回 None（保持宿主原始 Item 与 replay 引用）。"""

        try:
            if not self._plugin_enabled():
                return None
            output_items = kwargs.get("output_items")
            if not isinstance(output_items, list) or not output_items:
                return None
            session_id = self._session_id_of(kwargs)
            if not session_id:
                return None

            now = time.monotonic()
            kept: List[Any] = []
            removals: List[Dict[str, Any]] = []
            # 同一次响应内已被放行的目标：用于拦住"同一批次里对同一目标并行调用两次 reply"。
            accepted_targets: Set[str] = set()

            for item in output_items:
                tool_call = self._reply_tool_call_of(item)
                if tool_call is None:
                    kept.append(item)
                    continue
                call_id, target_id = tool_call
                feature = self._classify_target(session_id, target_id, now)
                if not feature and target_id and target_id in accepted_targets:
                    # 同一响应内重复指向同一目标：按重复回复处理。
                    feature = FEATURE_DUPLICATE
                if not feature:
                    if target_id:
                        accepted_targets.add(target_id)
                    kept.append(item)
                    continue
                if not self._strip_active(feature):
                    # 生成前剔除被关闭：放行，交给发送前兜底（仍未放行则不改写 items）。
                    if target_id:
                        accepted_targets.add(target_id)
                    kept.append(item)
                    continue
                removals.append({"feature": feature, "call_id": call_id, "target_msg_id": target_id})

            if not removals:
                return None

            for removal in removals:
                self._record_intercept(
                    event=removal["feature"],
                    stage=STAGE_PLANNER_STRIP,
                    session_id=session_id,
                    target_msg_id=removal["target_msg_id"],
                    call_id=removal["call_id"],
                    detail=self._describe(removal["feature"], session_id, removal["target_msg_id"], now, stage=STAGE_PLANNER_STRIP),
                )

            modified = dict(kwargs)
            modified["output_items"] = kept
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as exc:  # noqa: BLE001 - 拦截失败必须 fail-open
            self.ctx.logger.warning("生成前剔除失败，本轮放弃拦截（fail-open）: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Hook 3：记录本轮回复目标 + 标记违规轮（生成后、发送前）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="reply_round_marker",
        description="每次回复生成恰好触发一次：记录本轮目标，并标记自回复/重复回复轮供发送前兜底消费",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_before_post_process(self, **kwargs: Any) -> None:
        """只读判定 + 写入轮标记，不改写 kwargs。"""

        try:
            if not self._plugin_enabled():
                return
            session_id = self._session_id_of(kwargs)
            target_id = self._target_id_of(kwargs)
            if not session_id or not target_id:
                return

            now = time.monotonic()
            feature = self._classify_target(session_id, target_id, now)
            if feature:
                self._mark_blocked_round(session_id, target_id, feature, now)
                self._record_intercept(
                    event=feature,
                    stage=STAGE_ROUND_BLOCKED,
                    session_id=session_id,
                    target_msg_id=target_id,
                    detail=self._describe(feature, session_id, target_id, now, stage=STAGE_ROUND_BLOCKED),
                )
                return

            # 合法轮：登记目标（供后续轮次判定重复），并清掉该目标可能残留的旧轮标记。
            self._remember_replied_target(session_id, target_id, now)
            self._clear_blocked_round(session_id, target_id)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回复轮标记失败，已跳过: %s", exc)

    # ------------------------------------------------------------------
    # Hook 4：发送前兜底（最后一道闸）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.before_send",
        name="reply_send_abort",
        description="下发前最终校验：自回复直接中止；被标记为重复回复的轮次整体中止",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_send(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """命中即 ``abort``；否则返回 None 放行。"""

        try:
            if not self._plugin_enabled():
                return None
            message = kwargs.get("message")
            session_id = ""
            if isinstance(message, dict):
                session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                session_id = self._session_id_of(kwargs)
            target_id = self._target_id_of(kwargs)
            if not session_id or not target_id:
                return None
            # 先学会话类型（出站载荷里群聊才有 group_info），再做判定——包括被中止的那条也要学到。
            self._learn_chat_type(session_id, message)

            now = time.monotonic()
            # 本阶段**只认生成阶段留下的轮标记**，两个功能都走同一套判定。
            #
            # 为什么不在这里直接用 reply_message_id 判自回复：宿主分段发送时，
            # 非首段可能引用"上一条已发送分段"（`reply.py:_resolve_segment_reply_context`
            # 的 quote_previous 分支），此时 reply_message_id 是 **bot 自己的消息**——
            # 直接判自回复会把正常的错别字更正段整段误杀。
            # 轮标记由 `maisaka.reply.before_post_process` 用 reply 工具的真实目标写入，
            # 而首段的 reply_message_id 恒等于该目标，所以首个分段命中即中止整轮下发
            # （`reply.py:488-498`：sent=False 直接跳出分段循环）。
            marked = self._blocked_round(session_id, target_id, now)
            if marked is None:
                return None
            # 轮标记是 (过期时刻, 功能名)，功能名在下标 1。
            feature = marked[1]
            if feature == FEATURE_SELF:
                if not self.config.self_reply_guard.abort_at_send:
                    return None
                if not self._self_reply_guard_applies(session_id):
                    # 私聊豁免：生成阶段若因类型未知而误标，这里补一次纠正。
                    self._clear_blocked_round(session_id, target_id)
                    return None
            if feature == FEATURE_DUPLICATE and not self.config.duplicate_reply_guard.abort_at_send:
                return None
            self._clear_blocked_round(session_id, target_id)

            self._record_intercept(
                event=feature,
                stage=STAGE_SEND_ABORT,
                session_id=session_id,
                target_msg_id=target_id,
                detail=self._describe(feature, session_id, target_id, now, stage=STAGE_SEND_ABORT),
            )
            abort_message = f"{feature} 拦截：目标消息 {target_id}（会话 {session_id}）"
            return {"action": "abort", "abort_message": abort_message}
        except Exception as exc:  # noqa: BLE001 - 兜底失败不得阻断正常发送
            self.ctx.logger.warning("发送前兜底校验失败，本条按放行处理: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Hook 5：累积自身消息 ID 台账（observe）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_send",
        name="outbound_id_recorder",
        description="observe：记录本进程成功发出的消息平台 ID，作为“这是 bot 自己的消息”的精确依据",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_send(self, **kwargs: Any) -> None:
        """纯记账。"""

        try:
            if not self._self_guard_enabled() or not self.config.self_reply_guard.track_outbound_ids:
                return
            if not bool(kwargs.get("sent", False)):
                return
            message = kwargs.get("message")
            if not isinstance(message, dict):
                return
            session_id = str(message.get("session_id") or "").strip()
            message_id = str(message.get("message_id") or "").strip()
            if not session_id or not message_id:
                return
            self._learn_chat_type(session_id, message)
            self._remember_ids(session_id, {message_id})
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("自身出站消息 ID 记录失败，已跳过: %s", exc)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def _plugin_enabled(self) -> bool:
        return bool(self.config.plugin.enabled)

    def _self_guard_enabled(self) -> bool:
        return self._plugin_enabled() and bool(self.config.self_reply_guard.enabled)

    def _duplicate_guard_enabled(self) -> bool:
        return self._plugin_enabled() and bool(self.config.duplicate_reply_guard.enabled)

    def _strip_active(self, feature: str) -> bool:
        if feature == FEATURE_SELF:
            return bool(self.config.self_reply_guard.strip_at_planner)
        return bool(self.config.duplicate_reply_guard.strip_at_planner)

    def _classify_target(self, session_id: str, target_id: str, now: float) -> str:
        """返回命中的拦截功能名；空串表示放行。"""

        if not session_id or not target_id:
            return ""
        if (
            self._self_guard_enabled()
            and self._self_reply_guard_applies(session_id)
            and self._is_self_message(session_id, target_id, now)
        ):
            return FEATURE_SELF
        if self._duplicate_guard_enabled() and self._is_recent_reply_target(session_id, target_id, now):
            return FEATURE_DUPLICATE
        return ""

    def _self_reply_guard_applies(self, session_id: str) -> bool:
        """自回复拦截是否作用于该会话（私聊豁免）。

        私聊里"补充说明你自己发送的消息"是宿主的**设计内行为**
        （``maisaka_generator_base.py:182-189`` 专门为它写了提示词），而恋人插件的主动私聊
        正是这种场景：这一轮没有用户消息可锚，回复目标自然落在 bot 自己上一条发言。
        因此默认只拦**已确认的群聊**；会话类型未知时按放行处理（fail-open，宁可不拦也不误杀）。

        会话类型来自出站载荷的 ``message_info.group_info``：宿主构建出站消息时
        **只有群聊才会填 group_info**（``send_service.py:550-582``），私聊恒为 None。
        而"回复自己"这种情况必然以 bot 在该会话发过消息为前提，所以判定时类型通常已经学到。
        """

        if not bool(self.config.self_reply_guard.exempt_private_chat):
            return True
        return self._session_is_group.get(session_id) is True

    def _learn_chat_type(self, session_id: str, raw_message: Any) -> None:
        """从出站消息载荷学习会话类型（群聊 / 私聊）。"""

        if not session_id or not isinstance(raw_message, dict):
            return
        message_info = raw_message.get("message_info")
        if not isinstance(message_info, dict):
            return
        self._session_is_group[session_id] = bool(message_info.get("group_info"))

    def _is_self_message(self, session_id: str, target_id: str, now: float) -> bool:
        entries = self._self_ids.get(session_id)
        if not entries:
            return False
        expire_at = entries.get(target_id)
        if expire_at is None:
            return False
        if expire_at <= now:
            entries.pop(target_id, None)
            return False
        return True

    def _is_recent_reply_target(self, session_id: str, target_id: str, now: float) -> bool:
        """该目标是否属于"短期内已回复过"（命中才拦；延时补充放行返回 False）。"""

        entries = self._replied_targets.get(session_id)
        if not entries:
            return False
        last_at = entries.get(target_id)
        if last_at is None:
            return False
        elapsed = now - last_at
        if elapsed > float(self.config.duplicate_reply_guard.dedupe_window_seconds):
            return False
        # 去重窗口内、但已过延时补充阈值 → 视为"补充说明"，放行本次回复。
        if self._late_repeat_grace_applies(elapsed):
            return False
        return True

    def _late_repeat_grace_applies(self, elapsed: float) -> bool:
        """延时补充放行是否生效（距上次回复达到阈值即放行；只放宽拦截，不会收紧）。"""

        config = self.config.duplicate_reply_guard
        if not bool(config.allow_late_repeat):
            return False
        return elapsed >= float(config.late_repeat_after_seconds)

    def _describe(self, feature: str, session_id: str, target_id: str, now: float, *, stage: str) -> str:
        """构造人类可读的拦截说明（写入独立日志）。"""

        if feature == FEATURE_SELF:
            base = f"目标消息 {target_id} 是 bot 自己发出的消息（命中自身消息台账），按自回复拦截"
        else:
            config = self.config.duplicate_reply_guard
            window = int(config.dedupe_window_seconds)
            last_at = (self._replied_targets.get(session_id) or {}).get(target_id)
            if last_at is not None:
                elapsed = max(0, int(now - last_at))
                if bool(config.allow_late_repeat):
                    grace = int(config.late_repeat_after_seconds)
                    base = (
                        f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过"
                        f"（上次 {elapsed} 秒前，未达到 {grace} 秒延时补充阈值），按重复回复拦截"
                    )
                else:
                    base = (
                        f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过"
                        f"（上次 {elapsed} 秒前，延时补充放行已关闭），按重复回复拦截"
                    )
            else:
                base = f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过，按重复回复拦截"
        stage_text = {
            STAGE_PLANNER_STRIP: "已在生成前剔除该 reply 工具调用（未消耗回复生成 token）",
            STAGE_ROUND_BLOCKED: "回复已生成，已标记本轮为拦截轮，其下发将被中止",
            STAGE_SEND_ABORT: "已在下发前中止本次发送（平台侧不会看到该消息）",
        }.get(stage, "")
        return f"{base}；{stage_text}" if stage_text else base

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    def _remember_ids(self, session_id: str, message_ids: Set[str]) -> None:
        """登记自身消息 ID（带过期时间与容量淘汰）。"""

        now = time.monotonic()
        ttl = float(self.config.self_reply_guard.id_ttl_seconds)
        cap = int(self.config.self_reply_guard.max_tracked_ids)
        entries = self._self_ids.setdefault(session_id, {})
        for message_id in message_ids:
            if message_id:
                entries[message_id] = now + ttl
        self._prune_expiring(entries, now, cap)

    def _remember_replied_target(self, session_id: str, target_id: str, now: float) -> None:
        """登记"这个目标已经被回复过"。"""

        entries = self._replied_targets.setdefault(session_id, {})
        entries[target_id] = now
        window = float(self.config.duplicate_reply_guard.dedupe_window_seconds)
        expired = [key for key, last_at in entries.items() if now - last_at > window]
        for key in expired:
            entries.pop(key, None)
        if len(entries) > _MAX_REPLIED_TARGETS_PER_SESSION:
            for key, _ in sorted(entries.items(), key=lambda pair: pair[1])[
                : len(entries) - _MAX_REPLIED_TARGETS_PER_SESSION
            ]:
                entries.pop(key, None)

    def _mark_blocked_round(self, session_id: str, target_id: str, feature: str, now: float) -> None:
        rounds = self._blocked_rounds.setdefault(session_id, {})
        rounds[target_id] = (now + _BLOCKED_ROUND_TTL_SECONDS, feature)
        expired = [key for key, value in rounds.items() if value[0] <= now]
        for key in expired:
            rounds.pop(key, None)

    def _clear_blocked_round(self, session_id: str, target_id: str) -> None:
        rounds = self._blocked_rounds.get(session_id)
        if rounds:
            rounds.pop(target_id, None)

    def _blocked_round(self, session_id: str, target_id: str, now: float) -> Optional[Tuple[float, str]]:
        rounds = self._blocked_rounds.get(session_id)
        if not rounds:
            return None
        value = rounds.get(target_id)
        if value is None:
            return None
        if value[0] <= now:
            rounds.pop(target_id, None)
            return None
        return value

    @staticmethod
    def _prune_expiring(entries: Dict[str, float], now: float, cap: int) -> None:
        expired = [key for key, expire_at in entries.items() if expire_at <= now]
        for key in expired:
            entries.pop(key, None)
        if cap > 0 and len(entries) > cap:
            for key, _ in sorted(entries.items(), key=lambda pair: pair[1])[: len(entries) - cap]:
                entries.pop(key, None)

    # ------------------------------------------------------------------
    # 载荷解析
    # ------------------------------------------------------------------

    @staticmethod
    def _session_id_of(kwargs: Dict[str, Any]) -> str:
        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            return session_id
        message = kwargs.get("message")
        if isinstance(message, dict):
            return str(message.get("session_id") or "").strip()
        return ""

    @staticmethod
    def _target_id_of(kwargs: Dict[str, Any]) -> str:
        """取"被回复的目标消息 ID"：reply 轮用 reply_message_id，发送阶段用 reply_message_id。"""

        for key in ("reply_message_id", "target_message_id"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _reply_tool_call_of(item: Any) -> Optional[Tuple[str, str]]:
        """从单个输出 Item 里取出 ``reply`` 工具调用；不是 reply 调用时返回 None。

        Item 形态（``src/llm_models/request_snapshot.py:329-335``）::

            {"item_type": "FunctionCallItem",
             "meta": {...},
             "tool_call": {"call_id": "...", "func_name": "reply", "args": {"msg_id": "..."}, ...}}
        """

        if not isinstance(item, dict):
            return None
        if str(item.get("item_type") or "") != "FunctionCallItem":
            return None
        tool_call = item.get("tool_call")
        if not isinstance(tool_call, dict):
            return None
        func_name = str(tool_call.get("func_name") or "").strip()
        if func_name != GUARDED_TOOL_NAME:
            return None
        arguments = tool_call.get("args")
        target_id = ""
        if isinstance(arguments, dict):
            target_id = str(arguments.get("msg_id") or "").strip()
        return str(tool_call.get("call_id") or ""), target_id

    # ------------------------------------------------------------------
    # 独立日志
    # ------------------------------------------------------------------

    def _rebuild_intercept_log(self) -> None:
        """按当前配置（重新）构造拦截独立日志写入器。"""

        if self._intercept_log is not None:
            self._intercept_log.close()
            self._intercept_log = None
        if not self.config.intercept_log.enabled:
            return
        data_dir = getattr(self.ctx.paths, "data_dir", None)
        if not data_dir:
            self.ctx.logger.warning("ctx.paths.data_dir 不可用，拦截独立日志已禁用")
            return
        self._intercept_log = InterceptLogger(
            directory=Path(str(data_dir)),
            file_name=str(self.config.intercept_log.file_name or "intercept.jsonl"),
            max_bytes=int(self.config.intercept_log.max_file_size_kb) * 1024,
            backup_count=int(self.config.intercept_log.backup_count),
            on_error=lambda message: self.ctx.logger.warning("%s", message),
        )

    def _record_intercept(
        self,
        *,
        event: str,
        stage: str,
        session_id: str,
        target_msg_id: str,
        detail: str,
        call_id: str = "",
    ) -> None:
        """写一条拦截记录：独立日志 + （可选）主日志回显。"""

        if self._intercept_log is not None:
            self._intercept_log.record(
                event,
                stage=stage,
                session_id=session_id,
                target_msg_id=target_msg_id,
                call_id=call_id,
                detail=detail,
            )
        if not self.config.intercept_log.echo_to_main_log:
            return
        level = getattr(logging, str(self.config.intercept_log.echo_level or "info").upper(), logging.INFO)
        self.ctx.logger.log(
            level,
            "[拦截] %s / %s 会话=%s 目标消息=%s %s",
            event,
            stage,
            session_id,
            target_msg_id,
            detail,
        )


def create_plugin() -> DuplicateReplyGuardPlugin:
    """Runner 加载入口（宿主按此工厂函数实例化插件）。"""

    return DuplicateReplyGuardPlugin()
