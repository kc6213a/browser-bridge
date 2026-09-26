#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟扩展上报：把 20 条真实 ChatGPT user 消息逐条 POST 到 /turn。
等价于扩展抓到 data-message-id 后上报，喂进同一条 pipeline。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8787"
MSGS = [
    "假设用户量是 100 万",
    "假设获客成本是 50 元",
    "那么第一年收入大概是 5000 万",
    "假设用户量改成 80 万",
    "获客成本其实是 60 元",
    "刚才那个收入结论还成立吗",
    "我们之前假设的用户量现在是多少",
    "假设客单价是 100 元",
    "假设用户量再改成 120 万",
    "用户量这个假设改了好几次了",
    "获客成本我们最早的假设是多少",
    "假设留存率是 40%",
    "如果留存率是 60% 呢",
    "留存率和用户量有关系吗",
    "假设用户量是 200 万",
    "不对，用户量应该回到 100 万",
    "我们一共改过几次用户量",
    "现在的用户量假设是什么",
    "获客成本当前是多少",
    "把假设都列出来看看",
    "第一年收入 = 用户量 × 客单价",
]

# 强制直连 localhost，不走 HTTP(S)_PROXY
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post_turn(text, mid):
    payload = {
        "type": "BRIDGE_TURN",
        "source": "chatgpt",
        "role": "user",
        "text": text,
        "message_id": mid,
        "message_id_source": "attr:data-message-id",
    }
    req = urllib.request.Request(
        BASE + "/turn",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    for i, m in enumerate(MSGS, 1):
        mid = "msg-%02d" % i
        try:
            r = post_turn(m, mid)
        except Exception as e:
            print("[%02d] ERR %s :: %s" % (i, type(e).__name__, e))
            continue
        dec = r.get("records")  # server echoes records count in top-level? no -> use _runtime
        # 重新发起 /recent 取该 turn 的决策较繁；这里直接用返回的 records 字段（server 返回 records=数量）
        print("[%02d] mid=%s status=%s dup=%s records=%s :: %s"
              % (i, mid, r.get("runtime_status"), r.get("duplicate"),
                 r.get("records"), m))
        time.sleep(0.3)
    print("DONE sending 20 messages")


if __name__ == "__main__":
    main()
