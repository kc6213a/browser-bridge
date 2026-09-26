#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 8: 按原序重放真实 ChatGPT 消息到 8787 的 POST /turn。

红线执行：不改文本、不加预热、不重排（corpus 已按 at 排序）。
每条 POST 后打一行摘要（turn 序号 / role / 判定到的 accepted 事件类型）。
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8787"
CORPUS = "C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge/sprint9_verify/corpus.jsonl"


def post_turn(text, role, source):
    payload = json.dumps({"text": text, "role": role, "source": source}).encode("utf-8")
    req = urllib.request.Request(BASE + "/turn", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode("utf-8")[:300]}
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def main():
    msgs = [json.loads(l) for l in open(CORPUS, encoding="utf-8") if l.strip()]
    print(f"replaying {len(msgs)} messages...")
    for i, m in enumerate(msgs, 1):
        text, role = m["text"], m["role"]
        resp = post_turn(text, role, "sprint8-replay")
        if resp.get("error"):
            print(f"[{i:2d}] ERROR {resp['error'][:150]}")
            continue
        recs = ((resp.get("runtime") or {}).get("records")) or []
        accepted = [f"{r['type']}:accepted" for r in recs if r.get("judge_result") == "accepted"]
        rejected = [r["type"] for r in recs if r.get("judge_result") == "rejected"]
        tag = ",".join(accepted) if accepted else (",".join(f"{t}:rejected" for t in rejected) or "-")
        print(f"[{i:2d}] {role:9s} {text[:30].replace(chr(10), ' ')}  =>  {tag}", flush=True)
        time.sleep(1.0)
    print("replay done.")


if __name__ == "__main__":
    main()
