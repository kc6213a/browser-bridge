# Browser Bridge 项目记忆

## 定位与红线
- 浏览器扩展(extension/) + 本地接收端 127.0.0.1:8787(server.py)：ChatGPT/Claude/DeepSeek 网页会话 → POST 本地 → `Conversation Agent/runtime/` 做假设/结论/依赖图谱推理。
- 两个独立 git 仓库：CA(runtime 后端) 与 browser-bridge(扩展/面板)，改动分别 commit+push。
- 🔴 `runtime/hosts/`(含真实对话标题)、`sprint*_verify/`(含 API key) **不入库、不推 GitHub**（已进 CA/.gitignore）。

## 硬常识（改码前必读）
1. NEW_TOPIC label prompt 极敏感：任何改动必引起三值横跳。新增能力一律走「独立第二次 LLM 调用」，`build_judge_prompt` 零改动是回归闸门。
2. topic id = `topic:<sha1(title)前8位>`，LLM 算不出；要引用必须明文列候选并校验，否则降级 null。
3. 验收数字可溯源（凯文硬要求）：时间戳链 代码mtime<产物mtime<commit；重放脚本把 `inspect.getsource` 快照写报告；非 NEW_TOPIC 事件 confidence 出现 {0.95,0.5,0.1} 之外的真值=布尔路径活跃。
4. 判 accepted 只能读 `received/runtime/events.jsonl`（`/turn` 响应 records 是计数非对象）。DEPENDENCY_DECLARED 走 regex 快路径(confidence 恒0.95)不能当旧代码证据。
5. 改盘上 state 前必须先 kill server（state 常驻内存每 turn 回写，否则被覆盖）；面板读 bridge 镜像 `received/runtime/hosts/<host>/<proj>/state.json`，CA 权威与镜像要同步改。
6. 重放前清状态：`received/runtime/*` + canonical state + 归档 turns.jsonl；server `_seen_keys` 去重(message_id 缺失退化 hash(role+text))。

## 当前状态（2026-10-09）
- #40 hostname 分区：**fixed**。CA `runtime/hosts/<host>/<proj>/{state.json,event_log.jsonl}` 权威 + bridge 镜像；`/state?host=&project=`。跨 host 悬空 parent_id 已清空。
- #40-B host+project 二级分区：**已实现，待真机验**。路由键 `host::project`；共享 `extension/project_id.js`(content.js/panel.js 均调用防漂移，manifest 前置)；历史无 URL/project 字段→全归 `_no_project`，新数据从今天分区。正则实测修正：ChatGPT `/\/g\/(g-p-[0-9a-f]{8,})/i`(hex 后跟拼音 slug，停在 `-`)；DeepSeek `/\/a\/chat\/s\/(...)/i` 前缀 `s-`。冒烟两 project 各 1 事件 1 节点互不相干。
- #43 DeepSeek 只抓 thinking：**fixed 待 commit**。根因 L1 `querySelector` 单数；现行 `assistantPrimary:'[class*="ds-assistant-message"]'`(类名特征排除 thinking，不依赖 DOM 层级)。首版 `".ds-markdown"` 误选 thinking 容器内同名节点。
- #45 EventStore 坏行容错：**fixed**(CA `runtime/event_store.py` commit aa710b2)。写入端 json round-trip 自校验；读取端坏行跳过+quarantine。全量 66 passed。

## Open Backlog
- #14 L2 失控：ChatGPT 输入框占位「你今天在想些什么？」误抓。凯文倾向根因修法(L2 只抓 `[data-message-author-role]`)，暂不实现。
- #22 L1/L2 双写：同消息两层各抓→两条 turn→重复 NEW_TOPIC 判定。候选：跨层文本去重 或 L1 命中跳 L2。未定。
- #41 extractor 抽内容≠抽议题结构：建议 C 不动，等真实长对话。
- #42 平台 project 功能对定位影响：现象实证完成，定位 open；① project 分区已落地(#40-B)。
- #44 page load 全量重抓→同消息多 turn。候选：content.js 记 last_reported_message_id。未做。
- #46 流式早抓：生成中 `.ds-assistant-message` 未渲染→退回抓占位+thinking 入库。同族#44。修法 debounce+完成态检测，高风险(动 MutationObserver 触发层)，待单独评估。
- #47 页面标题误抓：查证是用户粘进输入框(#36 近亲，非 L2 噪音)，原 L2 过滤方案划掉，待拍板。

## 待闭环
- ⚠️ `_legacy_no_project` 并发改动：曾发现 server.py:278 / 盘上目录出现 agent 未写的 `_legacy_no_project`（疑似外部改动）。当前 /health 仅列 `_no_project` 三桶（未加载 `_legacy`）。若盘上残留目录需清理或确认来源。
- #40-B 真机验证：重载扩展 → ChatGPT 项目A/项目B 各发一条 + DeepSeek 一条 → 查 hosts/chatgpt.com/ 是否冒两个 g-p-* 目录、面板标签两边不同。
