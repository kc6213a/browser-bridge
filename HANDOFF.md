# 人工待办清单（Browser Bridge）— ⚠️ v0.1 遗留，已被 v0.2 作废

> **本文已过时，仅作历史证据保留。**
> v0.2 起分工改变：扩展内置三层选择器（站点专用 → 通用启发式）与自诊断模式，
> **用户不需要 F12**，所有 DOM 信息由扩展自己 POST 到 `http://localhost:8787/diagnose`。
> 新的操作指南见 `install.md`。下面「选择器留 null / 需人工 F12」等条目均已失效。

已跑通的部分（我做的，有证据）：
`--load-extension` 真实加载扩展 → content.js 注入 → MutationObserver 捕获 →
background service worker 转发 → 本地接收端 127.0.0.1:8787 落盘并打印。
自建测试页 3 条消息全部抵达（见 received.log）。

卡住的部分：真实站点（chatgpt.com / claude.ai）在我的自动化浏览器里只返回
Cloudflare 403「正在进行安全验证」，MSG_CANDIDATES=0，content.js 未注入，
因此**消息 DOM 选择器一律留 null，不猜**。

---

## 1. 确认消息 DOM 选择器
- **谁做**：人（在已登录的浏览器里）
- **为什么我做不了**：自动化访问返回 403 挑战页，页面里没有任何消息节点；
  没有真实节点可抓，写出来的选择器必然是猜测，属于伪造证据。
- **人该怎么做**：
  1. 打开 chatgpt.com（或 claude.ai），随便选一个已有会话
  2. F12 → Elements，点中一条**完整消息**（含正文）的那个元素
  3. 右键 → Copy → Copy outerHTML，把前 500 字符发给我
  4. 再对同一条消息记录：区分 user / assistant 的属性名（如 `data-message-author-role`）
- **做完后我怎么接**：把 `messageRoot / roleAttr / textNode` 三个值填进
  `extension/content.js` 的 `SEL`，然后用 `selftest` 模式对这份真实 outerHTML
  做一次离线解析 dryrun，确认能抽出正文后再交付。

## 2. 提供登录态 / 过 Cloudflare
- **谁做**：人
- **为什么我做不了**：自动化浏览器被判定为 bot（403 + Turnstile）；
  要拿到登录态只能挂载你真实的浏览器 profile，那等于读取你的已登录会话与 cookie，
  属于需要你明确授权的操作，且有锁死你正在运行的 profile 的风险，我不擅自做。
- **人该怎么做**：在你日常使用的 Edge/Chrome 里加载此扩展即可，不需要给我任何账号信息。
  （若确要我在自动化里复用登录态，需你授权我复制一份 profile 副本启动，而非直接用原目录。）
- **做完后我怎么接**：登录态由你的浏览器提供，扩展加载后即可工作；我只需核对
  `/recent` 是否开始收到消息。

## 3. 把扩展加载进你日常在用的浏览器
- **谁做**：人（约 30 秒）
- **为什么我做不了**：我能在自动化实例里用 `--load-extension` 加载（已验证），
  但那只作用于我自己的临时 profile，不会出现在你日常浏览器里；
  而 `edge://extensions` 页面的「加载已解压的扩展程序」依赖系统原生文件选择对话框，
  浏览器自动化无法操作它。
- **人该怎么做**：
  1. 打开 `edge://extensions`（Chrome 则是 `chrome://extensions`）
  2. 打开右下角「开发人员模式」
  3. 点「加载已解压的扩展程序」
  4. 选目录：`C:\Users\Kevin Chan\WorkBuddy\conversation-agent-bridge\extension`
  5. 确认接收端在跑：`python conversation-agent-bridge/server.py`
- **做完后我怎么接**：你在网页发一条测试消息，我读 `received.log` 或
  `curl http://127.0.0.1:8787/recent` 验证原文是否进来。

## 4. 验证真实站点上 content.js 是否被挑战页阻断注入
- **谁做**：人（顺手确认）
- **为什么我做不了**：我的实例里页面停在 CF 挑战页，`data-bridge-ready` 始终为 null，
  `INJECTED_LOGLINE=false`；无法区分「被 CF 阻断」还是「挑战页本身不触发注入」。
- **人该怎么做**：加载扩展后打开 chatgpt.com，F12 Console 搜 `[browser-bridge]`，
  应看到 `content script ready on chatgpt.com`。
- **做完后我怎么接**：若没有这行日志，我改为「background 侧定时轮询 + 动态注入
  （chrome.scripting.executeScript）」的兜底方案。

## 5. 合规确认
- **谁做**：人（决策）
- **为什么我做不了**：这是数据合规判断，不是技术问题。
- **人该怎么做**：确认把网页会话内容转发到本地 8787 端口是否符合你所在组织的
  数据处理规定（尤其涉及工作账号/公司数据时）。
- **做完后我怎么接**：如需加固，我把接收端改为仅监听 127.0.0.1（已是）+ 加 token 校验 + 落盘加密。

## 6. 流式输出的节流策略
- **谁做**：人（确认需求）/ 我（改代码）
- **为什么我做不了**：我不知道你要「逐 token 流式转发」还是「每条消息只发一次完整文本」，
  这是产品决策；我目前的实现在 `characterData` 变更时会重复 upsert 同一节点（已用
  WeakSet 去重，但流式场景下会导致只发第一帧）。
- **人该怎么做**：告诉我选 A（流式增量）还是 B（消息完成后发一次）。
- **做完后我怎么接**：A → 加 debounce + 按 messageRoot 覆盖式发送；B → 等 assistant
  停止生成（检测停止按钮消失 / 文本稳定 1.5s）再发。
