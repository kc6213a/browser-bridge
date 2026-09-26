#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sprint 9 离线单测：不调 LLM、不依赖 server。

覆盖任务书硬约束：
 1. 每轮无条件恰好 1 条 NEW_TOPIC 候选（含首条零信号消息）
 2. 同轮不产生重复 NEW_TOPIC
 3. entity_change 与 NEW_TOPIC 并存（不压制）
 4. context_turns = recent_turns 最近 3 条（不含当前消息），空则 []
 5. judge prompt 带上下文段落（空则省略）
 6. taxonomy NEW_TOPIC.description 已更新、auto_confirm 仍 0.85
"""
import sys

CA = "C:/Users/Kevin Chan/WorkBuddy/Conversation Agent"
sys.path.insert(0, CA)

from runtime.signal_detector import build_candidates, extract_signals  # noqa: E402
from runtime.event_judge import build_judge_prompt  # noqa: E402
from runtime.contracts import load_taxonomy  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


# ---- 1/4: 首条零信号消息 -> 1 条 NEW_TOPIC，context_turns == [] ----
sig = extract_signals("水电费", [], 1)
cands = build_candidates(sig)  # 模拟 agent_runtime 的无参调用（走 threading.local）
nt = [c for c in cands if c.candidate_type == "NEW_TOPIC"]
check("1a 首条消息产生恰好 1 条 NEW_TOPIC", len(nt) == 1)
check("1b 首条 evidence_turns == [1]", nt and nt[0].evidence_turns == [1])
check("1c 首条 context_turns == []", nt and nt[0].context_turns == [])
check("1d 首条 span == 原文", nt and nt[0].span == "水电费")

# ---- 4: context_turns 取最近 3 条、不含当前 ----
recent = ["消息A", "消息B", "消息C", "消息D", "消息E"]
sig = extract_signals("当前消息X", recent, 9)
cands = build_candidates(sig)
nt = [c for c in cands if c.candidate_type == "NEW_TOPIC"]
check("4a context_turns == 最近 3 条", nt and nt[0].context_turns == ["消息C", "消息D", "消息E"])
check("4b context 不含当前消息", nt and "当前消息X" not in nt[0].context_turns)
check("4c span == 当前消息全文", nt and nt[0].span == "当前消息X")

# ---- 2/3: entity_change 与 NEW_TOPIC 并存，且 NEW_TOPIC 恰好 1 条 ----
sig = extract_signals("假设用户量改成 80 万", ["假设用户量是 100 万"], 4)
cands = build_candidates(sig)
types = [c.candidate_type for c in cands]
nt = [c for c in cands if c.candidate_type == "NEW_TOPIC"]
check("3a entity_change 与 NEW_TOPIC 并存", "ASSUMPTION_CHANGED" in types and "NEW_TOPIC" in types)
check("3b NEW_TOPIC 恰好 1 条（不重复）", len(nt) == 1)

# 显式传参路径（单测可注入，不依赖 threading.local）
sig = extract_signals("随便一句话", ["上一条"], 7)
cands2 = build_candidates(sig, recent_turns=["a", "b"], turn=7, message="随便一句话")
nt2 = [c for c in cands2 if c.candidate_type == "NEW_TOPIC"]
check("3c 显式传参路径同样恰好 1 条", len(nt2) == 1 and nt2[0].context_turns == ["a", "b"])

# ---- 5: judge prompt 上下文段落 ----
spec = load_taxonomy().event_types["NEW_TOPIC"]
cand_with_ctx = nt[0]
prompt = build_judge_prompt(cand_with_ctx, spec)
check("5a 带上下文 -> prompt 含【最近消息】", "【最近消息】" in prompt)
check("5b prompt 含【当前消息】", "【当前消息】" in prompt)
check("5c prompt 含新判定规则（延续/回答不算）", "同题反复" in prompt and "助手回复" in prompt)
check("5d 上下文在 Candidate 之前", prompt.find("【最近消息】") < prompt.find("Candidate："))

sig0 = extract_signals("水电费", [], 1)
cand0 = [c for c in build_candidates(sig0) if c.candidate_type == "NEW_TOPIC"][0]
prompt0 = build_judge_prompt(cand0, spec)
check("5e 无上下文 -> prompt 省略【最近消息】段落", "【最近消息】\n" not in prompt0)
check("5f 无上下文规则仍在（只根据当前消息判断）", "只根据当前消息本身判断" in prompt0)

# ---- 6: taxonomy ----
check("6a NEW_TOPIC.description 已更新", "同题反复" in spec["description"] and "此前未出现的独立议题" in spec["description"])
check("6b auto_confirm 仍 0.85", float(spec["confidence"]["auto_confirm"]) == 0.85)

# ---- 汇总 ----
print()
if FAILED:
    print(f"FAILED: {len(FAILED)} -> {FAILED}")
    sys.exit(1)
print("ALL PASS")
