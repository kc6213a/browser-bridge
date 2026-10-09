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

# {turn_no(int): at(str ISO)} —— 仅供 GET /state 附加 at_last 用。
# 纯内存派生视图，不写回 turns.jsonl / state.json，重启由 _prime_from_disk 重建。
_turn_at = {}

# ---- 按 hostname 分区 runtime（每个 host 一套 state + event_log）----
# 2026-10-09 再加一层：按「会话/项目」分区（host + project 两级）。
#   ChatGPT   URL /g/g-p-<hex>-<slug>/...  -> 项目 id  `g-p-<hex>`
#   DeepSeek  URL /a/chat/s/<uuid>         -> 会话 id  `s-<uuid>`（无项目概念，一个会话=一个容器）
#   抓不到    -> `_no_project` 兜底桶
# 迁移前的旧混合文件仍留在 AGENT_HOME/runtime/ 下（state.json / event_log.jsonl），
# 只作为 _archive_pre_split 的备份来源，server 不再读写它们。
HOSTS_ROOT = os.path.join(AGENT_HOME, "runtime", "hosts")          # CA 侧（权威）
BRIDGE_HOSTS_ROOT = os.path.join(RUNTIME_DIR, "hosts")             # Bridge 侧（面板读的镜像）
DEFAULT_HOST = "_unknown"                                          # source 缺失/未知时的兜底桶
DEFAULT_PROJECT = "_no_project"                                    # 抓不到 project_id 时的兜底桶

# "host::project" -> {"obj","llm","state","state_file","event_log",
#                     "bridge_state","bridge_events","host","project"}
RUNTIMES = {}

# 仍然保留：全局接线状态（导入的 runtime 类 + llm），各 host 桶共享同一个 llm 实例。
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


def _safe_host(raw):
    """hostname -> 桶名。空 / unknown / 无意义值 -> _unknown；其余做路径安全化。"""
    s = (raw or "").strip()
    if not s or s.lower() in ("unknown", "null", "none", "undefined"):
        return DEFAULT_HOST
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch in "._-") else "_")
    return "".join(out) or DEFAULT_HOST


def _safe_project(raw):
    """project_id -> 桶名。空 / null / 无意义值 -> _no_project；其余做路径安全化。"""
    s = (raw or "").strip()
    if not s or s.lower() in ("unknown", "null", "none", "undefined"):
        return DEFAULT_PROJECT
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch in "._-") else "_")
    return "".join(out) or DEFAULT_PROJECT


def _bucket_key(host, project):
    """RUNTIMES 的内部键。host 与 project 都不含 '::'，用双冒号分隔不会歧义。"""
    return "%s::%s" % (host, project or DEFAULT_PROJECT)


def _host_paths(host, project=None):
    """一个 (host, project) 的四条路径：CA 权威 state/event_log + Bridge 镜像 state/events。

    目录结构：hosts/<host>/<project>/{state.json,event_log.jsonl}
    """
    proj = project or DEFAULT_PROJECT
    ca = os.path.join(HOSTS_ROOT, host, proj)
    br = os.path.join(BRIDGE_HOSTS_ROOT, host, proj)
    return {
        "host": host,
        "project": proj,
        "state_file": os.path.join(ca, "state.json"),
        "event_log": os.path.join(ca, "event_log.jsonl"),
        "bridge_state": os.path.join(br, "state.json"),
        "bridge_events": os.path.join(br, "events.jsonl"),
    }


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

    # 分区后：这里只做「接线」（把类与 llm 存起来），真正建桶交给 init_host_runtime。
    RUNTIME.update({
        "status": "wired",
        "llm": llm,
        "_save_state": save_state,
        "_AgentRuntime": AgentRuntime,
        "_load_or_init_state": load_or_init_state,
        "_EventStore": EventStore,
    })
    _log("RUNTIME wired home=%s llm=%s hosts_root=%s"
         % (AGENT_HOME, (llm.model if llm else "none"), HOSTS_ROOT))


def init_host_runtime(host, project=None):
    """为一个 (host, project) 建独立 runtime（state + event_log 各自一份）。"""
    proj = project or DEFAULT_PROJECT
    key = _bucket_key(host, proj)
    if RUNTIME["status"] != "wired":
        raise SystemExit("[FATAL] runtime 未接线，无法为 %s 建桶" % key)
    p = _host_paths(host, proj)
    os.makedirs(os.path.dirname(p["state_file"]), exist_ok=True)
    os.makedirs(os.path.dirname(p["bridge_state"]), exist_ok=True)

    state = RUNTIME["_load_or_init_state"](p["state_file"], conversation_id="browser-bridge")
    rt = RUNTIME["_AgentRuntime"](
        store=RUNTIME["_EventStore"](p["event_log"]), state=state, llm=RUNTIME["llm"]
    )
    RUNTIMES[key] = {
        "host": host,
        "project": proj,
        "obj": rt,
        "state": state,
        "llm": RUNTIME["llm"],
        "_save_state": RUNTIME["_save_state"],
        "status": "wired",
        **{k: v for k, v in p.items() if k not in ("host", "project")},
    }
    _log("BUCKET wired %s state=%s event_log=%s nodes=%d"
         % (key, p["state_file"], p["event_log"], len(state.get("nodes") or [])))
    return RUNTIMES[key]


def get_runtime(host, project=None):
    """按需建桶（不在锁内调用 persist，避免死锁）。"""
    key = _bucket_key(host, project)
    bucket = RUNTIMES.get(key)
    if bucket is not None:
        return bucket
    with _lock:
        bucket = RUNTIMES.get(key)
        if bucket is None:
            bucket = init_host_runtime(host, project)
    return bucket


def _prime_known_hosts():
    """启动时把已迁移的 (host, project) 桶预建起来，避免首个 /state 才建桶。

    兼容两种目录形态：
      hosts/<host>/<project>/   （新，两级）
      hosts/<host>/             （迁移前的一级，state.json 直接躺在 host 目录下）
    后者按 _no_project 归档处理。
    """
    buckets = set()
    if os.path.isdir(HOSTS_ROOT):
        for name in sorted(os.listdir(HOSTS_ROOT)):
            hp = os.path.join(HOSTS_ROOT, name)
            if not os.path.isdir(hp):
                continue
            subs = [s for s in sorted(os.listdir(hp))
                    if os.path.isdir(os.path.join(hp, s))]
            if subs:
                for s in subs:
                    buckets.add((name, s))
            else:
                buckets.add((name, DEFAULT_PROJECT))
    if not buckets:
        buckets.add((DEFAULT_HOST, DEFAULT_PROJECT))
    for h, p in sorted(buckets):
        try:
            init_host_runtime(h, p)
        except Exception as e:
            _log("BUCKET_INIT_ERROR %s :: %s: %s" % (h, type(e).__name__, e))
    _log("buckets primed: %s" % ",".join(sorted(RUNTIMES.keys())))


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
            # at_last 的原料：turn -> 采集时间
            tn = rec.get("_turn")
            at = rec.get("at")
            if isinstance(tn, int) and isinstance(at, str):
                _turn_at[tn] = at
            if isinstance(rec.get("text"), str):
                texts.append(rec["text"])
    _recent_texts.extend(texts[-RECENT_WINDOW:])
    if _turn_seq:
        _log("primed %d dedupe keys, %d turn timestamps from existing %s"
             % (len(_seen_keys), len(_turn_at), TURNS_PATH))


def _fmt_at(at):
    """ISO 8601 UTC -> 本地时区 'YYYY-MM-DD HH:MM'；不可用返回 ''。"""
    if not isinstance(at, str) or len(at) < 16:
        return ""
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        # 解析失败回退到原行为，不崩
        return at[:16].replace("T", " ")


def _last_at_for(node):
    """节点最后活跃时间：source_turns 中最大 turn 对应的采集时间。

    取 max 而非 [-1]，因为 source_turns 不保证有序；
    若最大 turn 缺 at，向次大退让，直到找到第一条有时间戳的。
    """
    st = node.get("source_turns")
    if isinstance(st, str):
        try:
            st = json.loads(st)
        except json.JSONDecodeError:
            st = []
    if not isinstance(st, (list, tuple)):
        return ""
    nums = []
    for t in st:
        try:
            nums.append(int(t))
        except (TypeError, ValueError):
            pass
    for t in sorted(set(nums), reverse=True):
        at = _fmt_at(_turn_at.get(t))
        if at:
            return at
    return ""


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

def persist(result, state, bucket=None):
    """把 TurnResult 落盘。result.records / result.effects 全部写，不挑不选。

    bucket 非 None 时：events 与 state 镜像写到该 (host, project) 自己的目录（分区）。
    effects 仍是全局单流（id 必须跨桶单调递增，after_id 才成立），
    但每条打上 host / project 标记，GET /effects?host=&project= 可按桶过滤。
    """
    global _effect_seq

    events_path = (bucket or {}).get("bridge_events") or BRIDGE_EVENTS_PATH
    state_path = (bucket or {}).get("bridge_state") or BRIDGE_STATE_PATH
    host = (bucket or {}).get("host") or DEFAULT_HOST
    project = (bucket or {}).get("project") or DEFAULT_PROJECT

    with _lock:
        if result.records:
            os.makedirs(os.path.dirname(events_path), exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as f:
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
                    item = {"id": _effect_seq, "source_event_id": source_event_id,
                            "host": host, "project": project}
                    item.update(eff)
                    _effects_cache.append(item)
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")

        if state is not None:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, state_path)


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

    host = _safe_host(source)
    # 2026-10-09：第二级分区键。老客户端不带 project_id -> 自动落 _no_project 兜底桶。
    project = _safe_project(payload.get("project_id"))

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
    try:
        bucket = get_runtime(host, project)
        rt = bucket["obj"]
    except SystemExit:
        raise
    except Exception as e:
        rt = None
        bucket = None
        _log("BUCKET_INIT_ERROR turn=%d %s :: %s: %s"
             % (turn_no, _bucket_key(host, project), type(e).__name__, e))

    if rt is None:
        runtime_block = {"status": "error", "error": "runtime not wired",
                         "records": [], "effects": [], "decisions": []}
        _log("RUNTIME_ERROR turn=%d %s :: runtime not wired"
             % (turn_no, _bucket_key(host, project)))
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
            persist(result, rt.state, bucket)
            try:
                RUNTIME["_save_state"](bucket["state_file"], rt.state)
            except Exception as e:  # 状态落盘失败不应该吞掉这一轮结果
                _log("STATE_SAVE_ERROR turn=%d %s :: %s: %s"
                     % (turn_no, _bucket_key(host, project), type(e).__name__, e))

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
        # at_last 的原料：本 turn 的采集时间进内存映射（不落盘，state.json 不感知）
        if isinstance(payload.get("at"), str):
            _turn_at[turn_no] = payload["at"]
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
                    "partitioned": True,
                    "default_host": DEFAULT_HOST,
                    "hosts_root": HOSTS_ROOT,
                    "legacy_event_log": EVENT_LOG,
                    "legacy_state_file": STATE_FILE,
                    "bridge_effects": BRIDGE_EFFECTS_PATH,
                },
                "hosts": {
                    key: {
                        "host": b.get("host"),
                        "project": b.get("project"),
                        "status": b.get("status"),
                        "nodes": len((b.get("state") or {}).get("nodes") or []),
                        "version": (b.get("state") or {}).get("version"),
                        "state_file": b.get("state_file"),
                        "event_log": b.get("event_log"),
                        "bridge_state": b.get("bridge_state"),
                    }
                    for key, b in sorted(RUNTIMES.items())
                },
            })
        elif path == "/recent":
            self._json(200, {"count": len(_received), "items": _received[-20:]})
        elif path == "/effects":
            self._effects(parse_qs(parsed.query))
        elif path == "/state":
            self._state(parse_qs(parsed.query))
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
        """GET /effects?after_id=N[&host=H][&project=P] —— 返回 id > N 的 effects；缺省返回全部。

        effects 是全局单流（id 跨桶单调递增，after_id 才有意义），
        每条带 host / project 标记；给了就按对应维度过滤。
        """
        raw = (query.get("after_id") or [""])[0]
        if raw.strip() == "":
            after_id = -1          # 缺省 -> 全部
        else:
            try:
                after_id = int(raw)
            except ValueError:
                self._json(400, {"ok": False, "error": "after_id must be an integer"})
                return
        host_raw = (query.get("host") or [""])[0]
        host = _safe_host(host_raw) if host_raw.strip() else None
        proj_raw = (query.get("project") or [""])[0]
        project = _safe_project(proj_raw) if proj_raw.strip() else None
        with _lock:
            items = [e for e in _effects_cache if e.get("id", -1) > after_id]
        if host is not None:
            items = [e for e in items if e.get("host") == host]
        if project is not None:
            items = [e for e in items if e.get("project") == project]
        self._json(200, {
            "ok": True,
            "count": len(items),
            "after_id": after_id,
            "host": host,
            "project": project,
            "items": items,
        })

    def _state(self, query):
        """GET /state?host=H[&project=P] —— 返回该 (host, project) 自己的 state（分区）。

        - host 缺省/未知 -> 兜底桶 _unknown；project 缺省/抓不到 -> _no_project。
        - 文件存在且可解析：原样返回 JSON 内容（前端按 state 直接渲染）。
        - 文件缺失 / 为空 / 解析失败：返回 {"ok": true, "state": null}
          （前端据此显示「尚无数据」）。
        """
        host_raw = (query.get("host") or [""])[0]
        host = _safe_host(host_raw)
        proj_raw = (query.get("project") or [""])[0]
        project = _safe_project(proj_raw)
        state_path = _host_paths(host, project)["bridge_state"]
        meta = {"host": host, "project": project}
        if not os.path.exists(state_path):
            self._json(200, dict({"ok": True, "state": None}, **meta))
            return
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as e:
            self._json(500, dict({"ok": False, "error": "state read failed: %s" % e}, **meta))
            return
        if not content:
            self._json(200, dict({"ok": True, "state": None}, **meta))
            return
        try:
            state = json.loads(content)
        except json.JSONDecodeError:
            # 半写/损坏也不崩前端：当作「尚未就绪」
            self._json(200, dict({"ok": True, "state": None}, **meta))
            return
        # 面板要标「来源会话」，这里顺手把桶的 project 写进响应（不落盘）。
        if isinstance(state, dict):
            state.setdefault("project", project)
            state.setdefault("host", host)
        # 附加 at_last：纯响应层派生，只加字段不写回 state.json。
        # 节点没有可用时间戳时给空串，前端据此不渲染。
        if isinstance(state, dict) and isinstance(state.get("nodes"), list):
            with _lock:
                for node in state["nodes"]:
                    if isinstance(node, dict) and "at_last" not in node:
                        node["at_last"] = _last_at_for(node)
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
    _prime_known_hosts()
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
