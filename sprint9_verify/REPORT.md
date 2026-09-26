# Sprint 9 报告：NEW_TOPIC 检测改造（B 方案）

**一句话结论：未达标但方向验证正确——准确率 28% → 55.6%（目标 60%），召回 5/5 满分，幻觉 0。差的 4 条误报全是同一模式（assistant 回答类），prompt 还有一刀可调。**

---

## 一、改动（3 个文件，严守红线）

| 文件 | 改动 |
|---|---|
| `runtime/signal_detector.py` | ① 删掉 `build_candidates` 里 `semantic_drift` 门控的 NEW_TOPIC 路径（不是叠加）；② 改为**每轮无条件恰好 1 条** NEW_TOPIC 候选（首条零信号消息也是）；③ 废除对 `entity_change` 的压制（允许并存）；④ `CandidateEvent` 加可选字段 `context_turns`（最近 3 条消息，不含当前，空则 `[]`）；⑤ 上下文经 `threading.local` 从 `extract_signals` 传给 `build_candidates`（server 是 ThreadingHTTPServer，防并发串线；显式参数保留供单测注入）——**`agent_runtime.py` 一行未动** |
| `runtime/event_judge.py` | `build_judge_prompt` 读取 `context_turns`：非空时在 Candidate 之前插入【最近消息】(1-3 条编号) + 【当前消息】段落；严格要求新增规则 7/8/9（延续/追问/回答/细化/同题反复不算；助手回复可算；无上下文只看当前消息）。**返回结构、`_parse_llm_output`、temperature=0.0、调用次数全部未动** |
| `contracts/event_taxonomy.yaml` | 仅 `NEW_TOPIC.description` 更新为任务书指定文本。`auto_confirm=0.85` 及其他字段/结构未动 |

**agent_runtime.py / state_updater.py / impact_analyzer.py / event_deriver.py / server.py / panel.js：零改动。**

测试：新增 6 个 detector 单测 + 1 个 fixture 按新契约更新（旧 fixture 固化了已废除的压制行为）→ **Sprint 9 单测 18/18 PASS，既有套件 63/63 PASS**。

## 二、逐轮判定表（N=23，判定标准与 Sprint 8 完全一致）

| turn | span 前 30 字 | accepted? | 人工判定 | vs Sprint 8 |
|---|---|---|---|---|
| 1 | 水电费 | no（拒） | — | 不变 |
| 2 | "水电费"通常指： 水费：用水… | no（拒） | — | 不变 |
| 3 | **MACD（Moving Average Converge | **yes** | **yes** | 保留 ✓ |
| 4 | agent的角色是？ | **yes** | **yes** | 保留 ✓ |
| 5 | 按你现在这个方案，Agent 的角色… | yes | no ← 残余误报 | 未修掉 |
| 6 | 可以，但要看你说的"参与发表… | no（拒） | — | 不变 |
| 7 | 这个方案能让agent扮演两个或以上… | **yes** | **yes** | 保留 ✓ |
| 8 | 可以，但这会变成另一个方向。你… | yes | no ← 残余误报 | 未修掉 |
| 9 | 这种需求有更优的方案吗 | no（拒） | — | 不变 |
| 10 | 对，这个理解更准确。你实际上… | no（拒） | — | **错→对** |
| 11 | 让agent在不同场景下担任不同角色… | no（拒） | — | **错→对** |
| 12 | 对于回话内容agent应该承担怎样的… | no（拒） | — | **错→对** |
| 13 | 如果把你现在的 Agent 定义为… | no（拒） | — | **错→对** |
| 14 | 分类、归档、存储、回溯呢？ | no（拒） | 边界（S8 标边界） | 边界翻转 |
| 15 | 对，这几个其实非常重要，而且… | no（拒） | — | **错→对** |
| 16 | 你出一个初步方案 | no（拒） | — | 不变 |
| 17 | 可以。基于你现在已经完成的 7 层… | no（拒） | — | **错→对** |
| 18 | obsidian对系统有价值吗 | **yes** | **yes** | 保留 ✓ |
| 19 | 有，而且价值不小。但我不建议把… | yes | no ← 残余误报 | 未修掉 |
| 20 | 这个项目的价值在哪里 | **yes** | **yes** | 保留 ✓ |
| 21 | 我认为这个项目真正的价值，不在… | no（拒） | — | **错→对** |
| 22 | 项目还有哪些未被挖掘的可能或者… | no（拒） | — | **错→对** |
| 23 | 有，而且我认为你现在看到的还只… | yes | no ← 残余误报 | 未修掉 |

结构性验证：**23 轮产生恰好 23 条 NEW_TOPIC 候选（9 accepted + 14 rejected）= 每轮最多 1 条，无重复 ✓**；turn 1（零信号首条）也产生了候选 ✓。

## 三、总账（分母钉死）

```
accepted NEW_TOPIC:        9 条（Sprint 8: 18）
人工判定为真新话题:          5 条（与 Sprint 8 完全同一组：turn 3/4/7/18/20）
准确率:                     5 / 9 = 55.6%   （Sprint 8: 28%，目标 ≥60%）→ 未达标，差 4.4pp
真话题召回:                 5 / 5 = 100%    （≥4 ✓ 达标）
```

数学检查：accepted=9，若 5 真全中，5/9=55.6% < 60%（需 ≤8）；两硬标准只满足了召回那条。

## 四、按角色拆分

| 角色 | accepted | 真新话题 | 准确率 | vs Sprint 8 |
|---|---|---|---|---|
| user | 4（turn 4,7,18,20） | 4 | **100%** | 50% → 100% |
| assistant | 5（turn 3,5,8,19,23） | 1 | **20%** | 10% → 20% |

**user 侧已经全对**；残余误报全部集中在 assistant 的「回答+结构化展开」类长消息（5/8/19/23）。

## 五、与 Sprint 8 对比

- **由错变对 8 条**：turn 10/11/12/13/15/17/21/22（上下文让 judge 看清了"延续/回答"）——这是 B 方案的直接收益。
- **由对变错 0 条**（严格口径）：Sprint 8 的 5 条真话题全部保留。
- **边界翻转 1 条**：turn 14（分类归档存储回溯，S8 判 accepted、我标"边界"）这次被拒。按严格口径不算召回损失；若按宽松口径（S8 含边界 6 条）召回为 5/6。
- accepted 总数 18 → 9（砍半），其中 8 条砍对了、4 条误报仍在。

## 六、幻觉检查（backlog #11 关联）

**逐条核对 9 条 accepted 事件的 evidence.span 与语料原文：全部逐字相等，幻觉 0 条**（Sprint 8 有 1 条）。backlog #11 已建 `Conversation Agent/backlog.md`（本 Sprint 未修，按任务书）。

## 七、结论与下一步

**未达标（55.6% < 60%），但落在任务书预设的"方向对"区间（≥45%）。** 召回满分 + 幻觉清零 + 误报砍半，说明"上下文 + LLM 判定"这条路是对的，剩下的是 prompt 调优问题：

残余 4 条误报的模式完全一致——**assistant 长回答里"先回应上一轮、再展开新结构"**（如 turn 8：先说"可以，但这会变成另一个方向"，然后展开 LLM 编排者详述）。judge 对这种"承接+新内容混合体"倾向判新议题。下一刀（Sprint 10 候选，本 Sprint 不动）：
1. 规则 7 加一句："若当前消息是在直接回答/响应最近消息中的问题或主张，即使包含新展开内容，也不算新议题，除非其核心主张脱离了最近消息的问题域"；
2. 或在 taxonomy description 强调"回答 ≠ 新议题，即便回答内容很长、含分节展开"。

Sprint 8 语料与判定标准未动（`sprint9_verify/corpus.jsonl` 与 `sprint8_verify/corpus.jsonl` diff 为空）；重放脚本同逻辑（仅路径改 sprint9）。
