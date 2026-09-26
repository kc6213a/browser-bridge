#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 8: 从归档里捞出真实 ChatGPT 抓取的消息，按时间原序构建 replay 语料。

来源（两条真实会话，按 at 时间戳排序合并，共 24 条，去重后 23 条）：
  A. turns_v0.3_contaminated.bak      原始行 [2]-[7]   19:51-19:52  水电费/MACD 会话
  B. _archive_v0.3/turns_before_sprint3.jsonl 原始行 [4]-[21]  19:53-20:14  agent 项目会话

排除：
  - v0.3 [24]-[28]：20:22 的整页重抓（UI 噪音 + 历史消息重复抓取），非对话流。
  - 一切 src=localhost / sprint*-test / e2e://direct 的测试注入。
去重：全文完全相同的重抓消息（如「按你现在这个方案…」1576 字在两个文件里都有）只保留首现。
红线：不改文本、不加预热、不重排（仅按 at 稳定排序）。
"""
import json

BB = "C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge"
OUT = f"{BB}/sprint8_verify/corpus.jsonl"

segments = [
    (f"{BB}/received/turns_v0.3_contaminated.bak", 2, 7),
    (f"{BB}/received/_archive_v0.3/turns_before_sprint3.jsonl", 4, 21),
]

msgs = []
seen_text = set()
dropped_dup = 0
for path, lo, hi in segments:
    lines = [l for l in open(path, encoding="utf-8") if l.strip()]
    # 按原始行号切片（1-based，闭区间），再筛 chatgpt 来源
    for t in (json.loads(l) for l in lines[lo - 1:hi]):
        if not str(t.get("source", "")).startswith("chatgpt"):
            continue
        text = t.get("text") or ""
        if text in seen_text:
            dropped_dup += 1
            continue
        seen_text.add(text)
        msgs.append({
            "at": t.get("at"),
            "role": t.get("role"),
            "text": text,
            "origin_file": path.replace("\\", "/").split("/")[-1],
        })

msgs.sort(key=lambda m: m["at"] or "")  # 稳定排序：同刻保持文件内相对顺序

with open(OUT, "w", encoding="utf-8") as f:
    for m in msgs:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")

print(f"corpus: {len(msgs)} msgs (dedup dropped {dropped_dup}) -> {OUT}")
for i, m in enumerate(msgs, 1):
    print(f"[{i:2d}] {m['at']}  {m['role']:9s}  len={len(m['text']):5d}  {m['text'][:38]}".replace("\n", " "))
