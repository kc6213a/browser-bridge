#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Goal A 验证（Sprint 6 任务书 选项 2）：走真实 8787 的 POST /turn。

用同一条结构化依赖语句（正则检测、不调 LLM），先以 assistant 身份发、再以 user 身份发，
验证「同一句话，role 决定要不要落状态」：
  - assistant -> 事件记录、state_diff 为空（不改状态）
  - user      -> 事件记录、state_diff 含 edges_added（改状态）

仅读取/追加真实 runtime（server 自己落盘），不手动改任何 runtime 文件。
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
STATE_FILE = r"C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/received/runtime/state.json"
EVENTS_FILE = r"C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/received/runtime/events.jsonl"
MSG = "第一年收入 = 用户量 × 留存率"   # 结构化依赖，正则检测、不调 LLM


def post_turn(text, role):
    payload = json.dumps({
        "text": text,
        "role": role,
        "source": "sprint6-verify",
    }).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/turn", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode("utf-8")}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def counts(st):
    return len((st or {}).get("edges", [])), len((st or {}).get("nodes", []))


def main():
    before = load_json(STATE_FILE)
    be, bn = counts(before)
    print("[before] edges=%d nodes=%d" % (be, bn))

    # 顺序：先 assistant（应记录但不改状态），再 user（应改状态）
    r1 = post_turn(MSG, "assistant")
    print("[assist] POST /turn ->", json.dumps(r1, ensure_ascii=False)[:200])
    time.sleep(1.5)
    r2 = post_turn(MSG, "user")
    print("[user  ] POST /turn ->", json.dumps(r2, ensure_ascii=False)[:200])

    after = load_json(STATE_FILE)
    ae, an = counts(after)
    print("[after ] edges=%d nodes=%d  (delta edges=%+d nodes=%+d)"
          % (ae, an, ae - be, an - bn))

    evs = []
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    evs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    print("\n--- last 3 events (assistant, then user) ---")
    for e in evs[-3:]:
        sd = e.get("state_diff") or {}
        print("event_id=%-16s type=%-20s source_role=%-9s judge=%-9s state_diff_keys=%s"
              % (e.get("event_id"), e.get("type"), e.get("source_role"),
                 e.get("judge_result"), list(sd.keys())))


if __name__ == "__main__":
    main()
