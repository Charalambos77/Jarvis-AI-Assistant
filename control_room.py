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

  * A call to a tool that costs money which would take its service, or its
    pipeline, over a spending limit. It is held even when the tool is on
    "always allow". Amounts are read from the call's own arguments (amount,
    price, cost, total…) in whatever unit the service charges; a money call
    with no readable amount counts as going over any limit that is set, so the
    user always sees it. Allowed calls add their amount to what was spent.

Risk labels and rules live in the tool catalogue (connectors/tool_catalog.py);
limits and spending in data/spending.json; held calls live in memory, like the
command gate's pending list, so they end with the run that made them.
"""
import asyncio
import json
import os
import threading
import time
import uuid

from connectors import tool_catalog
import run_control

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
        return {"error": f"{tool} neither costs money nor is destructive, so it is decided in the new-tool review."}
    fields = dict(TOOL_DECISIONS[decision], decided_at=tool_catalog.now_iso())
    tool_catalog.set_tool(service, tool, **fields)
    return {"status": "ok", "service": service, "tool": tool, "decision": decision}


# ---------------------------------------------------------------------------
# Spending limits
# ---------------------------------------------------------------------------

SPENDING_PATH = os.path.join(tool_catalog.BASE_DIR, "data", "spending.json")
# Argument names a paid call puts its amount under, most specific first.
AMOUNT_KEYS = ("amount", "total", "total_amount", "price", "cost", "value", "amount_usd", "price_usd",
               "cost_usd", "credits", "max_price", "budget", "quantity_price")
_SPEND_LOCK = threading.RLock()


def _load_spending() -> dict:
    with _SPEND_LOCK:
        try:
            with open(SPENDING_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except Exception as e:
            print(f"[Control room] Could not read {SPENDING_PATH}: {e}")
            data = {}
        data.setdefault("limits", {}).setdefault("service", {})
        data["limits"].setdefault("pipeline", None)
        data.setdefault("spent", {}).setdefault("service", {})
        data["spent"].setdefault("pipeline", {})
        return data


def _save_spending(data: dict) -> None:
    with _SPEND_LOCK:
        os.makedirs(os.path.dirname(SPENDING_PATH), exist_ok=True)
        tmp = SPENDING_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, SPENDING_PATH)


def call_amount(args: dict) -> float | None:
    """The amount a paid call would spend, if its arguments say."""
    def _num(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = v.replace(",", "").strip().lstrip("$€£").strip()
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    lowered = {str(k).lower(): v for k, v in (args or {}).items()}
    for key in AMOUNT_KEYS:
        if key in lowered:
            value = _num(lowered[key])
            if value is not None:
                return abs(value)
    for value in lowered.values():               # one level down, e.g. {"order": {"amount": 5}}
        if isinstance(value, dict):
            found = call_amount(value)
            if found is not None:
                return found
    return None


def spending() -> dict:
    """Limits and what was spent, for the page."""
    data = _load_spending()
    money_services = sorted({t["service"] for t in risky_tools() if t["risk"] == "costs_money"}
                            | set(data["limits"]["service"]))
    return {
        "pipeline_limit": data["limits"]["pipeline"],
        "pipelines": data["spent"]["pipeline"],
        "services": [{"service": s, "limit": data["limits"]["service"].get(s),
                      "spent": round(data["spent"]["service"].get(s, 0.0), 4)} for s in money_services],
    }


def set_limit(scope: str, service: str = "", limit=None) -> dict:
    """Set or clear one limit. scope is "service" (with a service name) or "pipeline" (every pipeline)."""
    if scope not in ("service", "pipeline"):
        return {"error": "scope must be 'service' or 'pipeline'."}
    if scope == "service" and not service:
        return {"error": "Say which service the limit is for."}
    value = None
    if limit not in (None, ""):
        try:
            value = float(str(limit).replace(",", "").strip().lstrip("$€£"))
        except ValueError:
            return {"error": f"'{limit}' is not a number."}
        if value < 0:
            return {"error": "A limit can't be negative."}
    with _SPEND_LOCK:
        data = _load_spending()
        if scope == "pipeline":
            data["limits"]["pipeline"] = value
        elif value is None:
            data["limits"]["service"].pop(service, None)
        else:
            data["limits"]["service"][service] = value
        _save_spending(data)
    return {"status": "ok", "scope": scope, "service": service, "limit": value}


def over_limit(service: str, plan_id: str, amount: float | None) -> str | None:
    """Why this call would break a limit, or None if it wouldn't."""
    data = _load_spending()
    checks = [(data["limits"]["service"].get(service), data["spent"]["service"].get(service, 0.0),
               f"{service}'s spending limit")]
    if plan_id:
        checks.append((data["limits"]["pipeline"], data["spent"]["pipeline"].get(plan_id, 0.0),
                       f"the spending limit for pipeline {plan_id}"))
    for limit, spent, what in checks:
        if limit is None:
            continue
        if amount is None:
            return f"Jarvis can't tell how much this call costs, and {what} is {limit:g}."
        if spent + amount > limit:
            return f"This call ({amount:g}) would take {what} over: {spent:g} of {limit:g} is spent already."
    return None


def record_spend(service: str, plan_id: str, amount: float | None) -> None:
    if not amount:
        return
    with _SPEND_LOCK:
        data = _load_spending()
        data["spent"]["service"][service] = data["spent"]["service"].get(service, 0.0) + amount
        if plan_id:
            data["spent"]["pipeline"][plan_id] = data["spent"]["pipeline"].get(plan_id, 0.0) + amount
        _save_spending(data)


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

    money = spec.get("risk") == "costs_money"
    request_id = uuid.uuid4().hex[:12]
    event = {"event_type": "control_room_check", "source": agent_id,
             "data": {"request_id": request_id, "service": service, "tool": tool, "risk": spec.get("risk")}}
    if event_logger:
        event_logger(event)          # the pipeline's logger stamps its plan_id on the event
    plan_id = str(event.get("plan_id") or "")
    amount = call_amount(args) if money else None
    limit_reason = over_limit(service, plan_id, amount) if money else None

    if spec.get("rule") == "always_allow" and not limit_reason:
        if money:
            record_spend(service, plan_id, amount)
        return None

    if event_logger:
        event_logger({"event_type": "control_room_waiting", "source": agent_id,
                      "data": {"request_id": request_id, "service": service, "tool": tool,
                               "risk": spec.get("risk"), "limit": limit_reason}})

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
        "plan_id": plan_id,
        "amount": amount,
        "limit_reason": limit_reason or "",
        "asked_at": tool_catalog.now_iso(),
        "asked_ts": time.time(),
    }
    with _LOCK:
        _PENDING[request_id] = item
    if _notifier:
        try:
            what = "spend money" if money else "do something destructive"
            where = f" in pipeline {item['plan_id']}" if item["plan_id"] else ""
            why = f" {limit_reason}" if limit_reason else ""
            _notifier(f"{role or agent_id or 'An agent'}{where} wants to call {tool} on {service}, which can "
                      f"{what}.{why} It is waiting for you in the Control room.")
        except Exception as e:
            print(f"[Control room] Could not announce the call: {e}")

    while True:
        with _LOCK:
            decided = _DECIDED.pop(request_id, None)
            # A run stopped from here takes its held calls with it, unanswered.
            if decided is None and run_control.stop_requested(plan_id or None):
                _PENDING.pop(request_id, None)
                decided = {"decision": "deny", "reason": "The user stopped this pipeline."}
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
        if money:
            record_spend(service, plan_id, amount)
        return None
    said = f' They said: "{decided["reason"].rstrip(".")}".' if decided.get("reason") else ""
    return _denied(tool, f"The user denied this call to {tool} in the Control room.{said}")
