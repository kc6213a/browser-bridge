# Browser Bridge 项目记忆

## 定位
浏览器扩展(extension/) + 本地接收端 127.0.0.1:8787(server.py)：把 ChatGPT/Claude 网页会话消息 POST 到本地，
由 `Conversation Agent/runtime/` 做「假设/结论/依赖」图谱推理，落盘 state.json + events.jsonl。
**两个独立 git 仓库**：`Conversation Agent`(runtime 后端) 与 `browser-bridge`(扩展/面板)，改动需分别 commit+push。
`sprint*_verify/` 历来不入库（脚本内含 API key）。

## 当前进度（截至 2026-09-27）
- Sprint 1–12：三层选择器/role 隔离/NEW_TOPIC 三值化，已完成。稳定基线：三遍一致 [3,4,7,8,18,20]，
  precision=0.833 / recall=1.000 / flips=0。历史细节见 `.workbuddy/memory/2026-09-2*.md` 日志。
- Sprint 13：few-shot 锚定 NEW_TOPIC 边界（只改 prompt 常量 FEW_SHOT_EXAMPLES）。
- Sprint 14：测试迁移到 label 契约（pytest 61 passed）+ 真机 role 链路打通（修 IPv4/IPv6 错位）。
- Sprint 15：panel.js 改 Shadow DOM + content.js L2 过滤，断「面板→抓取→改 state」回环。
- Sprint 16 ✅：修 assumption 判据（按 candidate_type 分派，非 NEW_TOPIC 走布尔路径）。CA main=589ff47。
- Sprint 17 ✅：议题树 v0.1（judge 第二次独立 LLM 调用产 parent_topic_id；state 写 parent_id；面板缩进）。
  CA `548d0d5` / BB `02d6d36`。⚠️ 已知限制：紧邻「聊聊 X → X 里的 Y 呢」建不出树（规则7 细化不算新议题），待拍板。
- **下一步（待凯文选）**：① 议题树加强（关联边/折叠/多层）② 清 backlog（#11 幻觉 span / #13 JSON 截断 /
  chrome 误抓）③ 放宽「细化不算新议题」？

## 硬常识（踩过的坑，改代码前必读）
1. **NEW_TOPIC 的 label prompt 极度敏感**：往 `build_judge_prompt` 插入任何新段落或让 LLM 多返回字段，
   都会引起三值横跳（实测 precision 0.833→0.667、recall 1.000→0.800）。
   **新增能力一律走「独立第二次 LLM 调用」**，改完用 `git diff` 确认 `build_judge_prompt` 零改动再回归。
2. **topic id = `topic:<sha1(title)前8位>`，LLM 算不出**：需 LLM 引用 topic id 时必须明文列出候选，
   校验返回值在允许集合内，否则降级 null。
3. **验收数字必须可溯源**（凯文硬要求）：① 时间戳链 代码 mtime < 产物 mtime < commit；
   ② 重放脚本把 `inspect.getsource(关键函数)` 快照写进报告（排除 stale `__pycache__`）；
   ③ 非 NEW_TOPIC 事件 confidence 出现 {0.95,0.5,0.1} 之外的真值 = 布尔路径活跃。
4. **判 accepted 只能读 `received/runtime/events.jsonl`**（/turn 响应体的 records 是计数不是对象）。
   ⚠️ 字段口径：`confirmed`/`label` 是顶层字段，`judge_result` 是字符串，turn 在 `evidence.turns[0]`（顶层无 turn）。
   ⚠️ DEPENDENCY_DECLARED 走 regex 快路径绕过 LLM（confidence 恒 0.95、label=None），不能当「旧代码」证据。
5. **重放前必须清状态**：server 有 `_seen_keys` 去重（message_id 缺失退化为 hash(role+text)），
   需清空 `received/runtime/*` + canonical state.json/event_log.jsonl + 归档旧 turns.jsonl。
6. 冻结基准：`sprint10_verify/verdicts.json`（严格 truth {3,4,7,18,20}，宽松含 14）；
   语料 `sprint9_verify/corpus.jsonl`（N=23）。复用：`sprint16_verify/verify_evidence.py`、
   `sprint17_verify/verify_s17.py`。

## Backlog
- #7（凯文提，未修）：state_updater 内联派生 vs event_deriver 外置派生的职责边界，v0.2 统一。
- #8（已由 Sprint 4 修）：assistant 消息污染 state → on_turn 按 source_role 分路，非 user 只记录不动 state。
- #11 幻觉 span / #13 JSON 截断（均未修）。
- #14（2026-09-27 升级优先级，凯文拍板）：Chrome 误抓影响用户可见内容——L2 把 ChatGPT 输入框占位文案
  「你今天在想些什么？」误抓进消息流（新证据：噪音开始进 panel/state）。
  修法两条：① 轻：CHROME_NOISE 词表加「你今天在想些什么？」；② 根：L2 只抓 `[data-message-author-role]`
  或类似强信号节点。凯文倾向 ②（词表永远填不完），执行前先评估 ② 落地后 L2 对现有站点还能兜住什么。
  ⚠️ 仅记录不做实现；同时约束：不改面板视觉、不补 Policy（v0.2 的事）、议题树 v0.1 只做一级嵌套。
- #18（2026-09-27 凯文拍板结论，**不实现**）：**DeepSeek 网页端没有稳定属性可抓**——DOM 类名是混淆 hash
  （`dc1f7bee` 之类），不存在 ChatGPT 那种 `data-message-author-role`。空适配器跑 [L2] 的实测代价：
  一次抓 **137 条候选**、灌 **175 条噪音事件**（turns.jsonl 中 `chat.deepseek.com` = turn 107~281），
  state 从 version 21 涨到 163 / 68 nodes，并连带把旧消息「假设测试字段是 1」重抓、assumption 节点复活。
  结论：DeepSeek 需要**单独的 L1 适配器**，但当前 diagnose 数据不足以写选择器。
  **依赖：必须先修完 #14（L2 失控），再单独立 Sprint 投 DeepSeek。**
  现状：Sprint 18 已从 manifest 撤回（`git diff extension/` 为空，deepseek 0 处），**不再尝试**。
  污染已清理：两份 state.json 重置到 version 21 / 5 nodes / 21 events（备份 `*.bak_pre_s18revert`）。
  ⚠️ 若重启 server 前又进消息，内存里的 163 版会覆盖写回盘上。
- #19（2026-09-27 凯文记，**不动**）：重载扩展后，已打开页面里的**旧 content.js 仍存活**并继续
  `chrome.runtime.sendMessage` → Console 刷 "Extension context invalidated" 红字。
  影响：仅日志噪音，不影响功能（新脚本已接管），但用户视觉上能看到。
  修法（二选一，未定）：① `post()` 里 catch 该特定错误静默忽略；② boot/发送前检测 `chrome.runtime?.id` 是否存在。
  优先级：**低**（只在扩展重载后、且页面未刷新时出现；刷新页面即消失）。
- #20（2026-09-27 凯文拍板，**不动**）：Event Judge 附加输出 `cognitive_relevance`。
  来源：另一 GPT 提的「会话价值判断层」建议，凯文**部分采纳**。
  - ✅ 采纳：judge 在现有调用里**多吐一个字段**（`low|medium|high` + reason），
    **不做新层、不改链路**（沿用硬常识 1：新增能力走独立第二次 LLM 调用，
    `build_judge_prompt` 零改动是回归闸门）。
  - ❌ 未采纳：① 新增 Significance Judge 层（链路已太长）；
    ② 临时议题过期机制（需要时间轴，属另一个 Sprint 的工作量）。
  - 依赖：先修 **#14（L2 失控）+ #13（event_log 完整性）**。
  - 触发条件：**freeze 一周后**，实测若发现「简单问答污染严重」才做。
- #21（2026-09-28 重新定性，**高优先级**）：**采集层 role 错标 —— 所有下游问题的地基**。
  新证据：236 条 user 消息里约 4 条是 assistant 长文被错标成 user（采集层 role 错乱，非用户粘贴）。
  历史锚点：由 2026-09-27 真机 turn 7/8（同一条 assistant 回复 L1/L2 各抓、role 都记 user，致 #8 防线失效、USER_REFERENCES_PAST:acc+2×DEPENDENCY_DECLARED:acc 已进 state）升级而来；原 #8a 即此现象子条目。
  影响链条：① Gate 4 的 21 条"user 认知"至少 4 条非用户说的；② schema 加 role 也修不了（role 本身错）；
  ③ 换 Transport 也修不了（错标在采集层内部）。定性从"独立 bug"升级为下游 #34/#35/#36 共同地基。
  优先级：下一 Sprint 第一优先。状态：open 高优先级。
- #22（2026-09-27 真机发现，**不动**）：**L1/L2 双写仍在**。
  证据：同一条消息被两层各抓一次 → 两条 turn、两个不同 `message_id`
  （turn 12 = layer2 `h:7251cc6d` fallback hash；turn 13 = layer1 `attr:data-message-id` `315b39f9-…`），
  content.js 的 `sentIds` 只在**同 message_id** 上去重，跨层 mid 不同 → 去重失效。
  turn 7/8 同理（同一条 assistant 回复的 L2/L1 两份，长度 2820 / 2650）。
  后果：turn 12 与 13 **各产一个 `NEW_TOPIC:acc`**，同一议题被重复判定。
  修法候选（未定）：content.js 内跨层去重（文本归一化后判重），或 L1 命中时直接跳过 L2。
- #23（2026-09-27 真机发现，**已删除，结案**）：**基线里曾保留了一个假议题**。
  证据：保留节点「你今天在想些什么？」来自 turn 10，抓自 `layer=2 / generic:div,article / role=user`，
  正是 #14 认定的 ChatGPT **输入框占位文案**噪音，却被 judge 判成 `NEW_TOPIC:acc` 落成 topic 节点。
  02:20 那次 state 重置按「turn ≤ 25 全留」的口径把它一起保留了（5 个节点之一）。
  **✅ 03:23 凯文拍板删除并已执行**（理由：五个字段本身已是可溯源标本，不必在面板上再挂个活标本）：
  两份 state.json 同时删 `topic:71e9f4f5` 节点 + 事件 `evt_f55e6a58937c`（NEW_TOPIC/accepted/turn 10），
  **events 21→20、nodes 5→4、version 21→20**（version 沿用 `== len(events)` 口径），两份 md5 一致。
  ⚠️ 残留（按「其他一律不动」保留）：`working_state.recent_changes` 里的
  `NEW_TOPIC@turn=10` / `NEW_TOPIC:accepted@turn=10` 及 `relevant_turns` 的 10，未同步剔除。

