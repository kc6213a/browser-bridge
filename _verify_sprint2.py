"""Sprint 2 离线验收：喂凯文指定的两句话，看 nodes 里有没有真的长出 assumption 版本链。"""
import json
import os
import sys
import tempfile

HOME = "C:/Users/Kevin Chan/WorkBuddy/Conversation Agent"
sys.path.insert(0, HOME)

from runtime.agent_runtime import AgentRuntime, load_or_init_state  # noqa: E402
from runtime.event_store import EventStore  # noqa: E402
from runtime.llm_adapter import default_llm  # noqa: E402
from runtime.signal_detector import _extract_entity_value_pairs  # noqa: E402

tmp = tempfile.mkdtemp(prefix="bridge-sprint2-")
evlog = os.path.join(tmp, "event_log.jsonl")
stfile = os.path.join(tmp, "state.json")

msgs = ["假设预算是 200 万", "预算其实是 100 万"]

llm = default_llm()
state = load_or_init_state(stfile, conversation_id="sprint2-offline")
rt = AgentRuntime(store=EventStore(evlog), state=state, llm=llm)

recent = []
for i, m in enumerate(msgs, 1):
    print("=== turn %d :: %r" % (i, m))
    print("  pairs :", _extract_entity_value_pairs(m))
    res = rt.on_turn(message=m, turn=i, recent_turns=list(recent), llm=llm)
    print("  status: %s | records=%d effects=%d"
          % (res.status, len(res.records), len(res.effects)))
    for e in res.effects:
        print("  effect:", json.dumps(e, ensure_ascii=False))
    for d in res.decisions:
        print("  decide:", d.get("type"), d.get("judge_result"), d.get("confidence"),
              d.get("rule_id"), d.get("actions"))
    recent.append(m)

print()
print("=== state.nodes ===")
print(json.dumps(state["nodes"], ensure_ascii=False, indent=2))

node = next((n for n in state["nodes"] if n["id"] == "assumption:预算"), None)
print()
if node is None:
    print("[FAIL] nodes 里找不到 assumption:预算")
    sys.exit(1)
vs = node["versions"]
ok = len(vs) == 2 and vs[0]["value"] == "200 万" and vs[0]["status"] == "superseded" \
    and vs[1]["value"] == "100 万" and vs[1]["status"] == "active" \
    and node["status"] == "active" and node["health"] == "ok"
print("[%s] versions=%d  v1=%s(%s)  v2=%s(%s)  status=%s health=%s source_turns=%s"
      % ("PASS" if ok else "FAIL", len(vs), vs[0]["value"], vs[0]["status"],
         vs[1]["value"], vs[1]["status"], node["status"], node["health"], node["source_turns"]))
sys.exit(0 if ok else 1)
