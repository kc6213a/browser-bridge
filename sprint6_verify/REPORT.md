# Sprint 6 执行报告：role 真机验证 + 面板降噪

## 一句话结论
**role 通了。** 同一句话，`user` 说进 state、`assistant` 说不进（事件照记、`state_diff` 为空）——
这一点在**真实 ChatGPT 会话数据**和**受控 `POST /turn` 复现**里都成立。面板「最近变化」已不再出现 `rejected`。

---

## 目标 A：role 验证

### 证据 1 —— 真实机器数据（received/runtime/events.jsonl，共 40 条历史事件）
- `source_role` 字段：**40/40 全部具备**；取值集合 `['assistant', 'user']`，**无一为 `"?"`**。
- 真实 ChatGPT 抓取（content.js）已经带上了 role。
- user 事件会改状态，assistant 事件不改状态：

| 事件 | type | source_role | judge | state_diff |
|---|---|---|---|---|
| `evt_65281aa4725c` | ASSUMPTION_INTRODUCED | **user** | accepted | 非空（`nodes_added` / `versions_added`）→ 改了状态 |
| `evt_4a069bec806f` | DEPENDENCY_DECLARED | **assistant** | accepted | `{}` → 未改状态（但已记录） |

### 证据 2 —— 受控复现：走 8787 `POST /turn`（任务书 选项 2）
对**同一句** `第一年收入 = 用户量 × 留存率`，先 assistant 后 user：

| event_id | type | source_role | judge | state_diff |
|---|---|---|---|---|
| `evt_7e612692cb56` | DEPENDENCY_DECLARED | **assistant** | accepted | `[]`（空，未改状态） |
| `evt_cefe9e81e895` | NEW_TOPIC | **assistant** | accepted | `[]`（空，未改状态） |
| `evt_e0cd7a5750bf` | DEPENDENCY_DECLARED | **user** | accepted | `edges_added: 第一年收入←留存率` → **改了状态** |
| `evt_bcb0647ca4e5:stale` | CONCLUSION_STALE | **user** | derived | 派生 stale |

**state.json 变化**：`edges 2 → 3`（user 加了边）；assistant 那两条没有产生任何 edges/nodes 变化。

> 机制（runtime/agent_runtime.py:319-338）：
> `judged["source_role"] = role`；`if is_user: state_diff = _apply_to_state(...) else: state_diff = {}`。
> 即「仅 user 角色作用于 state；其余角色事件照记、状态不动」。

---

## 目标 B：面板过滤 rejected

改 `extension/panel.js`：新增 `isRejectedNoise()`，`renderRecentChanges()` 先过滤 `judge_result == "rejected"`
的行，再取过滤后最新 5 条倒序显示。

- 行格式：`TYPE:judge_result@turn=N`（无 judge 时形如 `TYPE@turn=N`，保留）。
- 独立验证：8790（真实 panel.js + mock state）注入浏览器，断言 `hasRejected=false`、`hasAccepted=true`。
- 截图：`panel/shots/panel_filtered.png`（「最近变化」只剩 accepted / derived / 无 judge 的行）。

---

## 改动清单（严守红线）
- **改**：`extension/panel.js`（仅最近变化的过滤逻辑）。
- **未动**：`runtime/*`、`contracts/*`、`content.js`、`server.py`。
- **未重启 8787**：全程用独立端口 8790/8791 做面板验证，用完即释放；8787 始终 HTTP 200。

## 副作用说明（预期验收产物，非 bug）
按任务书 选项 2 走真实 8787 的 `POST /turn`，会在真实 runtime 留下：
- 新节点 `验证字段`（user 消息「假设 sprint6 验证字段是 7」）；
- 新边 `第一年收入 ← 留存率`（user 消息「第一年收入 = 用户量 × 留存率」）；
- `events.jsonl` 40 → 46 行。

这些正是「user 消息 → state.json 有变化」的验收证据。

## 待办
- ChatGPT 限流恢复后，用真机复核一次（content.js 实测抓取），闭环 Sprint 6 的真机那一环。
