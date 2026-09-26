#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 7 离线单元测试：_apply_new_topic（不依赖 server / LLM）。"""
import hashlib
import sys

sys.path.insert(0, r"C:/Users/Kevin Chan/WorkBuddy/Conversation Agent")

from runtime.state_updater import apply_event, _topic_node_id, _topic_title_from_span  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


def fresh_state():
    return {"version": 1, "nodes": [], "edges": [], "events": [], "working_state": {}}


def topic_event(span, confirmed=True, turns=None, eid="evt_t1"):
    return {
        "event_id": eid,
        "type": "NEW_TOPIC",
        "confirmed": confirmed,
        "evidence": {"turns": turns or [1], "span": span},
        "source_role": "user",
    }


# 1) 确定性 id
t = "我们来聊聊 A"
check("title_from_span", _topic_title_from_span(t) == t)
check("node_id_deterministic",
      _topic_node_id(t) == "topic:" + hashlib.sha1(t.encode("utf-8")).hexdigest()[:8],
      _topic_node_id(t))

# 2) 建节点 + 字段
st = fresh_state()
ns, diff = apply_event(st, topic_event(t))
node = next((n for n in ns["nodes"] if n["type"] == "topic"), None)
check("node_created", node is not None)
if node:
    check("node_fields",
          node["id"] == _topic_node_id(t) and node["title"] == t
          and node["status"] == "active" and node["source_turns"] == [1]
          and node["versions"] == [] and node["metadata"] == {},
          node["id"])
    check("diff_nodes_added", diff["nodes_added"] == [node["id"]])

# 3) 幂等：同 title 再触发 -> 不建重复
ns2, diff2 = apply_event(ns, topic_event(t, eid="evt_t2"))
topics = [n for n in ns2["nodes"] if n["type"] == "topic"]
check("idempotent_no_dup", len(topics) == 1)
check("idempotent_skipped", "already exists" in (diff2.get("skipped") or ""), diff2.get("skipped"))

# 4) rejected（confirmed=False）-> 不建节点
st3 = fresh_state()
ns3, diff3 = apply_event(st3, topic_event(t, confirmed=False))
check("rejected_no_node", not [n for n in ns3["nodes"] if n["type"] == "topic"])
check("rejected_skipped_note", "confirmed is not True" in (diff3.get("skipped") or ""))

# 5) 换行清洗 + 30 字截断
long_span = "第一行\n第二行" + "很长的内容" * 10
title = _topic_title_from_span(long_span)
check("newline_removed", "\n" not in title and "\r" not in title)
check("truncated_30", len(title) == 30, "len=%d" % len(title))

print("\nRESULT:", "ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(0 if not FAILS else 1)
