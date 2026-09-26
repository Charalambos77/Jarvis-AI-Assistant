"""The Suggestions page: the plan split into parts, other ways to do each, and swapping them in.

  * After research, the execution plan is split into named parts, and Jarvis writes
    the best way, the cheapest and the best-result way for each part and for the
    whole plan.
  * The owner can swap a suggestion in for one part or for the whole plan, undo it,
    or go back to Jarvis's plan, until the execution agents start.
  * The pipeline reads the choice back at the execution blueprint gate, so what the
    owner picked is what runs.

No model or network is used: the model's answer is a fixed stub.
"""
import json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agents import plan_suggestions as ps


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_suggestions_")
ps.BASE_DIR = tmp

ROSTER = [
    {"agent_id": "competitor_finder_exec_1", "role": "Competitor Finder", "brief": "List 20 competitors.",
     "tools_needed": ["google_search"], "output_spec": {"required_keys": ["competitors"]}},
    {"agent_id": "report_writer_exec_1", "role": "Report Writer", "brief": "Write the report.",
     "tools_needed": ["google_docs"], "output_spec": {"required_keys": ["report"]}},
    {"agent_id": "publisher_exec_1", "role": "Publisher", "brief": "Share the doc.",
     "tools_needed": [], "output_spec": {}},
]

ANSWER = {
    "parts": [
        {"name": "Find the competitors", "goal": "20 real competitors", "approach": "Web search",
         "agent_ids": ["competitor_finder_exec_1"], "estimate": {"cost": "free", "time": "10 min", "quality": "good"}},
        # The publisher is left out on purpose: it must still end up in a part.
        {"name": "Write the report", "goal": "A Google Doc", "approach": "One writer",
         "agent_ids": ["report_writer_exec_1", "made_up_exec_9"]},
    ],
    "current_plan": {"is": ["cheapest"], "note": "Free tools only.", "followed_owner_instructions": True,
                     "estimate": {"cost": "free", "time": "30 min", "quality": "good"}},
    "suggestions": {
        "plan": [
            {"kind": "best_result", "title": "Use a paid market database", "summary": "Deeper data.",
             "estimate": {"cost": "$49/month", "time": "20 min", "quality": "excellent"},
             "pros": ["Real revenue numbers"], "cons": ["Costs money"], "services": ["crunchbase_api"],
             "parts": [{"name": "Pull the market data", "agents": [
                 {"agent_id": "market_puller_exec_1", "role": "Market Puller", "brief": "Pull from Crunchbase.",
                  "tools_needed": ["crunchbase_api"]}]},
                       {"name": "Write it up", "agents": [
                 {"agent_id": "report_writer_exec_1", "role": "Report Writer", "brief": "Write from the data."}]}]},
            {"kind": "nonsense_kind", "title": "", "summary": "no title, dropped",
             "parts": [{"name": "x", "agents": [{"agent_id": "a_exec_1", "brief": "b"}]}]},
        ],
        "part 1": [
            {"kind": "best", "also": ["fastest"], "title": "Search plus review sites", "summary": "Also G2 and Capterra.",
             "agents": [{"agent_id": "competitor_finder_exec_1", "role": "Competitor Finder",
                         "brief": "Search Google, G2 and Capterra."},
                        {"role": "Review Reader", "brief": "Read the top reviews."}]},
            {"kind": "best_result", "title": "Hire an analyst agent per country", "summary": "Split by country.",
             "agents": [{"agent_id": "analyst_exec_1", "role": "Analyst", "brief": "Country A."}]},
            {"kind": "cheapest", "title": "Nothing to run", "agents": []},
        ],
        "Write the report": [
            {"kind": "cheapest", "title": "Plain markdown file", "summary": "No Google Docs.",
             "agents": [{"agent_id": "md_writer_exec_1", "role": "Markdown Writer", "brief": "Write report.md."}]},
        ],
        "part 99": [{"kind": "best", "title": "Part that doesn't exist",
                     "agents": [{"agent_id": "x_exec_1", "brief": "x"}]}],
    },
}

ps._ask_model = lambda prompt: ANSWER
events = []
state = ps.build("p1", "Proj", "Find my competitors, use Google Docs.", ROSTER, {"summary": "found things"},
                 event_logger=events.append)

# ---- 1. splitting the plan -------------------------------------------------------------
check("the file is written beside the master blueprint",
      os.path.exists(os.path.join(tmp, "Let Jarvis Handle It", "Proj", "Implementation plan", "Final Plans",
                                  "suggestions_p1.json")))
check("status is ready", state["status"] == "ready" and not state["locked"])
names = [p["name"] for p in state["parts"]]
check("parts keep the model's names and order, plus one for the forgotten agent",
      names == ["Find the competitors", "Write the report", "Everything else"])
check("every agent in the plan is in exactly one part, in the original order",
      [a["agent_id"] for a in ps.execution_agents(state)] == [a["agent_id"] for a in ROSTER])
check("an agent the model made up is not added", "made_up_exec_9" not in json.dumps(state["parts"]))
check("part ids are numbered", [p["part_id"] for p in state["parts"]] == ["part_1", "part_2", "part_3"])
check("the verdict on the current plan is kept",
      state["current_plan"]["kinds"] == ["cheapest"] and state["current_plan"]["followed_owner"])

# ---- 2. the suggestions ------------------------------------------------------------------
by_scope = {}
for s in state["suggestions"]:
    by_scope.setdefault(s["scope"], []).append(s)
check("a whole-plan suggestion is kept; one with no title is dropped", len(by_scope["plan"]) == 1)
check("'part 1' and a part named by its name both map to parts",
      len(by_scope["part_1"]) == 2 and len(by_scope["part_2"]) == 1)
check("a suggestion for a part that doesn't exist is dropped", "part_99" not in by_scope)
check("a suggestion with no agents is dropped",
      all(s["title"] != "Nothing to run" for s in state["suggestions"]))
best = by_scope["part_1"][0]
check("kinds are kept, with extras", best["kind"] == "best" and best["kinds"] == ["best", "fastest"])
ids = [a["agent_id"] for a in best["agents"]]
check("suggested agents never reuse an id the plan has", "competitor_finder_exec_1" not in ids and len(set(ids)) == 2)
check("an agent with no id gets one from its role", any(i.startswith("review_reader_exec_") for i in ids))
check("events say suggestions are ready",
      any(e.get("event_type") == "suggestions_ready" and e["data"]["suggestions"] == 4 for e in events))

# ---- 3. swapping one part in -----------------------------------------------------------
r = ps.apply("p1", "Proj", best["id"])
check("apply to a part works", r.get("ok"))
st = r["state"]
part1 = st["parts"][0]
check("the part now runs the suggested agents", [a["role"] for a in part1["agents"]] == ["Competitor Finder", "Review Reader"])
check("the part says it's the owner's choice", part1["source"] == f"suggestion:{best['id']}")
check("the other parts are untouched", [a["agent_id"] for a in st["parts"][1]["agents"]] == ["report_writer_exec_1"])
check("that suggestion is marked in the plan",
      next(s for s in st["suggestions"] if s["id"] == best["id"])["status"] == "applied")
other = by_scope["part_1"][1]
r = ps.apply("p1", "Proj", other["id"])
st = r["state"]
check("a second pick for the same part replaces the first",
      [a["role"] for a in st["parts"][0]["agents"]] == ["Analyst"]
      and next(s for s in st["suggestions"] if s["id"] == best["id"])["status"] == "open")
all_ids = [a["agent_id"] for a in ps.execution_agents(st)]
check("agent ids stay unique across the plan", len(all_ids) == len(set(all_ids)))

# ---- 4. undo and reset -------------------------------------------------------------------
r = ps.undo("p1", "Proj")
check("undo puts the first pick back", [a["role"] for a in r["state"]["parts"][0]["agents"]] == ["Competitor Finder", "Review Reader"])
r = ps.reset("p1", "Proj")
check("reset goes back to Jarvis's plan",
      [a["agent_id"] for a in ps.execution_agents(r["state"])] == [a["agent_id"] for a in ROSTER]
      and all(s["status"] == "open" for s in r["state"]["suggestions"]))
r = ps.undo("p1", "Proj")
check("a reset can be undone too", r["state"]["parts"][0]["source"].startswith("suggestion:"))

# ---- 5. swapping the whole plan in -------------------------------------------------------
whole = by_scope["plan"][0]
r = ps.apply("p1", "Proj", whole["id"])
st = r["state"]
check("the whole plan is replaced by the suggestion's parts",
      [p["name"] for p in st["parts"]] == ["Pull the market data", "Write it up"])
check("swapped-in parts get ids no old part suggestion points at",
      all(p["part_id"].startswith(whole["id"] + "_part_") for p in st["parts"])
      and not any(s["scope"] == p["part_id"] for s in st["suggestions"] for p in st["parts"]))
check("part suggestions no longer fit and say so",
      all(s["status"] == "outdated" for s in st["suggestions"] if s["scope"] != "plan"))
check("an outdated suggestion can't be applied", "error" in ps.apply("p1", "Proj", best["id"]))
check("an unknown suggestion is refused", "error" in ps.apply("p1", "Proj", "nope"))
r = ps.undo("p1", "Proj")
check("undoing the whole-plan swap brings the parts and their suggestions back",
      len(r["state"]["parts"]) == 3 and not any(s["status"] == "outdated" for s in r["state"]["suggestions"]))

# ---- 6. the pipeline reads the choice, then the plan is final -------------------------------
from multi_agent_coordinator import take_chosen_plan, write_plan_suggestions
import multi_agent_coordinator as mac
saved = []
mac.save_agent_plan_file = lambda plan_id, plan, project: saved.append(json.loads(json.dumps(plan)))
ps.apply("p1", "Proj", by_scope["part_2"][0]["id"])
agent_plan = {"execution_agents": [dict(a) for a in ROSTER]}
log = []
changed = take_chosen_plan("p1", "Proj", agent_plan, log.append, lock=False)
check("the pipeline picks up the swapped-in part", changed and
      [a["role"] for a in agent_plan["execution_agents"]][-2:] == ["Markdown Writer", "Publisher"])
check("the plan file is saved with the parts", saved and saved[-1]["plan_parts"][1]["name"] == "Write the report")
check("the owner is told which parts use their choice",
      any("Write the report" in (e.get("data") or {}).get("message", "") for e in log))
check("not locked before the gate is approved", not ps.load("p1", "Proj")["locked"])
check("nothing changes a second time", not take_chosen_plan("p1", "Proj", agent_plan, None, lock=False))
take_chosen_plan("p1", "Proj", agent_plan, None, lock=True)
check("approving the gate locks the plan", ps.load("p1", "Proj")["locked"])
check("a locked plan can't be changed",
      "error" in ps.apply("p1", "Proj", best["id"]) and "error" in ps.undo("p1", "Proj")
      and "error" in ps.reset("p1", "Proj"))
check("a pipeline with no suggestions is left alone", not take_chosen_plan("nope", "Proj", {"execution_agents": []}))

# ---- 7. the model failing never blocks the pipeline -----------------------------------------
def boom(prompt):
    raise RuntimeError("quota")
ps._ask_model = boom
st = write_plan_suggestions("p2", "Proj", "task", {"execution_agents": ROSTER}, {})
check("a failure still records the plan as one part",
      st["status"] == "failed" and "quota" in st["error"] and len(st["parts"]) == 1
      and len(ps.execution_agents(st)) == 3)
ps._ask_model = lambda prompt: ANSWER

# ---- 8. the pipeline wiring ------------------------------------------------------------------
src = open("multi_agent_coordinator.py", encoding="utf-8").read()
gate = src[src.index("# Phase 4b"):src.index("if exec_retry >= MAX_RETRIES")]
check("suggestions are written before the execution gate opens",
      gate.index("write_plan_suggestions") < gate.index('gate_id="execution_blueprint"'))
check("the choice is read back when the gate is answered",
      gate.index('gate_id="execution_blueprint"') < gate.index("take_chosen_plan") < gate.index('if gate2.get("approved")'))
check("a re-planned roster gets new suggestions", gate.count("write_plan_suggestions") == 2)
check("execution locks the plan on a resume too",
      "take_chosen_plan(plan_id, project_name, agent_plan, event_logger, lock=True)" in
      src[src.index("if not skip_execution:"):src.index("run_execution_phase(agent_plan")])
phase4 = src[src.index("# Phase 4: Plan the execution agents"):src.index("# Phase 5: Gate")]
check("a restart while the gate is open keeps the owner's plan instead of planning again",
      phase4.index("plan_suggestions.load") < phase4.index("take_chosen_plan") < phase4.index("plan_execution_agents(")
      and 'not kept.get("locked")' in phase4)
brain = open("agents/brain.py", encoding="utf-8").read()
check("the Brain is told to research the ways to do the work", "RESEARCH THE WAYS TO DO THE WORK" in brain)

# ---- 9. the routes and the page --------------------------------------------------------------
import jarvis
jarvis.plan_suggestions.BASE_DIR = tmp
app = jarvis.app.test_client()
ps._ask_model = lambda prompt: ANSWER
ps.build("p3", "Proj", "Find my competitors.", ROSTER, {"x": 1})
with jarvis.PLAN_STORE_LOCK:
    jarvis.PLAN_STORE.append({"id": "p3", "project_name": "Proj", "task_summary": "Competitor research",
                              "current_gate": "execution_blueprint", "phase": "execution",
                              "agent_plan": {"execution_agents": ROSTER}, "master_blueprint": {"x": 1},
                              "gate_data": {"execution_agents": ROSTER}})
with jarvis.PIPELINE_LOCK:
    g = jarvis._get_gate_state("p3")
    g["current_gate"] = "execution_blueprint"
    g["gate_data"] = {"execution_agents": ROSTER, "master_blueprint": {}}

r = app.get("/suggestions").get_json()
item = next(p for p in r["plans"] if p["plan_id"] == "p3")
check("the list shows the pipeline by its summary", item["task"] == "Competitor research" and item["gate_open"])
check("the nav count counts plans waiting with open ideas", r["waiting"] >= 1)
view = app.get("/suggestions/p3").get_json()
check("the page gets parts, suggestions and labels", len(view["parts"]) == 3 and view["kind_labels"]["cheapest"] == "Cheapest"
      and view["gate_open"] and not view["can_undo"])
check("the page isn't sent the undo history", "history" not in view and "original_parts" not in view)
check("an unknown pipeline is a 404", app.get("/suggestions/zzz").status_code == 404)
pick = next(s for s in view["suggestions"] if s["scope"] == "part_2")
r = app.post("/suggestions/p3/apply", json={"suggestion_id": pick["id"]})
check("apply through the page works", r.status_code == 200 and r.get_json()["can_undo"])
with jarvis.PIPELINE_LOCK:
    live = jarvis._get_gate_state("p3")["gate_data"]["execution_agents"]
check("the open gate shows the swapped plan straight away", any(a["role"] == "Markdown Writer" for a in live))
with jarvis.PLAN_STORE_LOCK:
    stored = next(p for p in jarvis.PLAN_STORE if p["id"] == "p3")
check("so does the plan page", any(a["role"] == "Markdown Writer" for a in stored["agent_plan"]["execution_agents"]))
r = app.post("/suggestions/p3/undo")
check("undo through the page works", r.status_code == 200 and not r.get_json()["can_undo"])
check("a bad apply says why", app.post("/suggestions/p3/apply", json={"suggestion_id": "x"}).status_code == 400)
r = app.post("/suggestions/p3/refresh")
check("asking again starts a new round", r.status_code == 200)
import time
for _ in range(50):
    if ps.load("p3", "Proj")["status"] == "ready":
        break
    time.sleep(0.05)
check("the new round finishes", ps.load("p3", "Proj")["status"] == "ready")
ps.lock("p3", "Proj")
check("asking again is refused once execution started", app.post("/suggestions/p3/refresh").status_code == 400)
check("the page is served", app.get("/suggestions.html").status_code == 200)

nav = open("nav_ui.js", encoding="utf-8").read()
check("every page's nav has Suggestions", 'key: "suggestions"' in nav and '"suggestions.html": "suggestions"' in nav)
page = open("suggestions.html", encoding="utf-8").read()
check("the page loads the shared nav", '<script src="nav_ui.js"></script>' in page)
check("the plan page's approval links to Suggestions", "suggestions.html?plan=" in open("plan.html", encoding="utf-8").read())

print("\nAll checks passed.")
