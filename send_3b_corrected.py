#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 3b 真机验收发送器（修正顺序版）。

与 send_longsession.py 是**同一组 21 句**，唯一的差别：
把「第一年收入 = 用户量 × 客单价」这句依赖声明从末尾（原第 21 句）
移到第 4 句（紧跟在「那么第一年收入大概是 5000 万」之后）。

原因（Sprint 3b 顺序边界）：
impact_analyzer 只在 ASSUMPTION_CHANGED 时沿 state.edges 反向遍历；
若依赖边在变化的假设之后才声明，则变化发生时边还不存在 -> 结论不会被标 stale。
修正顺序让依赖边先于「用户量改成 80 万」等变化建立，闭环才能真正触发。
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8787"
MSGS = [
    "假设用户量是 100 万",
    "假设获客成本是 50 元",
    "假设客单价是 100 元",             # 客单价先声明，保证依赖边能建出两条
    "那么第一年收入大概是 5000 万",
    "第一年收入 = 用户量 × 客单价",   # 原第 21 句上移：依赖边先建立（此时 用户量/客单价 均已存在）
    "假设用户量改成 80 万",           # 此处变化 -> 第一年收入 应被标 stale
    "获客成本其实是 60 元",
    "刚才那个收入结论还成立吗",
    "我们之前假设的用户量现在是多少",
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
]

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
        print("[%02d] mid=%s status=%s dup=%s records=%s :: %s"
              % (i, mid, r.get("runtime_status"), r.get("duplicate"),
                 r.get("records"), m))
        time.sleep(0.3)
    print("DONE sending 21 messages (corrected order)")


if __name__ == "__main__":
    main()
