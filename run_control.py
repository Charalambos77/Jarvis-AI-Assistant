"""Pause, resume and stop for running pipelines.

The pipeline calls `checkpoint()` between agent steps (before the Brain plans,
before each cycle's research, review, synthesis, execution, QA and deployment).
A paused run waits at its next checkpoint until it is resumed; a stopped run
raises `PipelineStopped` there, which ends the run cleanly with its progress
saved, so /pipeline/resume can pick it up later from where it stopped.

An agent step that is already running is never cut off: pause and stop take
effect when that step finishes. A run waiting at an approval gate stops at once.

State lives in memory, keyed by plan id: a run that is paused when the app
closes is simply not running after a restart, and resumes like any other.
"""
import asyncio
import threading

RUNNING = "running"
PAUSED = "paused"
STOPPING = "stopping"

POLL_SECONDS = 1.0

_LOCK = threading.Lock()
_STATES: dict[str, dict] = {}
_notify = None


class PipelineStopped(Exception):
    """Raised at a checkpoint when the user asked the run to stop."""


def set_notifier(fn):
    global _notify
    _notify = fn


def _say(text: str):
    if _notify:
        try:
            _notify(text)
        except Exception:
            pass


def _entry(plan_id: str) -> dict:
    """Must be called while holding _LOCK."""
    return _STATES.setdefault(str(plan_id), {"state": RUNNING, "at": None})


def register(plan_id: str):
    """A run is starting: it begins unpaused, whatever an earlier run left behind."""
    with _LOCK:
        _STATES[str(plan_id)] = {"state": RUNNING, "at": None}


def forget(plan_id: str):
    with _LOCK:
        _STATES.pop(str(plan_id), None)


def get_state(plan_id: str) -> dict:
    with _LOCK:
        entry = _STATES.get(str(plan_id))
        return dict(entry) if entry else {"state": None, "at": None}


def all_states() -> dict[str, dict]:
    with _LOCK:
        return {pid: dict(e) for pid, e in _STATES.items()}


def pause(plan_id: str) -> dict:
    with _LOCK:
        entry = _STATES.get(str(plan_id))
        if not entry:
            return {"error": f"Pipeline {plan_id} is not running"}
        if entry["state"] == STOPPING:
            return {"error": f"Pipeline {plan_id} is already stopping"}
        entry["state"] = PAUSED
    _say(f"Pipeline {plan_id} will pause after its current step.")
    return {"status": PAUSED, "plan_id": str(plan_id)}


def resume(plan_id: str) -> dict:
    with _LOCK:
        entry = _STATES.get(str(plan_id))
        if not entry:
            return {"error": f"Pipeline {plan_id} is not running"}
        if entry["state"] == STOPPING:
            return {"error": f"Pipeline {plan_id} is stopping"}
        entry["state"] = RUNNING
    _say(f"Pipeline {plan_id} resumed.")
    return {"status": RUNNING, "plan_id": str(plan_id)}


def stop(plan_id: str) -> dict:
    with _LOCK:
        entry = _STATES.get(str(plan_id))
        if not entry:
            return {"error": f"Pipeline {plan_id} is not running"}
        entry["state"] = STOPPING
    _say(f"Pipeline {plan_id} will stop after its current step.")
    return {"status": STOPPING, "plan_id": str(plan_id)}


def stop_requested(plan_id: str | None) -> bool:
    if not plan_id:
        return False
    with _LOCK:
        entry = _STATES.get(str(plan_id))
        return bool(entry and entry["state"] == STOPPING)


async def checkpoint(plan_id: str | None, label: str, event_logger=None):
    """Wait here while the run is paused; raise PipelineStopped if it was stopped.

    `label` names the step about to start, shown in the Control room as where the
    run is waiting. A run with no plan id, or one not registered here, passes
    straight through.
    """
    if not plan_id:
        return
    pid = str(plan_id)
    announced = False
    while True:
        with _LOCK:
            entry = _STATES.get(pid)
            if not entry:
                return
            entry["at"] = label
            state = entry["state"]
        if state == STOPPING:
            raise PipelineStopped(f"Stopped before {label}")
        if state == RUNNING:
            if announced and event_logger:
                event_logger({"event_type": "narrative", "data": {
                    "phase": "control", "message": f"Resumed: {label}.", "icon": "▶️"}})
            return
        if not announced:
            announced = True
            if event_logger:
                event_logger({"event_type": "narrative", "data": {
                    "phase": "control", "message": f"Paused before {label}.", "icon": "⏸️"}})
        await asyncio.sleep(POLL_SECONDS)
