# -*- coding: utf-8 -*-
"""
extract.py —— Extractor Gate 实验（离线，独立于 runtime）。

目标：在干净输入上验证「能否把真实对话压缩成可回溯的认知结构」
      （assumptions / conclusions / dependencies），且每条 evidence 都能
      溯源到原文子串。

硬约束（严格遵守）：
- 只用标准库（urllib），不 import runtime.*，不依赖项目任何模块。
- 不碰 content.js / signal_detector / event_judge / event_store /
  state_updater / server.py，只读取 input/<name>.json，写 output/<name>_result.json。
- evidence / from / to 必须是对话原文的连续子串 —— 用 `in` 校验，
  不通过则标记 *_invalid: true。
- 第一次结果即结论，不自动改 prompt 重试。
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(HERE, "input")
OUTPUT_DIR = os.path.join(HERE, "output")

DEFAULT_BASE_URL = "https://api.deepseek.com"
# 2026-10-28 凯文拍定：写**实际在跑的模型**，消除静默别名（deepseek-chat 会被服务端别名成
# deepseek-flash）。与 server.py 的 .env(OPENAI_MODEL=deepseek-flash) 对齐；显式写死，不靠别名。
# 仍保留 os.environ.get("OPENAI_MODEL") 优先（手动覆盖用），这里只是缺省值。
DEFAULT_MODEL = "deepseek-flash"
TIMEOUT = 90.0


def llm_complete(prompt, api_key, base_url, model):
    """最小 OpenAI 兼容调用（复制 runtime/llm_adapter 的请求格式，但不 import）。"""
    endpoint = (base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
    if not api_key:
        raise SystemExit("missing OPENAI_API_KEY")
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}],
         "temperature": 0.0, "stream": False},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise SystemExit("HTTP %s :: %s" % (e.code, e.read().decode("utf-8", "replace")[:400]))
    except urllib.error.URLError as e:
        raise SystemExit("network error :: %s" % e.reason)
    data = json.loads(raw)
    return data["choices"][0]["message"]["content"]


def parse_json_block(text):
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no json block in response")
    return json.loads(s[start:end + 1])


PROMPT = """你是一个对话结构抽取器。下面是一次真实对话（turn=轮次, role=角色, text=内容）。
请从中抽取三类认知结构，并以严格 JSON 返回，不要包含任何解释文字、不要使用 markdown 代码围栏：

{
  "assumptions":  [{"text": "抽取出的预设/前提性陈述", "evidence": "必须是从对话文本中逐字复制的连续子串（不能改写、不能省略）", "source_turn": 轮次数字}],
  "conclusions":  [{"text": "抽取出的结论/发现/推导结果", "evidence": "逐字子串", "source_turn": 轮次数字}],
  "dependencies": [{"from": "概念A（逐字子串）", "to": "概念B（逐字子串）", "evidence": "支撑该依赖的逐字子串", "source_turn": 轮次数字}]
}

规则：
- assumptions = 对话中被当作前提/预设的陈述（定义、已知事实、被采纳的假设）。
- conclusions = 对话推导出的判断、结论、对比结论、数值结论。
- dependencies = 概念之间的推导/依赖关系（A 推导/支撑 B）；from 和 to 都必须是对话里出现的概念短语（逐字子串）。
- evidence / from / to 必须是对话 text 中出现的连续子串、逐字一致；否则会被判校验失败。
- source_turn 填该内容出现所在的轮次数字。
- 每条尽量精炼，不要重复。只返回 JSON。

对话内容：
%s"""


def validate(items, corpus, fields):
    """对每条的指定字段做子串校验，返回 (校验后列表, 通过数, 总数)。"""
    out, ok, total = [], 0, 0
    for it in items:
        d = dict(it)
        for f in fields:
            val = d.get(f, "")
            bad = not (isinstance(val, str) and val in corpus)
            if bad:
                d[f + "_invalid"] = True
            else:
                ok += 1
            total += 1
        out.append(d)
    return out, ok, total


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "async"
    with open(os.path.join(INPUT_DIR, name + ".json"), encoding="utf-8") as f:
        turns = json.load(f)
    corpus = "\n".join(t["text"] for t in turns)
    prompt = PROMPT % json.dumps(turns, ensure_ascii=False)

    raw = llm_complete(prompt, None, None, None)
    parsed = parse_json_block(raw)

    corpus_check = "\n".join(t["text"] for t in turns)
    asm, a_ok, a_tot = validate(parsed.get("assumptions", []), corpus_check, ["evidence"])
    con, c_ok, c_tot = validate(parsed.get("conclusions", []), corpus_check, ["evidence"])
    dep, d_ok, d_tot = validate(parsed.get("dependencies", []), corpus_check, ["from", "to", "evidence"])

    result = {
        "input": name,
        "assumptions": asm,
        "conclusions": con,
        "dependencies": dep,
        "validation": {
            "evidence_pass": a_ok + c_ok + d_ok,
            "evidence_total": a_tot + c_tot + d_tot,
            "assumptions_count": len(asm),
            "conclusions_count": len(con),
            "dependencies_count": len(dep),
        },
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, name + "_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    v = result["validation"]
    rate = (v["evidence_pass"] / v["evidence_total"] * 100) if v["evidence_total"] else 0
    print("[%s] assumptions=%d conclusions=%d dependencies=%d | evidence pass=%d/%d (%.0f%%) -> %s"
          % (name, v["assumptions_count"], v["conclusions_count"], v["dependencies_count"],
             v["evidence_pass"], v["evidence_total"], rate, out_path))


if __name__ == "__main__":
    main()
