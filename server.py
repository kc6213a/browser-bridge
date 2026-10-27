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
import queue
import sys
import threading
import uuid
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
# 旁挂 SSE 落盘目录：与 received/ 物理隔离，不进 turns.jsonl、不喂 Runtime、不去重。
# 用于 #22 SSE 方案在 S1-S7 场景验证期间的数据收集，验证通过后可整体删除该目录。
SSE_DIR = os.environ.get("BRIDGE_SSE_DIR") or os.path.join(BASE_DIR, "received_sse")
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
_sse_lock = threading.Lock()
_received = []
_recent_texts = []
_seen_keys = dict()   # key: (host:str, project:str) -> value: set[str]  (#50 二级分区去重)
_turn_seq = 0
_diag_count = 0

# [#22 旁挂] SSE 腿内存态（server 重启即丢，运行期内足够；不持久化，避免复杂度）
_seen_sse_msgids = {}   # conversationId -> set of message ids（history 按 id 补差）
_sse_health = {}        # host -> {"last_turn_at": float|None, "turn_count": int}
_ws_flag_alerted = set()  # 已告警 resume_with_websockets 的 host（每 host 仅一次）
# [新判据 2026-10-27 凯文拍定] 只在「history 补到"刚产生"的新消息（服务端确有消息产出），
# 但实时 SSE 腿在同期窗口内没抓到任何 turn」时才告警 —— 消息在产生、实时腿没抓到 = 真失效。
# 取代旧判据「连续 N 次打开会话无 SSE turn」：旧判据在"只看历史、未发言"场景必误报
# （没发言本就不该有 turn，却被计成腿失效；23:25 复测已实证）。
SSE_FRESH_WINDOW_SEC = 300  # 秒：createTime / last_turn_at 距现在的"新鲜"窗口

# ---- SSE -> CA 回流（2026-10-28）----
# 背景：ChatGPT DOM 采集已永久关闭（CAPTURE_DISABLE_HOSTS），数据只走 SSE + history。
# 但 _sse 原只落盘 received_sse/、不喂 Runtime，导致 ChatGPT 数据断流、进不了 CA 推理层。
# 回流 = 把 SSE 抓到的消息补差后转成 /turn payload，交给 process_turn（同一条链路：
# 去重走 _dedupe_key + _seen_keys[(host,project)]，判定走 rt.on_turn，落盘走 persist）。
# 走 process_turn 而非直接写 turns.jsonl：后者只是原始落盘、不产事件（NEW_TOPIC 等）。
# 默认开（2026-10-28 复核 #52 后改）：DeepSeek 余额已恢复（实测 /user/balance HTTP 200,
# is_available=true, total_balance=12.81 CNY），且实测 62 条消息 101 次判定全部 runtime=ok。
# 仍可用 env BRIDGE_SSE_REFLOW=0 手动关。
# 运行期的兜底是「连续失败熔断」（见 SSE_REFLOW_FAIL_LIMIT）——欠费复发时自动停，不灌 402。
SSE_REFLOW_ENABLED = os.environ.get("BRIDGE_SSE_REFLOW", "1") != "0"
# 旧会话确认闸门（2026-10-28 凯文拍定）：单次补差消息数 <= 本值 -> 自动回流（新/短会话，零摩擦）；
# 超过 -> **不自动回流**，落 received_sse/reflow_pending.jsonl 待确认，避免打开一个长历史会话
# 就瞬间烧掉上百次 LLM 判定。确认后仍受 MAX_PER_BATCH 硬上限约束。
SSE_REFLOW_AUTO_MAX = int(os.environ.get("BRIDGE_SSE_REFLOW_AUTO_MAX", "20"))
# 连续 LLM 失败熔断：worker 连续 N 次判定失败（典型=HTTP 402 欠费/网络错）就自动关闭回流并告警，
# 防止欠费复发时把队列里剩下的全打一遍 402、灌爆日志。置 0 可禁用熔断。
SSE_REFLOW_FAIL_LIMIT = int(os.environ.get("BRIDGE_SSE_REFLOW_FAIL_LIMIT", "3"))
# 预估判定次数用的经验系数：2026-10-28 实测 62 条消息 -> 101 次 LLM 调用（1.63 次/条）。
# 一条消息可能同时产出 NEW_TOPIC / ASSUMPTION_INTRODUCED 等多个候选，各调一次。
# 仅用于「预估」展示给用户做确认决策，不参与任何判定逻辑。
SSE_REFLOW_CALL_RATIO = 1.63
# 单次回流上限（2026-10-28 凯文拍定）：即便用户已确认，也只回流「最近」的 N 条，
# 更早的标 skipped_too_many。默认值是安全阀，不是业务口径；要全量回流就把上限调大。
SSE_REFLOW_MAX_PER_BATCH = 50
# system：基本是空消息（text=''），喂进去只是白烧一次 LLM 判定 -> 滤。
# tool：占约 16.7%，是 AI 调函数的执行结果、不是用户认知；判断/结论里不该有（凯文拍定保持过滤）。
SSE_REFLOW_SKIP_ROLES = {"system", "tool"}
_sse_reflow_q = queue.Queue()   # 异步：LLM 判定慢，不能在 _sse 持锁路径里同步跑
_sse_reflow_fails = 0           # worker 连续失败计数（成功即清零）
REFLOW_PENDING_PATH = os.path.join(SSE_DIR, "reflow_pending.jsonl")

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


def _is_reserved_project(p):
    """保留桶：以下划线开头但**不是**当前兜底桶 `_no_project` 的项目 id，
    视为「历史/隐藏」桶，不出现在面板项目列表、也不经 URL 直接打开。

    约定：迁移前的混合数据统一归档为 `_legacy_no_project`；
    任何未来以下划线开头（非 `_no_project`）的桶都按保留处理，避免被误列成项目。
    """
    return bool(p) and p != DEFAULT_PROJECT and p.startswith("_")


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
    proj = project or DEFAULT_PROJECT
    if _is_reserved_project(proj):
        # 保留桶不可经运行时直接打开：退回当前兜底桶，历史数据保持只读
        proj = DEFAULT_PROJECT
    key = _bucket_key(host, proj)
    bucket = RUNTIMES.get(key)
    if bucket is not None:
        return bucket
    with _lock:
        bucket = RUNTIMES.get(key)
        if bucket is None:
            bucket = init_host_runtime(host, proj)
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
                    if os.path.isdir(os.path.join(hp, s))
                    and not _is_reserved_project(s)]
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

def _dedupe_key(payload, host=None, project=None):
    # host/project 入参仅用于选择外层分桶（见 _prime_from_disk / process_turn），
    # 内层键字符串形态保持 "id:..." / "hash:..." 不变 —— 去重键字符串零改动，回归闸门。
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
            # #50：历史 turn 按 (host, project or '_no_project') 分桶，跨项目不再共享去重。
            # pre-#40-B 的 project_id=None -> _safe_project -> '_no_project' 桶，与运行时未分区桶口径一致。
            host = _safe_host(rec.get("host") or rec.get("source") or "unknown")
            project = _safe_project(rec.get("project_id"))
            _seen_keys.setdefault((host, project), set()).add(_dedupe_key(rec, host, project))
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
        total_keys = sum(len(s) for s in _seen_keys.values())
        _log("primed %d dedupe keys across %d buckets, %d turn timestamps from existing %s"
             % (total_keys, len(_seen_keys), len(_turn_at), TURNS_PATH))


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

    bucket = (host, project)
    key = _dedupe_key(payload, host, project)
    with _lock:
        bset = _seen_keys.setdefault(bucket, set())
        if key in bset:
            _log("TURN DUP  bucket=%s skipped message_id=%s source=%s key=%s" % (bucket, mid, mid_source, key))
            return {
                "ok": True,
                "duplicate": True,
                "turn": None,
                "runtime_status": "skipped_duplicate",
                "message_id": mid,
            }
        bset.add(key)
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


def _sse_reflow_candidates(msgs, stat):
    """从 SSE 消息里挑出「值得回流」的候选，按 createTime 升序稳定排序。

    抽出来给闸门与入队共用：闸门要先知道条数才能决定「自动 or 待确认」。
    返回 [(role, text, message_id, createTime|None), ...]（**未**应用上限，由调用方裁）。
    """
    cands = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in SSE_REFLOW_SKIP_ROLES:
            stat["skipped_roles"] += 1
            continue
        text = (m.get("text") or "").strip()
        if not text:
            stat["skipped_roles"] += 1
            continue
        ct = m.get("createTime")
        cands.append((role or "?", text, m.get("id"),
                      ct if isinstance(ct, (int, float)) else None))

    # 显式按 createTime 升序（旧->新）稳定排序：不依赖「服务端数组方向」这个隐含假设。
    # 实证：23:21 那批 108 条整体是升序，但非严格（前 5 条出现 10:00:55 -> 10:00:53 的秒级倒退），
    # 稳定排序可修正抖动，同时保证将来服务端改方向也不会让抽取层读到倒序时间线。
    # 只有全部消息都带 createTime 时才排；否则保持原数组顺序，不引入额外假设。
    if cands and all(c[3] is not None for c in cands):
        before_ids = [c[2] for c in cands]
        cands.sort(key=lambda c: c[3])          # stable：同秒保持原相对顺序
        stat["sorted_by_create_time"] = True
        if [c[2] for c in cands] != before_ids:
            stat["reordered"] = True            # 排序确实改变了顺序，留痕便于验收
    else:
        stat["sorted_by_create_time"] = False
    return cands


def _sse_reflow_put(cands, host, project, stat):
    """把候选裁到上限后入队（内部用）。"""
    if len(cands) > SSE_REFLOW_MAX_PER_BATCH:
        stat["skipped_too_many"] = len(cands) - SSE_REFLOW_MAX_PER_BATCH
        cands = cands[-SSE_REFLOW_MAX_PER_BATCH:]
    for role, text, mid, _ct in cands:
        _sse_reflow_q.put({
            "text": text,
            "role": role,
            "source": host,
            "message_id": mid,
            "message_id_source": "sse",
            "project_id": project,      # None -> process_turn 落 _no_project 兜底桶
            "layer": "sse",
        })
        stat["enqueued"] += 1
    return stat


def _sse_reflow_gate(msgs, host, project, cid=None):
    """旧会话确认闸门（2026-10-28 凯文拍定）。

    - 候选数 <= SSE_REFLOW_AUTO_MAX：新/短会话，自动回流，零摩擦。
    - 候选数  > SSE_REFLOW_AUTO_MAX：长历史会话，**不自动回流**，落
      received_sse/reflow_pending.jsonl 待确认（附预估判定次数），避免瞬间烧掉上百次判定。
    返回 stat dict（含 status: 'enqueued' | 'pending_confirm' | 'disabled'）。
    """
    stat = {"enqueued": 0, "skipped_roles": 0, "skipped_too_many": 0, "status": "disabled"}
    if not SSE_REFLOW_ENABLED or not host:
        return stat

    cands = _sse_reflow_candidates(msgs, stat)
    n = len(cands)
    stat["candidates"] = n
    stat["est_calls"] = int(round(n * SSE_REFLOW_CALL_RATIO))

    if n == 0:
        stat["status"] = "enqueued"     # 无候选，等价于已处理
        return stat

    if n <= SSE_REFLOW_AUTO_MAX:
        _sse_reflow_put(cands, host, project, stat)
        stat["status"] = "enqueued"
        _log("SSE_REFLOW enqueue=%d skipped_roles=%d skipped_too_many=%d host=%s project=%s q=%d"
             % (stat["enqueued"], stat["skipped_roles"], stat["skipped_too_many"],
                host, project, _sse_reflow_q.qsize()))
        return stat

    # 超过自动阈值 -> 挂起待确认
    pid = uuid.uuid4().hex[:12]
    rec = {
        "pending_id": pid,
        "requested_at": datetime.now().isoformat(timespec="milliseconds"),
        "host": host,
        "project": project,
        "conversationId": cid,
        "candidates": n,
        "est_calls": stat["est_calls"],
        "status": "pending",
        # 存候选本身，确认时无需回到 sse jsonl 里按行号找（行号会随并发写入漂移）
        "messages": [{"role": r, "text": t, "id": i, "createTime": c}
                     for (r, t, i, c) in cands],
    }
    try:
        os.makedirs(SSE_DIR, exist_ok=True)
        with open(REFLOW_PENDING_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        _log("SSE_REFLOW_PENDING_WRITE_ERROR :: %s: %s" % (type(e).__name__, e))

    stat["status"] = "pending_confirm"
    stat["pending_id"] = pid
    _log("SSE_REFLOW_PENDING pending_id=%s host=%s project=%s cid=%s candidates=%d est_calls=%d "
         "-> 超自动阈值 %d，需确认后放行（POST /sse_reflow_confirm）"
         % (pid, host, project, cid, n, stat["est_calls"], SSE_REFLOW_AUTO_MAX))
    return stat


def _sse_reflow_list_pending(only_pending=True):
    """列出待确认记录。"""
    out = []
    if not os.path.exists(REFLOW_PENDING_PATH):
        return out
    try:
        with open(REFLOW_PENDING_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if only_pending and r.get("status") != "pending":
                    continue
                out.append({k: v for k, v in r.items() if k != "messages"})
    except Exception as e:
        _log("SSE_REFLOW_PENDING_READ_ERROR :: %s: %s" % (type(e).__name__, e))
    return out


def _sse_reflow_confirm(pending_id=None, confirm_all=False):
    """放行挂起的回流。返回 (已放行的记录数, 入队条数, 明细list)。"""
    if not os.path.exists(REFLOW_PENDING_PATH):
        return 0, 0, []
    try:
        with open(REFLOW_PENDING_PATH, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except Exception as e:
        _log("SSE_REFLOW_PENDING_READ_ERROR :: %s: %s" % (type(e).__name__, e))
        return 0, 0, []

    recs = []
    for l in lines:
        try:
            recs.append(json.loads(l))
        except Exception:
            continue

    done, enq, detail = 0, 0, []
    for i, r in enumerate(recs):
        if r.get("status") != "pending":
            continue
        if not confirm_all and pending_id and r.get("pending_id") != pending_id:
            continue
        stat = {"enqueued": 0, "skipped_roles": 0, "skipped_too_many": 0}
        cands = [(m.get("role"), m.get("text"), m.get("id"), m.get("createTime"))
                 for m in (r.get("messages") or [])]
        _sse_reflow_put(cands, r.get("host"), r.get("project"), stat)
        r["status"] = "confirmed"
        r["confirmed_at"] = datetime.now().isoformat(timespec="milliseconds")
        r["enqueued"] = stat["enqueued"]
        r["skipped_too_many"] = stat["skipped_too_many"]
        recs[i] = r
        done += 1
        enq += stat["enqueued"]
        detail.append({"pending_id": r.get("pending_id"), "cid": r.get("conversationId"),
                       "enqueued": stat["enqueued"],
                       "skipped_too_many": stat["skipped_too_many"]})
        _log("SSE_REFLOW_CONFIRM pending_id=%s cid=%s enqueued=%d skipped_too_many=%d"
             % (r.get("pending_id"), r.get("conversationId"),
                stat["enqueued"], stat["skipped_too_many"]))

    if done:
        try:
            with open(REFLOW_PENDING_PATH, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception as e:
            _log("SSE_REFLOW_PENDING_WRITE_ERROR :: %s: %s" % (type(e).__name__, e))
    return done, enq, detail


def _sse_reflow_worker():
    """后台单线程消费回流队列：一条一次 process_turn（= 一次 LLM 判定）。

    连续失败熔断：SSE_REFLOW_FAIL_LIMIT 次连续失败（典型=HTTP 402 欠费 / 网络错）后
    自动把 SSE_REFLOW_ENABLED 置 False 并清空队列——欠费复发时不能把剩下的全打一遍 402、
    更不能灌爆 received.log。成功一次即清零计数。
    """
    global SSE_REFLOW_ENABLED, _sse_reflow_fails
    while True:
        item = _sse_reflow_q.get()
        try:
            process_turn(item)
            _sse_reflow_fails = 0
        except SystemExit:
            raise
        except Exception as e:
            _sse_reflow_fails += 1
            _log("SSE_REFLOW_ERROR (%d/%d) :: %s: %s"
                 % (_sse_reflow_fails, SSE_REFLOW_FAIL_LIMIT, type(e).__name__, e))
            if SSE_REFLOW_FAIL_LIMIT and _sse_reflow_fails >= SSE_REFLOW_FAIL_LIMIT:
                SSE_REFLOW_ENABLED = False
                dropped = 0
                try:
                    while True:
                        _sse_reflow_q.get_nowait()
                        _sse_reflow_q.task_done()
                        dropped += 1
                except Exception:
                    pass
                _log("SSE_REFLOW_AUTO_DISABLED 连续 %d 次判定失败 -> 已自动关闭回流并丢弃队列 %d 条。"
                     "排查后如需恢复：确认 DeepSeek 余额，然后重启 server（或 BRIDGE_SSE_REFLOW=1）。"
                     % (_sse_reflow_fails, dropped))
                _sse_reflow_fails = 0
        finally:
            _sse_reflow_q.task_done()


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
                "reflow_enabled": SSE_REFLOW_ENABLED,
                "reflow_queue": _sse_reflow_q.qsize(),
                "reflow_pending": len(_sse_reflow_list_pending()),
                "turns": len(_received),
                "diagnoses": _diag_count,
                "recv_dir": RECV_DIR,
                "dedupe_keys": sum(len(s) for s in _seen_keys.values()),
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
        elif path == "/sse_reflow_pending":
            # 列出「超自动阈值、挂起待确认」的回流批次，供人工决定是否放行。
            items = _sse_reflow_list_pending()
            self._json(200, {
                "ok": True,
                "count": len(items),
                "auto_max": SSE_REFLOW_AUTO_MAX,
                "max_per_batch": SSE_REFLOW_MAX_PER_BATCH,
                "enabled": SSE_REFLOW_ENABLED,
                "items": items,
            })
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
        if _is_reserved_project(project):
            # 保留桶不对外暴露：当作「尚无数据」返回，避免误列历史
            self._json(200, dict({"ok": True, "state": None}, host=host, project=project))
            return
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
        elif path == "/sse":
            self._sse()
        elif path == "/sse_reflow_confirm":
            # 放行挂起的回流批次。body: {"pending_id": "xxx"} 或 {"all": true}
            body = self._read_json()
            if "__error__" in body:
                self._json(400, {"ok": False, "error": body["__error__"]})
                return
            confirm_all = bool(body.get("all"))
            pid = body.get("pending_id")
            if not confirm_all and not pid:
                self._json(400, {"ok": False,
                                 "error": "need pending_id or all=true"})
                return
            if not SSE_REFLOW_ENABLED:
                self._json(409, {"ok": False,
                                 "error": "reflow disabled (BRIDGE_SSE_REFLOW=0 或已被熔断自动关闭)"})
                return
            done, enq, detail = _sse_reflow_confirm(pending_id=pid, confirm_all=confirm_all)
            self._json(200, {"ok": True, "confirmed": done, "enqueued": enq, "detail": detail})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _sse(self):
        """旁挂入口：只落盘，不调 Runtime、不写 received/、不影响 /turn。

        按 kind 分支（#22 前提2a / 前提3）：
          - turn / history：维护 _seen_sse_msgids（history 按 message.id 补差，只落 SSE 没记录过的）
          - session_open：累加 _sse_health，连续 N 次无 turn 则告警（SSE 腿失效粗监控）
          - ws_flag：落 ws_flag_detected（每 host 一次）
        payload 外层结构：{host, project, kind, payload: <d.data>}
        """
        payload = self._read_json()
        if "__error__" in payload:
            self._json(400, {"ok": False, "error": payload["__error__"]})
            return

        kind = payload.get("kind")
        inner = payload.get("payload") or {}
        host = payload.get("host")
        project = payload.get("project")

        with _sse_lock:
            if kind == 'turn':
                cid = inner.get('conversationId')
                msgs = inner.get('messages') or []
                new_msgs = []
                if cid and msgs:
                    s = _seen_sse_msgids.setdefault(cid, set())
                    for m in msgs:
                        mid = m.get('id') if isinstance(m, dict) else None
                        if mid and mid in s:
                            continue          # 流式增量重复上报同一条，跳过
                        new_msgs.append(m)
                        if mid:
                            s.add(mid)
                elif msgs:
                    new_msgs = list(msgs)     # 无 cid：无法补差，原样回流（靠 process_turn 去重兜底）
                _sse_reflow_gate(new_msgs, host, project, cid=cid)
                # 实时 SSE turn 命中 -> 记下时间戳，作为「实时腿正在工作」的证据
                if host:
                    h = _sse_health.setdefault(host, {"last_turn_at": None, "turn_count": 0})
                    h["last_turn_at"] = datetime.now().timestamp()
                    h["turn_count"] += 1
            elif kind == 'history':
                cid = inner.get('conversationId')
                msgs = inner.get('messages') or []
                if cid and msgs:
                    seen = _seen_sse_msgids.setdefault(cid, set())
                    new_msgs = []
                    skipped = 0
                    for m in msgs:
                        mid = m.get('id') if isinstance(m, dict) else None
                        if mid and mid in seen:
                            skipped += 1
                            continue
                        new_msgs.append(m)
                        if mid:
                            seen.add(mid)
                    inner['messages'] = new_msgs
                    inner['added_count'] = len(new_msgs)
                    inner['skipped_count'] = skipped
                    # 回流：只喂补差后的新消息（已按 message.id 去重），避免重复判定。
                    # 走闸门：长历史会话（> SSE_REFLOW_AUTO_MAX）挂起待确认，不自动烧判定。
                    inner['reflow'] = _sse_reflow_gate(new_msgs, host, project, cid=cid)
                    # [新判据] 新增消息里若含"刚产生"的（createTime 距今 < 窗口），说明服务端
                    # 确实产出了新消息；此时若实时腿在同期窗口内没抓到任何 turn，才是真失效。
                    # 若补的只是旧历史（createTime 很老），说明用户只是首次进会话补数据，不算。
                    if new_msgs and host:
                        nowts = datetime.now().timestamp()
                        max_ct = None
                        for m in new_msgs:
                            ct = m.get('createTime') if isinstance(m, dict) else None
                            if isinstance(ct, (int, float)):
                                max_ct = ct if max_ct is None else max(max_ct, ct)
                        fresh = max_ct is not None and (nowts - max_ct) < SSE_FRESH_WINDOW_SEC
                        h = _sse_health.get(host) or {}
                        last_turn = h.get('last_turn_at')
                        sse_recent = last_turn is not None and (nowts - last_turn) < SSE_FRESH_WINDOW_SEC
                        if fresh and not sse_recent:
                            self._sse_alert(host, len(new_msgs), max_ct, last_turn)
            elif kind == 'session_open':
                # 仅登记 host 内存态，不再驱动告警。
                # 旧判据「连续 N 次打开会话无 turn」已废弃：只看历史未发言也会攒满计数误报
                # （2026-10-27 23:25 复测实证：3 次开会话均未发言 -> 误报一次）。
                if host:
                    _sse_health.setdefault(host, {"last_turn_at": None, "turn_count": 0})
            elif kind == 'ws_flag':
                if host and host not in _ws_flag_alerted:
                    _ws_flag_alerted.add(host)
                    kind = 'ws_flag_detected'  # 改写 kind 落地
                else:
                    # 已告警过：不重复落盘，避免刷
                    self._json(200, {"ok": True, "note": "ws_flag already alerted"})
                    return
            # 其他 kind（install/skip/error/history_raw 等）原样落盘

        # history 改写过 inner，需写回 payload 再落盘
        if kind == 'history':
            payload['payload'] = inner

        os.makedirs(SSE_DIR, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        fname = os.path.join(SSE_DIR, "sse_%s.jsonl" % day)
        rec = {
            "received_at": datetime.now().isoformat(timespec="milliseconds"),
            "host": host,
            "project": project,
            "kind": kind,
            "payload": payload,
        }
        line = json.dumps(rec, ensure_ascii=False)
        with _sse_lock:
            with open(fname, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        _log("SSE kind=%s host=%s project=%s bytes=%d -> %s"
             % (kind, host, project, len(line), os.path.basename(fname)))
        self._json(200, {"ok": True, "file": os.path.basename(fname)})

    def _sse_alert(self, host, added_count, max_ct=None, last_turn_at=None):
        """落 sse_zero_alert 告警（2026-10-27 新判据）。
        语义：history 补到了"刚产生"的新消息（服务端确有消息产出），但实时 SSE 腿在同期
        窗口内没抓到任何 turn —— 消息在产生、实时腿却没抓到 = 真实失效。
        补纯旧历史（createTime 老）不触发：那只是首次进会话补数据，不是腿坏了。
        注意：调用方 _sse 已持 _sse_lock，这里不再获取，直接落盘 + 日志。
        """
        os.makedirs(SSE_DIR, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        fname = os.path.join(SSE_DIR, "sse_%s.jsonl" % day)
        nowts = datetime.now().timestamp()
        newest_age = round(nowts - max_ct, 1) if max_ct else None
        turn_age = round(nowts - last_turn_at, 1) if last_turn_at else None
        rec = {
            "received_at": datetime.now().isoformat(timespec="milliseconds"),
            "host": host,
            "project": None,
            "kind": "sse_zero_alert",
            "payload": {
                "host": host,
                "reason": "fresh_messages_but_no_turn",
                "added_count": added_count,
                "window_sec": SSE_FRESH_WINDOW_SEC,
                "newest_msg_age_sec": newest_age,
                "last_turn_age_sec": turn_age,
                "message": ("history 补到 %d 条新消息，最新一条产生于 %s 秒前（新鲜窗口 %d 秒内），"
                            "但实时 SSE 腿在同期没抓到任何 turn（上次 turn 距今 %s）——"
                            "消息在产生而实时腿没抓到，判定实时腿真实失效"
                            "（检查是否切换 WebSocket 或扩展故障）"
                            % (added_count,
                               newest_age if newest_age is not None else "未知",
                               SSE_FRESH_WINDOW_SEC,
                               (str(turn_age) + " 秒") if turn_age is not None else "从未有过 turn"))
            }
        }
        line = json.dumps(rec, ensure_ascii=False)
        with open(fname, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _log("SSE_ZERO_ALERT host=%s reason=fresh_but_no_turn added=%d window=%ds"
             % (host, added_count, SSE_FRESH_WINDOW_SEC))

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
    if SSE_REFLOW_ENABLED:
        threading.Thread(target=_sse_reflow_worker, daemon=True,
                         name="sse-reflow").start()
        _stdout("sse->CA reflow worker started (enabled, skip_roles=%s)"
                % sorted(SSE_REFLOW_SKIP_ROLES))
    else:
        _stdout("sse->CA reflow worker DISABLED (BRIDGE_SSE_REFLOW=0)")
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
