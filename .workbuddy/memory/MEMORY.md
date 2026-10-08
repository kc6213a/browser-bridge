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
- #11 幻觉 span（未修）/ #13 JSON 截断（**2026-09-28 fixed**：删除 `event_log.jsonl` 第 292 行坏行，runtime 恢复）。
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
- #19（2026-09-27 凯文记，**已并入 #28**）：重载扩展后旧 content.js 仍存活刷 "Extension context invalidated" 红字——与 #28 同因（context invalidated 防护），已于 2026-09-27 commit `65483fd` 修复（`chrome.runtime.id` guard + catch 静默），#28 标 fixed，本条并入。
- #20（2026-09-27 凯文拍板，**不动**）：Event Judge 附加输出 `cognitive_relevance`。
  来源：另一 GPT 提的「会话价值判断层」建议，凯文**部分采纳**。
  - ✅ 采纳：judge 在现有调用里**多吐一个字段**（`low|medium|high` + reason），
    **不做新层、不改链路**（沿用硬常识 1：新增能力走独立第二次 LLM 调用，
    `build_judge_prompt` 零改动是回归闸门）。
  - ❌ 未采纳：① 新增 Significance Judge 层（链路已太长）；
    ② 临时议题过期机制（需要时间轴，属另一个 Sprint 的工作量）。
  - 依赖：先修 **#14（L2 失控）+ #13（event_log 完整性）**。
  - 触发条件：**freeze 一周后**，实测若发现「简单问答污染严重」才做。
- #21（2026-09-28 Sprint 19 结论，**已并入 #35**）：原以为「采集层 role 错标——所有下游地基」，经 Sprint 19 链路重查**推翻**——`server.py:264` 原样落盘、`content.js:85` 直接读 DOM 属性、L1 全量 120 条 role 分布正常（assistant:62/user:58）、turn 8 的 mid 关联到 `diagnose_20260927_003425.json` 实锤其在真实 ChatGPT DOM 即 `data-message-author-role="user"`（用户把 assistant 输出粘回输入框）→ **非采集层 bug，地基没坏**。统一根因（与 #35 同源）：系统把传输层「节点是 user 角色」等同语义层「内容由用户创作」，分不清「用户发的消息（role 事实）」与「用户输入内容原本来自哪（authorship 语义）」。频率低（1-1.5%），按用户拍板：不修、继续观察，回 freeze。
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
- #40（2026-10-08 DeepSeek 面板实测，open）：**state 未按平台分区，面板显示混合议题**。
  现象：DeepSeek 页打开面板，看到 ChatGPT 的议题混在一起。
  根因：state.json 全局唯一，节点无 source/platform 字段。
  影响：多平台（chatgpt/claude/deepseek）使用时无法区分议题来源。
  两种方向（待凯文拍板，未实现）：
    A. 保持全局，节点加 `source` 字段（轻：只改 schema 常量 + 面板按 source 着色/筛选；
       采集层 turn 已有 source 维度，state 节点补写即可，不碰 `build_judge_prompt`，无回归闸门风险）。
    B. 按 hostname 分区 state（重：采集/存储/state_updater/event_deriver/面板/抽取全改，多份 state.json）。
  依赖：**已有实证（turn 553）** —— 2026-10-09 用户在面板上亲眼看到「我看到面板了。里面的统计内部
    是包含的gpt部分的」，并已落成 topic 节点 → 混合现象确认，不再是假设；
    剩下的是主观判断（干扰 vs 帮助），待凯文看面板后拍板。
  状态：open。注：Sprint 20 实测 CA state 已含 deepseek turn 530~536 与 chatgpt turn 528/529 同池，
    混合已实际发生；方向 A 成本最低、且不动 judge 链路，倾向优先评估。
- #41（2026-10-08 p1_review 实验，open）：**extractor 抽「内容」不等于抽「议题结构」**。
  现象：陈述级内容（60%=15/25、v1 退役）抽得准；章节级子议题（seq50/seq43/B-1/v2 开工）没成节点。
  根因：当前 schema 只有 assumptions/conclusions/dependencies，缺 topics/subtopics/parent 维度。
  修法候选：
    A. schema 加 `topics` 桶（改 extract.py 输出 + 潜在下游消费方）；
    B. 依赖 markdown `##` 标题分段（格式依赖，只对结构化报告有效，对真实对话失效）；
    C. 先不动，等真实长对话再评估。
  建议：**C**（凯文：输入样本是单轮审查报告，不对；且「抽出的议题结构怎么用」未定）。
  状态：open。关联：与 extractor_gate 实验同源；p1_review 实测 evidence 24/24 但四子议题未浮出节点，
    印证「内容抽得准 ≠ 议题结构抽得出」。
- #42（2026-10-09 凯文观察，open）：**平台项目功能对产品定位的影响**。
  现象：ChatGPT/Claude 等网页端已有 project 功能（容器级）。
  启发：① 项目 ID 可作 state 分区键 → **天然解决 #40**（相当于给 #40 多一条分区轴：project 而非 hostname，
       且更贴合用户心智）；② 用户痛点可能从「会话内」升级到「项目级」；
       ③ 平台做了容器层，留了结构层给我们（分工：平台管容器，我们管结构）。
  风险：平台容器可能已满足多数用户；我们只服务「真需要结构」的那部分——定位收窄，不是坏事但要看清。
  依赖：**已有实证（turn 602）** —— 2026-10-09 用户真实对话「现在网页端的 llm 都有类似项目的功能，
    可以把同一项目的对话集…」已落成 topic 节点 → #42 现象侧实证完成。
    （注：若走 ①「project_id 作分区键」，其 DOM 可读取性仍是独立技术探针问题，非本条阻塞。）
  状态：open，仅记录，不动代码。
- #43（2026-10-09 诊断，**fixed 本次**）：**L1 `querySelector` 只取第一个匹配块，DeepSeek 只抓到 thinking**。
  根因：`content.js` collectSiteLayer 用 `el.querySelector(cfg.textSelector)`（**单数**），
  遇逗号选择器只返回 DOM 序第一个；DeepSeek 先渲染 `.ds-think-content`（思考）后渲染 `.ds-markdown`
  （正式回答）→ 正式回答整段丢失。
  证据：新段（turn≥400）50 条 assistant **全是思考文体**（「我们需要回答用户…」「让我来审阅一下这个…」
  「The user has pasted back my own previous review verbatim.」）；反证 turn 584/585 无可见 thinking 块
  时抓到了正式回答 → 机制自洽。非截断（L1 无 MAX_LEN，MAX_LEN 只在 L2 用）。
  修法：**`assistantPrimary` 字段 + 优先取正式回答块**（2026-10-09 已改 content.js，仅此一文件）。
  ⚠️ **首版修法选错过选择器**（`".ds-markdown"` 无效，真机二验仍抓 thinking）：
  DOM 探针（凯文实跑）证明 **thinking 容器里也有一个 `.ds-markdown`**（思考内容同样走 markdown 渲染器，
  DOM 序在正式回答之前，长度 16036 vs 正式回答 1798）→ 选到假的那个。
  现行值：`assistantPrimary: '[class*="ds-assistant-message"]'`（正式回答的 markdown 类名带此后缀，
  用类名特征排除 thinking，不依赖 DOM 层级；备选 `:scope > .ds-markdown` 未采用）。
  状态：fixed（待真机验证后 commit）。
- #44（2026-10-09 诊断，open）：**page load 全量重抓导致同一消息多条 turn**。
  现象：刷新页面时 bridge 抓整段可见历史，同一条消息被多次写入（turn 565/590 内容重复，长度 1057 vs 2570）。
  影响：turns.jsonl 有重复；可能污染 state 计数。
  修法候选：content.js 记录 `last_reported_message_id`，只发新的。
  状态：open（独立现象，不阻塞 #43）。
- #45（2026-10-09 凯文记，**优先级高**，open）：**EventStore 无坏行容错，单行损坏 = 整库加载失败 + 静默丢弃**。
  现象：`event_log.jsonl` 单行非 JSON → EventStore 加载整批抛异常 → **所有新事件静默丢弃**
  （只在 `turn._runtime.status=error` 才可见，面板/state 表面无异常）。
  根因：① 写入端无完整性校验（坏行实锤是**未走 JSON 序列化的裸文本**，如
  「n股价跌→企业融资困难→基本面恶化→股价再跌…」）；② 加载端遇坏行抛异常整批失败，不做跳过。
  复发史：**第三次** —— 2026-09-28（删第 292 行）、2026-10-09（删第 652 行，备份 `.bak_pre_652fix`），
  且两次都是清创（删行），**根因未修**。
  修法：① 写入前序列化成字符串并 `json.loads` 自校验（不合法就不落盘 + 报警）；
        ② 加载端遇坏行**跳过 + 记日志**，不阻塞整库，坏行另存 quarantine 文件。
  优先级：**高**（已复发三次，且丢失静默，属于数据完整性事故而非功能 bug）。
  关联：#13（event_log 完整性）是本条的**症状条**——#13 记「坏了要修」，#45 记「为什么会坏 + 怎么不再坏」；
    修 #45 即根治 #13。
  状态：open，未动代码（本次仅清创）。
- #46（2026-10-09 turn 648 实证，凯文立，**优先级高**，open）：**流式生成早期抓取中间状态入库**。
  现象：流式生成早期 `.ds-assistant-message` 类还没渲染 → `assistantPrimary` 查询返回 null →
  退回抓整个 `.ds-message` 容器 → **「正在思考 正在思考」占位 + thinking 一起入库**（turn 648，1394 字）。
  同族：**#44**（page load 全量重抓）——两者是同一族「抓太早 + 抓太多次」的不同表现。
  与 #43 的关系：**不同层**。#43 是选择器层（选错块，已修对）；#46 是触发层（抓太早，未修）。
  修 #43 不解决 #46，修 #46 也不需要 #43 的改动 → 分开做（凯文 01:16 拍板 A）。
  修法：等生成完成再抓（debounce + 完成态检测）。
  ⚠️ 风险提示（凯文）：改 debounce 是动 `MutationObserver` 触发层，改错会导致漏抓/延迟抓/永不抓，
  属高风险改动，需单独评估而非顺手改。
  证据：同一条消息「Python 异步的三种并发模型」被抓 3 次（turn 633/634/648），全为中间状态；
  而 645/646/647 抓到正文 → **抓取时机决定成败，非选择器**。
  状态：open。

