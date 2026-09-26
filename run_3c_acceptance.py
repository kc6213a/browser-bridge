# -*- coding: utf-8 -*-
"""Sprint 3c 真机验收（字面 21 句，依赖声明在最后）。

自包含：清状态 -> 起 server(新代码) -> 喂 21 句 -> 校验 state.json + events.jsonl。
用法：
    python run_3c_acceptance.py            # 完整验收（需 DeepSeek 可用）
    python run_3c_acceptance.py --smoke     # 只验证harness（起server+health，不发21句）

验收硬指标（来自任务书）：
  1) received/runtime/state.json 必须出现  conclusion:第一年收入  health == "stale"
  2) received/runtime/events.jsonl 必须出现派生 CONCLUSION_STALE，
     带 judge_result="derived" 且 source_event_id 指向真实历史 ASSUMPTION_CHANGED
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.request import ProxyHandler, Request

PY = r"C:/Users/Kevin Chan/.workbuddy/binaries/python/versions/3.13.12/python.exe"
BASE = r"C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge"
AGENT = r"C:/Users/Kevin Chan/WorkBuddy/Conversation Agent"
API_KEY = "sk-4f1bc912b7984d049532266ec49d0267"
API_URL = "https://api.deepseek.com"
PORT = 8787
BASE_URL = "http://127.0.0.1:%d" % PORT

SERVER_LOG = os.path.join(BASE, "acceptance_server.log")
STATE_CANON = os.path.join(AGENT, "runtime", "state.json")
ELOG_CANON = os.path.join(AGENT, "runtime", "event_log.jsonl")
STATE_BRIDGE = os.path.join(BASE, "received", "runtime", "state.json")
EVENTS_BRIDGE = os.path.join(BASE, "received", "runtime", "events.jsonl")
EFFECTS_BRIDGE = os.path.join(BASE, "received", "runtime", "effects.jsonl")
TURNS_BRIDGE = os.path.join(BASE, "received", "turns.jsonl")

# 与 send_longsession.py 完全一致（第 21 句是依赖声明，最后出现）
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


def kill_port():
    """杀掉占用 8787 的旧 server（Windows netstat + taskkill）。

    netstat 在中文 Windows 输出 GBK，必须用 bytes 读再 errors=ignore 解码，
    否则 text=True 会在子进程读取线程里抛 UnicodeDecodeError。
    """
    try:
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15)
        out = proc.stdout.decode("gbk", errors="ignore") if proc.stdout else ""
    except Exception:
        return
    for line in out.splitlines():
        if ":8787" in line and "LISTENING" in line:
            parts = line.split()
            pid = parts[-1]
            if pid.isdigit():
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=15,
                    )
                    print("[kill] terminated stale server pid=%s" % pid)
                except Exception as e:
                    print("[kill] taskkill failed for %s: %s" % (pid, e))


def clear_state():
    for p in (STATE_CANON, ELOG_CANON, STATE_BRIDGE, EVENTS_BRIDGE,
              EFFECTS_BRIDGE, TURNS_BRIDGE):
        try:
            if os.path.exists(p):
                os.remove(p)
                print("[clear] removed %s" % p)
        except Exception as e:
            print("[clear] warn %s: %s" % (p, e))


def start_server():
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = API_KEY
    env["OPENAI_BASE_URL"] = API_URL
    env["BRIDGE_LLM"] = "real"
    f = open(SERVER_LOG, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, os.path.join(BASE, "server.py")],
        cwd=BASE, env=env, stdout=f, stderr=subprocess.STDOUT,
    )
    # 等 /health 起来
    for _ in range(40):
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                if r.status == 200:
                    print("[server] up pid=%s" % proc.pid)
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy within 20s")


def _post_turn(text, mid):
    payload = {
        "type": "BRIDGE_TURN",
        "source": "chatgpt",
        "role": "user",
        "text": text,
        "message_id": mid,
        "message_id_source": "attr:data-message-id",
    }
    req = Request(
        BASE_URL + "/turn",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(ProxyHandler({}))
    with opener.open(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_all():
    opener_errs = 0
    for i, m in enumerate(MSGS, 1):
        mid = "msg-%02d" % i
        last_status = None
        # 错误重试：用不同 mid 绕过去重（原 mid 已在 turns.jsonl 记过）
        for attempt in range(4):
            try:
                r = _post_turn(m, mid if attempt == 0 else "%s-r%d" % (mid, attempt))
            except Exception as e:
                print("[%02d] ERR %s :: %s" % (i, type(e).__name__, e))
                time.sleep(8)
                continue
            last_status = r.get("runtime_status")
            if last_status != "error":
                print("[%02d] mid=%s status=%s records=%s :: %s"
                      % (i, mid, last_status, r.get("records"), m[:30]))
                break
            print("[%02d] LLM error (retry %d): %s" % (i, attempt, r.get("error")))
            time.sleep(8)
        else:
            opener_errs += 1
            print("[%02d] GAVE-UP after retries" % i)
        time.sleep(2.5)  # 节流，避免触发 DeepSeek 频率限制
    print("DONE sending %d messages (gave_up=%d)" % (len(MSGS), opener_errs))
    return opener_errs


def verify():
    print("\n=== 验收校验 ===")
    ok = True

    # 1) state.json: conclusion:第一年收入 health == stale
    try:
        with open(STATE_BRIDGE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print("FAIL: 无法读取 state.json: %s" % e)
        return False
    concl = None
    for n in state.get("nodes") or []:
        if n.get("id") == "conclusion:第一年收入":
            concl = n
            break
    if concl is None:
        print("FAIL: state.json 缺少节点 conclusion:第一年收入")
        ok = False
    elif concl.get("health") != "stale":
        print("FAIL: conclusion:第一年收入 health=%r，期望 'stale'" % concl.get("health"))
        ok = False
    else:
        print("[OK] state.json: conclusion:第一年收入 health == 'stale'")

    # 边（附加信息）
    edges = [e for e in (state.get("edges") or [])
             if e.get("type") == "depends_on"]
    print("     依赖边数量=%d: %s" % (len(edges), edges))

    # 2) events.jsonl: 派生 CONCLUSION_STALE，带 source_event_id（指向真实变化）
    try:
        with open(EVENTS_BRIDGE, "r", encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except Exception as e:
        print("FAIL: 无法读取 events.jsonl: %s" % e)
        return False
    derived_stale = []
    for l in lines:
        try:
            rec = json.loads(l)
        except Exception:
            continue
        if rec.get("type") == "CONCLUSION_STALE" and rec.get("judge_result") == "derived":
            derived_stale.append(rec)
    if not derived_stale:
        print("FAIL: events.jsonl 无派生 CONCLUSION_STALE (judge_result=derived)")
        ok = False
    else:
        good = [d for d in derived_stale
                if d.get("source_event_id") and d.get("affected_nodes")]
        if not good:
            print("FAIL: 派生 CONCLUSION_STALE 缺 source_event_id 或 affected_nodes: %r"
                  % derived_stale)
            ok = False
        else:
            for d in good:
                print("[OK] events.jsonl 派生 CONCLUSION_STALE: event_id=%s "
                      "source_event_id=%s affected_nodes=%s"
                      % (d.get("event_id"), d.get("source_event_id"),
                         d.get("affected_nodes")))
            # 校验 source_event_id 确实指向一条真实、且类型为 ASSUMPTION_CHANGED 的历史事件
            # （证明"归因正确"：标了 stale 必须确因某次假设变化而触发；指向不存在或错误类型 = 归因错误 = 硬失败）
            rec_by_id = {}
            for l in lines:
                if not l.strip():
                    continue
                try:
                    r = json.loads(l)
                except Exception:
                    continue
                eid = r.get("event_id")
                if eid is not None:
                    rec_by_id[eid] = r
            for d in good:
                sid = d.get("source_event_id")
                if sid not in rec_by_id:
                    print("FAIL: CONCLUSION_STALE event_id=%s 的 source_event_id=%s "
                          "指向不存在的事件（归因错误，第 2 条证据失效）"
                          % (d.get("event_id"), sid))
                    ok = False
                elif rec_by_id[sid].get("type") != "ASSUMPTION_CHANGED":
                    print("FAIL: CONCLUSION_STALE event_id=%s 的 source_event_id=%s "
                          "指向的事件类型是 %r 而非 'ASSUMPTION_CHANGED'（归因错误）"
                          % (d.get("event_id"), sid, rec_by_id[sid].get("type")))
                    ok = False
                else:
                    print("[OK] source_event_id=%s 指向真实已落库的 ASSUMPTION_CHANGED" % sid)

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true",
                    help="只验证harness（起server+health），不发21句")
    args = ap.parse_args()

    kill_port()
    clear_state()
    proc = start_server()
    try:
        if args.smoke:
            print("[smoke] harness OK (server up). 不发消息，退出。")
            return 0
        gave_up = send_all()
        ok = verify()
        if gave_up > 0:
            print("\n注意：有 %d 条消息因 LLM 错误放弃，验收可能不完整。" % gave_up)
        return 0 if ok else 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        # 兜底再清一次端口
        kill_port()
        print("[cleanup] server stopped")


if __name__ == "__main__":
    sys.exit(main())
