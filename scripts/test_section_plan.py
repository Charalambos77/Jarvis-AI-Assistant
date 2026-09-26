"""Planning a whole section, against the Flask test client.

A section is where one pipeline grows into an entire operation, so the things
worth proving are the ones that stop it staying one pipeline: that Jarvis lists
every ask and researches the whole undertaking, that he says whether the
founding pipeline is the beginning or one part, that the plan has as many parts
and agents as it needs, and that no ask the owner made is ever left without an
agent — even when the model forgets one, or is down altogether.

Runs against a throwaway database and a throwaway project folder, with the
model and the web search stubbed.
"""
import json
import os
import shutil
import tempfile
import time

import jarvis
import coordinator
import db
import sections as section_store

BASE = jarvis.BASE_DIR
FOLDER = "Zz Test Section Plan"
PROJECT_DIR = os.path.join(BASE, "Let Jarvis Handle It", FOLDER)

jarvis.speak = lambda text: None
jarvis._section_summariser = lambda brief, material, previous: ""
jarvis.handle_request = lambda text: f"Noted: {text}"
jarvis.SECTION_PLAN_SYNC = True           # run the planning inline

fd, TMP_DB = tempfile.mkstemp(suffix=".db")
os.close(fd)
jarvis.DB_PATH = TMP_DB
coordinator.DB_PATH = TMP_DB

STARTED = []


def fake_initiate(task, project_name=None, brief_path=None, task_summary=None):
    STARTED.append({"task": task, "project_name": project_name, "brief_path": brief_path})
    conn = db.get_connection(TMP_DB)
    db.save_pipeline(conn, {
        "id": "88", "task": task, "project_name": project_name or "Default Project",
        "status": "running", "gate_status": "idle", "phase": "research",
        "timestamp": time.time(), "task_summary": task_summary,
    })
    conn.close()
    return "88"


jarvis.initiate_pipeline = fake_initiate

# ---- the model and the web, stubbed by what each call asks for ------------
CALLS = []
MODEL = {"down": False, "understanding": None, "plan": None, "audits": [], "needs": []}
SEARCHED = []


def fake_model(instruction, context_text, parts=None):
    CALLS.append({"instruction": instruction, "context": context_text})
    if MODEL["down"]:
        raise RuntimeError("model unreachable")
    if "SECTION UNDERSTANDING" in instruction:
        return MODEL["understanding"]
    if "SECTION PLAN" in instruction:
        return MODEL["plan"]
    if "SECTION REQUIREMENTS" in instruction:
        return MODEL["needs"].pop(0) if MODEL["needs"] else {"requirements": []}
    if "SECTION AUDIT" in instruction:
        return MODEL["audits"].pop(0) if MODEL["audits"] else {"missed_asks": [], "parts": []}
    if '"brief_text"' in instruction:
        return {"brief_text": "Turn the Limassol rental research into a rental company."}
    return {"questions": []}


def fake_search(folder, query):
    SEARCHED.append(query)
    return {"status": "ok", "summary": f"Findings about {query}",
            "sources": [{"n": 1, "title": "Cyprus registrar", "url": "https://example.org/reg"}]}


jarvis._ask_model_json = fake_model
jarvis._section_web_search = fake_search

app = jarvis.app.test_client()
FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def cleanup():
    coordinator.set_active_section(None)
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass


# ---- a finished competitive-research pipeline -----------------------------
shutil.rmtree(PROJECT_DIR, ignore_errors=True)
mem = os.path.join(PROJECT_DIR, "memory", "high_value")
os.makedirs(mem, exist_ok=True)
with open(os.path.join(mem, "competitor_analyst_cycle1.json"), "w", encoding="utf-8") as f:
    f.write('{"competitor_prices": "Three rivals in Limassol charge 35 to 60 euro a day."}')

AGENT_PLAN = {
    "task_summary": "Competitive research on Limassol car rental",
    "cycles": [{
        "cycle_id": 1, "domain": "Competitive Research", "goal": "Who the rivals are",
        "lead_specialist": {"agent_id": "competitor_analyst_cycle1", "role": "Competitor Analyst",
                            "brief": "Own who the rivals are and what they charge."},
        "advisory_agents": [],
    }],
}
plans_dir = os.path.join(PROJECT_DIR, "Implementation plan", "Agents")
os.makedirs(plans_dir, exist_ok=True)
with open(os.path.join(plans_dir, "agent_plan_1.md"), "w", encoding="utf-8") as f:
    f.write("# Agent Spawn Plan\n\n```json\n" + json.dumps(AGENT_PLAN) + "\n```\n")

conn = db.get_connection(TMP_DB)
for pid in ("1", "2"):
    db.save_pipeline(conn, {
        "id": pid, "task": "Competitive research on Limassol car rental", "project_name": FOLDER,
        "status": "complete", "gate_status": "idle", "phase": "complete",
        "timestamp": time.time(), "task_summary": "Competitive research on Limassol car rental",
    })
conn.close()

# What the model answers: four asks, research questions, and a plan that is
# bigger than the old caps (10 parts, one with 7 agents) but forgets ask_4.
MODEL["understanding"] = {
    "goal": "A running car rental company in Limassol.",
    "kind": "a new company",
    "asks": [
        {"text": "Start a car rental company in Limassol", "quote": "start a rental company"},
        {"text": "Budget under 50,000 euro", "quote": "under 50k"},
        {"text": "Online booking website", "quote": "people book online"},
        {"text": "Airport pickup service", "quote": "pick up at the airport"},
    ],
    "research_questions": ["How to register a company in Cyprus",
                           "Car rental insurance rules in Cyprus",
                           "What a car rental startup needs end to end"],
    "position": {"kind": "part", "why": "Competitive research is one part of building the company."},
}
BIG_PART_AGENTS = [{"role": f"Fleet Specialist {n}", "brief": f"Own fleet task {n}.",
                    "covers": ["ask_1"]} for n in range(7)]
MODEL["plan"] = {
    "position": {"kind": "part", "founding_part": "Competitive Research",
                 "why": "The research is one part of the company."},
    "parts": [
        {"title": "Company Setup", "goal": "Register the company", "order": 1, "status": "todo",
         "covers": ["ask_1"], "deliverables": ["Registered company"],
         "agents": [{"role": "Company Registrar", "brief": "Own the registration.",
                     "covers": ["ask_1", "need_1"], "is_lead": True}]},
        {"title": "Competitive Research", "goal": "Who the rivals are", "order": 2, "status": "done",
         "agents": [{"role": "Competitor Analyst", "brief": "Own who the rivals are.",
                     "from_agent_ids": ["competitor_analyst_cycle1"], "covers": ["ask_1"]},
                    {"role": "Invented Analyst", "brief": "Own something.",
                     "from_agent_ids": ["never_ran_cycle9"]}]},
        {"title": "Finance", "goal": "Stay in budget", "order": 3, "depends_on": ["Company Setup"],
         "agents": [{"role": "Budget Controller", "brief": "Own the budget.",
                     "covers": ["Budget under 50,000 euro"]}]},
        {"title": "Fleet", "goal": "The cars", "order": 4, "agents": BIG_PART_AGENTS},
        {"title": "Booking Website", "goal": "Online booking", "order": 5,
         "agents": [{"role": "Booking Site Builder", "brief": "Own the site.", "covers": ["ask_3"]}]},
    ] + [
        {"title": f"Extra Part {n}", "goal": f"Extra {n}", "order": 5 + n,
         "agents": [{"role": f"Extra Agent {n}", "brief": f"Own extra {n}.", "covers": ["ask_1"]}]}
        for n in range(1, 6)
    ],
    "services": [{"name": "Stripe", "why": "Take booking payments."}],
}
# The research turns up what the owner never said: a licence (planned), a
# repeat of an ask (ignored), a deep dive, and then commercial insurance
# (which the plan forgets, so it must get a part of its own).
MODEL["needs"] = [
    {"requirements": [{"text": "Rental car operator licence", "why": "Required by Cyprus law."},
                      {"text": "Budget under 50,000 euro"}],
     "deep_dive_questions": ["Cyprus rental licence process"]},
    {"requirements": [{"text": "Commercial vehicle insurance", "why": "Rental cars need it."}]},
]
# The first audit finds an ask the understanding missed and covers it; it
# still forgets the airport pickup, so that ask must get a part of its own.
MODEL["audits"] = [
    {"missed_asks": [{"text": "Insurance for every car", "quote": "insured"}],
     "parts": [{"title": "Finance", "agents": [
         {"role": "Insurance Buyer", "brief": "Own the fleet insurance.",
          "covers": ["Insurance for every car"]}]}]},
    {"missed_asks": [], "parts": []},
]

# ---- 1. the gate: brief, then research and plan -----------------------------
r = app.post("/sections/intake/start", json={
    "plan_id": "1", "name": "Limassol Rentals",
    "brief": "I want to start a rental company, under 50k, people book online, "
             "and we pick up at the airport. Everything insured."})
draft_id = r.get_json()["draft_id"]
app.post("/sections/intake/picture", json={"draft_id": draft_id})

r = app.post("/sections/intake/plan", json={"draft_id": draft_id})
view = r.get_json()
check("planning a section runs and finishes", view["state"] == "done")

understand = next(c for c in CALLS if "SECTION UNDERSTANDING" in c["instruction"])
check("Jarvis is told to list every ask", "EVERY distinct thing the owner asked for" in
      understand["instruction"])
check("he reads the owner's own words", "we pick up at the airport" in understand["context"])
check("he reads what the founding pipeline found", "35 to 60 euro" in understand["context"])
check("he researches what it takes to do it fully and correctly",
      "FULLY and CORRECTLY" in understand["instruction"])
check("aimed above all at what the owner did not mention",
      "what the owner did NOT" in understand["instruction"])
check("he decides whether it is the beginning or one part",
      '"beginning" or "part"' in understand["instruction"])

check("every research question is searched, then the deep dive",
      len(SEARCHED) == 4 and SEARCHED[-1] == "Cyprus rental licence process")
labels = [s["label"] for s in view["steps"]]
check("the progress names each research question",
      "Researching what it takes: How to register a company in Cyprus" in labels)
check("the progress shows the deep dive", "Deep dive: Cyprus rental licence process" in labels)
needs_calls = [c for c in CALLS if "SECTION REQUIREMENTS" in c["instruction"]]
check("requirements are worked out from the research, twice",
      len(needs_calls) == 2 and "Findings about How to register" in needs_calls[0]["context"])
check("the second pass has the deep dive",
      "Findings about Cyprus rental licence process" in needs_calls[1]["context"])
plan_call = next(c for c in CALLS if "SECTION PLAN" in c["instruction"])
check("the research reaches the planner",
      "Findings about Car rental insurance rules in Cyprus" in plan_call["context"])
check("the planner is told there is no limit on parts or agents",
      "There is no limit on parts or agents" in plan_call["instruction"])
check("the planner is given every ask by id", "ask_4: Airport pickup service" in plan_call["context"])
check("and every requirement the research found",
      "need_1: Rental car operator licence" in plan_call["context"]
      and "need_2: Commercial vehicle insurance" in plan_call["context"])

plan, crew = view["plan"], view["crew"]
check("the plan is not capped at 8 parts", len(plan["parts"]) >= 10)
fleet = next(d for d in crew["departments"] if d["domain"] == "Fleet")
check("a part can have more than 6 agents", len(fleet["agents"]) == 7)
check("the founding pipeline is placed as one part", plan["position"]["kind"] == "part")
founding = next(p for p in plan["parts"] if p["title"] == "Competitive Research")
check("the part it did is done", founding["status"] == "done")
check("and credited to the founding pipeline", "1" in founding["plan_ids"])
check("parts run in order", [p["order"] for p in plan["parts"]] == list(range(1, len(plan["parts"]) + 1)))
finance = next(p for p in plan["parts"] if p["title"] == "Finance")
check("dependencies are kept as part ids", finance["depends_on"] == ["dept_company_setup"])
invented = [a for d in crew["departments"] for a in d["agents"] if a["role"] == "Invented Analyst"]
check("an agent_id that never ran is not claimed", invented and not invented[0]["from_agent_ids"])
check("an ask cited by its wording counts as covered",
      "Budget Controller" in view["coverage"]["covered"]["ask_2"])

ask_texts = {a["text"]: a["id"] for a in plan["asks"]}
needs = [a for a in plan["asks"] if a["origin"] == "research"]
check("requirements are kept apart from the owner's asks",
      [a["text"] for a in needs] == ["Rental car operator licence", "Commercial vehicle insurance"])
check("a requirement that repeats an ask is not added twice",
      sum(1 for a in plan["asks"] if a["text"] == "Budget under 50,000 euro") == 1)
check("a requirement keeps why it is needed", needs[0]["why"] == "Required by Cyprus law.")
check("a planned requirement is covered", view["coverage"]["covered"]["need_1"] == ["Company Registrar"])
check("the audit adds the ask Jarvis first missed", "Insurance for every car" in ask_texts)
check("and an agent for it",
      "Insurance Buyer" in view["coverage"]["covered"][ask_texts["Insurance for every car"]])
check("no ask is left without an agent", view["coverage"]["uncovered"] == [])
check("the forgotten ask and requirement got parts of their own",
      view["gaps_closed"] == ["ask_4", "need_2"])
gap = next(p for p in plan["parts"] if "ask_4" in p["covers"])
check("that part says what it is for", gap["goal"] == "Airport pickup service")

check("nothing is written before Create section",
      not os.path.exists(os.path.join(PROJECT_DIR, section_store.PLAN_FILE)))

# The user drops the agent covering the website: the window must say so.
edited = json.loads(json.dumps(crew))
for d in edited["departments"]:
    d["agents"] = [a for a in d["agents"] if a["role"] != "Booking Site Builder"]
r = app.post("/sections/intake/crew/set", json={"draft_id": draft_id, "crew": edited})
check("dropping an agent shows the ask it leaves uncovered",
      r.get_json()["coverage"]["uncovered"] == ["ask_3"])
app.post("/sections/intake/crew/set", json={"draft_id": draft_id, "crew": crew})

# The owner chooses which research requirements to add. Leaving one out drops
# the agent that was there only for it, but never one that also covers an ask.
r = app.post("/sections/intake/crew/set", json={
    "draft_id": draft_id, "crew": crew, "skipped_needs": ["need_2", "need_1"]}).get_json()
roles_left = {a["role"] for d in r["crew"]["departments"] for a in d["agents"]}
check("an unticked requirement is not counted as missing",
      "need_2" not in r["coverage"]["uncovered"] and "need_2" not in r["coverage"]["covered"])
check("the agent there only for it is left out",
      not any("Commercial vehicle insurance" in role for role in roles_left))
check("an agent that also covers an ask stays", "Company Registrar" in roles_left)
check("the owner's own asks cannot be unticked",
      app.post("/sections/intake/crew/set", json={
          "draft_id": draft_id, "crew": crew, "skipped_needs": ["ask_1"]}
      ).get_json()["coverage"]["covered"]["ask_1"] != [])
app.post("/sections/intake/crew/set",
         json={"draft_id": draft_id, "crew": crew, "skipped_needs": ["need_1"]})

# ---- 2. Create section stands the plan up -----------------------------------
r = app.post("/sections/create", json={"draft_id": draft_id})
check("the section is created", r.status_code == 200)
SID = r.get_json()["section"]["id"]
check("the plan is written", os.path.exists(os.path.join(PROJECT_DIR, section_store.PLAN_FILE)))
knowledge = os.path.join(PROJECT_DIR, "Knowledge")
with open(os.path.join(knowledge, section_store.PLAN_NOTE), encoding="utf-8") as f:
    note = f.read()
check("a readable plan is written", "## Every ask and requirement, and who covers it" in note)
check("it says where the founding pipeline fits", "one part of this section" in note)
with open(os.path.join(knowledge, section_store.RESEARCH_NOTE), encoding="utf-8") as f:
    research_note = f.read()
check("the research is kept as knowledge", "https://example.org/reg" in research_note)

detail = app.get(f"/sections/{SID}").get_json()
check("the dashboard gets the plan", len(detail["plan"]["parts"]) == len(plan["parts"]))
check("the dashboard gets the coverage", detail["coverage"]["uncovered"] == [])
check("the dashboard knows which parts can start now", "dept_company_setup" in detail["next_parts"])
check("a part waiting on another cannot start yet", "dept_finance" not in detail["next_parts"])

plan_on_disk = section_store.read_plan(FOLDER)
check("the choice is kept when the section is created",
      [a["id"] for a in plan_on_disk["asks"] if a.get("skipped")] == ["need_1"])
check("an unticked requirement is left out of the readable plan",
      "Rental car operator licence" not in note)

# On the dashboard: leave commercial insurance out, then add it back.
r = app.post(f"/sections/{SID}/needs", json={"skipped": ["need_1", "need_2"]}).get_json()
check("a requirement can be left out later",
      all(p.get("gap") is not True or "need_2" not in p["covers"] for p in r["plan"]["parts"]))
r = app.post(f"/sections/{SID}/needs", json={"skipped": []}).get_json()
check("adding requirements back covers every one of them", r["coverage"]["uncovered"] == [])
check("one nobody covered gets a part of its own again", "need_2" in r["gaps_closed"])
check("one an agent still covered needs no new part", "need_1" not in r["gaps_closed"])

# ---- 3. a pipeline started for one part knows it is one part ---------------
r = app.post("/pipeline/intake/start", json={
    "task": "Register the company", "section_id": SID, "part_id": "dept_company_setup"})
pdraft = r.get_json()["draft_id"]
check("a pipeline draft can be for one part", r.get_json()["part_id"] == "dept_company_setup")
brief = jarvis._intake_brief_markdown(jarvis._get_intake_draft(pdraft))
check("its brief says which part it is", "THIS PIPELINE IS PART 1: Company Setup" in brief)
check("its brief lists the asks that part answers for",
      "- Start a car rental company in Limassol" in brief)
check("its brief shows the whole plan", "Competitive Research [done]" in brief)
r = app.post("/pipeline/intake/approve", json={"draft_id": pdraft})
check("approving starts it", r.status_code == 200)
plan_now = section_store.read_plan(FOLDER)
setup = next(p for p in plan_now["parts"] if p["id"] == "dept_company_setup")
check("the part is marked in progress", setup["status"] == "in_progress" and "88" in setup["plan_ids"])

conn = db.get_connection(TMP_DB)
conn.execute("UPDATE pipelines SET status = 'complete' WHERE id = '88'")
conn.commit()
conn.close()
detail = app.get(f"/sections/{SID}").get_json()
setup = next(p for p in detail["plan"]["parts"] if p["id"] == "dept_company_setup")
check("the part is done when its pipeline finishes", setup["status"] == "done")
check("and the part after it can start", "dept_finance" in detail["next_parts"])

# ---- 4. dashboard edits keep the plan in step -------------------------------
crew_now = detail["crew"]
crew_now["departments"] = [d for d in crew_now["departments"] if d["domain"] != "Booking Website"]
r = app.post(f"/sections/{SID}/crew", json={"crew": crew_now}).get_json()
check("dropping a part on the dashboard drops it from the plan",
      all(p["title"] != "Booking Website" for p in r["plan"]["parts"]))
check("and shows the ask it leaves uncovered", r["coverage"]["uncovered"] == ["ask_3"])

# ---- 5. re-planning keeps hand edits, and closes the gap again --------------
r = app.post(f"/sections/{SID}/plan").get_json()
check("re-planning an existing section runs", r["state"] == "done")
roles = {a["role"] for d in r["crew"]["departments"] for a in d["agents"]}
check("the standing agents are kept", "Competitor Analyst" in roles and "Company Registrar" in roles)
check("re-planning leaves no ask uncovered", r["coverage"]["uncovered"] == [])
check("the finished part stays done",
      next(p for p in r["plan"]["parts"] if p["id"] == "dept_company_setup")["status"] == "done")

# ---- 6. with the model down, every sentence still gets an agent -------------
MODEL["down"] = True
SEARCHED.clear()
conn = db.get_connection(TMP_DB)
conn.execute("DELETE FROM section_pipelines")
conn.commit()
conn.close()
r = app.post("/sections/intake/start", json={
    "plan_id": "2", "name": "Fallback",
    "brief": "Open a second office in Paphos. Hire three staff for it."})
fd_id = r.get_json()["draft_id"]
view = app.post("/sections/intake/plan", json={"draft_id": fd_id}).get_json()
check("planning still finishes with the model down", view["state"] == "done")
check("it says it is degraded", bool(view.get("degraded")))
texts = [a["text"] for a in view["plan"]["asks"]]
check("each sentence the owner wrote becomes an ask",
      "Open a second office in Paphos." in texts and "Hire three staff for it." in texts)
check("and every one of them has an agent", view["coverage"]["uncovered"] == [])
check("the founding work is kept, done",
      any(p["title"] == "Competitive Research" and p["status"] == "done"
          for p in view["plan"]["parts"]))
app.post("/sections/intake/cancel", json={"draft_id": fd_id})
MODEL["down"] = False

# ---- 7. a section made without questions is still planned ------------------
conn = db.get_connection(TMP_DB)
conn.execute("DELETE FROM sections")
conn.commit()
conn.close()
os.remove(os.path.join(PROJECT_DIR, section_store.PLAN_FILE))
MODEL["audits"] = []
jarvis.SECTION_PLAN_ON_CREATE = True
r = app.post("/sections/create", json={"plan_id": "2", "name": "Quick", "brief": "Grow this."})
check("creating without questions starts the planning", r.get_json()["planning"] is True)
SID2 = r.get_json()["section"]["id"]
detail = app.get(f"/sections/{SID2}").get_json()
check("and the section ends up with a whole plan", len(detail["plan"]["parts"]) >= 10)
check("with every ask covered", detail["coverage"]["uncovered"] == [])

cleanup()
print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for label in FAILED:
        print("  - " + label)
    raise SystemExit(1)
print("All checks passed.")
