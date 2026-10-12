"""
#50 回归回放：去重键加 (host, project) 维度。

直接 import server 模块，在内存态重建 _seen_keys 分桶，验证：
  Case 1  跨项目同文本 -> 各自保留（不再被全局吞）
  Case 2  同项目重复 POST -> 仍 TURN DUP（去重仍有效）
  Case 3  历史 project_id=None 归 '_no_project' 桶，不污染 s-/g-p- 桶
  Case 4  b7d635d5 旧 None 桶 -> 新 s-b7d635d5 重发不再 DUP（#50 核心修复）
  Case 5  b7d635d5 同桶重发 -> 仍 DUP（去重仍有效）
  Case 6  跨 host 同文本 -> 各自保留

不启动真实 HTTP 服务；不读真实 turns.jsonl（避免副作用）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

# 单测隔离：redirect 落盘 + stub runtime，避免污染真实 turns.jsonl / 触发 LLM。
server.TURNS_PATH = os.path.join(tempfile.gettempdir(), "replay50_turns.jsonl")
open(server.TURNS_PATH, "w", encoding="utf-8").close()
server.get_runtime = lambda host, project=None: {"obj": None, "state_file": server.TURNS_PATH, "state": {}}


def reset():
    server._seen_keys = {}
    server._turn_seq = 0


def prime_none(host, project, records):
    """模拟 _prime_from_disk：历史 turn 按 (host, project) 分桶灌入。"""
    for rec in records:
        h = server._safe_host(rec.get("host") or rec.get("source") or "unknown")
        p = server._safe_project(rec.get("project_id"))
        server._seen_keys.setdefault((h, p), set()).add(server._dedupe_key(rec, h, p))


results = []


# --- Case 1：跨项目同文本，期望都进 ----------------------------------------
reset()
pa = {"source": "chat.deepseek.com", "project_id": "s-A", "role": "user", "text": "X", "message_id": "m1"}
pb = dict(pa, project_id="s-B")
prime_none("chat.deepseek.com", "s-A", [pa])
r1 = server.process_turn(pb)
results.append(("Case1 cross-project-not-deduped",
                r1["duplicate"] is False and ("chat.deepseek.com", "s-B") in server._seen_keys))


# --- Case 2：同项目重复 POST，期望第二次跳过 --------------------------------
reset()
p = {"source": "chat.deepseek.com", "project_id": "s-A", "role": "user", "text": "X", "message_id": "m1"}
r2a = server.process_turn(p)
r2b = server.process_turn(p)
results.append(("Case2 same-project-dedup-still-works",
                r2a["duplicate"] is False and r2b["duplicate"] is True))


# --- Case 3：历史无 project 不污染 s- 桶 ------------------------------------
reset()
hist = {"source": "chat.deepseek.com", "project_id": None, "role": "user", "text": "Y", "message_id": "m9"}
prime_none("chat.deepseek.com", None, [hist])
live = dict(hist, project_id="s-B")
r3 = server.process_turn(live)
results.append(("Case3 priming-no-project-isolated",
                r3["duplicate"] is False
                and ("chat.deepseek.com", "_no_project") in server._seen_keys
                and ("chat.deepseek.com", "s-B") in server._seen_keys))


# --- Case 4：b7d635d5 旧 None 桶 -> 新 s-b7d635d5 重发不再 DUP --------------
# 用 2026-10-12 02:35 诊断里 b7d635d5 的真实 message_id（content-hash 退化值）
# 复现：2026-10-08 在 project_id=None 抓过 -> 今天带 s-b7d635d5 重发应进库。
reset()
B7 = "s-b7d635d5-c7fa-4416-8d59-8c079c0e670b"
old_ids = ["h:01c64142", "h:036dfa1c", "h:7aa4ed03", "h:ba40b991", "h:7090af12", "h:8c1d2e3f"]
old_capture = [{"source": "chat.deepseek.com", "project_id": None,
                "role": "user", "text": "t-" + m, "message_id": m} for m in old_ids]
prime_none("chat.deepseek.com", None, old_capture)          # 模拟旧全局 None 桶
dup_on_resend = 0
for m in old_ids:
    p = {"source": "chat.deepseek.com", "project_id": B7,
         "role": "user", "text": "t-" + m, "message_id": m}
    if server.process_turn(p)["duplicate"]:
        dup_on_resend += 1
results.append(("Case4 b7d635d5 re-send not DUP (was the #50 bug)", dup_on_resend == 0))


# --- Case 5：b7d635d5 同桶重发 -> 仍 DUP（去重仍有效）----------------------
dup_same_bucket = 0
for m in old_ids:
    p = {"source": "chat.deepseek.com", "project_id": B7,
         "role": "user", "text": "t-" + m, "message_id": m}
    if server.process_turn(p)["duplicate"]:
        dup_same_bucket += 1
results.append(("Case5 b7d635d5 same-bucket re-dedup", dup_same_bucket == len(old_ids)))


# --- Case 6：跨 host 同文本 -> 各自保留 -------------------------------------
reset()
pc = {"source": "chat.deepseek.com", "project_id": "s-A", "role": "user", "text": "Z", "message_id": "mz"}
pg = dict(pc, source="chatgpt.com")
prime_none("chat.deepseek.com", "s-A", [pc])
r6 = server.process_turn(pg)
results.append(("Case6 cross-host-not-deduped",
                r6["duplicate"] is False and ("chatgpt.com", "s-A") in server._seen_keys))


# --- 报告 ------------------------------------------------------------------
all_pass = True
print("=" * 64)
print("#50 回放结果")
print("=" * 64)
for name, ok in results:
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    all_pass = all_pass and ok
print("-" * 64)
print("TOTAL: %d/%d PASS" % (sum(1 for _, ok in results if ok), len(results)))
print("=" * 64)
sys.exit(0 if all_pass else 1)
