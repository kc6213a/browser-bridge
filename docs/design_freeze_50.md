# #50 方案冻结：server 去重键加 (host, project) 维度

> 状态：**IMPLEMENTED（2026-10-12 已落地，单 commit 可 `git revert`）**
> 作者：agent（按凯文指令，仅出方案，不动 `server.py` / `extension/`）
> 关联 issue：#50（capture 漏抓——全局去重键不含 project），同族 #44 / #46（时机类），#40-B（二级分区）
> 冻结日期：2026-10-09

---

## 1. 根因（已确证，非假设）

b7d635d5 排查实测闭环：

- `server.py:68` `_seen_keys = set()` —— **进程级全局单 set，无 host/project 维度**。
- `server.py:319-325` `_dedupe_key(payload)` —— 只返回 `"id:"+message_id`（或退化 `"hash:"+sha1(role|text)`），**完全不含 project / host**。
- `server.py:328-355` `_prime_from_disk()` —— 读整份 `turns.jsonl`，对每条 `rec` 算 `_dedupe_key(rec)` 灌进**同一个全局 set**，不分项目。
- `server.py:471-494` `process_turn()` —— `if key in _seen_keys: return TURN_DUP`，跨所有项目共享。

实证：b7d635d5 那 5 条消息与 `turns.jsonl` 583–588 行（2026-10-08 `project_id=None` 抓的同一场对话）逐字相同，`received.log` 14:27 共 **25 条 `TURN DUP`** 证明 turn 到过 server 只是被去重吞。

**结论**：#40-B 建了二级分区桶，但 server 去重层是全局跨项目共享的 →「同一段话在 A 项目抓过，就永远不能在 B 项目再抓」。这是 #40-B 分区的正确性漏洞，不是 client `sentIds` transient（旧假设已推翻）。

---

## 2. 目标

1. **跨项目独立**：同一文本在不同 `(host, project)` 各自保留，不再被全局去重吞掉。
2. **同项目仍去重**：同一 `(host, project)` 内重复 POST（含网络重发、页面重抓）仍判 `TURN DUP`。
3. **零数据迁移**：磁盘 `turns.jsonl` 与 bucket 目录结构不变，只改内存去重分桶；重启后由 `_prime_from_disk` 重建，行为一致。
4. **单 commit、可 `git revert`**：只动 `server.py` 内存态，不碰 `extension/`。

---

## 3. 改动清单（冻结，待实现）

### 3.1 数据结构

```python
# server.py:68  —— 由全局 set 改为 分桶 dict
_seen_keys = dict()   # key: (host:str, project:str)  ->  value: set[str]
```

- 字典键：`(host, project)` 二元组。
- 字典值：去重键集合（原 set 内容），元素形态不变（`"id:..."` / `"hash:..."`）。
- `host`：`_safe_host(source)`；`project`：`_safe_project(project_id)`（None → `_no_project`）。

### 3.2 `_dedupe_key` 改签名

```python
# server.py:319-325  —— 加 host / project 入参
def _dedupe_key(payload, host=None, project=None):
    mid = payload.get("message_id")
    if isinstance(mid, str) and mid.strip():
        return "id:" + mid.strip()
    base = "%s|%s" % (payload.get("role") or "?", payload.get("text") or "")
    return "hash:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
```

**设计说明（请凯文拍板一处）**：
- 返回的**内层字符串不变**（`"id:..."` / `"hash:..."`）。`host/project` 入参的用途是**选择 / 计算外层桶**，不嵌进字符串本身——分桶由字典键完成。
- 保留 `host=None, project=None` 默认值是为兼容可能的旧调用点（priming 一定传），不改变现有 key 形态，保持「去重键字符串零改动」的回归闸门。
- 备选（不采用，除非你要）：把 `host/project` 嵌进字符串 `["%s|%s|%s" % (host, project, mid)]`。缺点：与历史 `id:`/`hash:` 形态不兼容，priming 重建时需重算，增加回放复杂度。**当前冻结选「外层 dict 分桶、内层字符串不变」**。

### 3.3 `_prime_from_disk` 改造

```python
# server.py:328-355  —— 历史 turn 按 (host, project or '_no_project') 分桶
def _prime_from_disk():
    global _turn_seq
    if not os.path.exists(TURNS_PATH):
        return
    texts = []
    with open(TURNS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            _turn_seq += 1
            host = _safe_host(rec.get("host") or rec.get("source") or "unknown")
            project = _safe_project(rec.get("project_id"))
            bucket = (host, project)
            _seen_keys.setdefault(bucket, set()).add(_dedupe_key(rec, host, project))
            tn = rec.get("_turn"); at = rec.get("at")
            if isinstance(tn, int) and isinstance(at, str):
                _turn_at[tn] = at
            if isinstance(rec.get("text"), str):
                texts.append(rec["text"])
    _recent_texts.extend(texts[-RECENT_WINDOW:])
    if _turn_seq:
        total = sum(len(s) for s in _seen_keys.values())
        _log("primed %d dedupe keys across %d buckets, %d turn timestamps from existing %s"
             % (total, len(_seen_keys), len(_turn_at), TURNS_PATH))
```

关键：历史 `project_id=None`（pre-#40-B，如 2026-10-08 那批）→ `_safe_project` → `_no_project` 桶，与运行时未分区桶口径一致。

### 3.4 `process_turn` 改造

```python
# server.py:471-494  —— 查对应桶
def process_turn(payload):
    global _turn_seq
    text = payload.get("text") or ""
    role = payload.get("role") or "?"
    source = payload.get("source") or "unknown"
    mid = payload.get("message_id")
    mid_source = payload.get("message_id_source")

    host = _safe_host(source)
    project = _safe_project(payload.get("project_id"))
    bucket = (host, project)

    key = _dedupe_key(payload, host, project)
    with _lock:
        bset = _seen_keys.setdefault(bucket, set())
        if key in bset:
            _log("TURN DUP  bucket=%s skipped message_id=%s source=%s key=%s"
                 % (bucket, mid, mid_source, key))
            return {
                "ok": True,
                "duplicate": True,
                "turn": None,
                "runtime_status": "skipped_duplicate",
                "message_id": mid,
            }
        bset.add(key)
        _turn_seq += 1
        turn_no = _turn_seq
        recent = list(_recent_texts[-RECENT_WINDOW:])
    # ... 后续不变
```

---

## 4. 兼容性说明

- **pre-#40-B 历史归 `_no_project`**：历史 `turns.jsonl` 里 `project_id=None` 的 turn，priming 时 `_safe_project(None)` → `_no_project`，落在 `(_safe_host(source), "_no_project")` 桶；运行时未带 `project_id` 的 turn 也落同一桶。**不跨桶撞** s-/g-p- 桶。
- **磁盘格式零变更**：`turns.jsonl` 每行结构不变，bucket 目录 `runtime/hosts/<host>/<project>/` 不变。仅内存去重分桶，无数据迁移。
- **重启幂等**：`_prime_from_disk` 重建的分桶结果与运行时一致，重复启动不产生偏差。
- **唯一行为变化（符合预期）**：原本跨项目 `TURN DUP` 的现在**不 DUP**。例：b7d635d5 那 5 条昨天在 `_no_project` 桶，今天在 `s-b7d635d5` 重发 → 落新桶 → 进库。这正是 #50 要修的。
- **不修复 `content.js` `sentIds.add` 提前标记**：那是独立次生 latent bug（post 失败也永久标记 → 网络抖动漏发），同族并入 #44 / #46，本冻结不动 client。

---

## 5. 回放测试（冻结，实现时用 pytest）

> 用 `_prime_from_disk` + `process_turn` 的等价内存态做单元回放；不依赖真实 server 进程。

### Case 1 —— 跨项目：同文本 A 抓过 → B 重发，期望都进

```python
def test_cross_project_not_deduped():
    _seen_keys.clear()
    payload_a = {"source": "chat.deepseek.com", "project_id": "s-A",
                 "role": "user", "text": "X", "message_id": "m1"}
    payload_b = dict(payload_a, project_id="s-B")   # 同文本，不同 project
    # 模拟 A 已抓（priming 或运行时）
    _seen_keys.setdefault(("chat.deepseek.com","s-A"), set()).add(_dedupe_key(payload_a, "chat.deepseek.com","s-A"))
    r = process_turn(payload_b)
    assert r["duplicate"] is False, "跨项目同文本应各自保留"
    assert ("chat.deepseek.com","s-B") in _seen_keys
```

### Case 2 —— 同项目重复 POST，期望第二次跳过（去重仍有效）

```python
def test_same_project_dedup_still_works():
    _seen_keys.clear()
    payload = {"source": "chat.deepseek.com", "project_id": "s-A",
               "role": "user", "text": "X", "message_id": "m1"}
    r1 = process_turn(payload)
    r2 = process_turn(payload)          # 同 (host, project) + 同 message_id
    assert r1["duplicate"] is False
    assert r2["duplicate"] is True, "同项目内重复 POST 仍应 TURN DUP"
```

### Case 3 —— 兼容性：历史无 project 不污染 s-/g-p- 桶

```python
def test_priming_no_project_isolated():
    _seen_keys.clear()
    hist = {"source": "chat.deepseek.com", "project_id": None,
            "role": "user", "text": "Y", "message_id": "m9", "_turn": 588, "at": "2026-10-08T..."}
    _prime_from_disk()  # 实际会读 TURNS_PATH；单测可 monkeypatch TURNS_PATH 为一行 hist
    # 运行时同文本带 s-B 重发，不应被历史 None 桶吞
    live = dict(hist, project_id="s-B")
    r = process_turn(live)
    assert r["duplicate"] is False
    assert ("chat.deepseek.com","_no_project") in _seen_keys   # 历史在 _no_project 桶
    assert ("chat.deepseek.com","s-B") in _seen_keys           # 新桶独立
```

---

## 6. 回滚方案

- **单 commit**：本次只改 `server.py`（4 处：L68 数据结构、L319-325 签名、L328-355 priming、L471-494 process_turn）。不跨文件、不动 `extension/`、不动磁盘格式。
- **回滚命令**：`git revert <sha>` 即回到全局 `set()` 形态，零副作用。
- **回滚后行为**：回到「跨项目共享去重」旧语义（即 #50 现象复现），但不丢任何已落库数据（`turns.jsonl` 不变）。

---

## 7. 风险 / 注意（冻结时已知）

1. **内存占用**：dict 分桶后总 key 数不变，仅多一层 dict 壳，可忽略（当前 ~1953 keys / 4 桶 → 分桶后总量不变）。
2. **唯一语义变化**：跨项目同文本不再 DUP → 同一对话在多个项目下会各自留痕。这是预期修复，不是回归；面板/state 按桶隔离，互不污染。
3. **`_safe_host` 依赖**：priming 用 `rec.get("host") or rec.get("source")`；确认 `rec` 字段存在（历史行有 `source`，无 `host` → 走 `source` 分支，与运行时 L479 一致）。
4. **`_log` 多打 `bucket=`**：仅日志增强，不影响返回结构；`/health` 的 `dedupe_keys` 计数（L635）应改为 `sum(len(s) for s in _seen_keys.values())` 以反映总分桶键数（兼容性微调，列入实现清单）。
5. **冻结边界**：本方案只解决 #50 去重分桶；#48/#49（extractor 噪声、state_updater 丢 value）、#44/#46（client 时机类）不在本冻结范围。

---

## 8. 实现 Checklist（冻结后动代码时用）

- [ ] L68 `_seen_keys = dict()`
- [ ] L319-325 `_dedupe_key(payload, host=None, project=None)`（内层字符串不变）
- [ ] L328-355 `_prime_from_disk` 按 `(host, project)` 分桶，`_safe_host(rec.get("host") or rec.get("source"))`
- [ ] L471-494 `process_turn` 算 `bucket=(host, project)`，`_seen_keys.setdefault(bucket,set())` 查/加
- [ ] L635 `/health` `dedupe_keys` 计数改 `sum(len(s) for s in _seen_keys.values())`
- [ ] 单测 Case 1 / 2 / 3 全绿
- [ ] 重启 server，`/health` 验证 `dedupe_keys` 与 `buckets` 数；观察 `received.log` 确认 b7d635d5 重发不再 `TURN DUP`
- [ ] 单 commit，`git revert` 可回
