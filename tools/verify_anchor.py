#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
锚点核验脚本 —— "强制锚点 + 无锚丢弃" 规则的执行体。

【B 遗留可用工具】2026-10-13 由 `docs/_verify_anchor.py` 提升而来。
- 独立于 Runtime：不 import 任何 runtime 模块、不建节点、不调 judge、不调 LLM。
- 纯机械：子串匹配，同一输入必得同一输出，可复跑、可做 CI 闸（退出码非 0 即拦）。
- 本轮 B 全流程里唯一不依赖 LLM、也不依赖人工判读的产物。
- 用法见 eval_b_automation.md §0.4「选 2 减法版」第 4 步。

职责（纯机械，不调 LLM）：
  读一份 B 标准版 markdown（每条带 [TURN nnn] + 「逐字片段」锚点），
  对每条锚点做子串核验：引用的「逐字片段」是否确实出现在对应 turn 的原文里。
  - 是 → PASS（有原文出处，可落库）
  - 否 / 无锚（衍生·语境类无「」引号）/ turn 找不到 → FAIL → 按规则 DISCARD（丢弃，禁止落库）

锚点接口定义（写进 B 抽取接口）：
  锚点 = (turn号, 原文逐字片段)
  核验 = 子串匹配（片段 ⊆ turn.text）
  丢弃规则 = 无锚(无「」引号) 或 匹配失败 → 丢弃；模型禁止"编一个看起来像的 turn 号"凑锚。

INVALID_ANCHOR 负样本机制（2026-10-12 新增）：
  标准版里被人工标 `INVALID_ANCHOR` 的 bullet，其失败锚点被视为"预期失败"——
  它们是 LLM 编锚的实例集（负样本），保留不修、不删，喂回本脚本做回归：
    - NEG_OK    ：标记行确实 FAIL（闸门仍拦得住）→ 正常，不计错误
    - NEG_LEAK  ：标记行居然全部 PASS（负样本失效）→ 报警，说明源文变了或标记错了
  非标记行的 FAIL 才是真错误（DISCARD_FAIL），必须人工处理。

用法：
  python tools/verify_anchor.py [B_STANDARD_MD] [PROJECT_ID] [TURNS_JSONL]
  默认：docs/b_standard_s84d586ee.md / s-84d586ee-c389-42e9-ab52-1480224fdd81 / received/turns.jsonl

输出：
  - stdout：逐条 PASS/NEG_OK/DISCARD_FAIL/... + 汇总
  - docs/_anchor_report_<pid>.md：可复跑的证据报告（含丢弃清单 + 负样本清单）
  - 退出码：有"非预期失败"（DISCARD_FAIL / DISCARD_NO_ANCHOR）或"负样本泄漏"则非 0（供 CI / 自动闸使用）

历史实弹结果（s-84d586ee，2026-10-12）：
  58 锚点 → 46 PASS / 12 预期失败（INVALID_ANCHOR 负样本）/ 0 非预期失败 / 0 泄漏，退出码 0。
  12 条负样本见 docs/_anchor_negatives_84d586ee.md（保留不修不删，做回归用）。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MD = os.path.join(ROOT, "docs", "b_standard_s84d586ee.md")
DEFAULT_PID = "s-84d586ee-c389-42e9-ab52-1480224fdd81"
DEFAULT_TURNS = os.path.join(ROOT, "received", "turns.jsonl")

# 锚点行：- **[TURN ...]** 「...」...
BULLET_RE = re.compile(r"^\s*-\s+\*\*(?:【[^】]*】\s*)?\[TURN\s*([^\]]+)\]\*\*(.*)$")
QUOTE_RE = re.compile(r"「([^「」]+)」")
# 从 turn 串里抠数字（处理 916 / 1615 / 1617 / 1208→1210 / 1755 衍生 / 1932 语境）
TURN_NUM_RE = re.compile(r"(\d+)")
# INVALID_ANCHOR 标记（人工标的负样本，预期失败）
INVALID_RE = re.compile(r"INVALID_ANCHOR", re.IGNORECASE)


def load_turn_texts(turns_path, project_id):
    """返回 {turn_no(int): text(str)} —— 取该 project_id 下每条 turn 的原文。"""
    texts = {}
    with open(turns_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("project_id") != project_id:
                continue
            tn = rec.get("_turn")
            t = rec.get("text")
            if isinstance(tn, int) and isinstance(t, str):
                texts.setdefault(tn, t)
    return texts


def strip_md(s):
    """剥掉 markdown 强调标记（** * `），用于宽松子串核验。
    B 标准版里把源文 **State Updater** 写成 State Updater 是自然渲染，
    不应误判为'非逐字'。真正的 gloss/编造（如改错别字、加自己的话）剥不掉，仍会 FAIL。"""
    return s.replace("**", "").replace("`", "").replace("*", "")


def norm(s):
    """剥 markdown + 折叠所有空白（含换行）用于宽松子串核验。"""
    return re.sub(r"\s+", "", strip_md(s))


def verify_fragment(fragment, turn_nums, turn_texts):
    """
    返回 (ok, mode, detail)
      ok: True/False
      mode: 'exact' / 'normalized' / 'fail'
      detail: 命中的 turn 号 或 失败原因
    """
    raw = fragment.strip()
    if not raw:
        return False, "fail", "空片段"
    for tn in turn_nums:
        t = turn_texts.get(tn)
        if t is None:
            continue
        if raw in t:
            return True, "exact", tn
        if norm(raw) in norm(t):
            return True, "normalized", tn
    missing = [tn for tn in turn_nums if tn not in turn_texts]
    if missing:
        return False, "fail", "turn 找不到: %s" % missing
    return False, "fail", "片段非任何引用 turn 的子串"


def parse_bullet(line, turn_texts):
    """
    解析一条 bullet，返回若干锚点 dict：
      {turns:[int], fragment:str|None, raw_turn_str:str, has_quote:bool}
    """
    m = BULLET_RE.match(line)
    if not m:
        return None
    raw_turn_str = m.group(1).strip()
    rest = m.group(2)
    turn_nums = [int(x) for x in TURN_NUM_RE.findall(raw_turn_str)]
    quotes = QUOTE_RE.findall(rest)
    has_quote = len(quotes) > 0
    if not has_quote:
        return [{
            "turns": turn_nums,
            "fragment": None,
            "raw_turn_str": raw_turn_str,
            "has_quote": False,
        }]
    anchors = []
    for q in quotes:
        anchors.append({
            "turns": turn_nums,
            "fragment": q,
            "raw_turn_str": raw_turn_str,
            "has_quote": True,
        })
    return anchors


def main():
    md_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MD
    pid = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PID
    turns_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_TURNS

    turn_texts = load_turn_texts(turns_path, pid)

    with open(md_path, encoding="utf-8") as f:
        lines = f.readlines()

    results = []        # (status, raw_turn_str, fragment, detail, line_no)
    invalid_bullet = set()  # 行号集合（仅真正的锚点 bullet 且被标 INVALID_ANCHOR）
    line_had_fail = {}  # line_no -> True（该行产生了预期/非预期失败）
    for i, line in enumerate(lines, 1):
        anchors = parse_bullet(line, turn_texts)
        if anchors is None:
            continue
        invalid_flag = bool(INVALID_RE.search(line))
        if invalid_flag:
            invalid_bullet.add(i)
        for a in anchors:
            if not a["has_quote"]:
                if invalid_flag:
                    results.append(("NEG_OK", a["raw_turn_str"], None,
                                    "标记 INVALID_ANCHOR 的无锚条目（预期失败）", i))
                    line_had_fail[i] = True
                else:
                    results.append(("DISCARD_NO_ANCHOR", a["raw_turn_str"], None,
                                    "无「」逐字锚点（衍生/语境/复述类）→ 丢弃", i))
                continue
            ok, mode, detail = verify_fragment(a["fragment"], a["turns"], turn_texts)
            if ok:
                results.append(("PASS", a["raw_turn_str"], a["fragment"],
                                "%s@turn%d" % (mode, detail), i))
            else:
                if invalid_flag:
                    results.append(("NEG_OK", a["raw_turn_str"], a["fragment"],
                                    "标记 INVALID_ANCHOR，预期失败（负样本）：%s" % detail, i))
                    line_had_fail[i] = True
                else:
                    results.append(("DISCARD_FAIL", a["raw_turn_str"], a["fragment"],
                                    detail, i))

    # 负样本回归泄漏：被标 INVALID_ANCHOR 却没有产生任何失败 → 闸门退化 / 标记失效
    for i in invalid_bullet:
        if not line_had_fail.get(i):
            results.append(("NEG_LEAK", "-", None,
                            "标记 INVALID_ANCHOR 但所有锚点 PASS（负样本失效，须复核源文/标记）", i))

    n_pass = sum(1 for r in results if r[0] == "PASS")
    n_no_anchor = sum(1 for r in results if r[0] == "DISCARD_NO_ANCHOR")
    n_fail = sum(1 for r in results if r[0] == "DISCARD_FAIL")
    n_neg_ok = sum(1 for r in results if r[0] == "NEG_OK")
    n_neg_leak = sum(1 for r in results if r[0] == "NEG_LEAK")
    total = n_pass + n_no_anchor + n_fail + n_neg_ok + n_neg_leak

    # stdout 报告
    print("=== 锚点核验报告 ===")
    print("B 标准版: %s" % os.path.relpath(md_path, ROOT))
    print("project_id: %s" % pid)
    print("引用 turn 总数(去重): %d" % len(turn_texts))
    print("锚点条目: %d | PASS %d | 预期失败(负样本) %d | 非预期失败 %d | 无锚丢弃 %d | 负样本泄漏 %d" %
          (total, n_pass, n_neg_ok, n_fail, n_no_anchor, n_neg_leak))
    print("-" * 60)
    tag_map = {"PASS": "✅", "DISCARD_NO_ANCHOR": "🟡", "DISCARD_FAIL": "🔴",
               "NEG_OK": "🟣", "NEG_LEAK": "⚠️"}
    for status, tstr, frag, detail, ln in results:
        tag = tag_map[status]
        frag_disp = ("「%s」" % frag) if frag else "(无引号)"
        print("%s L%-4d [TURN %s] %s %s" % (tag, ln, tstr, frag_disp, detail))

    # 写证据报告
    report_path = os.path.join(ROOT, "docs",
                               "_anchor_report_%s.md" % (pid.split("-")[1] if "-" in pid else "x"))
    with open(report_path, "w", encoding="utf-8") as out:
        out.write("# 锚点核验报告（机械子串核验，不调 LLM）\n\n")
        out.write("- B 标准版: `%s`\n" % os.path.relpath(md_path, ROOT))
        out.write("- project_id: `%s`\n" % pid)
        out.write("- 引用 turn 总数(去重): %d\n" % len(turn_texts))
        out.write("- 锚点条目: **%d** | PASS **%d** | 预期失败(负样本) **%d** | 非预期失败 **%d** | 无锚丢弃 **%d** | 负样本泄漏 **%d**\n\n"
                  % (total, n_pass, n_neg_ok, n_fail, n_no_anchor, n_neg_leak))
        out.write("## 非预期失败（须人工补锚或剔除，不落库）\n\n")
        unexpected = [r for r in results if r[0] in ("DISCARD_FAIL", "DISCARD_NO_ANCHOR")]
        if not unexpected:
            out.write("（无）\n")
        for status, tstr, frag, detail, ln in unexpected:
            frag_disp = ("「%s」" % frag) if frag else "(无「」逐字锚点)"
            out.write("- **[%s]** L%d TURN %s %s — %s\n" % (status, ln, tstr, frag_disp, detail))
        out.write("\n## 负样本（已标 INVALID_ANCHOR，预期失败，做回归用，不修不删）\n\n")
        negs = [r for r in results if r[0] == "NEG_OK"]
        if not negs:
            out.write("（无标记行）\n")
        for status, tstr, frag, detail, ln in negs:
            frag_disp = ("「%s」" % frag) if frag else "(无引号)"
            out.write("- **[NEG_OK]** L%d TURN %s %s — %s\n" % (ln, tstr, frag_disp, detail))
        if n_neg_leak:
            out.write("\n## ⚠️ 负样本泄漏（须复核）\n\n")
            for status, tstr, frag, detail, ln in [r for r in results if r[0] == "NEG_LEAK"]:
                out.write("- **[NEG_LEAK]** L%d %s\n" % (ln, detail))
        out.write("\n## 全部核验\n\n")
        for status, tstr, frag, detail, ln in results:
            frag_disp = ("「%s」" % frag) if frag else "(无引号)"
            out.write("- %s L%d TURN %s %s %s\n" % (status, ln, tstr, frag_disp, detail))
    print("-" * 60)
    print("证据报告: %s" % os.path.relpath(report_path, ROOT))

    # 退出码：非预期失败 或 负样本泄漏 才非 0
    return 1 if (n_no_anchor + n_fail + n_neg_leak) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
