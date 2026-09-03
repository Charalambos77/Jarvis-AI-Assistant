"""End-to-end exercise of the clarification gate against the Flask test client."""
import io, os, json, shutil, sys

import jarvis
from google import genai

BASE = jarvis.BASE_DIR
app = jarvis.app.test_client()

# Never actually launch agents during the test.
STARTED = []
def fake_initiate(task, project_name=None, brief_path=None, task_summary=None):
    STARTED.append({"task": task, "project_name": project_name,
                    "brief_path": brief_path, "task_summary": task_summary})
    return "99"
jarvis.initiate_pipeline = fake_initiate
jarvis.speak = lambda text: None

def set_model_reply(payload):
    genai.FAKE["response"] = json.dumps(payload)

def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)

# ---- 1. start a draft ------------------------------------------------------
r = app.post("/pipeline/intake/start", json={"task": "Build a website for my dentist client"})
draft = r.get_json()
did = draft["draft_id"]
project = draft["project_name"]
check("draft created, no pipeline yet", r.status_code == 200 and did and not STARTED)
check("no plan row created", len(jarvis.PLAN_STORE) == 0 or all(p["id"] != "99" for p in jarvis.PLAN_STORE))

# ---- 2. upload files of several types --------------------------------------
data = {
    "draft_id": did,
    "files": [
        (io.BytesIO(b"\x89PNG\r\n\x1a\nfake"), "logo.png"),
        (io.BytesIO(b"%PDF-1.4 fake brochure"), "../../escape.pdf"),
        (io.BytesIO(b"\x00\x01binary"), "mystery.bin"),
    ],
}
r = app.post("/pipeline/intake/upload", data=data, content_type="multipart/form-data")
files = r.get_json()["files"]
names = [f["name"] for f in files]
check("all three files stored", len(files) == 3)
check("path traversal stripped", "escape.pdf" in names and not any("/" in n or ".." in n for n in names))
inputs_dir = os.path.join(BASE, "Let Jarvis Handle It", project, "Inputs")
check("files landed in Inputs/", all(os.path.exists(os.path.join(inputs_dir, n)) for n in names))
check("paths hidden from the client", all("path" not in f for f in files))

# duplicate name must not overwrite
r = app.post("/pipeline/intake/upload", data={
    "draft_id": did, "files": [(io.BytesIO(b"second logo"), "logo.png")]
}, content_type="multipart/form-data")
names = [f["name"] for f in r.get_json()["files"]]
check("duplicate upload kept separately", "logo (2).png" in names)

# thumbnail route only serves this draft's files
r = app.get(f"/intake-file/{did}/logo.png")
check("thumbnail served", r.status_code == 200)
r = app.get(f"/intake-file/{did}/second_brain.db")
check("unknown file refused", r.status_code == 404)

# ---- 3. remove one ---------------------------------------------------------
r = app.post("/pipeline/intake/remove_file", json={"draft_id": did, "name": "logo (2).png"})
check("file removed from draft", len(r.get_json()["files"]) == 3)
check("file removed from disk", not os.path.exists(os.path.join(inputs_dir, "logo (2).png")))

# ---- 4. question round -----------------------------------------------------
set_model_reply({"questions": [
    {"question": "Who is the website for?", "gist": "who is it for"},
    {"question": "Does she need online booking?", "gist": "booking needed"},
]})
r = app.post("/pipeline/intake/questions", json={"draft_id": did, "details": "She is a dentist."})
qs = r.get_json()["questions"]
check("two questions returned", len(qs) == 2)

r = app.post("/pipeline/intake/answer", json={
    "draft_id": did, "question": qs[0]["question"], "answer": "Dr Elena, a dentist in Limassol"})
body = r.get_json()
check("next question handed back", body["next"]["question"] == qs[1]["question"] and not body["done"])

r = app.post("/pipeline/intake/answer", json={
    "draft_id": did, "question": qs[1]["question"], "answer": "Yes, online booking"})
check("batch exhausted", r.get_json()["done"] is True)

# ---- 5. picture returns MORE questions (the loop) ---------------------------
set_model_reply({"questions": [{"question": "What are her opening hours?", "gist": "opening hours"}]})
r = app.post("/pipeline/intake/picture", json={"draft_id": did})
check("picture can loop back to questions", len(r.get_json().get("questions", [])) == 1)

app.post("/pipeline/intake/answer", json={
    "draft_id": did, "question": "What are her opening hours?", "answer": "9 to 5, closed Sunday"})

# ---- 6. skip the rest ------------------------------------------------------
set_model_reply({"questions": [{"question": "Ignored", "gist": "ignored"}]})
app.post("/pipeline/intake/questions", json={"draft_id": did})
r = app.post("/pipeline/intake/skip", json={"draft_id": did})
check("skip clears pending questions", r.get_json()["status"] == "skipped")

# ---- 7. the picture --------------------------------------------------------
set_model_reply({"plan_text": "# Plan\n\nA booking site for Dr Elena."})
r = app.post("/pipeline/intake/picture", json={"draft_id": did})
plan = r.get_json()["plan_text"]
check("plan painted", "Dr Elena" in plan)

# ---- 8. edit is cleaned and handed back, never built ------------------------
set_model_reply({"plan_text": "# Plan\n\nA booking site for Dr Elena, in blue."})
r = app.post("/pipeline/intake/edit", json={"draft_id": did, "edited_text": "booking site, blue"})
check("edit cleaned and returned", "blue" in r.get_json()["plan_text"])
check("editing did not start a pipeline", not STARTED)

# edit survives a model failure without losing the user's words
genai.FAKE["response"] = "not json at all"
r = app.post("/pipeline/intake/edit", json={"draft_id": did, "edited_text": "MY EXACT WORDS"})
check("failed cleaning keeps user's text", r.get_json()["plan_text"] == "MY EXACT WORDS")
set_model_reply({"plan_text": "# Plan\n\nA booking site for Dr Elena, in blue."})
app.post("/pipeline/intake/edit", json={"draft_id": did, "edited_text": "final plan"})

# ---- 9. approve — the only thing that builds -------------------------------
r = app.post("/pipeline/intake/approve", json={"draft_id": did})
res = r.get_json()
check("pipeline started once", len(STARTED) == 1 and res["plan_id"] == "99")
check("short summary preserved for labels",
      STARTED[0]["task_summary"] == "Build a website for my dentist client")
check("project name reused, so Inputs/ matches", STARTED[0]["project_name"] == project)

brief_path = res["brief_path"]
brief = open(brief_path, encoding="utf-8").read()
check("brief written to Brief/clarified_brief.md", brief_path.endswith("clarified_brief.md"))
check("brief carries the details", "She is a dentist." in brief)
check("brief carries every Q&A", "Dr Elena, a dentist in Limassol" in brief
      and "Yes, online booking" in brief and "9 to 5, closed Sunday" in brief)
check("brief lists the files", "Inputs/logo.png" in brief and "Inputs/mystery.bin" in brief)
check("brief carries the approved plan", "in blue" in brief)
check("brief states the research freedom rule", "search the web freely" in brief)
check("full brief handed to the agents", STARTED[0]["task"] == brief)
check("draft cleared after approval", jarvis._get_intake_draft(did) is None)

# ---- 10. cancel leaves nothing --------------------------------------------
r = app.post("/pipeline/intake/start", json={"task": "Throwaway job"})
d2 = r.get_json()
app.post("/pipeline/intake/upload", data={
    "draft_id": d2["draft_id"], "files": [(io.BytesIO(b"junk"), "junk.txt")]
}, content_type="multipart/form-data")
junk_dir = os.path.join(BASE, "Let Jarvis Handle It", d2["project_name"], "Inputs")
check("cancel test file exists first", os.path.exists(os.path.join(junk_dir, "junk.txt")))
app.post("/pipeline/intake/cancel", json={"draft_id": d2["draft_id"]})
check("cancel removed the files", not os.path.exists(junk_dir))
check("cancel removed the draft", jarvis._get_intake_draft(d2["draft_id"]) is None)
check("cancel started nothing", len(STARTED) == 1)

# ---- 11. the tool no longer launches a pipeline -----------------------------
res = jarvis.start_pipeline_local({"task": "Some complex job"})
check("start_pipeline only opens the gate", res["status"] == "awaiting_details")
check("no pipeline started by the tool", len(STARTED) == 1)
check("UI told to ask about details", jarvis.UI_ACTION["type"] == "pipeline_intake_ask")

# ---- 12. saying no still builds the old way --------------------------------
res = jarvis.start_pipeline_local({"task": "Just build it", "skip_intake": True})
check("skip_intake builds immediately", res["status"] == "pipeline_started" and len(STARTED) == 2)

# ---- 13. a dead model must not trap the user -------------------------------
r = app.post("/pipeline/intake/start", json={"task": "Model is down"})
d3 = r.get_json()["draft_id"]
genai.FAKE["response"] = "}{ broken"
r = app.post("/pipeline/intake/questions", json={"draft_id": d3, "details": "x"})
check("broken model yields no questions rather than an error", r.get_json()["questions"] == [])
r = app.post("/pipeline/intake/picture", json={"draft_id": d3})
body = r.get_json()
check("broken model still yields an editable brief", bool(body["plan_text"]) and bool(body["degraded"]))
app.post("/pipeline/intake/cancel", json={"draft_id": d3})

# cleanup
shutil.rmtree(os.path.join(BASE, "Let Jarvis Handle It", project), ignore_errors=True)
print("\nAll checks passed.")
