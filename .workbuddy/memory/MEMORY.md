# Browser Bridge 项目记忆

## 项目定位
浏览器桥接扩展 + 本地接收端(127.0.0.1:8787)：把 ChatGPT/Claude 等网页会话消息 POST 到本地，
由 agent_runtime 做"假设/结论/依赖"图谱推理（state.json + events.jsonl 落盘）。

## Sprint 进度
- v0.1 / v0.2：扩展三层选择器 + 自诊断，已作废（见 HANDOFF.md）。
- 3b：ASSUMPTION_CHANGED → Impact → Deriver → apply → 落盘。
- 3c：依赖声明(DEPENDENCY_DECLARED) 触发 stale 派生。改动仅限
  state_updater._apply_dependency_declared + agent_runtime.on_turn；不碰 impact_analyzer / event_deriver。
  代码方向凯文已确认（2026-09-26）。真机验收 = run_3c_acceptance.py（21 句 + 真实 DeepSeek），
  14:25 自动化跑完 + 两份证据贴出后关闭。
- 4（2026-09-26 完成）：隔离 assistant 消息对 state 的污染（#8 漏洞）。
  只改 `Conversation Agent/runtime/agent_runtime.py` 的 on_turn：注入 source_role 并按 role=="user" 分路
  （非 user 只记录不动 state）。server.py 已透传 role，不动。pytest 58 passed，3c 离线回归仍 PASS。
- 5（2026-09-26 进行中）：真机 role 链路确认。目标 = 扩展抓到真实 ChatGPT 消息时 role 能正确传到 on_turn。
  结论前置：content.js 已透传 role（POST payload 含 `role: it.role`，ChatGPT 分支用
  `roleAttr: data-message-author-role` 从真实 DOM 读）——任务书"role 是 ? → 改 content.js"的兜底分支
  当前代码已实现过，**无需改 content.js**。server 端接 role 也已确认（server.py 第264/284/344行）。
  隔离预检（临时端口+临时 recv，同份 server.py=Sprint4 runtime）：用 role="user" 发 2 条测试消息，
  确认 turns.jsonl 两条 role=="user"、state.json 建出 assumption:测试字段 v1=1/v2=2、
  events.jsonl 两事件 source_role=="user"，且 DeepSeek 当前可用（runtime_status=ok）。
  已清 canonical + bridge 两份脏状态，起真实 server(8787, Sprint4代码, deepseek-chat) 后台等真机消息。
  待凯文在 ChatGPT 发 2 条真实消息后，捞 turns/state/events 三份证据关闭 5。
  约束：不改任何 runtime 模块；只改 content.js（实际需要才改，目前不需要）；不改 server.py 的 role 兜底。

## Backlog
- #7（2026-09-26 凯文提）：state_updater 内联派生（backfill_stale_events 从 apply_event 的 diff 弹出）
  与 event_deriver 外置派生的职责边界，需在 v0.2 统一。隐患：若之后加新派生类型
  （如 CONCLUSION_REACTIVATED），"派生到底在 state_updater 内部产生还是 event_deriver 统一产生"
  会出现分歧。当前不修，等 3c 过了再说。
- #8（2026-09-26 凯文提，已由 Sprint 4 修复）：assistant 消息（模型的假设/条件句）被当成用户假设
  写进 state、持续污染真实状态。修复 = on_turn 按 source_role 分路，非 user 只记录不动 state。
