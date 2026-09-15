"""
Agents asking the user for a tool they don't have.

An agent used to be stuck with the tools the Brain guessed at planning time. When
those didn't fit it either made do or decided on its own that no tool could help
— pipeline 8's AI Industry Analyst answered "no tool calls are appropriate" and
wrote its niche definition from memory.

Now every agent can call `request_tool`. The request is parked on the Commands
page, beside the terminal commands Antigravity asks to run, and the user
approves or rejects it:

  * Jarvis has the tool (it just wasn't given to this agent) — approving adds it
    to the agent straight away, and the call returns that it can use it now;
  * Jarvis has no working connection for it — approving puts it on the
    pipeline's tool list so it can be connected at the API/MCP step, and the
    agent is told it can't use it in this run;
  * rejected, or nobody answers within WAIT_SECONDS — the agent carries on
    without it and must say what it couldn't do.

A request for something the agent already has is answered at once, without
bothering the user. Requests live in memory, like the command gate's pending list.
"""
import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timezone

REQUEST_TOOL_NAME = "request_tool"

DECLARATION = {
    "name": REQUEST_TOOL_NAME,
    "description": (
        "Ask the user for a tool you don't have, or a better one for this job. The user approves "
        "or rejects it on the Commands page and this call returns their answer. If it is approved "
        "and Jarvis has the tool, it is added to your tools straight away."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "The tool or service you need, e.g. 'inspect_website' or 'crunchbase'."},
            "why": {"type": "string", "description": "What you need it for, and what you can't do without it."},
        },
        "required": ["tool", "why"],
    },
}

# Same patience as a terminal command on the Commands page.
WAIT_SECONDS = int(os.getenv("JARVIS_COMMAND_WAIT", "600"))
POLL_SECONDS = 1.0
_LOG_LIMIT = 200

_PENDING: dict[str, dict] = {}
_DECIDED: dict[str, dict] = {}
_LOG: list[dict] = []
_LOCK = threading.Lock()
_notifier = None


def set_notifier(fn) -> None:
    """Called with a one-line message whenever an agent asks for a tool."""
    global _notifier
    _notifier = fn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending() -> list[dict]:
    """Every tool request waiting for an answer, oldest first."""
    with _LOCK:
        items = [dict(item) for item in _PENDING.values()]
    items.sort(key=lambda i: i["asked_ts"])
    now = time.time()
    for item in items:
        item["waited"] = int(now - item["asked_ts"])
        item["expires_in"] = max(0, int(item["deadline"] - now))
    return items


def log() -> list[dict]:
    """Answered requests, newest first."""
    with _LOCK:
        return [dict(item) for item in reversed(_LOG)]


def decide(request_id: str, decision: str, reason: str = "") -> dict:
    """Approve or reject one waiting request."""
    decision = (decision or "").strip().lower()
    if decision not in ("approve", "reject"):
        return {"error": "decision must be 'approve' or 'reject'."}
    with _LOCK:
        item = _PENDING.pop((request_id or "").strip(), None)
        if item is None:
            return {"error": "No agent is waiting on that request — it may already have been answered or timed out."}
        _DECIDED[item["request_id"]] = {
            "decision": "approved" if decision == "approve" else "rejected",
            "reason": (reason or "").strip(),
        }
    return {"status": "ok", "request_id": item["request_id"], "tool": item["tool"], "decision": decision}


def availability(tool: str, project_name: str, context: str, handlers: dict) -> dict:
    """Whether Jarvis could give this agent `tool` now, needs it set up, or has nothing for it."""
    from agents.tool_executor import get_tools_for_execution_agent, _resolve_tool_key

    declarations, new_handlers, unavailable = get_tools_for_execution_agent([tool], project_name, context=context)
    added = [d for d in declarations if d["name"] not in handlers and d["name"] != REQUEST_TOOL_NAME]
    if added:
        names = ", ".join(d["name"] for d in added)
        return {"state": "ready", "detail": f"Jarvis has it ({names}).", "declarations": added,
                "handlers": {d["name"]: new_handlers[d["name"]] for d in added if d["name"] in new_handlers}}
    if unavailable:
        detail = unavailable[0]
        state = "needs_setup" if ("not yet configured" in detail or "did not start" in detail) else "no_connector"
        return {"state": state, "detail": detail, "declarations": [], "handlers": {}}
    try:
        key = _resolve_tool_key(tool, allow_llm=False)
    except Exception:
        key = tool
    return {"state": "already", "detail": f"it maps to {key}, which you already have",
            "declarations": [], "handlers": {}}


async def ask(agent_id: str, role: str, kind: str, brief: str, tool: str, why: str,
              info: dict, event_logger=None) -> dict:
    """Park one request until it is answered or times out. Returns {"decision", "reason"}."""
    request_id = uuid.uuid4().hex[:12]
    event = {"event_type": "tool_request_waiting", "source": agent_id,
             "data": {"request_id": request_id, "tool": tool, "why": why, "availability": info["state"]}}
    if event_logger:
        event_logger(event)          # the pipeline's logger stamps its plan_id on the event

    now = time.time()
    item = {
        "request_id": request_id,
        "agent_id": agent_id,
        "role": role,
        "kind": kind,
        "plan_id": str(event.get("plan_id") or ""),
        "brief": (brief or "")[:400],
        "tool": tool,
        "why": (why or "")[:600],
        "availability": info["state"],
        "availability_detail": info["detail"],
        "asked_at": _now(),
        "asked_ts": now,
        "deadline": now + WAIT_SECONDS,
    }
    with _LOCK:
        _PENDING[request_id] = item
    if _notifier:
        try:
            where = f" in pipeline {item['plan_id']}" if item["plan_id"] else ""
            _notifier(f"{role or agent_id}{where} is asking for the tool '{tool}'. "
                      "Approve or reject it on the Commands page.")
        except Exception as e:
            print(f"[Tool request] Could not announce the request: {e}")

    while True:
        with _LOCK:
            decided = _DECIDED.pop(request_id, None)
            if decided is None and time.time() >= item["deadline"]:
                _PENDING.pop(request_id, None)
                decided = {"decision": "timeout", "reason": ""}
        if decided is not None:
            break
        await asyncio.sleep(POLL_SECONDS)

    record = {**item, "decision": decided["decision"], "reason": decided["reason"], "decided_at": _now()}
    with _LOCK:
        _LOG.append(record)
        del _LOG[:-_LOG_LIMIT]
    if event_logger:
        event_logger({"event_type": "tool_request_resolved", "source": agent_id,
                      "data": {"request_id": request_id, "tool": tool, "decision": decided["decision"]}})
    return decided


async def handle(tool_args: dict, *, agent_config: dict, kind: str, project_name: str,
                 declarations: list, handlers: dict, event_logger=None) -> tuple[dict, bool]:
    """Answer an agent's request_tool call. Returns (what the agent is told, whether its tools changed).

    An approved tool Jarvis has is added to `declarations` and `handlers` in place,
    so the caller only needs to send its next message with the updated tools.
    """
    tool = str((tool_args or {}).get("tool") or "").strip()
    why = str((tool_args or {}).get("why") or "").strip()
    if not tool:
        return {"status": "error", "error": "Say which tool you need in 'tool', and why in 'why'."}, False

    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "")
    brief = agent_config.get("brief", "")
    loop = asyncio.get_running_loop()
    # Resolving an unfamiliar name can ask the model, so keep it off the event loop.
    info = await loop.run_in_executor(
        None, lambda: availability(tool, project_name, f"{role}: {brief}"[:400], handlers))

    if info["state"] == "already":
        return {"status": "already_available",
                "message": f"No need to ask: '{tool}' {info['detail']}. Use the tools you have."}, False

    decided = await ask(agent_id, role, kind, brief, tool, why, info, event_logger)

    if decided["decision"] != "approved":
        if decided["decision"] == "timeout":
            said = f"Nobody answered within {WAIT_SECONDS // 60} minutes, so the request was turned down."
        else:
            said = "The user rejected the request."
            if decided["reason"]:
                said += f' They said: "{decided["reason"].rstrip(".")}".'
        return {"status": "rejected",
                "message": f"{said} Carry on without {tool}, and say plainly in your answer what you "
                           "could not do because of it."}, False

    if info["state"] == "ready":
        declarations.extend(info["declarations"])
        handlers.update(info["handlers"])
        names = ", ".join(d["name"] for d in info["declarations"])
        return {"status": "approved", "message": f"Approved. You can call {names} now."}, True

    needed = agent_config.setdefault("tools_needed", [])
    if tool not in needed:
        needed.append(tool)
    return {"status": "approved_not_connected",
            "message": f"The user approved {tool}, but Jarvis can't use it yet: {info['detail']}. It is now on "
                       "this pipeline's tool list so it can be connected at the API/MCP step. You cannot use "
                       "it in this run — carry on without it and say so in your answer."}, False
