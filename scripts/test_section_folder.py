"""A section started from a folder (the IDE's "make a section from this folder").

The folder takes the founding pipeline's place and everything after that is the
same gate: brief, questions, research, plan, Create section. What is worth
proving is that Jarvis is shown the folder's real work, that the folder itself
is never written to, and that a folder can only become one section.

Runs against a throwaway database and throwaway folders, model and web stubbed.
"""
import io
import os
import shutil
import tempfile

import jarvis
import coordinator
import db
import sections as section_store

jarvis.speak = lambda text: None
jarvis._section_summariser = lambda brief, material, previous: ""
jarvis.handle_request = lambda text: f"Noted: {text}"
jarvis.SECTION_PLAN_SYNC = True
jarvis.SECTION_PLAN_ON_CREATE = False

fd, TMP_DB = tempfile.mkstemp(suffix=".db")
os.close(fd)
jarvis.DB_PATH = TMP_DB
coordinator.DB_PATH = TMP_DB

CALLS = []


def fake_model(instruction, context_text, parts=None):
    CALLS.append({"instruction": instruction, "context": context_text})
    if "SECTION UNDERSTANDING" in instruction:
        return {"goal": "Ship the booking app.", "kind": "a software product",
                "asks": [{"text": "Take payments online", "quote": "take payments"}],
                "research_questions": ["What a booking app needs to launch"],
                "position": {"kind": "beginning", "why": "The folder is the first version."}}
    if "SECTION REQUIREMENTS" in instruction:
        return {"requirements": [{"text": "Privacy policy", "why": "Personal data is stored."}]}
    if "SECTION PLAN" in instruction:
        return {"position": {"kind": "beginning", "founding_part": "Current App",
                             "why": "The folder is the first version."},
                "parts": [
                    {"title": "Current App", "goal": "What the folder already has", "order": 1,
                     "status": "done", "agents": [{"role": "App Maintainer",
                                                   "brief": "Own the existing code."}]},
                    {"title": "Payments", "goal": "Take payments", "order": 2,
                     "agents": [{"role": "Payments Engineer", "brief": "Own payments.",
                                 "covers": ["ask_1"]}]}]}
    if "SECTION AUDIT" in instruction:
        return {"missed_asks": [], "parts": []}
    if '"brief_text"' in instruction:
        return {"brief_text": "Turn this booking app into a launched product."}
    return {"questions": []}


jarvis._ask_model_json = fake_model
jarvis._section_web_search = lambda folder, q: {"status": "ok", "summary": f"About {q}",
                                                "sources": []}
app = jarvis.app.test_client()
FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


# ---- an IDE folder with real work in it -----------------------------------
WORK = tempfile.mkdtemp(prefix="Zz Booking App ")
with open(os.path.join(WORK, "README.md"), "w", encoding="utf-8") as f:
    f.write("# Booking app\nLets customers book rental cars online.\n")
with open(os.path.join(WORK, "app.py"), "w", encoding="utf-8") as f:
    f.write("def book(car):\n    return 'booked'\n")
os.makedirs(os.path.join(WORK, "node_modules", "junk"))
with open(os.path.join(WORK, "node_modules", "junk", "index.js"), "w") as f:
    f.write("INSTALLED DEPENDENCY")
before = sorted(os.path.join(r, n) for r, _, ns in os.walk(WORK) for n in ns)
MADE = []


def cleanup():
    coordinator.set_active_section(None)
    shutil.rmtree(WORK, ignore_errors=True)
    for name in MADE:
        shutil.rmtree(os.path.join(section_store.SECTIONS_ROOT, name), ignore_errors=True)
    try:
        os.remove(TMP_DB)
    except OSError:
        pass


# ---- 1. the refusals --------------------------------------------------------
check("a relative folder is refused",
      app.post("/sections/intake/start", json={"folder_path": "some/folder"}).status_code == 400)
check("a missing folder is refused",
      app.post("/sections/intake/start",
               json={"folder_path": os.path.join(WORK, "nope")}).status_code == 404)

# ---- 2. the same gate, with the folder as the founding work -----------------
r = app.post("/sections/intake/start", json={"folder_path": WORK, "brief": "Launch it."})
check("a folder opens a section draft", r.status_code == 200)
d = r.get_json()
draft_id = d["draft_id"]
MADE.append(d["folder"])
check("the section is named after the folder", d["name"] == os.path.basename(WORK))
check("its files go under Let Jarvis Handle It, not into the folder",
      not os.path.isabs(d["folder"]) and d["source_path"] == os.path.normpath(WORK))

app.post("/sections/intake/questions", json={"draft_id": draft_id, "brief": "Launch it."})
asked = CALLS[-1]["context"]
check("the questions are shown what the folder contains",
      "Lets customers book rental cars online." in asked and "def book(car)" in asked)
check("installed dependencies are left out", "INSTALLED DEPENDENCY" not in asked)
check("and told never to re-ask it", "WHAT THE FOLDER ALREADY CONTAINS" in asked)

app.post("/sections/intake/picture", json={"draft_id": draft_id})
view = app.post("/sections/intake/plan", json={"draft_id": draft_id}).get_json()
check("the folder goes through research and planning", view["state"] == "done")
understand = next(c for c in CALLS if "SECTION UNDERSTANDING" in c["instruction"])
check("the planner treats the folder as the founding work",
      "THE FOUNDING WORK" in understand["context"] and "def book(car)" in understand["context"])
check("it can be the beginning of the section", view["plan"]["position"]["kind"] == "beginning")
check("what the folder already does is a done part",
      any(p["title"] == "Current App" and p["status"] == "done" for p in view["plan"]["parts"]))
check("research requirements are found for it too",
      any(a["origin"] == "research" for a in view["plan"]["asks"]))
check("every ask is covered", view["coverage"]["uncovered"] == [])

# ---- 3. Create section ------------------------------------------------------
r = app.post("/sections/create", json={"draft_id": draft_id})
check("the section is created from the folder", r.status_code == 200)
section = r.get_json()["section"]
check("it remembers the folder it came from", section["source_path"] == os.path.normpath(WORK))
check("it has no founding pipeline", section["founding_plan_id"] is None)
SID = section["id"]
check("its plan is written in its own folder",
      os.path.exists(os.path.join(section_store.SECTIONS_ROOT, d["folder"], section_store.PLAN_FILE)))
after = sorted(os.path.join(r_, n) for r_, _, ns in os.walk(WORK) for n in ns)
check("the picked folder is never written to", before == after)
check("a folder can only become one section",
      app.post("/sections/intake/start", json={"folder_path": WORK}).status_code == 409)
check("including on the skip path",
      app.post("/sections/create", json={"folder_path": WORK}).status_code == 409)

r = app.post("/pipeline/intake/start", json={"task": "Add payments", "section_id": SID,
                                              "part_id": "dept_payments"})
brief = jarvis._intake_brief_markdown(jarvis._get_intake_draft(r.get_json()["draft_id"]))
check("a pipeline in the section is told where the code is", os.path.normpath(WORK) in brief)
check("and which part it is", "THIS PIPELINE IS PART 2: Payments" in brief)

# ---- 4. without questions, and cancelling -----------------------------------
WORK2 = tempfile.mkdtemp(prefix="Zz Second Folder ")
with open(os.path.join(WORK2, "notes.md"), "w", encoding="utf-8") as f:
    f.write("Research article draft about rental markets.\n")
r = app.post("/sections/intake/start", json={"folder_path": WORK2})
d2 = r.get_json()
up = app.post("/sections/intake/upload", data={
    "draft_id": d2["draft_id"], "files": (io.BytesIO(b"x"), "extra.txt")},
    content_type="multipart/form-data")
made_dir = os.path.join(section_store.SECTIONS_ROOT, d2["folder"])
check("a dropped file lands in the new section folder",
      os.path.exists(os.path.join(made_dir, "Inputs", "extra.txt")))
app.post("/sections/intake/cancel", json={"draft_id": d2["draft_id"]})
check("cancelling leaves no section folder behind", not os.path.exists(made_dir))

jarvis.SECTION_PLAN_ON_CREATE = True
r = app.post("/sections/create", json={"folder_path": WORK2, "brief": "Write the article."})
check("a folder can become a section without questions", r.status_code == 200)
MADE.append(r.get_json()["section"]["folder"])
check("and is planned right after", r.get_json()["planning"] is True)
detail = app.get("/sections/" + r.get_json()["section"]["id"]).get_json()
check("with a whole plan", len(detail["plan"]["parts"]) >= 2)
shutil.rmtree(WORK2, ignore_errors=True)

cleanup()
print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for label in FAILED:
        print("  - " + label)
    raise SystemExit(1)
print("All checks passed.")
