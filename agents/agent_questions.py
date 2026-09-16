"""Agents asking the user a question, and waiting for the answer.

An agent could ask for a tool it lacked, but never for a fact only the user has.
Pipeline 9 is full of the result: "just starting to incorporate AI" was redefined
to fit a pre-picked list, Malaysia was counted as English-speaking, and a rejection
note that said "keep the selected countries in the logs" was read three ways.

Now any agent can call `ask_user`, as often as it needs. The question pops up
wherever the user is in Jarvis, their answer goes back into that agent's own run,
and nobody has to guess. Questions live in memory, like the command gate's list.
"""
import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timezone

ASK_USER_NAME = "ask_user"

DECLARATION = {
    "name": ASK_USER_NAME,
    "description": (
        "Ask the user a question and wait for their answer. Use it when something about what they "
        "want is genuinely unclear or ambiguous, when two of their instructions pull in different "
        "directions, or when a choice is theirs to make — never for something you could look up. "
        "Ask one question at a time, in plain words; you can ask as many as you need."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, in plain words the user will understand."},
            "why": {"type": "string", "description": "Why you need it: what you cannot decide without their answer."},
            "options": {
                "type": "array", "items": {"type": "string"},
                "description": "Optional. The answers you think are likely, so they can pick one instead of typing.",
            },
        },
        "required": ["question"],
    },
}

# Same patience as every other thing waiting on the user.
WAIT_SECONDS = int(os.getenv("JARVIS_QUESTION_WAIT", os.getenv("JARVIS_COMMAND_WAIT", "600")))
POLL_SECONDS = 1.0
_LOG_LIMIT = 200

_PENDING: dict[str, dict] = {}
_ANSWERED: dict[str, dict] = {}
_LOG: list[dict] = []
_LOCK = threading.Lock()
_notifier = None


def set_notifier(fn) -> None:
    """Called with a one-line message whenever an agent asks the user something."""
    global _notifier
    _notifier = fn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pending() -> list[dict]:
    """Every question waiting for an answer, oldest first."""
    with _LOCK:
        items = [dict(item) for item in _PENDING.values()]
    items.sort(key=lambda i: i["asked_ts"])
    now = time.time()
    for item in items:
        item["waited"] = int(now - item["asked_ts"])
        item["expires_in"] = max(0, int(item["deadline"] - now))
    return items


def log() -> list[dict]:
    """Questions already answered or given up on, newest first."""
    with _LOCK:
        return [dict(item) for item in reversed(_LOG)]


def answer(request_id: str, text: str = "", skipped: bool = False) -> dict:
    """Answer one waiting question, or skip it and let the agent decide for itself."""
    text = (text or "").strip()
    if not text and not skipped:
        return {"error": "Write an answer, or skip the question to let the agent decide."}
    with _LOCK:
        item = _PENDING.pop((request_id or "").strip(), None)
        if item is None:
            return {"error": "No agent is waiting on that question — it may already have been answered or timed out."}
        _ANSWERED[item["request_id"]] = {"answer": text, "skipped": bool(skipped)}
    return {"status": "ok", "request_id": item["request_id"], "agent_id": item["agent_id"]}


async def ask(agent_id: str, role: str, kind: str, brief: str, question: str, why: str,
              options: list[str], plan_hint: str = "", event_logger=None) -> dict:
    """Park one question until it is answered, skipped, or the wait runs out."""
    request_id = uuid.uuid4().hex[:12]
    event = {"event_type": "question_waiting", "source": agent_id,
             "data": {"request_id": request_id, "question": question, "why": why, "options": options}}
    if event_logger:
        event_logger(event)          # the pipeline's logger stamps its plan_id on the event

    now = time.time()
    item = {
        "request_id": request_id,
        "agent_id": agent_id,
        "role": role,
        "kind": kind,
        "plan_id": str(event.get("plan_id") or plan_hint or ""),
        "brief": (brief or "")[:400],
        "question": question,
        "why": (why or "")[:600],
        "options": [str(o) for o in (options or []) if str(o).strip()][:6],
        "asked_at": _now(),
        "asked_ts": now,
        "deadline": now + WAIT_SECONDS,
    }
    with _LOCK:
        _PENDING[request_id] = item
    if _notifier:
        try:
            where = f" in pipeline {item['plan_id']}" if item["plan_id"] else ""
            _notifier(f"{role or agent_id}{where} is asking: {question}")
        except Exception as e:
            print(f"[Question] Could not announce the question: {e}")

    while True:
        with _LOCK:
            given = _ANSWERED.pop(request_id, None)
            if given is None and time.time() >= item["deadline"]:
                _PENDING.pop(request_id, None)
                given = {"answer": "", "skipped": False, "timed_out": True}
        if given is not None:
            break
        await asyncio.sleep(POLL_SECONDS)

    record = {**item, **given, "answered_at": _now()}
    with _LOCK:
        _LOG.append(record)
        del _LOG[:-_LOG_LIMIT]
    if event_logger:
        event_logger({"event_type": "question_answered", "source": agent_id,
                      "data": {"request_id": request_id, "question": question,
                               "answer": given.get("answer", ""), "skipped": bool(given.get("skipped")),
                               "timed_out": bool(given.get("timed_out"))}})
    return given


async def handle(tool_args: dict, *, agent_config: dict, kind: str, event_logger=None) -> dict:
    """Answer an agent's ask_user call: what the agent is told to do next."""
    question = " ".join(str((tool_args or {}).get("question") or "").split())
    why = " ".join(str((tool_args or {}).get("why") or "").split())
    options = (tool_args or {}).get("options") or []
    if not question:
        return {"status": "error", "error": "Put your question in 'question', in plain words."}

    given = await ask(
        agent_config.get("agent_id", "unknown"), agent_config.get("role", ""), kind,
        agent_config.get("brief", ""), question, why, options, event_logger=event_logger,
    )

    if given.get("answer"):
        return {"status": "answered", "question": question, "answer": given["answer"],
                "message": "This is the user's own answer. Follow it exactly, and say in your findings "
                           "that you asked and what they said."}
    if given.get("skipped"):
        return {"status": "skipped", "question": question,
                "message": "The user chose not to answer and left it to you. Decide it yourself, and say "
                           "plainly in your findings which way you decided and that they did not answer."}
    return {"status": "no_answer", "question": question,
            "message": f"Nobody answered within {WAIT_SECONDS // 60} minutes. Carry on with the most "
                       "reasonable reading, and say plainly in your findings what you asked, that nobody "
                       "answered, and which assumption you made."}
