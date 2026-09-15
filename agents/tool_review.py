"""
Human check-ins for agents that keep calling tools.

Agents used to be cut off after six tool calls. An agent halfway through a real
search simply lost everything it had found ("Exceeded max tool-call turns
without a final answer") — pipeline 8's Country Viability Researcher made six
good searches and handed back nothing.

There is no limit now. Instead, every REVIEW_EVERY rounds of tool calls (one model
reply asking for tools, however many at once) an agent pauses and
asks, on the Commands page alongside the other approvals, whether it should keep
going:

  * Continue — it gets another REVIEW_EVERY rounds before asking again;
  * Stop — it is told to stop using tools and give its final answer with what it
    has, so the work already done is kept;
  * nobody answers within WAIT_SECONDS — it is stopped the same way, so a
    forgotten pipeline finishes with what it found instead of waiting all night.

Whatever reason the user types goes to the agent with the answer. A paused agent
waits with asyncio.sleep, not a blocked thread, so the other agents in its cycle
keep working. Reviews live in memory, like the command gate's pending list.
"""
import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from google.genai import types

# Rounds of tool calls, the same unit as the old six-turn cap.
REVIEW_EVERY = max(1, int(os.getenv("JARVIS_TOOL_REVIEW_EVERY", "6")))
# Same patience as every other approval on the Commands page.
WAIT_SECONDS = int(os.getenv("JARVIS_COMMAND_WAIT", "600"))
POLL_SECONDS = 1.0
_LOG_LIMIT = 200

# Every stop message contains this, whatever led to the stop.
STOP_INSTRUCTION = (
    "Do not call any more tools. Give your final JSON answer now, using what you have found so "
    "far, and say plainly what you did not get to."
)

_PENDING: dict[str, dict] = {}
_DECIDED: dict[str, dict] = {}
_LOG: list[dict] = []
_LOCK = threading.Lock()
_notifier = None


def set_notifier(fn) -> None:
    """Called with a one-line message whenever an agent starts waiting for a review."""
    global _notifier
    _notifier = fn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _describe(call: dict) -> str:
    args = call.get("args") or {}
    for key in ("query", "url", "relative_path", "title", "path"):
        if args.get(key):
            return f"{call.get('tool', '')}: {str(args[key])[:160]}"
    return str(call.get("tool", ""))


def pending() -> list[dict]:
    """Every agent waiting for an answer, oldest first."""
    with _LOCK:
        items = [dict(item) for item in _PENDING.values()]
    items.sort(key=lambda i: i["asked_ts"])
    now = time.time()
    for item in items:
        item["waited"] = int(now - item["asked_ts"])
        item["expires_in"] = max(0, int(item["deadline"] - now))
    return items


def log() -> list[dict]:
    """Answered reviews, newest first."""
    with _LOCK:
        return [dict(item) for item in reversed(_LOG)]


def decide(request_id: str, decision: str, reason: str = "") -> dict:
    """Answer one waiting review: "continue" or "stop"."""
    decision = (decision or "").strip().lower()
    if decision not in ("continue", "stop"):
        return {"error": "decision must be 'continue' or 'stop'."}
    with _LOCK:
        item = _PENDING.pop((request_id or "").strip(), None)
        if item is None:
            return {"error": "No agent is waiting on that review — it may already have been answered or timed out."}
        _DECIDED[item["request_id"]] = {"decision": decision, "reason": (reason or "").strip()}
    return {"status": "ok", "request_id": item["request_id"], "agent_id": item["agent_id"], "decision": decision}


async def checkpoint(agent_id: str, role: str, kind: str, brief: str, calls: list[dict],
                     event_logger=None) -> dict:
    """Pause until the user answers or the wait runs out.

    Returns {"decision": "continue" | "stop" | "timeout", "reason": str}.
    """
    request_id = uuid.uuid4().hex[:12]
    event = {
        "event_type": "tool_review_waiting",
        "source": agent_id,
        "data": {"request_id": request_id, "role": role, "kind": kind, "tool_calls": len(calls)},
    }
    if event_logger:
        # The pipeline's logger stamps its plan_id onto the event it is given.
        event_logger(event)

    now = time.time()
    item = {
        "request_id": request_id,
        "agent_id": agent_id,
        "role": role,
        "kind": kind,
        "plan_id": str(event.get("plan_id") or ""),
        "brief": (brief or "")[:400],
        "tool_calls": len(calls),
        "recent_calls": [_describe(c) for c in calls[-5:]],
        "review_every": REVIEW_EVERY,
        "asked_at": _now(),
        "asked_ts": now,
        "deadline": now + WAIT_SECONDS,
    }
    with _LOCK:
        _PENDING[request_id] = item
    if _notifier:
        try:
            where = f" in pipeline {item['plan_id']}" if item["plan_id"] else ""
            _notifier(f"{role or agent_id}{where} has made {len(calls)} tool calls. "
                      "Continue or stop it on the Commands page.")
        except Exception as e:
            print(f"[Tool review] Could not announce the review: {e}")

    while True:
        with _LOCK:
            review = _DECIDED.pop(request_id, None)
            if review is None and time.time() >= item["deadline"]:
                _PENDING.pop(request_id, None)
                review = {"decision": "timeout", "reason": ""}
        if review is not None:
            break
        await asyncio.sleep(POLL_SECONDS)

    with _LOCK:
        _LOG.append({**item, **review, "decided_at": _now()})
        del _LOG[:-_LOG_LIMIT]
    if event_logger:
        event_logger({"event_type": "tool_review_resolved", "source": agent_id,
                      "data": {"request_id": request_id, "decision": review["decision"], "tool_calls": len(calls)}})
    return review


def stop_message(review: dict | None) -> str:
    """What an agent is told when it is stopped, by the user or by the wait running out."""
    review = review or {}
    if review.get("decision") == "timeout":
        return (f"Nobody answered the review of your progress within {WAIT_SECONDS // 60} minutes, so "
                f"you are stopped here. {STOP_INSTRUCTION}")
    said = f' They said: "{review["reason"].rstrip(".")}".' if review.get("reason") else ""
    return f"The user reviewed your progress and asked you to stop using tools.{said} {STOP_INSTRUCTION}"


def with_review_note(tool_response_parts: list, review: dict) -> list:
    """The next message after a Continue: the tool results, plus whatever the user said."""
    if not review.get("reason"):
        return tool_response_parts
    note = f'The user reviewed your progress and let you keep going. They said: "{review["reason"].rstrip(".")}".'
    return list(tool_response_parts) + [types.Part.from_text(text=note)]


async def finish_without_tools(loop, chat, tool_response_parts: list, review: dict | None = None) -> str | None:
    """After a stop, get the agent's final answer without running another tool.

    The tool results from its last calls go back with the stop message, so nothing
    it just found is lost. If it asks for tools anyway, those calls are refused and
    it is asked once more. Returns None if it still gives no answer.
    """
    message = list(tool_response_parts) + [types.Part.from_text(text=stop_message(review))]
    for _ in range(2):
        response = await loop.run_in_executor(None, lambda m=message: chat.send_message(m))
        if not response.function_calls:
            return response.text or ""
        message = [
            types.Part.from_function_response(
                name=fc.name,
                response={"status": "error", "error": "Not run: tool use was stopped. "
                                                      "Give your final JSON answer now."},
            )
            for fc in response.function_calls
        ]
    return None
