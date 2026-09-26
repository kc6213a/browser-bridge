# Browser Bridge 安装说明（傻瓜版，v0.2）

你不需要懂技术，不需要打开开发者工具（F12），不需要登录任何账号给我。
照下面 7 步做，约 1 分钟。

> 目录位置：`C:\Users\Kevin Chan\WorkBuddy\conversation-agent-bridge`

---

## 第 0 步：先启动本地接收端（不启动的话扩展抓到的东西没地方送）

双击或在本目录打开终端执行：

```
python server.py
```

看到这一行就说明启动成功：

```
[...] receiver v0.2 listening on http://127.0.0.1:8787  (pid=xxxx) recv_dir=...\received
```

**这个黑窗口要一直开着**，关掉就收不到东西了。
（关闭它 = 停止接收；要停止就在这个窗口按 `Ctrl+C`。）

---

## 第 1 步：打开 Chrome / Edge

用你**平时登录着 ChatGPT / Claude 的那个浏览器**。

---

## 第 2 步：地址栏输入扩展页地址并回车

- Edge 用户输：`edge://extensions`
- Chrome 用户输：`chrome://extensions`

> 截图占位：`screenshots/01-extensions-page.png`
> 这里应该看到：**一个空的扩展列表页面**（或你已装过的其他扩展）。

---

## 第 3 步：打开「开发者模式」

- Edge：页面**左下角**有个开关「开发人员模式」，打开它
- Chrome：页面**右上角**有个开关「开发者模式」，打开它

打开后页面上会多出三个按钮：「加载已解压的扩展程序」「打包扩展程序」「更新」。

> 截图占位：`screenshots/02-developer-mode.png`
> 这里应该看到：**开发者模式开关是蓝色/打开的**，并且出现「加载已解压的扩展程序」按钮。

> **这一步最容易漏。如果你后面什么都没收到，先回来看这一步。**

---

## 第 4 步：点「加载已解压的扩展程序」

在弹出的文件夹选择框里，粘贴或浏览到这个目录并**选中它（不用进到里面）**：

```
C:\Users\Kevin Chan\WorkBuddy\conversation-agent-bridge\extension
```

点「选择文件夹」。

> 截图占位：`screenshots/03-load-unpacked.png`
> 这里应该看到：**扩展列表里多出一张卡片，名字叫「Browser Bridge」，版本 0.2.0**。

> 如果这里报「未能成功加载扩展程序 / 清单文件缺失」，说明你选到了 `extension` 的**子目录**，
> 或者选错了文件夹 —— 回到上一步重新选 `...\browser-bridge\extension` 这一层。

---

## 第 5 步：打开 ChatGPT（或 Claude），随便发一条消息

就正常聊天：打开一个会话，发一句「你好」，等它回完。

扩展在你发消息、收到回复的瞬间会自动工作，你不需要点任何按钮。

---

## 第 6 步：看本地接收端日志有没有东西进来

回到第 0 步那个黑窗口，应该看到类似这样的行：

```
[...] DIAGNOSE layer=1 site_hit=True selector=[data-message-author-role] count=5 -> diagnose_20260926_015043.json
[...] TURN source=chatgpt.com layer=1 role=user len=12 :: '你好...'
```

同时这些文件会出现：

- `browser-bridge/received/diagnose_<时间戳>.json` —— **诊断报告**（关键：它记录扩展命中了哪一层选择器、抓到几条、真实 DOM 长什么样）
- `browser-bridge/received/turns.jsonl` —— 抓到的消息原文
- `browser-bridge/received.log` —— 上面那些日志的留档

> 截图占位：`screenshots/04-receiver-log.png`
> 这里应该看到：**至少有一行 `DIAGNOSE`，最好还有几行 `TURN`**。

---

## 第 7 步：把结果告诉我

不管有没有收到东西，**都把下面两样发给我**，我来判断下一步：

1. 黑窗口里的日志（复制文本即可）
2. `browser-bridge/received/` 目录里最新那个 `diagnose_*.json` 文件

**你全程不需要按 F12。**

---

## 三种结果的含义（给我看的，你不用管）

| 现象 | 说明 | 我接下来做什么 |
|---|---|---|
| 有 `DIAGNOSE` 且 `site_selector_hit=true` | 第一层站点专用选择器命中 | 锁定该选择器，开始抓正式数据 |
| 有 `DIAGNOSE` 但 `site_selector_hit=false` | 站点改版了，走的是第二层兜底 | 我读报告里的 `dom_signature`，补一个新的第一层选择器 |
| 什么都没有 | 扩展没加载成功 | 回到**第 3 步**（开发者模式）和第 4 步（目录选对没有） |

---

## 常见问题

**Q：扩展卡片上有个「错误」按钮 / 显示红色？**
A：点它，把红字复制给我。最常见原因是第 4 步目录选错。

**Q：我关掉了黑窗口怎么办？**
A：重新执行第 0 步。扩展会一直重试，接收端一回来就能收到。

**Q：会不会泄露我的聊天内容？**
A：数据只发到你自己电脑的 `127.0.0.1:8787`，不经过任何外网服务器，
落盘在 `browser-bridge/received/` 目录里。涉及公司/工作账号的数据请自行判断合规性。

**Q：我不想用了怎么卸载？**
A：回到第 2 步的扩展页面，点「Browser Bridge」卡片上的「删除/移除」即可，
然后 `Ctrl+C` 关掉黑窗口。
