# -*- coding: utf-8 -*-
"""Sprint 5 预检（隔离）：验证 server 端 role 链路 + LLM 可用性。

不在真实 received/runtime 上跑，避免污染 Sprint 5 真机验收。
起一个临时端口 8799 的 server（同一份 server.py = Sprint 4 runtime 代码），
用 role="user" 发 2 条测试消息，核对：
  1) turns.jsonl 两条 role == "user"（不是 "?"）
  2) state.json 出现 assumption:测试字段，v1=1 / v2=2
  3) events.jsonl 两条事件 source_role == "user"
跑完自停 server、删临时目录。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.request import ProxyHandler, Request

PY = r"C:/Users/Kevin Chan/.workbuddy/binaries/python/versions/3.13.12/python.exe"
BASE = r"C:/Users/Kevin Chan/WorkBuddy/2026-09-26-01-50-43/browser-bridge"
API_KEY = "sk-4f1bc912b7984d049532266ec49d0267"
API_URL = "https://api.deepseek.com"
PORT = 8799
BASE_URL = "http://127.0.0.1:%d" % PORT

TMP = os.path.join(BASE, ".sprint5_preflight")
SERVER_LOG = os.path.join(TMP, "server.log")
STATE = os.path.join(TMP, "runtime", "state.json")
EVENTS = os.path.join(TMP, "runtime", "events.jsonl")
TURNS = os.path.join(TMP, "turns.jsonl")


def kill_port():
    try:
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=15)
        out = proc.stdout.decode("gbk", errors="ignore") if proc.stdout else ""
    except Exception:
        return
    for line in out.splitlines():
        if (":%d" % PORT) in line and "LISTENING" in line:
            pid = line.split()[-1]
            if pid.isdigit():
                try:
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=15)
                except Exception:
                    pass


def start_server():
    os.makedirs(TMP, exist_ok=True)
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = API_KEY
    env["OPENAI_BASE_URL"] = API_URL
    env["BRIDGE_LLM"] = "real"
    env["BRIDGE_PORT"] = str(PORT)
    env["BRIDGE_RECV_DIR"] = TMP
    f = open(SERVER_LOG, "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(BASE, "server.py")],
                            cwd=BASE, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                if r.status == 200:
                    print("[server] up pid=%s" % proc.pid)
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("server did not become healthy")


def post_turn(text, mid):
    payload = {
        "type": "BRIDGE_TURN",
        "source": "chatgpt",
        "role": "user",
        "text": text,
        "message_id": mid,
        "message_id_source": "attr:data-message-id",
    }
    req = Request(BASE_URL + "/turn",
                  data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  method="POST", headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(ProxyHandler({}))
    with opener.open(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    kill_port()
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP, exist_ok=True)
    proc = None
    try:
        proc = start_server()
        msgs = ["假设测试字段是 1", "假设测试字段改成 2"]
        statuses = []
        for i, m in enumerate(msgs, 1):
            try:
                r = post_turn(m, "pre-%02d" % i)
                statuses.append(r.get("runtime_status"))
                print("[%02d] status=%s records=%s :: %s"
                      % (i, r.get("runtime_status"), r.get("records"), m))
            except Exception as e:
                print("[%02d] POST ERR %s: %s" % (i, type(e).__name__, e))
                statuses.append("ERR")
            time.sleep(2)
        time.sleep(1)

        print("\n=== 预检核对 ===")
        ok = True

        # 1) turns.jsonl role
        roles = []
        if os.path.exists(TURNS):
            with open(TURNS, "r", encoding="utf-8") as f:
                for line in f.read().splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    roles.append((rec.get("message_id"), rec.get("role")))
        print("turns.jsonl roles:", roles)
        if all(r == "user" for _, r in roles) and len(roles) >= 2:
            print("[OK] 两条记录 role 都是 'user'（非 '?'）")
        else:
            print("FAIL: turns.jsonl role 不达标")
            ok = False

        # 2) state.json 节点
        node = None
        if os.path.exists(STATE):
            with open(STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
            for n in state.get("nodes") or []:
                if n.get("id") == "assumption:测试字段":
                    node = n
                    break
        if node is None:
            print("FAIL: state.json 无 assumption:测试字段（LLM 可能 429 -> runtime_status=%s）"
                  % statuses)
            ok = False
        else:
            vers = node.get("versions") or []
            vals = [(v.get("version"), v.get("value"), v.get("status")) for v in vers]
            print("state.json assumption:测试字段 versions:", vals)
            if vals == [(1, "1", "superseded"), (2, "2", "active")] or \
               (len(vals) == 2 and vals[0][1] == "1" and vals[1][1] == "2"):
                print("[OK] 节点两版 v1=1 / v2=2")
            else:
                print("FAIL: 节点版本不达标")
                ok = False

        # 3) events.jsonl source_role
        srcs = []
        if os.path.exists(EVENTS):
            with open(EVENTS, "r", encoding="utf-8") as f:
                for line in f.read().splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    srcs.append((rec.get("type"), rec.get("source_role")))
        print("events.jsonl source_role:", srcs)
        user_srcs = [s for s in srcs if s[1] == "user"]
        if len(user_srcs) >= 2:
            print("[OK] events.jsonl 至少两条 source_role=='user'")
        else:
            print("FAIL: events.jsonl source_role 不达标")
            ok = False

        print("\nPREFLIGHT:", "PASS" if ok else "FAIL",
              "| runtime_statuses:", statuses)
        return 0 if ok else 1
    finally:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
        kill_port()
        try:
            shutil.rmtree(TMP)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
