#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 7 验收驱动：走真实 8787 POST /turn。

- 预热 1 条：semantic_drift 需要 recent_turns 非空（全新会话第一条永远 drift=0，
  这是检测器已知局限，Sprint 7 不修）——预热只填 recent_turns，预期 0 records。
- 3 条验收消息：任务书示例句只有 6-7 字，低于 SEMANTIC_DRIFT_MIN_LEN=8 必不触发，
  故换成 >=8 字、无数字值（避免 entity 分支压制 NEW_TOPIC）的等价新话题句。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8787"
STATE_URL = BASE + "/state"

MSGS = [
    ("warmup", "开始吧"),                              # 预热，预期 0 records
    ("topic1", "我们来聊聊新的存储芯片方案"),
    ("topic2", "现在讨论一下获客渠道的问题"),
    ("topic3", "换个话题讲讲供应链的近况"),
]


def post_turn(text, role="user"):
    payload = json.dumps({"text": text, "role": role,
                          "source": "sprint7-verify"}).encode("utf-8")
    req = urllib.request.Request(BASE + "/turn", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    for tag, text in MSGS:
        resp = post_turn(text)
        print("[%s] turn=%s records=%s status=%s :: %s"
              % (tag, resp.get("turn"), resp.get("records"),
                 resp.get("runtime_status"), text))
        time.sleep(1.0)

    time.sleep(1.0)
    with urllib.request.urlopen(STATE_URL, timeout=10) as r:
        state = json.loads(r.read().decode("utf-8"))

    topics = [n for n in (state or {}).get("nodes", []) if n.get("type") == "topic"]
    print("\n--- topic nodes (%d) ---" % len(topics))
    for t in topics:
        print(json.dumps(t, ensure_ascii=False))
    print("\ntotal nodes=%d edges=%d version=%s"
          % (len((state or {}).get("nodes", [])),
             len((state or {}).get("edges", [])),
             (state or {}).get("version")))


if __name__ == "__main__":
    main()
