# Browser Bridge — 本地会话采集 → Conversation Agent 推理

浏览器扩展抓 ChatGPT / DeepSeek 网页会话 → POST 到本机 `127.0.0.1:8787`（`server.py`）→ 交给
`Conversation Agent/runtime/` 做 NEW_TOPIC / ASSUMPTION 判定并落 state + event_log。

**状态页**：本文件只讲「现在跑成什么样、怎么判断它坏了没有」。设计细节见
`docs/design_sse_passive_tap.md`、历史见 `CHANGELOG.md`。

---

## 1. 当前状态（2026-10-28）

| 项 | 状态 |
|---|---|
| DeepSeek 采集 | DOM 采集，正常；**进 CA 推理** |
| ChatGPT 采集 | **只走 SSE 旁挂**（SSE 实时腿 + history 补差），DOM 采集**已永久关闭**（`chatgpt.com` / `chat.openai.com` 写死在 `extension/content.js` 的 `CAPTURE_DISABLE_HOSTS`） |
| ChatGPT 是否进 CA 推理 | **默认否。** `BRIDGE_SSE_REFLOW=0`（默认）时消息只落 `received_sse/`，**不产 NEW_TOPIC / ASSUMPTION 事件，议题树不更新** |
| 想让 ChatGPT 也分析 | 停 server → 设 `BRIDGE_SSE_REFLOW=1` → 重启（见第 2 节） |
| 模型 | **配置写死 `deepseek-flash`**（落在项目根 `.env`，由 `server.py` 的 `_load_dotenv()` 读取），与服务端实际模型一致 → 启动探针**绿** |

> ⚠️ **「`received_sse/` 有数据但 CA 里没事件」不是坏了**，是默认的省钱配置。
> 浏览器打开 <http://127.0.0.1:8787/health>，看到 `reflow_disabled: true` + `reflow_warning`
> 非空，就是这个状态；启动黑窗口里也会打一整块 `*** WARNING: SSE->CA REFLOW DISABLED ***`。

---

## 2. 开/关回流（ChatGPT 数据进不进 CA）

开关只认**启动时的环境变量**，运行中改无效，必须重启 server：

```bat
:: 关（默认）：只落盘，不推理
python server.py

:: 开：抓到的消息回流进 CA（会烧 LLM 判定次数）
set BRIDGE_SSE_REFLOW=1
python server.py
```

PowerShell：`$env:BRIDGE_SSE_REFLOW="1"; python server.py`

**验收纪律（凯文拍定）**：临时开回流做验收，验完**立刻关回 0 并重启**，不等指令。

开着的成本保护三层（都是默认值，可用同名环境变量调）：

| 保护 | 默认值 | 行为 |
|---|---|---|
| 自动回流阈值 | `BRIDGE_SSE_REFLOW_AUTO_MAX=5` | 单次补差 ≤5 条自动回流；>5 条**挂起**到 `received_sse/reflow_pending.jsonl`，人工 `POST /sse_reflow_confirm` 才放行 |
| 单次硬上限 | 50 | 即便已确认，也只回流最近 50 条，更早标 `skipped_too_many` |
| 熔断 | 连续失败 3 次 / HTTP 402 | 网络类连续 3 次才熔断；**402 欠费第一次就熔断**（欠费不是抖动）。熔断后 `/health` 的 `reflow_disabled_reason` 写明原因 |

挂起的批次：`GET /sse_reflow_pending` 看，`POST /sse_reflow_confirm` 放行。

---

## 3. 它到底是活着还是坏了（自查表，按顺序看）

1. 打开 <http://127.0.0.1:8787/health>
2. `ok: true` + `runtime.status: "wired"` → 服务活着、Runtime 接上了
3. `reflow_warning` 非空 → 回流关着（**预期**，不是故障）
4. `model_drift_warning` 非空 → 模型漂移告警（第 4 节）
5. `received_sse/` 目录文件在涨 → SSE 采集在跑
6. 都没问题但数据没进去 → 看黑窗口启动日志 + `received.log` 末尾

---

## 4. 模型漂移探针（启动时**必做**）

为什么：配置曾写 `deepseek-chat`，服务端静默别名到 `deepseek-flash` —— 请求返回 200，响应体
`model` 字段被换掉，代码和日志毫无察觉，等于判定器被换了个没人知道。

**已修（2026-10-28 凯文拍定）**：配置写成**实际在跑的模型** `deepseek-flash`，落在项目根 `.env`：

```ini
OPENAI_MODEL=deepseek-flash     # 只放非敏感配置；API key 仍在系统环境变量 OPENAI_API_KEY
```

> 🔴 **硬规则（不可违反；注释会被忽略，这条 README 不会）**：`.env` **永久禁止放敏感项**。
> API key / token / cookie **一律只走** ① 系统环境变量（如 `OPENAI_API_KEY`）或 ② `.env.local`
> （已在 `.gitignore`，绝不入库）。`.env` 只放非敏感配置（如 `OPENAI_MODEL`）。
> 理由：`.env` 随仓库走、会被人读、会被复制粘贴；密钥写进 `.env` = 迟早入库或外泄。
> 这条不靠 `.env` 文件里的注释提醒——那是给人看的、会被忽略——写在这里是**强制约定**。

`server.py` 启动第一步 `_load_dotenv()` 读它（极简解析、无第三方依赖；**只补环境里没有的变量**，
已 `export` 的值优先）。所以**直接 `python server.py` 就够了**，不用手写 `export`。
改模型 = 改 `.env` 一行 + 重启；`.env.local`（若用于放密钥）已进 `.gitignore`，不入库。

凯文拍板理由（原话精神）：不是「行为零变化」，是**告警疲劳** —— 黄警常亮等于没告警，
将来真漂移也会被忽略。**现在绿了，再变黄才是真信号。**

探针每次启动必跑，两查：

1. `GET /models`：配置模型是否还在可用列表里（不在 = 已下架/改名，漂移前兆）
2. 打一次最小 `chat/completions`（`max_tokens=1`，成本可忽略）：响应体 `model` 字段 == 配置模型？

不符 → 启动日志打 `*** WARNING: MODEL DRIFT ***` 星号块 + `/health` 的 `model_drift_warning`
标黄（`model_drift` 给出 配置/实际/可用列表）。探针自己没跑完（网络错、402）**同样标黄**——
没验上 ≠ 没问题。探针只告警，不熔断、不改模型、不阻断启动（换模型 = 换判定器，必须人拍）。

**绿灯时 `/health` 不占版面**：`model_drift` 为 `null`、`model_drift_warning` 为 `null`，
只有启动日志留一行 `model probe ok: configured=... listed=True actual=...`。
**看到它俩任何一个非空，就是真漂移，别忽略。**

验证记录（2026-10-28）：`.env` 生效后重启 → `/health` `runtime.model=deepseek-flash`、
`model_drift=None`、`model_drift_warning=None`；启动日志 `MODEL_PROBE_OK ... "match": true`。


---

## 5. 常用命令

```bat
python server.py                      :: 起接收端（黑窗口别关）
curl http://127.0.0.1:8787/health     :: 状态总览（先看这里）
```

扩展：`chrome://extensions` → 开发人员模式 → 加载已解压的扩展程序 → 选 `extension/` 目录。
**重载扩展后必须刷新已打开的标签页**（旧 content script 会一直跑到刷新为止）。

---

## 6. 目录

| 路径 | 内容 |
|---|---|
| `server.py` | 接收端（落盘 + 接 Runtime + SSE 回流 + 探针） |
| `extension/` | MV3 扩展：`content.js`（DOM 采集，isolated world）、`sse_tap.js` / `sse_parse.js`（SSE 旁挂，MAIN world） |
| `received/` | 主采集落盘（`turns.jsonl` / `runtime/`…） |
| `received_sse/` | SSE 旁挂落盘（**与 `received/` 物理隔离**，回流前不进 CA） |
| `.env` | 启动配置（**非敏感**，如 `OPENAI_MODEL=deepseek-flash`）；🔴 **密钥绝不放这里**，key/token/cookie 走系统环境变量或 `.env.local`（已 gitignore） |
| `received.log` | 运行日志（仿真的行带 `[SMOKE]` 前缀，不是真业务） |
| `docs/` | 设计/诊断文档；`docs/design_sse_passive_tap.md` 是 SSE 方案主文档 |

---

## 7. 待办（会挡住事情的那种）

- 🔴 **8 个 commit 未推 GitHub**（PAT 失效：`Invalid username or token`，`git log @{u}..HEAD` 实测 8 条）。
  需凯文在 GUI 跑一次 `git push` 重授权或给新 PAT：
  `d4dd5fe` / `df729af` / `0fa910d` / `8819f5a`（本轮 SSE 回流系列）+ `ba5506c` / `e48291b` / `16fdb60` / `ff04e07`（更早未推）。
  新改动（模型探针 + `.env` 加载器 + 本 README + `.env` / `.gitignore`）先堆在工作区，**不新增 commit**，等 push 通了一起走。
- 📌 本轮新增/改动**均未提交**（等 push 一起走）：`server.py`（漂移探针 + `.env` 加载器）、新建 `README.md`、新建 `.env`、`.gitignore` 加 `.env.local`。
- `docs/` 与 `.workbuddy/memory/` 按凯文要求**暂不提交**，等验收完统一处理。
- 三毒桶删除前的备份留在 `_backup_before_purge_20261027/`（已进 `.gitignore`，不会推上 GitHub）。
