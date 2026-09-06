"""End-to-end exercise of sections against the Flask test client.

A section is a lasting workspace grown out of one finished pipeline, so the
things worth proving are the ones that only show up over time: that a section's
knowledge survives into the next pipeline, that work done inside it stays inside
it, and that closing a section never destroys the work done in it.

Runs against a throwaway database and a throwaway project folder.
"""
import io
import os
import shutil
import sys
import tempfile
import time

import jarvis
import coordinator
import db
import sections as section_store

BASE = jarvis.BASE_DIR
FOLDER = "Zz Test Section Project"
PROJECT_DIR = os.path.join(BASE, "Let Jarvis Handle It", FOLDER)

# Never talk to a model, never launch agents, never speak.
jarvis.speak = lambda text: None
jarvis._section_summariser = lambda brief, material, previous: ""
STARTED = []


def fake_initiate(task, project_name=None, brief_path=None, task_summary=None):
    STARTED.append({"task": task, "project_name": project_name,
                    "brief_path": brief_path, "task_summary": task_summary})
    # The real one writes a pipelines row before returning, and the section's
    # own listing reads pipelines back out of that table — so a stub that skips
    # it would make the section look empty for the wrong reason.
    conn = db.get_connection(TMP_DB)
    db.save_pipeline(conn, {
        "id": "77", "task": task, "project_name": project_name or "Default Project",
        "status": "running", "gate_status": "idle", "phase": "research",
        "timestamp": time.time(), "task_summary": task_summary,
    })
    conn.close()
    return "77"


jarvis.initiate_pipeline = fake_initiate
jarvis.handle_request = lambda text: f"Noted: {text}"

# The section gate's three model calls, stubbed by what each one asks for. The
# contexts are kept so the tests can prove what Jarvis was actually shown.
MODEL_CONTEXTS = []
NEXT_QUESTIONS = []
# What the crew call returns, when a test wants the model's judgement rather
# than the mechanical crew the code falls back to.
NEXT_CREW = []


def fake_model(instruction, context_text, parts=None):
    MODEL_CONTEXTS.append({"instruction": instruction, "context": context_text})
    if "STANDING CREW" in instruction:
        if not NEXT_CREW:
            raise RuntimeError("no crew stubbed")
        return NEXT_CREW.pop(0)
    if "the cleaned" in instruction:
        return {"plan_text": "CLEANED: " + instruction.split("EDITED TEXT:\n")[-1]}
    if '"brief_text"' in instruction:
        if NEXT_QUESTIONS:
            return {"questions": NEXT_QUESTIONS.pop(0)}
        return {"brief_text": "A booking business built on the Limassol research."}
    return {"questions": NEXT_QUESTIONS.pop(0) if NEXT_QUESTIONS else []}


jarvis._ask_model_json = fake_model

# A throwaway database so the real second brain is never touched.
fd, TMP_DB = tempfile.mkstemp(suffix=".db")
os.close(fd)
jarvis.DB_PATH = TMP_DB
coordinator.DB_PATH = TMP_DB

app = jarvis.app.test_client()

FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def cleanup():
    coordinator.set_active_section(None)
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE, "Let Jarvis Handle It", "Zz Test Section Project Two"),
                  ignore_errors=True)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass


# ---- a finished pipeline to grow the section from --------------------------
shutil.rmtree(PROJECT_DIR, ignore_errors=True)
mem = os.path.join(PROJECT_DIR, "memory", "high_value")
os.makedirs(mem, exist_ok=True)
with open(os.path.join(mem, "market_analyst_cycle1.json"), "w", encoding="utf-8") as f:
    f.write('{"pricing_findings": "Rental prices in Limassol peak in August at 90 euro a day."}')

# The pipeline's real agent plan, exactly as save_agent_plan_file leaves it: a
# readable page ending in the whole plan as JSON. This is what a section's
# standing crew is built from, so the fixture has to be the real shape.
AGENT_PLAN = """{
  "task_summary": "Research the Cyprus car rental market",
  "cycles": [
    {
      "cycle_id": 1,
      "domain": "Market Pricing",
      "goal": "What renting costs in Limassol, and when",
      "lead_specialist": {
        "agent_id": "market_analyst_cycle1",
        "role": "Market Analyst",
        "brief": "Own what the Limassol rental market charges and when it peaks.",
        "tools_needed": ["google_search"],
        "memory_query": "rental pricing"
      },
      "advisory_agents": [
        {
          "agent_id": "seasonality_analyst_cycle1_adv_1",
          "role": "Seasonality Analyst",
          "brief": "Own how demand moves through the year.",
          "tools_needed": ["google_search"]
        },
        {
          "agent_id": "ghost_cycle1_adv_2",
          "role": "Ghost Analyst",
          "brief": ""
        }
      ]
    },
    {
      "cycle_id": 2,
      "domain": "Fleet Operations",
      "goal": "What keeping the cars on the road costs",
      "lead_specialist": {
        "agent_id": "fleet_cost_analyst_cycle2",
        "role": "Fleet Cost Analyst",
        "brief": "Own the cost of running and maintaining the fleet."
      },
      "advisory_agents": [
        {
          "agent_id": "market_analyst_cycle2_adv_1",
          "role": "Market Analyst",
          "brief": "Own what the Limassol rental market charges and when it peaks."
        }
      ]
    }
  ]
}"""
plans_dir = os.path.join(PROJECT_DIR, "Implementation plan", "Agents")
os.makedirs(plans_dir, exist_ok=True)
with open(os.path.join(plans_dir, "agent_plan_1.md"), "w", encoding="utf-8") as f:
    f.write("# Agent Spawn Plan - Plan ID: 1\n\n## Full JSON Payload\n```json\n"
            + AGENT_PLAN + "\n```\n")

conn = db.get_connection(TMP_DB)
db.save_pipeline(conn, {
    "id": "1", "task": "Research the Cyprus car rental market", "project_name": FOLDER,
    "status": "complete", "gate_status": "idle", "phase": "complete",
    "timestamp": time.time(), "task_summary": "Research the Cyprus car rental market",
})
conn.close()

# ---- 1. creating a section -------------------------------------------------
r = app.post("/sections/create", json={
    "plan_id": "1",
    "name": "Cyprus Car Rental",
    "brief": "Turning this research into an actual rental business.",
})
check("a finished pipeline can become a section", r.status_code == 200)
section = r.get_json()["section"]
SID = section["id"]
check("the section keeps the pipeline's folder", section["folder"] == FOLDER)
check("the founding pipeline is filed under it", section["pipeline_count"] == 1)

check("a pipeline cannot become two sections",
      app.post("/sections/create", json={"plan_id": "1", "brief": "again"}).status_code == 409)
check("an unknown pipeline is refused",
      app.post("/sections/create", json={"plan_id": "999"}).status_code == 404)
check("plan_id is required",
      app.post("/sections/create", json={"brief": "x"}).status_code == 400)

# ---- 2. the founding pipeline's memory became readable knowledge ------------
knowledge = os.path.join(PROJECT_DIR, "Knowledge")
check("a Knowledge folder was written", os.path.isdir(knowledge))
notes = [n for n in os.listdir(knowledge) if n.endswith(".md")]
check("the agent's JSON became a markdown note",
      any("pricing" in n.lower() for n in notes))
check("the living summary exists", section_store.SUMMARY_NAME in notes)
check("the section note exists", os.path.exists(os.path.join(PROJECT_DIR, "Section.md")))

with open(os.path.join(knowledge, "pricing findings.md"), encoding="utf-8") as f:
    note = f.read()
check("the note carries frontmatter", note.startswith("---"))
check("the note keeps what the agent found", "90 euro a day" in note)

# Harvesting twice must not duplicate the same paragraph.
section_store.harvest_pipeline_memory(FOLDER)
with open(os.path.join(knowledge, "pricing findings.md"), encoding="utf-8") as f:
    twice = f.read()
check("re-harvesting does not duplicate knowledge", twice.count("90 euro a day") == 1)

# ---- 3. the sidebar ---------------------------------------------------------
listing = app.get("/sections").get_json()["sections"]
check("the section appears in the sidebar", any(s["id"] == SID for s in listing))
check("the sidebar knows which pipelines are taken", "1" in listing[0]["plan_ids"])

# ---- 4. the dashboard -------------------------------------------------------
detail = app.get(f"/sections/{SID}").get_json()
check("the dashboard names the section", detail["section"]["name"] == "Cyprus Car Rental")
check("the dashboard lists the founding pipeline",
      len(detail["pipelines"]) == 1 and detail["pipelines"][0]["founding"])
check("the dashboard lists knowledge notes", len(detail["knowledge"]) >= 1)
check("an unknown section is refused", app.get("/sections/nope").status_code == 404)

# ---- 5. work done inside a section stays inside it --------------------------
conn = db.get_connection(TMP_DB)
db.add_task(conn, "Register the company", section_id=SID)
db.add_task(conn, "Buy milk")
db.add_note(conn, "Insurance quote is 400", tags="cost", section_id=SID)
db.add_note(conn, "Dentist on Tuesday", tags="cost")
conn.close()

brain_tasks = [t["content"] for t in app.get("/tasks").get_json()["tasks"]]
check("the brain's task list excludes section work", brain_tasks == ["Buy milk"])
sec_tasks = [t["content"] for t in app.get(f"/tasks?section_id={SID}").get_json()["tasks"]]
check("the section's task list is its own", sec_tasks == ["Register the company"])

brain_notes = [n["content"] for n in app.get("/notes").get_json()["notes"]]
check("the brain's notes exclude section notes", brain_notes == ["Dentist on Tuesday"])
found = [n["content"] for n in app.get(f"/notes?query=cost&section_id={SID}").get_json()["notes"]]
check("searching inside a section finds its notes", found == ["Insurance quote is 400"])

# ---- 6. focused, not sealed -------------------------------------------------
app.post(f"/sections/{SID}/enter")
check("entering a section focuses Jarvis on it",
      (coordinator.get_active_section() or {}).get("id") == SID)

scoped = coordinator._scope_to_section("add_task", {"content": "x"})
check("a task made inside a section belongs to it", scoped.get("section_id") == SID)
scoped = coordinator._scope_to_section("get_tasks", {})
check("reading tasks inside a section reads its own", scoped.get("section_id") == SID)
scoped = coordinator._scope_to_section("get_tasks", {"scope": "brain"})
check("Jarvis can still look at the wider brain when asked",
      "section_id" not in scoped and not scoped.get("include_sections"))
scoped = coordinator._scope_to_section("get_tasks", {"scope": "everything"})
check("and at everything at once", scoped.get("include_sections") is True)
check("scope never reaches the database layer", "scope" not in scoped)

snapshot = jarvis.get_snapshot_local()
check("the snapshot shows the section you are in",
      [t["content"] for t in snapshot["tasks"]] == ["Register the company"])
check("the snapshot says which section that is", snapshot["section"]["id"] == SID)

app.post("/sections/exit")
check("leaving a section releases the focus", coordinator.get_active_section() is None)
check("outside a section nothing is scoped",
      "section_id" not in coordinator._scope_to_section("add_task", {"content": "x"}))

# ---- 7. the section remembers its conversation ------------------------------
app.post(f"/sections/{SID}/chat", json={"text": "What did we learn about pricing?"})
r = app.post(f"/sections/{SID}/chat", json={"text": "And about insurance?"})
check("talking inside a section replies", "reply" in r.get_json())
messages = app.get(f"/sections/{SID}").get_json()["messages"]
check("the conversation persists to the section", len(messages) == 4)
check("it is stored in order", messages[0]["content"] == "What did we learn about pricing?")
check("both sides are kept", [m["role"] for m in messages] == ["user", "jarvis"] * 2)
check("an empty message is refused",
      app.post(f"/sections/{SID}/chat", json={"text": "  "}).status_code == 400)
app.post("/sections/exit")

# ---- 8. a new pipeline inside the section starts knowing it -----------------
r = app.post("/pipeline/intake/start",
             json={"task": "Build the booking website", "section_id": SID})
draft_id = r.get_json()["draft_id"]
check("a draft can belong to a section", r.get_json()["section_id"] == SID)

draft = jarvis._get_intake_draft(draft_id)
check("the new pipeline reuses the section's folder",
      jarvis._draft_project_name(draft) == FOLDER)

brief = jarvis._intake_brief_markdown(draft)
check("the brief carries the section's knowledge", "Section context" in brief)
check("the brief carries what the section is for",
      "Turning this research into an actual rental business." in brief)
check("the brief points at the section's own notes", "pricing findings.md" in brief)

context = jarvis._intake_context_text(draft)
check("the clarification gate is told not to re-ask what is known",
      "ALREADY" in context.upper() and "SECTION" in context.upper())

STARTED.clear()
r = app.post("/pipeline/intake/approve", json={"draft_id": draft_id, "details": "Keep it simple."})
check("approving inside a section starts the pipeline", r.status_code == 200)
check("the pipeline was told which folder it lives in",
      STARTED and STARTED[0]["project_name"] == FOLDER)
check("the pipeline is filed under the section",
      app.get(f"/sections/{SID}").get_json()["section"]["pipeline_count"] == 2)
check("the brief was written inside the section's folder",
      STARTED[0]["brief_path"].startswith(PROJECT_DIR))

# ---- 9. the summary is yours to edit ---------------------------------------
app.post(f"/sections/{SID}/update", json={"summary": "August is the peak. Price for it."})
check("an edited summary is kept verbatim",
      app.get(f"/sections/{SID}").get_json()["summary"] == "August is the peak. Price for it.")
app.post(f"/sections/{SID}/update", json={"brief": "Now a booking platform."})
check("the brief can be rewritten",
      app.get(f"/sections/{SID}").get_json()["section"]["brief"] == "Now a booking platform.")

# ---- 10. the clarification gate on the way in ------------------------------
# Making a section asks the same kind of questions making a pipeline does, and
# nothing may exist until "Create section" is pressed.
FOLDER2 = "Zz Test Section Project Two"
PROJECT_DIR2 = os.path.join(BASE, "Let Jarvis Handle It", FOLDER2)
shutil.rmtree(PROJECT_DIR2, ignore_errors=True)
mem2 = os.path.join(PROJECT_DIR2, "memory", "high_value")
os.makedirs(mem2, exist_ok=True)
with open(os.path.join(mem2, "hardware_analyst_cycle1.json"), "w", encoding="utf-8") as f:
    f.write('{"vram_sizing": "A 24GB card runs the q4 model comfortably."}')

conn = db.get_connection(TMP_DB)
db.save_pipeline(conn, {
    "id": "2", "task": "Research local model hardware", "project_name": FOLDER2,
    "status": "complete", "gate_status": "idle", "phase": "complete",
    "timestamp": time.time(), "task_summary": "Research local model hardware",
})
conn.close()

check("the gate refuses an unknown pipeline",
      app.post("/sections/intake/start", json={"plan_id": "999"}).status_code == 404)
check("the gate refuses a pipeline that is already a section",
      app.post("/sections/intake/start", json={"plan_id": "1"}).status_code == 409)

r = app.post("/sections/intake/start",
             json={"plan_id": "2", "name": "Local Brain", "brief": "Run models on my own box."})
check("a section draft opens", r.status_code == 200)
sdraft = r.get_json()["draft_id"]

# Files dropped in the window reach the folder before there is any section, so
# Jarvis can read them while asking — and cancelling has to take them away again.
r = app.post("/sections/intake/upload", data={
    "draft_id": sdraft,
    "files": (io.BytesIO(b"3090 vs 4090 notes"), "gpu notes.txt"),
}, content_type="multipart/form-data")
check("a dropped file is stored", r.get_json()["files"][0]["name"] == "gpu notes.txt")
dropped = os.path.join(PROJECT_DIR2, "Inputs", "gpu notes.txt")
check("it lands in the section's Inputs folder", os.path.exists(dropped))

MODEL_CONTEXTS.clear()
NEXT_QUESTIONS.append([{"question": "What will you run on it first?", "gist": "What first?"}])
r = app.post("/sections/intake/questions",
             json={"draft_id": sdraft, "brief": "Run models on my own box."})
questions = r.get_json()["questions"]
check("the gate asks before anything is created", len(questions) == 1)
shown = MODEL_CONTEXTS[0]["context"]
check("it is shown what the founding pipeline already found",
      "A 24GB card runs the q4 model comfortably." in shown)
check("it is told never to re-ask that", "Never ask the user about anything in here" in shown)
# It asked "what platform will host this workspace?" until it was told what a
# section already is — the app's own mechanics are not the user's business.
check("it is told what a section already is", "WHAT A SECTION ALREADY IS" in shown)
check("the app's mechanics are ruled out as questions",
      "hosting, platforms, storage, tooling, file formats" in shown)
asked = MODEL_CONTEXTS[0]["instruction"]
check("it is told to drop questions that change nothing",
      "if each plausible answer leads to the same work, drop it" in asked)
check("it is not given a number of questions to hit",
      "There is no limit on how many you ask" in asked)
check("it is shown what the user wrote", "Run models on my own box." in shown)
check("it is shown the dropped file", "gpu notes.txt" in shown)
check("nothing has been created yet", app.get("/sections").get_json()["sections"] and
      all(s["folder"] != FOLDER2 for s in app.get("/sections").get_json()["sections"]))

r = app.post("/sections/intake/answer", json={
    "draft_id": sdraft, "question": questions[0]["question"], "answer": "A coding assistant."})
check("answering the last question ends the round", r.get_json()["done"] is True)

r = app.post("/sections/intake/picture", json={"draft_id": sdraft})
check("Jarvis writes the section brief back",
      r.get_json()["brief_text"] == "A booking business built on the Limassol research.")
check("the answers reached the write-up",
      "A coding assistant." in MODEL_CONTEXTS[-1]["context"])

r = app.post("/sections/intake/edit",
             json={"draft_id": sdraft, "edited_text": "A local coding assistant, on my own box."})
check("the brief is mine to correct",
      "A local coding assistant, on my own box." in r.get_json()["brief_text"])

# Cancelling is the whole point of a gate: it has to leave nothing behind.
r = app.post("/sections/intake/start", json={"plan_id": "2", "name": "Throwaway"})
throwaway = r.get_json()["draft_id"]
app.post("/sections/intake/upload", data={
    "draft_id": throwaway,
    "files": (io.BytesIO(b"junk"), "throwaway.txt"),
}, content_type="multipart/form-data")
junk = os.path.join(PROJECT_DIR2, "Inputs", "throwaway.txt")
check("the abandoned draft's file was written", os.path.exists(junk))
app.post("/sections/intake/cancel", json={"draft_id": throwaway})
check("cancelling deletes what that draft dropped", not os.path.exists(junk))
check("cancelling leaves the other draft's file alone", os.path.exists(dropped))
check("cancelling created no section",
      all(s["folder"] != FOLDER2 for s in app.get("/sections").get_json()["sections"]))

# The last thing the gate does, once the brief is settled: propose the crew.
NEXT_CREW.append({"departments": [
    {"domain": "Model Selection", "goal": "Which models are candidates",
     "agents": [
         {"role": "Benchmark Analyst", "brief": "Own the standing comparison of candidates.",
          "is_lead": True, "tools_needed": ["google_search"],
          "from_agent_ids": ["benchmark_analyst_cycle1"], "why": "Ran in the founding pipeline."},
         {"role": "Quantization Expert", "brief": "Own the quantization floor per model.",
          "from_agent_ids": [], "why": "Nothing on disk covers this yet."}]},
    {"domain": "Nobody Home", "goal": "A label with no agents", "agents": []}]})
r = app.post("/sections/intake/crew", json={"draft_id": sdraft})
check("the gate proposes a crew before anything is created", r.status_code == 200)
proposed = r.get_json()["crew"]
check("a baby section with nobody in it never reaches the screen",
      [d["domain"] for d in proposed["departments"]] == ["Model Selection"])
check("the counts are what the footer shows",
      r.get_json()["counts"] == {"departments": 1, "agents": 2})
check("an agent_id no pipeline here ever ran is not honoured",
      all(a["origin"] == "brief" for a in proposed["departments"][0]["agents"]))
check("proposing a crew writes nothing",
      not os.path.exists(section_store.crew_path(FOLDER2)))

# The user cuts one, and that is what gets created.
proposed["departments"][0]["agents"] = [
    a for a in proposed["departments"][0]["agents"] if a["role"] != "Quantization Expert"]
r = app.post("/sections/intake/crew/set", json={"draft_id": sdraft, "crew": proposed})
check("their edit to the crew is kept on the draft",
      [a["role"] for a in r.get_json()["crew"]["departments"][0]["agents"]] == ["Benchmark Analyst"])
check("editing the crew still writes nothing",
      not os.path.exists(section_store.crew_path(FOLDER2)))

r = app.post("/sections/create", json={"draft_id": sdraft})
check("the draft creates the section", r.status_code == 200)
SID2 = r.get_json()["section"]["id"]
detail = app.get(f"/sections/{SID2}").get_json()
check("the section's brief is the one that was corrected",
      "A local coding assistant, on my own box." in detail["section"]["brief"])
check("the drops survive into the section", os.path.exists(dropped))
check("the crew the user approved is the one that was stood up",
      [a["role"] for d in detail["crew"]["departments"] for a in d["agents"]]
      == ["Benchmark Analyst"])

record = r.get_json()["brief_path"]
with open(record, encoding="utf-8") as f:
    written = f.read()
check("the clarification is recorded next to the pipeline's own brief",
      "What will you run on it first?" in written and "A coding assistant." in written)
check("what the user typed is kept as they typed it", "Run models on my own box." in written)

check("a spent draft cannot create a second section",
      app.post("/sections/create", json={"draft_id": sdraft}).status_code == 400)
app.post(f"/sections/{SID2}/delete")

# ---- 11. the standing crew -------------------------------------------------
# A section's crew is what the dashboard draws at rest and what the Brain plans
# from. The thing worth proving is that none of it is invented: every agent
# comes from one that really ran, and every claim about that is checked.

crew = section_store.read_crew(FOLDER)
domains = [d["domain"] for d in crew["departments"]]
check("creating a section stood up a crew", os.path.exists(section_store.crew_path(FOLDER)))
check("its baby sections are the pipeline's own cycles",
      domains == ["Market Pricing", "Fleet Operations"])

roles = [a["role"] for d in crew["departments"] for a in d["agents"]]
check("the agents are the ones that really ran",
      "Market Analyst" in roles and "Fleet Cost Analyst" in roles)
check("an agent with no brief is not an agent", "Ghost Analyst" not in roles)
check("a role that ran in two cycles is one standing agent",
      roles.count("Market Analyst") == 1)

analyst = next(a for d in crew["departments"] for a in d["agents"]
               if a["role"] == "Market Analyst")
check("it carries both of the agent_ids it ran as",
      sorted(analyst["from_agent_ids"]) == ["market_analyst_cycle1", "market_analyst_cycle2_adv_1"])
check("its evidence is what it actually recorded",
      "pricing findings" in analyst["evidence"])
check("every baby section has exactly one lead",
      all(sum(1 for a in d["agents"] if a["is_lead"]) == 1 for d in crew["departments"]))

check("the crew is readable in the folder as markdown",
      os.path.exists(os.path.join(knowledge, section_store.CREW_NOTE)))
detail = app.get(f"/sections/{SID}").get_json()
check("the dashboard is handed the crew",
      [d["domain"] for d in detail["crew"]["departments"]] == domains)

# The crew is only worth drawing if it is also what the next pipeline is planned
# from — otherwise the constellation is decoration.
section_row = jarvis.load_section(SID)
seed = section_store.crew_seed_text(section_row)
check("the crew is handed to a new pipeline", "Market Analyst" in seed)
check("with the rule that stops it re-inventing the cast",
      "reuse that agent's exact role name" in seed)
check("and it reached the brief the agents actually worked from",
      "The standing crew of section" in brief)

# A claim that an agent really ran is checked against the folder, because an
# invented agent wearing a Founding badge is the worst thing this could produce.
faked = section_store.verify_crew_provenance(FOLDER, section_store.normalise_crew({
    "departments": [{"domain": "Invented", "agents": [
        {"role": "Imaginary Strategist", "brief": "Sounds useful.",
         "origin": "founding", "from_agent_ids": ["never_existed_cycle9"]}]}]}))
invented = faked["departments"][0]["agents"][0]
check("an agent_id that never existed is thrown away", invented["from_agent_ids"] == [])
check("and the agent stops claiming it ran", invented["origin"] == "brief")

# Editing from the dashboard, and the edit surviving a re-read.
edited = {"departments": [d for d in crew["departments"] if d["domain"] == "Market Pricing"]}
edited["departments"][0]["agents"] = [
    a for a in edited["departments"][0]["agents"] if a["role"] != "Seasonality Analyst"]
r = app.post(f"/sections/{SID}/crew", json={"crew": edited})
check("the crew can be edited from the dashboard", r.status_code == 200)
saved = r.get_json()["crew"]
check("what was dropped is gone",
      [d["domain"] for d in saved["departments"]] == ["Market Pricing"]
      and [a["role"] for a in saved["departments"][0]["agents"]] == ["Market Analyst"])
check("and it is recorded as retired rather than merely absent",
      "seasonality analyst" in saved["retired_roles"]
      and "fleet operations" in saved["retired_domains"])

r = app.post(f"/sections/{SID}/refresh")
grown = r.get_json()["crew"]
check("re-reading the pipelines does not undo the edit",
      [d["domain"] for d in grown["departments"]] == ["Market Pricing"]
      and [a["role"] for a in grown["departments"][0]["agents"]] == ["Market Analyst"])

# A department a later pipeline introduces does get added, because the crew
# grows with the section.
LATER = dict(section_store.crew_from_agent_plans(FOLDER))
LATER["departments"] = [{"id": "d_new", "domain": "Insurance", "goal": "Cover",
                         "origin": "founding", "from_plan_ids": ["77"],
                         "agents": [{"role": "Insurance Analyst", "brief": "Own cover and excess.",
                                     "is_lead": True, "origin": "founding",
                                     "from_agent_ids": [], "from_plan_ids": ["77"],
                                     "evidence": [], "why": ""}]}]
after = section_store.merge_crew(grown, LATER)
check("a later pipeline can add a baby section",
      [d["domain"] for d in after["departments"]] == ["Market Pricing", "Insurance"])

# What the normaliser refuses, since it is the last thing between a bad crew and
# the constellation.
cleaned = section_store.normalise_crew({"departments": [
    {"domain": "Empty", "agents": []},
    {"domain": "Two Leads", "agents": [
        {"role": "First", "brief": "A job.", "is_lead": True},
        {"role": "Second", "brief": "Another job.", "is_lead": True},
        {"role": "First", "brief": "The same job again."},
        {"role": "No Brief", "brief": ""}]},
    {"domain": "two leads", "agents": [{"role": "Third", "brief": "A job."}]}]})
check("a baby section with nobody in it is dropped",
      [d["domain"] for d in cleaned["departments"]] == ["Two Leads"])
check("a duplicated role is dropped",
      [a["role"] for a in cleaned["departments"][0]["agents"]] == ["First", "Second"])
check("only one agent leads",
      [a["is_lead"] for a in cleaned["departments"][0]["agents"]] == [True, False])

# The gate's crew stage: proposed on the draft, and nothing written by it.
r = app.post("/sections/intake/start",
             json={"plan_id": "1", "name": "Never", "brief": "Never created."})
check("the gate refuses a pipeline that is already a section (crew stage too)",
      r.status_code == 409)

# ---- 12. closing a section never destroys the work -------------------------
check("deleting the section succeeds",
      app.post(f"/sections/{SID}/delete").status_code == 200)
check("the section is gone", app.get(f"/sections/{SID}").status_code == 404)
check("its folder is left alone", os.path.isdir(knowledge))
conn = db.get_connection(TMP_DB)
check("its pipelines are left alone", len(db.get_pipelines(conn)) >= 1)
check("its tasks return to the brain",
      "Register the company" in [t["content"] for t in db.get_tasks(conn)])
conn.close()

cleanup()
if FAILED:
    print(f"\n{len(FAILED)} check(s) failed.")
    sys.exit(1)
print("\nAll checks passed.")
