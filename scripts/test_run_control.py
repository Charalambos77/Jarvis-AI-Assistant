"""Pause, resume and stop for pipeline runs, from the Control room.

What has to hold:
  - a paused run waits between agent steps and carries on from the same step
    when resumed, without starting a second run;
  - a stopped run ends at its next step with its progress saved, is marked
    stopped, and can be resumed later;
  - a run waiting at an approval gate stops at once, and the gate closes;
  - the real pipeline checks in before the Brain starts, so a stop lands there.

Runs against a throwaway database, with a stand-in pipeline and no model calls.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time

import jarvis
import coordinator
import db
import multi_agent_coordinator
import run_control

jarvis.speak = lambda text: None
run_control.POLL_SECONDS = 0.05

fd, TMP_DB = tempfile.mkstemp(suffix=".db")
os.close(fd)
jarvis.DB_PATH = TMP_DB
coordinator.DB_PATH = TMP_DB
multi_agent_coordinator.DB_PATH = TMP_DB

app = jarvis.app.test_client()
FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---- run_control on its own -------------------------------------------------
async def unit():
    run_control.register("u1")
    await run_control.checkpoint("u1", "step one")
    check("an unpaused run passes a checkpoint", run_control.get_state("u1")["at"] == "step one")

    run_control.pause("u1")
    events = []
    task = asyncio.ensure_future(run_control.checkpoint("u1", "step two", events.append))
    await asyncio.sleep(0.2)
    check("a paused run waits at its checkpoint", not task.done())
    check("the pause is announced once", len(events) == 1 and "Paused before step two" in events[0]["data"]["message"])
    run_control.resume("u1")
    await asyncio.wait_for(task, 1)
    check("resuming lets it carry on", task.done() and not task.exception())

    run_control.pause("u1")
    task = asyncio.ensure_future(run_control.checkpoint("u1", "step three"))
    await asyncio.sleep(0.1)
    run_control.stop("u1")
    try:
        await asyncio.wait_for(task, 1)
        stopped = False
    except run_control.PipelineStopped:
        stopped = True
    check("stopping a paused run ends it", stopped)
    check("resuming a stopping run is refused", "error" in run_control.resume("u1"))
    run_control.forget("u1")

    check("pausing a run that isn't running is refused", "error" in run_control.pause("nope"))
    await run_control.checkpoint(None, "no plan")
    await run_control.checkpoint("unknown", "not registered")
    check("runs with no plan id or no registration pass straight through", True)

asyncio.run(unit())


# ---- a stand-in pipeline driven through the endpoints -----------------------
STEPS = []
GATE_RESULTS = []


# Holds the stand-in at its start, so a test can pause it before any step
# without racing the thread.
HOLD = threading.Event()
HOLD.set()


async def fake_pipeline(task, gate_fn, event_logger=None, plan_id=None, **kw):
    while not HOLD.is_set():
        await asyncio.sleep(0.02)
    for label in ("research", "synthesis"):
        await run_control.checkpoint(plan_id, label, event_logger)
        STEPS.append((plan_id, label))
        await asyncio.sleep(0.05)
    if "gate" in task:
        GATE_RESULTS.append(await gate_fn("final_qa", {}))
    await run_control.checkpoint(plan_id, "deployment", event_logger)
    STEPS.append((plan_id, "deployment"))
    return {"status": "complete"}

multi_agent_coordinator.run_full_pipeline = fake_pipeline


def plan_status(pid):
    with jarvis.PLAN_STORE_LOCK:
        for p in jarvis.PLAN_STORE:
            if p["id"] == pid:
                return p.get("status")


def active(pid):
    with jarvis.ACTIVE_PIPELINE_LOCK:
        return pid in jarvis.ACTIVE_PIPELINE_THREADS


def run_of(pid):
    runs = app.get("/control/runs").get_json()["runs"]
    return next((r for r in runs if r["plan_id"] == pid), None)


# Pause straight away: the run must wait before its first step.
HOLD.clear()
pid = jarvis.initiate_pipeline("pause test", project_name="Zz Run Control")
r = app.post("/pipeline/pause", json={"plan_id": pid})
HOLD.set()
check("pause is accepted", r.status_code == 200 and r.get_json()["status"] == "paused")
wait_for(lambda: (run_of(pid) or {}).get("at") == "research")
run = run_of(pid)
check("the Control room lists the paused run and where it waits",
      run and run["state"] == "paused" and run["at"] == "research")
time.sleep(0.3)
check("no step ran while paused", not [s for s in STEPS if s[0] == pid])

r = app.post("/pipeline/resume", json={"plan_id": pid})
check("resume unpauses the live run instead of starting a new one",
      r.status_code == 200 and r.get_json()["status"] == "pipeline_unpaused")
check("the run finishes after resuming", wait_for(lambda: not active(pid)))
check("every step ran exactly once",
      [s[1] for s in STEPS if s[0] == pid] == ["research", "synthesis", "deployment"])
check("a finished run leaves the Control room", run_of(pid) is None)

# Stop between steps: the step in hand finishes, the next never starts.
pid = jarvis.initiate_pipeline("stop test", project_name="Zz Run Control")
wait_for(lambda: (pid, "research") in STEPS)
r = app.post("/pipeline/stop", json={"plan_id": pid})
check("stop is accepted", r.status_code == 200 and r.get_json()["status"] == "stopping")
check("the run ends", wait_for(lambda: not active(pid)))
check("it stopped before deployment", (pid, "deployment") not in STEPS)
check("the plan is marked stopped", plan_status(pid) == "stopped")
conn = db.get_connection(TMP_DB)
saved = next(p for p in db.get_pipelines(conn) if p["id"] == pid)
conn.close()
check("the stopped status is saved to the database", saved["status"] == "stopped")
run = run_of(pid)
check("the stopped run stays in the Control room to resume", run and run["state"] == "stopped")

r = app.post("/pipeline/resume", json={"plan_id": pid})
check("a stopped run resumes as a new run", r.get_json().get("status") == "pipeline_resumed")
check("the resumed run finishes", wait_for(lambda: (pid, "deployment") in STEPS and not active(pid)))

# Stop while waiting at a gate: the gate closes and the run ends.
pid = jarvis.initiate_pipeline("gate test", project_name="Zz Run Control")
wait_for(lambda: jarvis.get_gate_status_local(pid)["gate_status"] == "waiting")
check("the run reaches its gate", jarvis.get_gate_status_local(pid)["current_gate"] == "final_qa")
check("the Control room shows the gate", (run_of(pid) or {}).get("gate") == "final_qa")
app.post("/pipeline/stop", json={"plan_id": pid})
check("stopping at a gate ends the run", wait_for(lambda: not active(pid)))
check("the gate is closed", jarvis.get_gate_status_local(pid)["gate_status"] == "idle")
check("the gate never returned an answer", not GATE_RESULTS)
check("the run stopped before deployment", (pid, "deployment") not in STEPS)

r = app.post("/pipeline/pause", json={"plan_id": ""})
check("pause without a plan id is refused", r.status_code == 400)
r = app.post("/pipeline/stop", json={"plan_id": "9999"})
check("stopping a run that isn't running is refused", r.status_code == 400)

check("voice and chat can pause and stop too",
      {"pause_pipeline", "stop_pipeline"} <= {t["name"] for t in coordinator.TOOLS})


# ---- waits inside an agent step end when their run is stopped ---------------
import control_room
from agents import agent_questions, tool_review


async def stop_soon(pid):
    await asyncio.sleep(0.3)
    run_control.stop(pid)


async def held_call():
    run_control.register("held-1")
    run_control.bind("held-1")
    control_room.POLL_SECONDS = 0.05
    real_find = control_room.tool_catalog.find_by_fn_name
    control_room.tool_catalog.find_by_fn_name = lambda fn: (
        "shop", "buy", {"risk": "destructive", "status": "enabled", "rule": "always_ask"})
    try:
        stopper = asyncio.ensure_future(stop_soon("held-1"))
        result = await asyncio.wait_for(control_room.check_call("shop__buy", {}), 3)
        await stopper
    finally:
        control_room.tool_catalog.find_by_fn_name = real_find
    check("a call held in the Control room is denied when its run is stopped",
          isinstance(result, dict) and "stopped this pipeline" in json.dumps(result))
    check("and it leaves the Control room's waiting list",
          not [c for c in control_room.pending_calls() if c.get("tool") == "buy"])
    run_control.forget("held-1")

    run_control.register("held-2")
    run_control.bind("held-2")
    agent_questions.POLL_SECONDS = 0.05
    stopper = asyncio.ensure_future(stop_soon("held-2"))
    given = await asyncio.wait_for(agent_questions.ask("a1", "Role", "research", "brief", "Which one?", "why", []), 3)
    await stopper
    check("an agent's question stops waiting when its run is stopped", given.get("skipped") is True)
    run_control.forget("held-2")

    run_control.register("held-3")
    run_control.bind("held-3")
    tool_review.POLL_SECONDS = 0.05
    stopper = asyncio.ensure_future(stop_soon("held-3"))
    review = await asyncio.wait_for(tool_review.checkpoint("a1", "Role", "research", "brief", []), 3)
    await stopper
    check("a tool review stops the agent when its run is stopped", review["decision"] == "stop")
    run_control.forget("held-3")

    run_control.register("held-4")
    run_control.bind("held-4")
    run_control.pause("held-4")
    stopper = asyncio.ensure_future(stop_soon("held-4"))
    stopped_in_loop = await asyncio.wait_for(run_control.hold("next tool call"), 3)
    await stopper
    check("an agent's tool loop waits while paused and learns it was stopped", stopped_in_loop is True)
    run_control.forget("held-4")
    run_control.bind(None)

asyncio.run(held_call())


# ---- the real pipeline checks in before the Brain starts --------------------
import importlib
real = importlib.reload(multi_agent_coordinator)
real.DB_PATH = TMP_DB
brain_calls = []
real.build_agent_plan = lambda *a, **k: brain_calls.append(1) or {"error": "should not run"}
run_control.register("real-1")
run_control.stop("real-1")
try:
    asyncio.run(real.run_full_pipeline("t", None, plan_id="real-1", project_name="Zz Run Control"))
    stopped = False
except run_control.PipelineStopped:
    stopped = True
run_control.forget("real-1")
check("a stopped run never reaches the Brain", stopped and not brain_calls)

page = open("control_room.html", encoding="utf-8").read()
check("the Control room page polls running pipelines", "/control/runs" in page)
check("the Control room page has pause, resume and stop",
      all(f"runAction('{a}'" in page for a in ("pause", "resume", "stop")))

shutil.rmtree(os.path.join(jarvis.BASE_DIR, "Let Jarvis Handle It", "Zz Run Control"), ignore_errors=True)
try:
    os.remove(TMP_DB)
except OSError:
    pass

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed.")
    sys.exit(1)
print("\nAll checks passed.")
