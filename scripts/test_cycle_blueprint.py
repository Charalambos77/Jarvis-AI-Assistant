"""The research gate's "Open Cycle N blueprint" button, end to end on the API.

A research gate only carries its agents and the brief check. What it is waiting
on, the synthesized blueprint (the lead's final pick, the disagreements, the
sources), is served from the file the pipeline saved, scoped to one pipeline.
"""
import os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import jarvis
import multi_agent_coordinator

app = jarvis.app.test_client()
PROJECT = "__cycle_blueprint_test__"
PLAN, OTHER_PLAN, UNSAFE_PLAN = "__bp_1", "__bp_2", "bad..id"

BLUEPRINT = {
    "summary": "Five countries picked.",
    "selected_countries": [{"country": "Nigeria", "gdp_2023_usd_billion": 506.6}],
    "disagreements": [{"description": "GDP for Ireland", "resolution_note": "lead's figure used"}],
    "sources": ["web_search: countries by GDP"],
    # A finding may quote a code fence; the file's own closing fence still wins.
    "notes": "an agent pasted ``` into its findings",
}


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def get(pid, cycle_id):
    r = app.get(f"/api/plans/{pid}/cycles/{cycle_id}/blueprint")
    return r.status_code, r.get_json()


agents_dir = os.path.join(jarvis.BASE_DIR, "Let Jarvis Handle It", PROJECT, "Implementation plan", "Agents")
try:
    with jarvis.PLAN_STORE_LOCK:
        for pid in (PLAN, OTHER_PLAN, UNSAFE_PLAN):
            jarvis.PLAN_STORE.append({"id": pid, "task": "t", "project_name": PROJECT, "status": "running"})

    # Written exactly the way the pipeline writes them.
    multi_agent_coordinator.save_cycle_blueprint_file(PLAN, 1, BLUEPRINT, PROJECT)
    multi_agent_coordinator.save_cycle_blueprint_file(OTHER_PLAN, 2, {"summary": "another run"}, PROJECT)

    code, d = get(PLAN, 1)
    check("the saved blueprint comes back whole", code == 200 and d["blueprint"] == BLUEPRINT and d["cycle_id"] == 1)

    code, d = get(PLAN, 2)
    check("another pipeline's blueprint is never served for this one",
          code == 404 and "another run" not in str(d) and "no blueprint yet" in d["error"])

    with open(os.path.join(agents_dir, f"cycle_blueprint_3_{PLAN}.md"), "w", encoding="utf-8") as f:
        f.write("# Cycle 3 Blueprint\nnot json at all\n")
    code, d = get(PLAN, 3)
    check("an unreadable blueprint file says so instead of showing nothing", code == 500 and "could not be read" in d["error"])

    check("an unknown plan is a 404", get("__no_such_plan__", 1)[0] == 404)
    check("a plan id that is unsafe in a file name is refused", get(UNSAFE_PLAN, 1)[0] == 400)
    check("a cycle id must be a number", app.get(f"/api/plans/{PLAN}/cycles/one/blueprint").status_code == 404)
finally:
    with jarvis.PLAN_STORE_LOCK:
        jarvis.PLAN_STORE[:] = [p for p in jarvis.PLAN_STORE if p["id"] not in (PLAN, OTHER_PLAN, UNSAFE_PLAN)]
    shutil.rmtree(os.path.join(jarvis.BASE_DIR, "Let Jarvis Handle It", PROJECT), ignore_errors=True)

print("\nAll cycle blueprint checks passed.")
