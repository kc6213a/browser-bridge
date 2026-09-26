#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Goal A 证据提取：从真实 received/runtime/events.jsonl 解析 role 隔离证明。

不修改任何 runtime 文件，只读。
"""
import json

EVENTS = r"C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/received/runtime/events.jsonl"

events = []
with open(EVENTS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

print("total events:", len(events))

all_have = all("source_role" in e for e in events)
none_q = all(e.get("source_role") not in (None, "", "?") for e in events)
print("all_have_source_role:", all_have)
print("none_is_qmark_or_empty:", none_q)

roles = sorted(set(e.get("source_role") for e in events))
print("distinct source_role values:", roles)

# user 事件里“有状态变化”的一条
user_changed = next((e for e in events
                     if e.get("source_role") == "user" and (e.get("state_diff") or {})), None)
# assistant 事件里“无状态变化但已记录”的一条
asst_nochange = next((e for e in events
                      if e.get("source_role") == "assistant" and not (e.get("state_diff") or {})), None)


def show(e, label):
    if not e:
        print("\n=== %s ===\nNOT FOUND" % label)
        return
    print("\n=== %s ===" % label)
    print("event_id    :", e.get("event_id"))
    print("type        :", e.get("type"))
    print("source_role :", e.get("source_role"))
    print("judge_result:", e.get("judge_result"))
    sd = e.get("state_diff") or {}
    print("state_diff  :", json.dumps(sd, ensure_ascii=False)[:500])


show(user_changed, "USER event that CHANGED state")
show(asst_nochange, "ASSISTANT event with NO state change (but recorded)")
