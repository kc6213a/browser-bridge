#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser-bridge 本地接收端 (v0.4 / Sprint 1)

相对 v0.3 的变化：
1. POST /turn 只做一件事：把消息喂给 Runtime，然后把 Result 原样落盘（D2）
       result = runtime.on_turn(message=text, turn=turn, recent_turns=recent, llm=llm)
       persist(result)   -> received/runtime/{events.jsonl, state.json, effects.jsonl}
   server 里没有任何 `if event_type == ...` 分支：
   HTTP <-> 输入输出转换之外的一切，都由 runtime.on_turn 封装。
2. 新增 GET /effects?after_id=N（D3）：返回 id > N 的 effects，缺省返回全部。无 SSE。
3. llm_adapter 换成 runtime/llm_adapter.py 的 OpenAICompatibleAdapter（str / list[dict] 皆可）。

落盘三件套（persist）：
    received/runtime/events.jsonl   result.records 全部（含 rejected / derived），逐行追加
    received/runtime/state.json     result 之后的 state 快照（整份覆盖写）
    received/runtime/effects.jsonl  result.effects，每条额外加 source_event_id + 单调递增 id

保留 v0.3 行为：
- message_id 去重键（content.js 抽取；缺失时退化为 hash(role+text)）
- GET  /health
- POST /diagnose
- GET  /recent
- GET  /test-page.html
无第三方依赖，仅标准库。

配置（环境变量）：
    CONVERSATION_AGENT_HOME  Runtime 项目根，默认 C:/Users/Kevin Chan/WorkBuddy/Conversation Agent
    BRIDGE_PORT              默认 8787
    BRIDGE_RECV_DIR          默认 <base>/received
    BRIDGE_LLM               real（默认，打真模型）| none（不调模型，Runtime 仍被调用）
"""
import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_AGENT_HOME = "C:/Users/Kevin Chan/WorkBuddy/Conversation Agent"

AGENT_HOME = os.environ.get("CONVERSATION_AGENT_HOME") or DEFAULT_AGENT_HOME
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
RECV_DIR = os.environ.get("BRIDGE_RECV_DIR") or os.path.join(BASE_DIR, "received")
LOG_PATH = os.environ.get("BRIDGE_LOG") or os.path.join(BASE_DIR, "received.log")
EVENT_LOG = os.environ.get("BRIDGE_EVENT_LOG") or os.path.join(AGENT_HOME, "runtime", "event_log.jsonl")
STATE_FILE = os.environ.get("BRIDGE_STATE_FILE") or os.path.join(AGENT_HOME, "runtime", "state.json")
LLM_MODE = (os.environ.get("BRIDGE_LLM", "real") or "real").strip().lower()
RECENT_WINDOW = 10

TURNS_PATH = os.path.join(RECV_DIR, "turns.jsonl")

# ---- Sprint 1: received/runtime/ 三件套 ----
RUNTIME_DIR = os.path.join(RECV_DIR, "runtime")
BRIDGE_EVENTS_PATH = os.path.join(RUNTIME_DIR, "events.jsonl")
BRIDGE_STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")
BRIDGE_EFFECTS_PATH = os.path.join(RUNTIME_DIR, "effects.jsonl")

_lock = threading.Lock()
_received = []
_recent_texts = []
_seen_keys = set()
_turn_seq = 0
_diag_count = 0

_effects_cache = []      # GET /effects 的内存视图（启动时从 effects.jsonl 灌回）
_effect_seq = 0          # 单调递增 effect id；跨重启续编号

RUNTIME = {
    "status": "not-wired",
    "home": AGENT_HOME,
    "error": None,
    "obj": None,
    "llm": None,
    "state_file": STATE_FILE,
}

os.makedirs(RECV_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)


def _log(msg):
    line = "[%s] %s" % (datetime.now().isoformat(timespec="seconds"), msg)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def _stdout(msg):
    """黑窗口 + 日志双写。"""
    print(msg, flush=True)
    _log(msg)


# ------------------------------------------------------------------
# Runtime 接线（显式、失败即死）
# ------------------------------------------------------------------

def init_runtime():
    """把 Bridge 接到 Conversation Agent 的 runtime 上。缺件 -> SystemExit（不静默降级）。"""
    if not os.path.isdir(AGENT_HOME):
        raise SystemExit(
            "[FATAL] CONVERSATION_AGENT_HOME 不存在: %s\n"
            "        设置该环境变量指向 Conversation Agent 项目根，或创建该目录。" % AGENT_HOME
        )
    runtime_pkg = os.path.join(AGENT_HOME, "runtime")
    agent_runtime_py = os.path.join(runtime_pkg, "agent_runtime.py")
    if not os.path.isfile(agent_runtime_py):
        raise SystemExit(
            "[FATAL] 在 %s 下找不到 runtime/agent_runtime.py\n"
            "        Runtime 没接上就是没接上，不会假装接上。" % AGENT_HOME
        )

    sys.path.insert(0, AGENT_HOME)
    try:
        from runtime.agent_runtime import (
            AgentRuntime,
            load_or_init_state,
            save_state,
        )
        from runtime.event_store import EventStore
        from runtime.llm_adapter import default_llm
    except Exception as e:
        raise SystemExit(
            "[FATAL] 导入 runtime 失败（%s: %s）\n        home=%s"
            % (type(e).__name__, e, AGENT_HOME)
        ) from e

    llm = None
    if LLM_MODE == "real":
        try:
            llm = default_llm()   # 缺 OPENAI_API_KEY 会抛 LLMError
        except Exception as e:
            raise SystemExit(
                "[FATAL] 构造 llm_adapter 失败（%s: %s）\n"
                "        需要 OPENAI_API_KEY；或用 BRIDGE_LLM=none 关闭模型调用。"
                % (type(e).__name__, e)
            ) from e
    elif LLM_MODE != "none":
        raise SystemExit("[FATAL] BRIDGE_LLM 只接受 real|none，收到 %r" % LLM_MODE)

    state = load_or_init_state(STATE_FILE, conversation_id="browser-bridge")
    rt = AgentRuntime(store=EventStore(EVENT_LOG), state=state, llm=llm)

    RUNTIME.update({
        "status": "wired",
        "obj": rt,
        "llm": llm,
        "state": state,
        "_save_state": save_state,
        "event_log": EVENT_LOG,
    })
    _log("RUNTIME wired home=%s llm=real(%s) event_log=%s state_file=%s"
         % (AGENT_HOME, llm.model if llm else "none", EVENT_LOG, STATE_FILE))


# ------------------------------------------------------------------
# 去重键
# ------------------------------------------------------------------

def _dedupe_key(payload):
    mid = payload.get("message_id")
    if isinstance(mid, str) and mid.strip():
        return "id:" + mid.strip()
    # 老客户端（v0.2 及更早）不带 message_id：退化为 role+text 哈希，照样去重
    base = "%s|%s" % (payload.get("role") or "?", payload.get("text") or "")
    return "hash:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _prime_from_disk():
    """重启后仍要去重：把已有 turns.jsonl 的键灌回内存。"""
    global _turn_seq
    if not os.path.exists(TURNS_PATH):
        return
    texts = []
    with open(TURNS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            _seen_keys.add(_dedupe_key(rec))
            _turn_seq += 1
            if isinstance(rec.get("text"), str):
                texts.append(rec["text"])
    _recent_texts.extend(texts[-RECENT_WINDOW:])
    if _turn_seq:
        _log("primed %d dedupe keys from existing %s" % (len(_seen_keys), TURNS_PATH))


def _prime_effects():
    """重启后 effect id 继续单调自增：从现有 effects.jsonl 灌回内存 + 取 max(id)。"""
    global _effect_seq
    if not os.path.exists(BRIDGE_EFFECTS_PATH):
        return
    with open(BRIDGE_EFFECTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            _effects_cache.append(item)
            eid = item.get("id")
            if isinstance(eid, int) and eid > _effect_seq:
                _effect_seq = eid
    if _effects_cache:
        _log("primed %d effects from %s (max id=%d)"
             % (len(_effects_cache), BRIDGE_EFFECTS_PATH, _effect_seq))


# ------------------------------------------------------------------
# persist：result -> received/runtime/ 三件套
# ------------------------------------------------------------------

def persist(result, state):
    """把 TurnResult 落盘。result.records / result.effects 全部写，不挑不选。"""
    global _effect_seq

    with _lock:
        if result.records:
            with open(BRIDGE_EVENTS_PATH, "a", encoding="utf-8") as f:
                for rec in result.records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if result.effects:
            with open(BRIDGE_EFFECTS_PATH, "a", encoding="utf-8") as f:
                for idx, eff in enumerate(result.effects):
                    # records 与 effects 在 Runtime 内部一一对应，按下标取源事件 id
                    source_event_id = None
                    if idx < len(result.records):
                        source_event_id = result.records[idx].get("event_id")
                    _effect_seq += 1
                    item = {"id": _effect_seq, "source_event_id": source_event_id}
                    item.update(eff)
                    _effects_cache.append(item)
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

        if state is not None:
            tmp = BRIDGE_STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, BRIDGE_STATE_PATH)


# ------------------------------------------------------------------
# /turn：去重 + 喂 Runtime + 落盘
# ------------------------------------------------------------------

def process_turn(payload):
    global _turn_seq
    text = payload.get("text") or ""
    role = payload.get("role") or "?"
    source = payload.get("source") or "unknown"
    mid = payload.get("message_id")
    mid_source = payload.get("message_id_source")

    key = _dedupe_key(payload)
    with _lock:
        if key in _seen_keys:
            _log("TURN DUP  skipped message_id=%s source=%s key=%s" % (mid, mid_source, key))
            return {
                "ok": True,
                "duplicate": True,
                "turn": None,
                "runtime_status": "skipped_duplicate",
                "message_id": mid,
            }
        _seen_keys.add(key)
        _turn_seq += 1
        turn_no = _turn_seq
        recent = list(_recent_texts[-RECENT_WINDOW:])

    # LLM 调用放锁外，避免阻塞 /health
    runtime_block = {"status": "not-called", "error": None, "records": [],
                     "effects": [], "decisions": []}
    rt = RUNTIME.get("obj")
    if rt is None:
        runtime_block = {"status": "error", "error": "runtime not wired",
                         "records": [], "effects": [], "decisions": []}
        _log("RUNTIME_ERROR turn=%d :: runtime not wired" % turn_no)
    else:
        try:
            result = rt.on_turn(
                message=text,
                turn=turn_no,
                recent_turns=recent,
                llm=RUNTIME.get("llm"),
                role=role,
                message_id=mid,
            )
            runtime_block = {
                "status": result.status,
                "error": result.error,
                "records": result.records,
                "effects": result.effects,
                "decisions": result.decisions,
                "signals": result.signals,
            }
            persist(result, rt.state)
            try:
                RUNTIME["_save_state"](STATE_FILE, rt.state)
            except Exception as e:  # 状态落盘失败不应该吞掉这一轮结果
                _log("STATE_SAVE_ERROR turn=%d :: %s: %s" % (turn_no, type(e).__name__, e))

            # 验收行：格式固定，不许改
            _stdout("[runtime] turn=%d records=%d effects=%d"
                    % (turn_no, len(result.records), len(result.effects)))
            for d in result.decisions:
                _log("  RUNTIME_DECISION turn=%d type=%s judge=%s conf=%s rule=%s level=%s actions=%s"
                     % (turn_no, d.get("type"), d.get("judge_result"), d.get("confidence"),
                        d.get("rule_id"), d.get("level"), d.get("actions")))
        except Exception as e:
            # 模型挂了也要把消息落盘，但状态必须标成 error，不许记成成功
            runtime_block = {
                "status": "error",
                "error": "%s: %s" % (type(e).__name__, e),
                "records": [], "effects": [], "decisions": [], "signals": [],
            }
            _log("RUNTIME_ERROR turn=%d :: %s: %s" % (turn_no, type(e).__name__, e))

    record = dict(payload)
    record["_turn"] = turn_no
    record["_runtime"] = runtime_block

    with _lock:
        _received.append(record)
        with open(TURNS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _recent_texts.append(text)
        del _recent_texts[:-RECENT_WINDOW]

    _log("TURN #%d source=%s layer=%s role=%s len=%d mid=%s mid_src=%s runtime=%s :: %r"
         % (turn_no, source, payload.get("layer"), role, len(text),
            mid, mid_source, runtime_block.get("status"), text[:80]))

    return {
        "ok": True,
        "duplicate": False,
        "turn": turn_no,
        "runtime_status": runtime_block.get("status"),
        "records": len(runtime_block.get("records") or []),
        "echo_len": len(text),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BrowserBridge/0.4"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            return {"__error__": "bad json: %s" % e}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._json(200, {
                "ok": True,
                "service": "browser-bridge-receiver",
                "version": "0.4.0",
                "pid": os.getpid(),
                "port": PORT,
                "turns": len(_received),
                "diagnoses": _diag_count,
                "recv_dir": RECV_DIR,
                "dedupe_keys": len(_seen_keys),
                "effects": len(_effects_cache),
                "runtime": {
                    "home": AGENT_HOME,
                    "status": RUNTIME["status"],
                    "llm": LLM_MODE,
                    "model": RUNTIME["llm"].model if RUNTIME.get("llm") else None,
                    "event_log": EVENT_LOG,
                    "state_file": STATE_FILE,
                    "bridge_events": BRIDGE_EVENTS_PATH,
                    "bridge_state": BRIDGE_STATE_PATH,
                    "bridge_effects": BRIDGE_EFFECTS_PATH,
                },
            })
        elif path == "/recent":
            self._json(200, {"count": len(_received), "items": _received[-20:]})
        elif path == "/effects":
            self._effects(parse_qs(parsed.query))
        elif path == "/state":
            self._state()
        elif path in ("/test-page.html", "/", "/index.html"):
            try:
                with open(os.path.join(BASE_DIR, "test-page.html"), "rb") as f:
                    data = f.read()
            except OSError:
                self._json(404, {"ok": False, "error": "test-page.html not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _effects(self, query):
        """GET /effects?after_id=N —— 返回 id > N 的 effects；缺省返回全部。"""
        raw = (query.get("after_id") or [""])[0]
        if raw.strip() == "":
            after_id = -1          # 缺省 -> 全部
        else:
            try:
                after_id = int(raw)
            except ValueError:
                self._json(400, {"ok": False, "error": "after_id must be an integer"})
                return
        with _lock:
            items = [e for e in _effects_cache if e.get("id", -1) > after_id]
        self._json(200, {
            "ok": True,
            "count": len(items),
            "after_id": after_id,
            "items": items,
        })

    def _state(self):
        """GET /state —— 透传 received/runtime/state.json。

        - 文件存在且可解析：原样返回 JSON 内容（前端按 state 直接渲染）。
        - 文件缺失 / 为空 / 解析失败：返回 {"ok": true, "state": null}
          （前端据此显示「尚无数据」）。
        不改动 /turn /effects /health /diagnose /recent 任何行为。
        """
        if not os.path.exists(BRIDGE_STATE_PATH):
            self._json(200, {"ok": True, "state": None})
            return
        try:
            with open(BRIDGE_STATE_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as e:
            self._json(500, {"ok": False, "error": "state read failed: %s" % e})
            return
        if not content:
            self._json(200, {"ok": True, "state": None})
            return
        try:
            state = json.loads(content)
        except json.JSONDecodeError:
            # 半写/损坏也不崩前端：当作「尚未就绪」
            self._json(200, {"ok": True, "state": None})
            return
        self._json(200, state)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/turn":
            self._turn()
        elif path == "/diagnose":
            self._diagnose()
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _turn(self):
        payload = self._read_json()
        if "__error__" in payload:
            self._json(400, {"ok": False, "error": payload["__error__"]})
            return
        self._json(200, process_turn(payload))

    def _diagnose(self):
        global _diag_count
        payload = self._read_json()
        if "__error__" in payload:
            self._json(400, {"ok": False, "error": payload["__error__"]})
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = os.path.join(RECV_DIR, "diagnose_%s.json" % ts)
        n = 0
        while os.path.exists(fname):
            n += 1
            fname = os.path.join(RECV_DIR, "diagnose_%s_%d.json" % (ts, n))
        with _lock:
            _diag_count += 1
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        _log("DIAGNOSE layer=%s site_hit=%s selector=%s count=%s message_id_source=%s -> %s"
             % (payload.get("layer"),
                payload.get("site_selector_hit"),
                payload.get("selector_used"),
                payload.get("message_count"),
                payload.get("message_id_source"),
                os.path.basename(fname)))
        _log("DIAGNOSE_DETAIL " + json.dumps({
            "url": payload.get("url"),
            "hostname": payload.get("hostname"),
            "sample_roles": payload.get("sample_roles"),
            "sample_texts": payload.get("sample_texts"),
            "sample_message_ids": payload.get("sample_message_ids"),
            "message_id_sources": payload.get("message_id_sources"),
            "dom_signature": (payload.get("dom_signature") or "")[:200],
        }, ensure_ascii=False))
        self._json(200, {"ok": True, "file": os.path.basename(fname)})

    def log_message(self, fmt, *args):
        pass


def main():
    init_runtime()
    _prime_from_disk()
    _prime_effects()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    _stdout("receiver v0.4 listening on http://%s:%d  (pid=%d) recv_dir=%s runtime=%s llm=%s"
            % (HOST, PORT, os.getpid(), RECV_DIR, RUNTIME["status"], LLM_MODE))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
