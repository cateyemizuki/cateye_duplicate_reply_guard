# 更新日志

## 1.1.0（2026-09-21）

- **新增「延时补充放行」**（`[duplicate_reply_guard]`，两项配置，默认开启）
  - `allow_late_repeat`（bool，默认 `true`）：对**非自己**的目标消息，距上次回复**达到或超过**阈值后，
    本次回复按**补充说明**放行；关闭后去重窗口内一律不放行（退回 1.0.0 行为）。
  - `late_repeat_after_seconds`（int，默认 `30`，范围 5–3600）：延时补充阈值（秒）。
  - 判定分档：**≤ 阈值 → 重复回复（拦）**；**> 阈值 → 延时补充（放行）**；**> `dedupe_window_seconds` → 已过期（放行）**。
  - 放行时三个阶段口径一致：生成前**不剔除** `output_items`、生成后**不写轮标记**、发送前**不 abort**，
    且**不写拦截记录**；放行后重新计时（该目标此后 30 秒内的再次回复重新被拦）。
  - **只放宽、不会收紧**：阈值设得比 `dedupe_window_seconds` 还大时以去重窗口为准。
  - **自回复拦截不受影响**：目标是 bot 自己的消息时恒拦（自回复判定优先，且不看已回复台账），
    与"非自己消息才适用本项"的语义一致。
  - 与宿主 `reply.py:_find_recent_reply_to_target` 的"你现在想再次回复这条消息，进行补充"设计对齐：
    默认只在 30 秒内不允许补，隔了一会儿的真正补充放行。
- **配置节字段顺序/展示**：`[duplicate_reply_guard]` 现在为
  `enabled` → `strip_at_planner` → `abort_at_send` → `dedupe_window_seconds` → `allow_late_repeat` → `late_repeat_after_seconds`，
  新字段带 zh-CN / en 双语 `label` 与 `hint`。
- **日志更可解释**：重复回复的拦截记录 `detail` 增加"未达到 N 秒延时补充阈值"；
  当 `allow_late_repeat` 关闭时写明"延时补充放行已关闭"，便于区分"为什么这次被拦"。
  插件加载与配置更新日志新增延时补充开关与阈值。
- **版本**：`_manifest.json` → `1.1.0`，`SUPPORTED_CONFIG_VERSION` 同步为 `1.1.0`（旧 `config.toml` 无需改动，
  新字段按默认值补齐）。
- **文档脱敏**：README 中的实测样例（群标识、消息 ID、bot 回复正文）统一替换为占位内容，
  仅保留时间与间隔等结构性信息；拦截日志样例同样改为占位 ID。

## 1.0.0（2026-09-20）

- **首个版本**：自回复拦截 + 重复回复拦截，两个功能各自独立开关。
- **功能：自回复拦截**（`[self_reply_guard]`）
  - 阻止 Planner 对 **bot 自己发出的消息** 调用 `reply`。
  - 自身消息 ID 两个来源：① `maisaka.planner.before_request` 扫描模型上下文，取出渲染为
    `is_self_message="true"` 的 `msg_id`（覆盖插件启动前的历史）；② `send_service.after_send`
    观察本进程发出的消息，累积出站消息 ID 台账（含其它插件以 bot 身份发出的消息）。
  - **默认支持私聊豁免**（`exempt_private_chat`）：会话类型从出站载荷的 `message_info.group_info`
    零 RPC 学得，只拦**已确认的群聊**；私聊里"补充说明你自己发送的消息"（恋人插件主动私聊场景）放行，
    类型未知时 fail-open。
  - 每个功能含**两个阶段**：生成前剔除（`strip_at_planner`，零 token、无失败反馈）与
    发送前中止（`abort_at_send`，最后一道闸），可分别关闭。
- **功能：重复回复拦截**（`[duplicate_reply_guard]`）
  - 按**目标消息 ID** 去重（不是按条数）：同一目标在 `dedupe_window_seconds`（默认 180 秒）内只允许回复一次；
    用户在你回复后又发新消息时目标是新消息 ID，不受影响。
  - 已回复目标由 `maisaka.reply.before_post_process`（每次回复生成恰好触发一次）登记；
    同一次 planner 响应内对同一目标并行调用两次 `reply` 也按重复处理。
  - 不受私聊豁免影响。
- **拦截独立日志**（`[intercept_log]`）
  - 拦截行为**不复用宿主主日志**，按 JSON Lines 追加写入插件 `data_dir` 下的
    `intercept.jsonl`，含 `event` / `stage`（`planner_strip`｜`round_blocked`｜`send_abort`）/
    `session_id` / `target_msg_id` / `call_id` / `detail` 字段。
  - 支持按大小轮转（`max_file_size_kb`、`backup_count`）与可选的主日志回显（`echo_to_main_log`、
    `echo_level`，默认关闭）；写盘异常只上报一次 warning，绝不影响拦截逻辑。
- **配置**：`[plugin]` / `[self_reply_guard]` / `[duplicate_reply_guard]` / `[intercept_log]` 共 4 节，
  字段级与节级均带 **zh-CN / en 双语翻译**（WebUI 按界面语言显示 `label` 与 `hint`）。
- **零能力依赖**：`capabilities: []`——只用 Hook 与 `ctx.logger` / `ctx.paths`，
  判定全部为本地内存查表（零 RPC）；所有 Hook 处理器 `error_policy=SKIP` 且内部兜底，
  拦截失败一律 fail-open 放行。
- **文档**：README 规范化重写（去掉英文段落，补「功能 / 安装 / 配置 / 工作原理 / 日志 / 限制与风险 /
  文件结构 / 版本历史 / 致谢与来源」结构），更新日志独立为本文件；「致谢与来源」写明
  **自回复拦截的功能方向与两层防线思路来源于 `maibot-self-reply-guard`**（`github.kumburovicbranko682-boop.self-reply-guard`，MIT），
  代码为独立实现、未复用其源码。
