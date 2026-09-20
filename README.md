# 自回复与重复回复拦截（cateye_duplicate_reply_guard）

MaiBot 插件：**生成前剔除 + 发送前中止**两层硬拦截，解决两个由 Planner（决策模型）自主决策引发、
而宿主默认没有任何硬拦截的问题：

| 功能 | 现象 | 默认 |
|---|---|---|
| **自回复拦截** | Planner 对 **bot 自己发出的消息** 调用 `reply`（行为风格里写着"不回复自己的消息"，但那是提示词约束，模型偶尔会违反）。**默认只作用群聊**，私聊豁免 | 开（私聊豁免开） |
| **重复回复拦截** | Planner 在**同一个内部轮次的后继回合**里，对**同一条目标消息**再次调用 `reply`，群里看到"这条消息被回了两次" | 开（去重窗口 180 秒） |

两个功能各自独立开关，四个动作（2 功能 × 2 阶段）也可分别关闭。

> **私聊为什么豁免**：宿主 `maisaka_generator_base.py:182-189` 明确支持"补充说明你自己发送的消息"
> （目标是 bot 自己时，replyer 提示词会换成"不要把你自己的发言当成别人的发言"）。恋人插件
> （mai-love）的**主动私聊**正是这个场景——那一轮没有被回复的用户消息，回复目标自然落在
> bot 自己上一条发言上。此时拦截会把主动续话**整条吞掉**，所以在私聊里默认放行；
> 群里回自己才是刷屏噪声。可用 `self_reply_guard.exempt_private_chat = false` 关闭豁免。

---

## 1. 为什么需要这个插件

线上实测（`logs/app_20260920_16*.jsonl`，17 分钟日志里命中 2 次）：

| 会话 | 目标消息 | 第 1 次回复 | 第 2 次回复 | 间隔 |
|---|---|---|---|---|
| 群 A | `123456789`（视频分享） | 16:59:56「兔子刚看完，挺可爱的捏」 | 17:00:09「兔子刷到自己了」 | 13 秒 |
| 群 B | `987654321`（图片） | 16:56:38「脚臭俩字一响…」 | 16:57:15「等等还得是专业对口…」 | 37 秒 |

两条内容**不同**、都带引用，所以不是"一条回复被重复下发"，而是 Planner 真的调用了两次 `reply`
（两次工具调用的 `msg_id` 相同、`call_id` 不同）。模型自己在第 3 回合的推理里也承认了
"机器人已经连续回复了两条"。

宿主既有机制为什么拦不住（源码已核对）：

- `reply` 工具**不返回** `pause_execution`（全项目仅 `wait.py` 会设），所以回复成功后
  `_handle_planner_response_actions` 落到 `CycleEnd("tool_continue")`
  （`src/maisaka/reasoning_engine.py:698`），`round_index += 1` 进入下一回合；
  下一回合上下文里已经有自己刚发的回复，但**没有任何"这条消息已经回过了"的状态**。
- `reply.py:_find_recent_reply_to_target` 只在 prompt 里加一句
  "你现在想再次回复这条消息，进行补充"——语义上**允许**补一条，不阻止发送。
- `_should_replace_reasoning` 相似度阈值 >0.9，实测不触发。
- `MAX_INTERNAL_ROUNDS = 10` 上限很宽，拦不到第 2 条。

---

## 2. 拦截矩阵

| 功能 | 阶段 | 触发点 | 动作 |
|---|---|---|---|
| 两个功能 | **生成前**（主拦截） | `maisaka.planner.after_response`（BLOCKING） | 从 `output_items` 里**剔除**命中的 `reply` 工具调用 → 该回合不产生回复，**不消耗回复生成的 token** |
| 两个功能 | **发送前**（兜底） | `send_service.before_send`（BLOCKING，`order=LATE`） | `abort` 本次下发 → 保证平台侧看不到 |

生成前剔除如果让该响应不再包含任何工具调用，宿主会走"无工具"分支**立即结束本轮**
（`_handle_planner_no_tool_retry` 直接返回 `should_end_after_no_tool=True`），**不存在重试风暴**；
万一返回的 `output_items` 无法反序列化，宿主只记 warning 并忽略（fail-open），不会把 bot 弄坏。

---

## 3. 判定依据（零能力依赖，全部本地判定）

| 判定 | 来源 | 说明 |
|---|---|---|
| **这是 bot 自己的消息** | ① `maisaka.planner.before_request` 扫描模型上下文，取出渲染为 `is_self_message="true"` 的 `msg_id` | 覆盖插件启动前的历史；拿到的正是模型**有可能选中**的那批 ID |
| | ② `send_service.after_send` 观察本进程发出的消息 | 宿主已把平台消息 ID 回填进 `message.message_id`（`src/services/send_service.py:691-711`），精确且零 RPC；也能覆盖**其它插件以 bot 身份**发出的消息 |
| **这个目标已经回复过** | `maisaka.reply.before_post_process` | 每次回复生成**恰好触发一次**（`reply.py:396-402`，生成成功且非空才触发），用它记录本轮回复目标 |
| **会话类型（私聊豁免）** | 出站载荷里的 `message_info.group_info` | 宿主构建出站消息时**只有群聊才填 `group_info`**（`src/services/send_service.py:550-582`），私聊恒为 `None`。该字段随 Hook 载荷传过来，所以 `send_service.before_send` / `after_send` 里零 RPC 就能学到会话类型。自回复拦截**默认只作用已确认的群聊**，类型未知时按放行处理（fail-open，宁可不拦也不误杀） |

**为什么要用"轮标记"而不是在发送阶段直接比对目标 ID**：宿主分段发送时，非首段可能引用
"上一条已发送分段"（`reply.py:_resolve_segment_reply_context` 的 `quote_previous` 分支），
此时 `reply_message_id` 是 **bot 自己的消息**——直接拿它判自回复会把正常的错别字更正段整段误杀。
轮标记由 `before_post_process` 用 reply 工具的**真实目标**写入，而首段的 `reply_message_id`
恒等于该目标，所以首个分段命中即中止整轮下发（`reply.py:488-498`：`sent=False` 直接跳出分段循环）。

> 本插件**不需要任何 ctx 能力**（manifest `capabilities` 为空），只用 Hook + `ctx.logger` / `ctx.paths` 两个辅助对象。

---

## 4. 安装与版本要求

- MaiBot **≥ 1.2.3**（只用 1.2.3 已有的 Hook 与组件装饰器，无需更高版本）
- maibot-plugin-sdk **≥ 2.8.0**
- 依赖：无 Python 包依赖

把 `cateye_duplicate_reply_guard` 整个目录放进 MaiBot 的 `plugins/` 下，重启 MaiBot（或按 WebUI 提示重载插件）。
`config.toml` 由 Runner 按配置模型自动生成，**不要手工创建**（含 BOM 会导致 TOML 解析失败）。

---

## 5. 配置说明

配置分 4 节，全部字段都带 **zh-CN / en 双语翻译**（WebUI 按界面语言显示 `label` 与 `hint`）。

### `[plugin]` 插件

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关闭后所有拦截均不生效 |
| `config_version` | str | `1.0.0` | 配置版本（UI 隐藏，勿改） |

### `[self_reply_guard]` 自回复拦截

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用自回复拦截 |
| `strip_at_planner` | bool | `true` | 生成前剔除（推荐；零 token、无失败反馈） |
| `abort_at_send` | bool | `true` | 发送前兜底 |
| `exempt_private_chat` | bool | `true` | **私聊豁免**：只拦已确认的群聊。关闭后私聊也拦（会把恋人插件的主动续话吞掉，不建议） |
| `scan_planner_context` | bool | `true` | 从 planner 上下文解析自身消息 ID |
| `track_outbound_ids` | bool | `true` | 记录自身出站消息的平台消息 ID |
| `id_ttl_seconds` | int | `3600` | 自身消息 ID 保留时长（60–86400） |
| `max_tracked_ids` | int | `1000` | 单会话最多记录多少条（50–20000） |

### `[duplicate_reply_guard]` 重复回复拦截

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用重复回复拦截 |
| `strip_at_planner` | bool | `true` | 生成前剔除（推荐） |
| `abort_at_send` | bool | `true` | 发送前兜底 |
| `dedupe_window_seconds` | int | `180` | 去重时间窗：同一目标在该窗口内只允许回复一次（10–3600） |

> 用户在你回复**之后**又发新消息时，目标是**新的消息 ID**，不受去重窗口影响——
> "回复 → 用户又说了一句 → 再回复"依然成立。

### `[intercept_log]` 拦截日志

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否写独立日志文件 |
| `file_name` | str | `intercept.jsonl` | 文件名（位于插件 `data_dir` 下） |
| `max_file_size_kb` | int | `1024` | 单文件上限，超过后轮转（16–102400） |
| `backup_count` | int | `3` | 保留的历史份数（0 = 直接截断） |
| `echo_to_main_log` | bool | `false` | 是否同时打到宿主主日志 |
| `echo_level` | enum | `info` | 回显级别（`debug`/`info`/`warning`） |

**只想观察不拦截**：把两个功能的 `strip_at_planner` 与 `abort_at_send` 都关掉即可——
拦截判定仍会执行并写日志（`round_blocked` 记录仍在），只是不产生实际动作。

---

## 6. 拦截独立日志

拦截行为**不复用宿主主日志**，按 JSON Lines 追加写入插件自己的数据目录：

```
data/plugins/github.cateye.duplicate-reply-guard/intercept.jsonl
```

一行一条记录，字段：

| 字段 | 取值 |
|---|---|
| `time` / `ts` | 本地时间 / Unix 时间戳 |
| `event` | `self_reply` ｜ `duplicate_reply` |
| `stage` | `planner_strip` ｜ `round_blocked` ｜ `send_abort` |
| `session_id` | 会话 ID |
| `target_msg_id` | 被回复的目标消息 ID |
| `call_id` | 命中时对应的 reply 工具 `call_id`（仅 `planner_strip`） |
| `detail` | 人类可读的说明（含上次回复距今秒数） |

真实样例：

```json
{"time": "2026-09-20 20:14:14", "ts": 1789906454.572, "event": "self_reply", "stage": "planner_strip", "session_id": "L1", "target_msg_id": "100000001", "call_id": "call_self", "detail": "目标消息 100000001 是 bot 自己发出的消息（命中自身消息台账），按自回复拦截；已在生成前剔除该 reply 工具调用（未消耗回复生成 token）"}
{"time": "2026-09-20 20:14:14", "ts": 1789906454.574, "event": "duplicate_reply", "stage": "planner_strip", "session_id": "L1", "target_msg_id": "123456789", "call_id": "call_dup", "detail": "目标消息 123456789 在 180 秒去重窗口内已被回复过（上次 0 秒前），按重复回复拦截；已在生成前剔除该 reply 工具调用（未消耗回复生成 token）"}
{"time": "2026-09-20 20:14:14", "ts": 1789906454.576, "event": "duplicate_reply", "stage": "send_abort", "session_id": "L1", "target_msg_id": "123456789", "detail": "目标消息 123456789 在 180 秒去重窗口内已被回复过（上次 0 秒前），按重复回复拦截；已在下发前中止本次发送（平台侧不会看到该消息）"}
```

统计示例：

```bash
# 各类拦截次数（需要 jq）
jq -r '[.event,.stage]|join("/")' intercept.jsonl | sort | uniq -c
```

超过 `max_file_size_kb` 后按 `intercept.jsonl.1`、`.2` … 轮转，最多保留 `backup_count` 份。

---

## 7. 行为变化、限制与风险

**这是"拦截"，不是"改写"。** 被拦下的回复不会改成别的说法，直接不发；Planner 会看到该轮没有回复动作
（生成前剔除的语义等价于"模型这一轮没有调用工具"）。

必须知道的限制：

1. **fail-open 单点**：Hook 走插件 Runner 进程的 RPC。插件进程崩溃/超时/未启用时，Hook 被跳过 →
   **完全失去保护**。另外阻塞型 Hook 会给每次 planner 响应和每次发送增加一次本地 IPC 往返（本插件处理器是
   纯内存查表，开销很小）。**这是插件方案相对于改宿主源码的固有代价。**
2. **只守护 `reply` 工具**（内置常量 `GUARDED_TOOL_NAME`）。其它插件用自己的工具/能力发出的消息不在生成前
   剔除范围内；发送前兜底也只认 `before_post_process` 写下的轮标记。
3. **CLI 控制台平台的本地渲染不经 `send_service`**，所以发送前兜底对 CLI 无效（生成前剔除仍然生效）。
4. **状态在内存中**：插件重启后去重窗口与自身消息台账清零。缓解：planner 上下文扫描会立刻从历史里
   重新学到自身消息 ID；去重窗口本身只有几分钟。
5. **关闭"生成前剔除"只留"发送前兜底"时**：重复回复仍会被**完整生成**（照烧 token），且 `reply` 工具会收到
   "发送失败"的观察结果，Planner 可能对同一目标再试一次——再试仍会被拦，但会重复消耗 token。
   **建议保持 `strip_at_planner = true`。**
6. **目标登记时机**：已回复目标是"回复生成成功"时登记的（早于真正下发）。若随后下发失败，窗口内对同一目标的
   下一次合法回复会被拦掉。窗口可调小以降低影响。
7. 与 `cateye_better_post-processing` 共存无冲突：两者都挂 `send_service.before_send`，
   它在 `EARLY`（引用方式抽取/文本规则），本插件在 `LATE`（最后一道闸）。
8. **这会关掉宿主"允许补一条"的设计**：宿主 `reply.py` 的 `_find_recent_reply_to_target` 会用
   "你现在想再次回复这条消息，进行补充"刻意允许补一条。本插件在去重窗口内**不允许**补——
   这正是"同一条被回两次"的来源。若你确实想保留"补一句"的能力，把窗口调小（例如 30 秒）即可两者兼顾：
   紧随其后的重复被拦，隔一会儿的真正补充放行。
9. **会话类型判定的已知边界**：`group_info` 为空被判为私聊。宿主只有在**群名拿不到**的极端情况下
   才会让群聊的 `group_info` 也为空（`send_service.py:552-564` 会先用会话上下文里的群名兜底），
   那种群聊里的自回复会漏拦一次——属于 fail-open 方向，不会误杀。若你的群聊出现这种情况，
   把 `exempt_private_chat` 关掉即可（代价是私聊也拦）。
10. **私聊里"回复自己"是放行的**：这是刻意的（见开头说明）。若你希望私聊也严格拦，
    设 `exempt_private_chat = false`；但恋人插件的主动续话会被整条吞掉。

---

## 8. 与核心补丁的分工

| 层次 | 负责 | 手段 |
|---|---|---|
| 条数上限（防刷屏） | 宿主核心（可选补丁） | `src/maisaka/runtime.py` 的 `MAX_REPLIES_PER_TURN`（按**条数**计数） |
| **目标级去重 + 自回复**（本插件） | 本插件 | 按**目标消息 ID** 判定，生成前剔除 + 发送前中止；自回复默认只作用群聊 |

两者互补：核心补丁把单轮可见回复压到 N 条，本插件保证**同一条消息不会被回两次**、**不会回自己**。
本插件不改动宿主任何源码，可热插拔、可按群/按需开关。

---

## 9. English quick reference

**What it does.** Two independent hard guards for MaiBot, neither of which the host provides:

- **Self-reply guard** — blocks the Planner from calling `reply` on the bot's *own* messages.
  **Private chats are exempt by default** (`self_reply_guard.exempt_private_chat = true`): the host
  explicitly supports "supplement your own message" (`maisaka_generator_base.py:182-189`), which is
  exactly the proactive-private-chat pattern. Only confirmed group chats are guarded; unknown chat
  types fail open. Chat type is learned from `message_info.group_info`, which the host fills
  **only for group chats** (`send_service.py:550-582`).
- **Duplicate-reply guard** — blocks a second `reply` on the *same target message* inside a configurable window (default 180s). Not affected by the private-chat exemption.

**How.** Two stages per feature, both individually switchable:

1. `maisaka.planner.after_response` (BLOCKING) — strips the offending `reply` tool call from
   `output_items` **before generation**, so no reply tokens are burned.
2. `send_service.before_send` (BLOCKING, `order=LATE`) — `abort` as the final backstop so the
   platform never sees the message.

**Detection.** Own message IDs come from a scan of the rendered planner context
(`is_self_message="true"`) plus a ledger of outbound platform message IDs observed in
`send_service.after_send`. Already-replied targets are recorded once per generation in
`maisaka.reply.before_post_process`, which also writes the per-round marker consumed by the
send stage. No `ctx` capability is required (`capabilities: []`).

**Dedicated log.** Every interception is appended as JSON Lines to
`data/plugins/github.cateye.duplicate-reply-guard/intercept.jsonl`, separate from the host log,
with size-based rotation. Optional echo to the main log is off by default.

**Requirements.** MaiBot ≥ 1.2.3, maibot-plugin-sdk ≥ 2.8.0, no Python dependencies.

**Key limitations.** Fails open if the plugin runner is down or times out; guards the `reply`
tool only; the send-stage backstop does not apply to the CLI platform; in-memory state resets on
restart (the context scan re-learns own message IDs immediately). Keep `strip_at_planner = true`:
with only the send-stage abort enabled, the duplicate reply is still generated (tokens spent) and
the `reply` tool observes a send failure. Private chats are exempt from the self-reply guard by
default, and an unknown chat type also fails open.

---

## 10. 变更日志

### 1.0.0
- 首版：自回复拦截 + 重复回复拦截，每个功能含"生成前剔除"与"发送前中止"两个阶段。
- **自回复拦截默认支持私聊豁免**（`exempt_private_chat`）：会话类型从出站载荷的
  `message_info.group_info` 零 RPC 学得，只拦已确认的群聊；私聊里"补充说明自己"（恋人插件
  主动私聊场景）放行。重复回复拦截不受影响。
- 配置 4 节，字段级与节级 **zh-CN / en 双语翻译**。
- 拦截行为写入插件独立 JSON Lines 日志（含大小轮转）。
- 零能力依赖（`capabilities: []`）。
