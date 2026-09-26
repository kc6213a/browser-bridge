# Sprint 7 执行报告：议题节点 v0.1

## 一句话
**议题节点建出来了。** 3 条新话题消息 → 3 个 `topic` 节点落进 state.json，面板第一次出现「议题」区块（在假设和结论之间），3 条带 [turn N]。

---

## 0. 清测试痕迹（先做，按执行顺序）
- 停旧 server（pid 4736）；归档 Sprint 6 测试痕迹：
  - `received/runtime/{events,state,effects}.jsonl` + `received/turns.jsonl` → `received/_archive_sprint7_20260926_135232/`
  - canonical `Conversation Agent/runtime/{state.json,event_log.jsonl}` → `runtime/_archive/*sprint7_20260926_135232*`
- 起新 server：**pid 38252**，v0.4.0，wired，llm=real(deepseek-chat)，turns=0，`/state → null`（干净基线）。

## 1. 改动（严守红线）
| 文件 | 改动 |
|---|---|
| `runtime/state_updater.py` | 加 `_apply_new_topic`（+ `_topic_title_from_span` / `_topic_node_id`）+ `apply_event` 的 `NEW_TOPIC` 分支 + `__all__`。**未动**任何既有 handler |
| `extension/panel.js` | 加 `renderTopics`，插在假设和结论之间；`title  [turn N]`；无节点显示「尚无议题」 |

- 节点结构严格按任务书：`{id: "topic:<sha1(title)前8位>", type: "topic", title, status: "active", source_turns: [N], versions: [], metadata: {}}`
- 确定性 id（hash 输入 = title）；同 title 重复触发 → 幂等不建重复
- **未动**：`contracts/*`、`agent_runtime.py`、`signal_detector.py`、`event_judge.py`、`server.py`
- 建节点门 = `event.confirmed is True`（与既有 handler 一致；rejected 在 `apply_event` 入口已被挡）。注：`apply_event` 拿不到 `judge_result`（runtime 落库时才合并），故 "uncertain"（confirmed=true 但低置信）也会建节点——与 ASSUMPTION_INTRODUCED 同语义，v0.1 接受。

## 2. 离线单元测试（先于真机）
`sprint7_verify/test_topic_handler.py` — **11/11 PASS**：确定性 id / 建节点字段 / diff / 幂等不重 / rejected 不建 / 换行清洗 / 30 字截断。`py_compile` + `node --check` 均过。

## 3. 真机验收（走真实 8787 POST /turn）
```
[warmup] turn=1 records=0 no_candidates   ← 预热，零污染
[topic1] turn=2 records=1 ok              ← 我们来聊聊新的存储芯片方案
[topic2] turn=3 records=1 ok              ← 现在讨论一下获客渠道的问题
[topic3] turn=4 records=1 ok              ← 换个话题讲讲供应链的近况
```
state.json topic 节点完整列表（nodes=3, edges=0, version=4）：
```json
{"id": "topic:b65dce89", "type": "topic", "title": "我们来聊聊新的存储芯片方案", "status": "active", "source_turns": [2], "versions": [], "metadata": {}}
{"id": "topic:654b3082", "type": "topic", "title": "现在讨论一下获客渠道的问题", "status": "active", "source_turns": [3], "versions": [], "metadata": {}}
{"id": "topic:db54eeff", "type": "topic", "title": "换个话题讲讲供应链的近况", "status": "active", "source_turns": [4], "versions": [], "metadata": {}}
```
面板截图：`sprint7_verify/panel/shots/panel_topics.png` —— 「议题」区块出现，3 条，顺序 假设→议题→结论 ✓（断言 topicCount=3、orderOk=true、hasTopicBlock=true）。

## 4. 检测器两个发现（按任务书**未修**，只绕开）
1. **全新会话第一条消息永远不触发 NEW_TOPIC**：`semantic_drift` 需要 `recent_turns` 非空，空时 drift=0。→ 用 1 条预热消息解决（turn=1, records=0）。
2. **`SEMANTIC_DRIFT_MIN_LEN=8`**：任务书示例句「我们来聊聊 A」(7字) / 「现在讨论 B」(6字) / 「换个话题讲 C」(7字)**全部低于门槛，一条都不会触发**。→ 换成 ≥8 字、无数字值（避免 entity 分支压制 NEW_TOPIC）的等价新话题句。`source_turns` 因此是 [2,3,4] 而非示例的 [1,2,3]。

## 5. 踩坑记录
- **Windows `SO_REUSEADDR` 允许双绑 8787**：一次误起未设 env 的 server，它真的绑上了 8787（与主 server 并存抢流量），已 kill。教训：起隔离实例必须 **先 export 环境变量再启动**；隔离实例务必用非 8787 端口。
- 面板隔离验证 8790/8791 用完即释放；主 server（38252, turns=4）全程健在。

## 6. 议题质量初判（供下个 Sprint 决策）
本轮 3/3 精准：每条消息恰好 1 个 topic，无噪音、无重复。但样本全是"标准话题开场白"。结合真实会话历史（导航文本、JS 代码都曾被判 NEW_TOPIC），噪音大概率还在——下一步建议：真机 ChatGPT 聊几轮自然话题，看「议题」区块长什么样，再决定是修检测还是直接做树。
