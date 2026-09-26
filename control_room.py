"""
The Control room — where tools that cost money or are destructive are decided.

Two kinds of thing wait here, and neither has a time limit: they stay until the
user answers, because spending money or deleting something by default is the
wrong way round.

  * A risky tool a connected service offers, waiting for its first decision:
      always ask   — enabled, and every call to it is held here first;
      always allow — enabled, calls run straight away;
      block        — never offered to an agent.
    The same three choices change a tool's rule later.

  * A call to a risky tool whose rule is "always ask". The agent that made it
    waits here — with asyncio.sleep, not a blocked thread, so the other agents
    in its cycle keep working — until the user allows it once or denies it.
    A denied call returns an error that says plainly the action did not happen.

Risk labels and rules live in the tool catalogue (connectors/tool_catalog.py);
held calls live in memory, like the command gate's pending list, so they end
with the run that made them.
"""
import asyncio
import json
import threading
import time
import uuid

from connectors import tool_catalog

POLL_SECONDS = 1.0
_LOG_LIMIT = 200
TOOL_DECISIONS = {
    "always_ask": {"status": "enabled", "rule": "always_ask"},
    "always_allow": {"status": "enabled", "rule": "always_allow"},
    "block": {"status": "disabled", "rule": "always_ask"},
}

_PENDING: dict[str, dict] = {}
_DECIDED: dict[str, dict] = {}
_LOG: list[dict] = []
_LOCK = threading.Lock()
_notifier = None


def set_notifier(fn) -> None:
    """Called with a one-line message whenever a call starts waiting here."""
    global _notifier
    _notifier = fn


# ---------------------------------------------------------------------------
# Risky tools
# ---------------------------------------------------------------------------

def risky_tools() -> list[dict]:
    """Every tool that costs money or is destructive, held ones first."""
    out = []
    for service, entry in tool_catalog.load().items():
        for name, tool in (entry.get("tools") or {}).items():
            if tool.get("risk") not in tool_catalog.RISKY:
                continue
            out.append({
                "service": service,
                "tool": name,
                "fn_name": tool.get("fn_name"),
                "risk": tool.get("risk"),
                "status": tool.get("status"),
                "rule": tool.get("rule", "always_ask"),
                "description": (tool.get("description") or "")[:400],
                "discovered_at": tool.get("discovered_at"),
            })
    out.sort(key=lambda t: (t["status"] != "held", t["service"], t["tool"]))
    return out


def held_tools() -> list[dict]:
    return [t for t in risky_tools() if t["status"] == "held"]


def decide_tool(service: str, tool: str, decision: str) -> dict:
    """Set a risky tool to always ask, always allow, or block."""
    decision = (decision or "").strip().lower()
    if decision not in TOOL_DECISIONS:
        return {"error": f"decision must be one of {', '.join(TOOL_DECISIONS)}."}
    current = ((tool_catalog.get(service) or {}).get("tools") or {}).get(tool)
    if not current:
        return {"error": f"{service} has no tool called {tool}."}
    if current.get("risk") not in tool_catalog.RISKY:
        return {"error": f"{tool} neither costs money nor is destructive, so it is decided on the Commands page."}
    fields = dict(TOOL_DECISIONS[decision], decided_at=tool_catalog.now_iso())
    tool_catalog.set_tool(service, tool, **fields)
    return {"status": "ok", "service": service, "tool": tool, "decision": decision}


# ---------------------------------------------------------------------------
# Held calls
# ---------------------------------------------------------------------------

def pending_calls() -> list[dict]:
    """Every call waiting for an answer, oldest first."""
    with _LOCK:
        items = [dict(item) for item in _PENDING.values()]
    items.sort(key=lambda i: i["asked_ts"])
    now = time.time()
    for item in items:
        item["waited"] = int(now - item["asked_ts"])
    return items


def call_log() -> list[dict]:
    """Calls already answered, newest first."""
    with _LOCK:
        return [dict(item) for item in reversed(_LOG)]


def decide_call(request_id: str, decision: str, reason: str = "") -> dict:
    """Allow one held call to run once, or deny it."""
    decision = (decision or "").strip().lower()
    if decision not in ("allow", "deny"):
        return {"error": "decision must be 'allow' or 'deny'."}
    with _LOCK:
        item = _PENDING.pop((request_id or "").strip(), None)
        if item is None:
            return {"error": "No call is waiting on that — it may already have been answered."}
        _DECIDED[item["request_id"]] = {"decision": decision, "reason": (reason or "").strip()}
    return {"status": "ok", "request_id": item["request_id"], "tool": item["tool"], "decision": decision}


def _denied(tool: str, why: str) -> dict:
    return {"status": "error", "action": tool,
            "error": f"{why} The action did NOT happen. Do not claim it did; say in your answer what you "
                     "could not do because of it."}


async def check_call(fn_name: str, args: dict, *, agent_id: str = "", role: str = "", kind: str = "",
                     event_logger=None) -> dict | None:
    """Hold a call to a risky tool until the user answers.

    Returns None when the call may run, or the error the agent should get
    instead. Tools that are not in the catalogue, or are neither costly nor
    destructive, pass straight through.
    """
    found = tool_catalog.find_by_fn_name(fn_name)
    if not found:
        return None
    service, tool, spec = found
    if spec.get("risk") not in tool_catalog.RISKY:
        return None
    if spec.get("status") != "enabled":
        return _denied(tool, f"{tool} is blocked in the Control room.")
    if spec.get("rule") == "always_allow":
        return None

    request_id = uuid.uuid4().hex[:12]
    event = {"event_type": "control_room_waiting", "source": agent_id,
             "data": {"request_id": request_id, "service": service, "tool": tool, "risk": spec.get("risk")}}
    if event_logger:
        event_logger(event)          # the pipeline's logger stamps its plan_id on the event

    item = {
        "request_id": request_id,
        "service": service,
        "tool": tool,
        "fn_name": fn_name,
        "risk": spec.get("risk"),
        "args": json.dumps(args or {}, default=str, ensure_ascii=False)[:2000],
        "agent_id": agent_id,
        "role": role,
        "kind": kind,
        "plan_id": str(event.get("plan_id") or ""),
        "asked_at": tool_catalog.now_iso(),
        "asked_ts": time.time(),
    }
    with _LOCK:
        _PENDING[request_id] = item
    if _notifier:
        try:
            what = "spend money" if spec.get("risk") == "costs_money" else "do something destructive"
            where = f" in pipeline {item['plan_id']}" if item["plan_id"] else ""
            _notifier(f"{role or agent_id or 'An agent'}{where} wants to call {tool} on {service}, which can "
                      f"{what}. It is waiting for you in the Control room.")
        except Exception as e:
            print(f"[Control room] Could not announce the call: {e}")

    while True:
        with _LOCK:
            decided = _DECIDED.pop(request_id, None)
        if decided is not None:
            break
        await asyncio.sleep(POLL_SECONDS)

    with _LOCK:
        _LOG.append({**item, **decided, "decided_at": tool_catalog.now_iso()})
        del _LOG[:-_LOG_LIMIT]
    if event_logger:
        event_logger({"event_type": "control_room_resolved", "source": agent_id,
                      "data": {"request_id": request_id, "tool": tool, "decision": decided["decision"]}})
    if decided["decision"] == "allow":
        return None
    said = f' They said: "{decided["reason"].rstrip(".")}".' if decided.get("reason") else ""
    return _denied(tool, f"The user denied this call to {tool} in the Control room.{said}")
