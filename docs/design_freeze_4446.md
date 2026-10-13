# #44/#46 方案冻结：虚拟滚动下的全量捕获（懒加载截断）

> 状态：**IMPLEMENTED（2026-10-13 已落地，单 commit 可 `git revert`）—— e2e 15/15 通过，真机验收待凯文配合**
> 作者：agent（按凯文指令，仅出方案，不动 `extension/` / `server.py`）
> 关联 issue：#44（page load 全量重抓→同消息多 turn）、#46（流式早抓）、二者子项「懒加载截断」
> 冻结日期：2026-10-13
> 前置：**页面可开已证实**（2026-10-13 10:02 `s-45d3da56` 真开，layer=1 `.ds-message` 命中）

---

## 0. 本次新增的硬证据（比 10-12 那轮更硬）

| 证据 | 内容 | 说明 |
|---|---|---|
| **机制级证据** | `dom_signature` 里出现 `--dsl-virtual-list-transform-y`、`--dsl-virtual-list-ios-co…` | DeepSeek 消息列表是 **`dsl-virtual-list` 虚拟滚动容器** —— DOM 里只保留视口内节点，**不是"没滚到位"，是"滚走就被销毁"** |
| **同一 URL 两次诊断条数不同** | 2026-10-13 10:02:51 `count=6`；10:02:57 `count=3`（同一 `s-45d3da56`） | 视口移动 → DOM 节点数变化，坐实虚拟滚动 |
| **跨天对照** | b7d635d5：2026-10-09 滚到底 12 条 vs 2026-10-12 首扫 6 条 | 同一会话，滚与不滚差一倍 |
| **触发层零滚动** | `content.js` L337-419 全文无 `scrollIntoView` / `scrollTop` 任何调用 | 采集完全被动，靠 `DOMContentLoaded` + `MutationObserver`（debounce 800ms / maxwait 3s） |

**这改变了问题定性**：#44/#46 不是"时机没等够"（等再久也不会把视口外的节点等出来），而是**采集面本身只有视口**。修法必须是"主动扩大采集面"，不能只调 debounce。

---

## 1. 根因（已确证）

1. **采集面 = 视口**：`run()` → `collect()` → `querySelectorAll('.ds-message')` 一次性全抓，但虚拟列表只把视口内消息放在 DOM 里。
2. **无主动滚动**：`content.js` 全程不滚动，页面打开即抓首屏；用户不手动滚，历史消息永不进 DOM。
3. **滚动回来是新的 DOM 节点**：虚拟列表销毁重建，`seen`（`WeakSet`，按节点判）失效；靠 `sentIds`（按 `message_id`）兜底——但 DeepSeek 无稳定 id，退化成 `h:hash(role+text)`，**流式输出中途抓取会因文本未定形而生成新哈希 → 重复 turn**（这正是 #46 的故障模式，也是 #22 双写的近亲）。

---

## 2. 目标

1. **全量**：页面打开后能把会话的历史消息都收进来（不再只有首屏 6 条）。
2. **不重复**：滚动重扫不产生重复 turn（依赖 `sentIds` + server 侧 #50 分桶去重，二者已就位）。
3. **不打扰**：滚完恢复原滚动位置，不劫持用户视口。
4. **不放大 #46**：流式生成中的消息不抓半截。
5. **低风险优先**：不动 `MutationObserver` 的触发语义（只新增一个独立的全量扫描动作）。

---

## 3. 方案（三选一，**请凯文拍板**）

### 方案 A（推荐）：一次性全量扫描 `fullScan()` —— 独立动作，不改观察层

> **凯文 2026-10-13 拍板：选 A**，理由（凯文原话）：虚拟滚动下**节点会被回收**——
> "滚到底再抓"只能拿到最终视口，中间的没了。所以必须"**分步下滚、每步抓、累积**"。
> 这同时判了**方案 B 的缺陷**：B 的"滚到底再回原位"若不累积，等于只抓最终视口，中间段落永久丢失。

```js
// 伪代码（冻结用，非最终实现）
async function fullScan({ restore = true } = {}) {
  const scroller = findVirtualScroller();   // 找带 --dsl-virtual-list-* 的滚动容器
  if (!scroller) return null;
  const originTop = scroller.scrollTop;

  // 🔴 必备：滚动期间暂停 MutationObserver（见 3.1.1）
  pauseObserver();
  const counts = [];
  let stable = 0;
  try {
    for (let i = 0; i < MAX_STEPS && stable < 3; i++) {
      scroller.scrollTop = scroller.scrollTop + scroller.clientHeight * 0.8;  // 分步下滚
      await sleep(STEP_WAIT_MS);              // 等虚拟列表渲染（建议 250–400ms）
      counts.push(run().items.length);        // 每步立即抓，累积进 sentIds
      stable = (counts[counts.length - 1] === counts[counts.length - 2]) ? stable + 1 : 0;
    }
  } finally {
    resumeObserver();                          // 无论成败必须恢复
    if (restore) scroller.scrollTop = originTop;   // 恢复视口（凯文要求，必须做）
  }
  return { counts, finalCount: counts[counts.length - 1] };
}
```

- **触发时机**（凯文 2026-10-13 拍板：**自动**，因为手动会忘）：
  1. `boot()` 后延迟 1.5s 自动跑一次（页面稳定后）；
  2. 保留一个手动重触发入口（面板/消息）作为兜底，但不依赖它。
- **不动** `MutationObserver` 的观察语义、`schedule()`、`DEBOUNCE_MS/MAX_WAIT_MS`（零改动是回归闸门）——只是**临时断开**再**原样接回**。
- **去重安全**：`run()` 里 `sentIds.has(message_id)` 已拦截；滚动把旧消息带回 DOM 也不会重发。

#### 3.1.1 🔴 补：fullScan 期间必须暂停 MutationObserver（凯文指出的协调问题）

**问题**：滚动会让虚拟列表大量增删 DOM 节点 → `MutationObserver` 触发 → `schedule()` → `run()`。而 `fullScan()` 自己也在调 `run()`。**两个采集路径并行**会：
- 互相打断对方的 debounce 时序（`firstScheduledAt` / `timer` 被外来触发重置）；
- 同一批节点被抓两次（浪费 POST，虽被去重挡但日志噪音）；
- 极端情况下 `run()` 重入（JS 单线程不会真并行，但 `setTimeout` 回调可能插在 `await sleep()` 中间，破坏"每步等渲染再抓"的假设）。

**修法（冻结）**：
```js
let observerPaused = false;
function pauseObserver()  { if (!observerPaused) { observer.disconnect(); observerPaused = true;  } }
function resumeObserver()  { if ( observerPaused) { observer.observe(document.documentElement || document.body,
                             { childList: true, subtree: true, characterData: true });
                           observerPaused = false; } }
```
- `pauseObserver()` 在 `fullScan()` 开头调用，`resumeObserver()` 放在 **`finally`**（异常也不能漏恢复，否则采集永久失效）。
- **`disconnect()` + 重新 `observe()` 不改变观察配置**，故仍满足"观察层零语义改动"的回归闸门。
- 恢复后应补跑一次 `run()`，把暂停期间落下的新消息收进来（流式回复在暂停期间仍在生成）。

**优点**：影响面最小、可关掉、可手动；**缺点**：长会话耗时（几十步 × 300ms）、滚动期间页面会动（后恢复）。

### 方案 B：只在 boot 时滚到底再回原位（更保守）

同 A，但**只做一次**、不提供手动重扫、不循环检测稳定。实现更简单，但对"用户翻到中间后新开的消息"覆盖不到。

### 方案 C：不滚动，改为"抓到多少算多少 + 在 diagnose 里记录缺口"

- 利用 `--dsl-virtual-list-*` 暴露的（可能存在的）总数信息，在 `diagnose` 里上报 `dom_count / total_count`，让缺口**可见**但不自动补。
- **零风险**，但**不解决问题**——只是把盲区量化。

> **agent 建议 A**（B 覆盖不全，C 不解决问题）。但 A 有视口跳动与耗时代价，且需要真机调参（`STEP_WAIT_MS` 与站点渲染速度相关）——**这条请你拍**。

---

## 4. #46 流式早抓（与 A 配套，但不并入同一改动）

- **现象**：生成中 `.ds-message` 文本未定形，此刻抓取 → 落库半截 + 文本变化后哈希变 → 重复 turn。
- **建议修法（独立第二次改动）**：在 `collect()` 时跳过"正在生成"的节点。判据候选（需真机确认哪个稳）：
  1. 该消息容器内存在 DeepSeek 的"停止生成"按钮；
  2. 容器带生成中的光标/动画元素；
  3. 文本在连续两次 `run()` 间发生变化（最通用，但要额外缓存上一次文本）。
- **⚠️ 高风险提示**（沿用 memory 既有评估）：这条动到采集判据，可能误伤正常消息；建议**先只做 A**，观察一段时间再单独评估 #46，**不要和 A 进同一个 commit**。

---

## 5. 兼容性说明

- **磁盘格式零变更**：不改 `turns.jsonl`、不改桶结构。
- **server 侧零改动**：#50 分桶去重已就位，滚动重扫带来的重复 POST 会被 `sentIds`（client）+ `_seen_keys`（server，按桶）双层拦住。
- **ChatGPT 不受影响**：ChatGPT 未见 `dsl-virtual-list`；`findVirtualScroller()` 找不到容器时 `fullScan()` 直接 `return null`，退化为现状（**必须实现这个兜底**）。
- **不修复 `sentIds.add` 提前标记**（L348 在 `post()` 前无条件执行）：那是独立 latent bug（post 失败也永久标记 → 网络抖动漏发），**本冻结不动**，保留 backlog。

---

## 6. 验收（两层：e2e 机制回归 + 真机对照）

> **为什么不用 jsdom**：虚拟滚动在 jsdom 里不可复现，造 fixture 是自欺。
> 改用**两层验收**：
> - **第一层（已落地，可复跑）**：`e2e_virtual_list/run.js` —— playwright-core + 本机 Edge headless，
>   加载**自建的虚拟滚动模拟页**（只渲染视口内消息、滚动即销毁，忠实复现 dsl-virtual-list 行为），
>   把真实 `extension/content.js` 注入进去跑。验的是**机制**，不是真实站点。
> - **第二层（需人工配合）**：下面的真机 6 步对照，验的是**真实 DeepSeek**。

### 6.1 e2e 机制回归（`node e2e_virtual_list/run.js`，当前 15/15 通过）

| # | 断言 |
|---|---|
| A0 | boot 后自动 fullScan 被触发并跑完 |
| A1 | 40 条消息**全部**入库（视口初始仅 ~3 条）→ 分步累积生效 |
| A1b | **走 L1 站点专用层**（防夹具退化到 L2） |
| A2 / A2b | message_id 无重复；**无「同文本翻转角色」重复采集** |
| A3 | 二次 fullScan 不产生新 turn（`sentIds` 去重生效） |
| A4 | 正确识别虚拟列表容器（`ran=true`） |
| A5 | 结束后 `scrollTop` 恢复原值 |
| A6 | observer 已恢复：fullScan 后新增消息仍被采集 |
| A7 | diagnose 上报 `virtual_list`（缺口可见） |
| B1–B5 | **非虚拟滚动页（ChatGPT 形态）零影响**：正常采集 / fullScan no-op / 无新增 turn / `virtual_list=null` / observer 仍工作 |

> ⚠️ **e2e 抓到的两件事（已修正，记录在案）**：
> 1. **冻结方案的终止判据是错的**：原方案用「消息条数连续 3 轮不变」判到底，但**虚拟列表的 DOM 条数本来就该恒定**
>    （那是虚拟化的意义），第 2 步就误判到底（实测 `steps=4`，只收到 10/40）。
>    **已改为「`scrollTop` 不再增长」**。→ 冻结方案被实弹修正，实现以代码为准。
> 2. **夹具自身 bug**：`data-bridge-site` 写在 `<body>` 上，而 `siteKey()` 读的是 `<html>`
>    → 整个用例悄悄跑的是 **L2 通用层**（角色按索引奇偶交替），于是出现「同文本翻转角色」的 32 组假阳性
>    与 7 vs 12 的抖动。修夹具后 15/15 且 ids=40/unique=40。
>    **教训**：加了 A1b/A2b 两道防呆断言，夹具再退化会立刻红。
>    （盘上 TURN 221/243 同文本异角色的未定性异常**不是**这个原因——真实站点走 L1；那条仍归 #22 族待查。）

### 6.2 真机验收流程（对 `s-45d3da56`，需凯文配合）

1. **基准**：手动滚到底 → 记录 `document.querySelectorAll('.ds-message').length` = `N_full`（盘上已知该会话远多于 6）。
2. **改前基线**：刷新页面、不滚动 → 记 `received.log` 里 `count=` 与落库 turn 数（已知 = 6）。
3. **改后**：刷新、不手动滚 → 由 `fullScan()` 自动收 → 落库 turn 数应逼近 `N_full`。
4. **不重复**：同一次会话内，落库 turn 数**不出现翻倍**（证明滚动重扫没产生重复）。
5. **不打扰**：`fullScan()` 结束后 `scrollTop` 回到原值（用面板/console 核对）。
6. **缺口可见**：`diagnose` 里报 `dom_count` 与 `virtual_list`（scrollHeight/clientHeight），缺口 != 0 时能被发现。

### 回归闸门（必须全绿才算过）

- [x] e2e 场景 2：ChatGPT 形态页面行为与改前完全一致 —— `fullScan()` no-op 兜底
- [x] 现有 `run()` / `schedule()` / debounce 参数**零改动**（仅新增 L4 段）
- [ ] `received.log` 无 `TURN DUP` 激增（真机观察）
- [ ] server `/health` 正常，`dedupe_keys` 无异常膨胀（真机观察）

---

## 7. 回滚方案

- **单 commit**：只动 `extension/content.js`（新增 `fullScan()` + `findVirtualScroller()` + boot 触发/`runtime.onMessage` 手动触发）。
- **回滚**：`git revert <sha>` → 回到"只抓首屏"，零副作用；已落库数据不受影响。
- **紧急关停**：把 `fullScan` 的自动触发改成手动触发即可（一行），无需 revert。

---

## 8. 风险 / 注意

1. **视口跳动**：自动滚动会让用户看到页面自己动。缓解 = 结束后恢复 `scrollTop`；若仍嫌打扰，**只开手动触发**。
2. **耗时**：长会话（数百条）可能要几十步 × 数百 ms = 十几秒。缓解 = `MAX_STEPS` 上限 + 稳定 3 轮即停。
3. **滚动触发大量 mutation** → 频繁 `schedule()`：debounce/maxwait 已能吸收；但需实测 CPU。
4. **站点反自动化**：程序化滚动理论上可能被识别。缓解 = 分步、拟人步长（0.8 屏）、不循环重扫。
5. **参数需真机调**：`STEP_WAIT_MS` / `MAX_STEPS` / 稳定轮数依赖站点渲染速度，**冻结阶段不定死**，实现时以真机观测为准。
6. **冻结边界**：本方案**只解决采集面不足**（虚拟滚动）。#51（`.txt` 附件正文不在 DOM）、#22（L1/L2 双写）、#14（L2 输入框误抓）**不在本冻结范围**。

---

## 9. 凯文拍板结果（2026-10-13，已定，不再待议）

| # | 决策点 | **已拍** |
|---|---|---|
| 1 | 方案 A / B / C | **A**（分步下滚+累积；B 只抓最终视口是错的，C 不解决问题） |
| 2 | 自动 vs 仅手动 | **自动**（boot 检测到虚拟列表即 fullScan；手动你会忘） |
| 3 | 恢复 `scrollTop` | **必须做**（否则每次采集都把视口踹到底部） |
| 4 | 暂停 MutationObserver | **必须做**（凯文指出的协调问题，见 §3.1.1，`finally` 恢复） |
| 5 | `STEP_WAIT_MS` / `MAX_STEPS` | 真机调，不定死 |
| 6 | #46 流式检测是否并入 | **不并入**（流式是"assistant 生成中 DOM 在变"，与虚拟滚动无关，风险高，单独评估） |
| 7 | 验收方式 | **不用 jsdom**（虚拟滚动造 fixture 是自欺），真机 6 步对照 + 4 项回归闸门 |

> 待实现时只剩 §8 Checklist 里的工程动作，**无需再回来问口径**。
