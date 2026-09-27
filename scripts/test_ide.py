"""Offline checks for the Workbench (ide.py).

Runs the IDE's routes on a bare Flask app against a throwaway project folder,
with the model replaced by canned answers, so nothing here needs a key, a CLI
or the rest of Jarvis. What is worth proving:

  * paths cannot leave the project folder, however they are spelled;
  * saving refuses to overwrite a file that changed underneath the editor;
  * a Review mission waits on its plan, stages edits as diffs, and writes
    nothing until a change is accepted; reject leaves the file alone; revert
    puts it back;
  * Autopilot applies, and a stale proposal is refused rather than clobbering;
  * a question gets an answer and no changes;
  * every message is a plan-and-edit mission; "/ultra ..." is accepted as an
    alias and stripped;
  * a folder from anywhere on the PC opens in place, only once it was opened,
    and files can be copied in without silently replacing anything;
  * the terminal runs in the project folder, streams output, and refuses the
    hard denylist;
  * the routes answer only this PC.

    PYTHONPATH=. python scripts/test_ide.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask

import ide

PROJECT = "Zz Workbench Test"
TMP = tempfile.mkdtemp(prefix="jarvis-ide-")
ide.PROJECTS_ROOT = os.path.join(TMP, "Let Jarvis Handle It")
ide.STORE_DIR = os.path.join(TMP, "data", "ide")
ide.MISSIONS_FILE = os.path.join(ide.STORE_DIR, "missions.json")
ide.FOLDERS_FILE = os.path.join(ide.STORE_DIR, "folders.json")
ide.SETTINGS_PATH = os.path.join(TMP, "settings.json")
ROOT = os.path.join(ide.PROJECTS_ROOT, PROJECT)

app = Flask(__name__)
app.register_blueprint(ide.blueprint)
client = app.test_client()
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}

failures, passed = [], 0


def check(name, ok, detail=""):
    global passed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def get(url):
    r = client.get(url, environ_base=LOCAL)
    return r.status_code, r.get_json()


def post(url, body=None, remote="127.0.0.1"):
    r = client.post(url, json=body or {}, environ_base={"REMOTE_ADDR": remote})
    return r.status_code, r.get_json()


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# Canned model: the prompt decides the reply, and every call is recorded.
CALLS = []
REPLIES = {}


def fake_model(model_id, system, prompt):
    CALLS.append({"model": model_id, "system": system, "prompt": prompt})
    key = "plan" if system is ide.PLAN_SYSTEM else "edit"
    return REPLIES[key]


ide.call_model = fake_model
os.environ.setdefault("GEMINI_API_KEY", "fake")

try:
    print("projects and files")
    code, r = post("/ide/projects", {"name": PROJECT})
    check("create project", code == 200 and os.path.isdir(ROOT), r)
    code, r = post("/ide/projects", {"name": "../escape"})
    check("project name with a slash refused", code == 400, r)
    code, r = get("/ide/projects")
    check("project listed", any(p["name"] == PROJECT for p in r["projects"]))

    code, r = post("/ide/entry", {"op": "create", "project": PROJECT, "path": "src/app.py", "kind": "file"})
    check("create file in new folder", code == 200 and os.path.isfile(os.path.join(ROOT, "src", "app.py")), r)
    with open(os.path.join(ROOT, "src", "app.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a - b\n")
    os.makedirs(os.path.join(ROOT, "node_modules", "junk"))
    open(os.path.join(ROOT, "node_modules", "junk", "x.js"), "w").close()

    code, r = get(f"/ide/tree?project={PROJECT}")
    paths = [e["path"] for e in r["entries"]]
    check("tree lists the file", "src/app.py" in paths, paths)
    check("tree skips node_modules", not any(p.startswith("node_modules") for p in paths), paths)

    code, r = post("/ide/file", {"project": PROJECT, "path": "/etc/hosts.txt", "content": "x"})
    check("a leading slash means the project root", code == 200 and os.path.isfile(os.path.join(ROOT, "etc", "hosts.txt")), r)
    for bad in ["../outside.txt", "src/../../x", "C:/Windows/win.ini", "..\\x.txt"]:
        code, r = post("/ide/file", {"project": PROJECT, "path": bad, "content": "x"})
        check(f"write outside refused: {bad}", code == 400, r)
    code, r = get(f"/ide/file?project={PROJECT}&path=../../ide.py")
    check("read outside refused", code == 400, r)
    if hasattr(os, "symlink"):
        try:
            os.symlink(TMP, os.path.join(ROOT, "link"))
            code, r = post("/ide/file", {"project": PROJECT, "path": "link/evil.txt", "content": "x"})
            check("write through a symlink out refused", code == 400, r)
            os.remove(os.path.join(ROOT, "link"))
        except OSError:
            pass

    code, f = get(f"/ide/file?project={PROJECT}&path=src/app.py")
    check("read file", code == 200 and "return a - b" in f["content"], f)
    code, r = post("/ide/file", {"project": PROJECT, "path": "src/app.py",
                                 "content": "def add(a, b):\n    return a - b  # mine\n", "hash": f["hash"]})
    check("save with matching hash", r["status"] == "saved", r)
    code, r = post("/ide/file", {"project": PROJECT, "path": "src/app.py", "content": "stale", "hash": f["hash"]})
    check("save over a newer disk version is a conflict", r["status"] == "conflict" and "# mine" in r["content"], r)
    check("conflict left the file alone", "# mine" in read("src/app.py"))
    with open(os.path.join(ROOT, "logo.bin"), "wb") as b:
        b.write(b"\x00\x01\x02")
    code, r = get(f"/ide/file?project={PROJECT}&path=logo.bin")
    check("binary file not opened as text", r.get("binary") is True, r)

    print("review mission")
    REPLIES["plan"] = json.dumps({
        "kind": "change", "understanding": "add() subtracts; it should add.", "simple": True,
        "steps": ["Fix add()", "Add a README"], "read": ["src/app.py"], "change": ["src/app.py", "README.md"],
        "questions": []})
    REPLIES["edit"] = "```json\n" + json.dumps({
        "edits": [
            {"path": "src/app.py", "action": "replace", "find": "return a - b", "replace": "return a + b"},
            {"path": "README.md", "action": "write", "content": "# Calc\n"},
            {"path": "../../evil.txt", "action": "write", "content": "nope"},
        ],
        "commands": [{"command": "python -c \"print('hi')\"", "why": "smoke test"}],
        "summary": "Fixed add and added a README."}) + "\n```"
    m = ide.start_mission(PROJECT, "add() is broken, fix it", engine="jarvis", model="gemini:gemini-2.5-flash",
                          mode="review", context={"open_file": "src/app.py", "selection": "return a - b"},
                          background=False)
    m = ide.get_mission(m["id"])
    check("review mission waits on its plan", m["status"] == "awaiting_plan", m["status"])
    check("plan prompt carried the open file and selection",
          "THE OWNER HAS THIS FILE OPEN: src/app.py" in CALLS[0]["prompt"] and "THE OWNER SELECTED" in CALLS[0]["prompt"])
    check("nothing written before approval", "a - b" in read("src/app.py") and not os.path.exists(os.path.join(ROOT, "README.md")))

    ide.approve_plan(m["id"], "keep it short", background=False)
    m = ide.get_mission(m["id"])
    check("edit prompt carried the plan notes", "keep it short" in CALLS[-1]["prompt"])
    check("edit prompt carried file contents", "=== src/app.py ===" in CALLS[-1]["prompt"])
    check("fenced JSON parsed; mission in review", m["status"] == "review", m.get("error"))
    by_path = {c["path"]: c for c in m["changes"]}
    check("two changes staged, the escaping one dropped", set(by_path) == {"src/app.py", "README.md"}, list(by_path))
    check("escaping edit logged", any("outside the project" in l["text"] for l in m["log"]))
    check("still nothing on disk", "a - b" in read("src/app.py") and not os.path.exists(os.path.join(ROOT, "README.md")))
    check("diff stats", by_path["src/app.py"]["stats"] == {"added": 1, "removed": 1}, by_path["src/app.py"]["stats"])
    check("command suggested, not run", m["commands"] and m["commands"][0]["status"] == "suggested")
    code, listed = get(f"/ide/missions?project={PROJECT}")
    check("list has no file contents", "before" not in listed["missions"][0]["changes"][0])
    code, full = get(f"/ide/missions/{m['id']}/changes/{by_path['src/app.py']['id']}")
    check("change fetch has before/after", "a - b" in full["before"] and "a + b" in full["after"])

    code, r = post(f"/ide/missions/{m['id']}/changes/{by_path['src/app.py']['id']}", {"decision": "accept"})
    check("accept writes the file", code == 200 and "return a + b" in read("src/app.py"), r)
    code, r = post(f"/ide/missions/{m['id']}/changes/{by_path['README.md']['id']}", {"decision": "reject"})
    check("reject writes nothing", not os.path.exists(os.path.join(ROOT, "README.md")))
    check("mission done once all decided", r["status"] == "done", r["status"])
    code, r = post(f"/ide/missions/{m['id']}/changes/{by_path['src/app.py']['id']}", {"decision": "revert"})
    check("revert restores the file", "return a - b" in read("src/app.py"), read("src/app.py"))
    code, r = post(f"/ide/missions/{m['id']}/changes/all", {"decision": "bogus"})
    check("unknown decision refused", code == 400, r)

    print("follow-up, stale proposal, autopilot")
    REPLIES["edit"] = json.dumps({"edits": [{"path": "src/app.py", "action": "write", "content": "print('v2')\n"}],
                                  "summary": "Rewrote it."})
    ide.follow_up(m["id"], "now rewrite it", background=False)
    m = ide.get_mission(m["id"])
    check("follow-up plans again and waits", m["status"] == "awaiting_plan", m["status"])
    check("follow-up prompt has the history", "add() is broken" in CALLS[-1]["prompt"] and "now rewrite it" in CALLS[-1]["prompt"])
    ide.approve_plan(m["id"], background=False)
    m = ide.get_mission(m["id"])
    pending = [c for c in m["changes"] if c["status"] == "pending"]
    with open(os.path.join(ROOT, "src", "app.py"), "w", encoding="utf-8") as f:
        f.write("someone else edited this\n")
    r = ide.decide_change(m["id"], pending[0]["id"], "accept")
    check("stale proposal refused, file kept", r.get("errors") and "someone else" in read("src/app.py"), r.get("errors"))

    REPLIES["edit"] = json.dumps({"edits": [{"path": "notes/todo.md", "action": "write", "content": "- ship\n"},
                                            {"path": "src/app.py", "action": "replace", "find": "not there", "replace": "x"}],
                                  "summary": "Added notes."})
    a = ide.start_mission(PROJECT, "add a todo", model="gemini:gemini-2.5-flash", mode="autopilot", background=False)
    a = ide.get_mission(a["id"])
    check("autopilot applies without asking", a["status"] == "done" and read("notes/todo.md") == "- ship\n", a["status"])
    check("replace that doesn't match is reported, not applied",
          any("not found" in l["text"] for l in a["log"]) and "someone else" in read("src/app.py"))
    check("autopilot change is marked applied", a["changes"][0]["status"] == "applied")
    ide.decide_change(a["id"], a["changes"][0]["id"], "revert")
    check("reverting a created file removes it", not os.path.exists(os.path.join(ROOT, "notes", "todo.md")))

    REPLIES["edit"] = json.dumps({"edits": [
        {"path": "src/app.py", "action": "replace", "find": "someone else", "replace": "Jarvis"},
        {"path": "src/app.py", "action": "replace", "find": "not there", "replace": "x"}], "summary": "Half fits."})
    h = ide.start_mission(PROJECT, "half an edit", model="gemini:gemini-2.5-flash", mode="autopilot", background=False)
    h = ide.get_mission(h["id"])
    check("autopilot holds a half-fitting edit for review",
          h["status"] == "review" and h["changes"][0]["status"] == "pending" and "someone else" in read("src/app.py"),
          (h["status"], h["changes"]))
    ide.decide_change(h["id"], "all", "reject")

    REPLIES["plan"] = json.dumps({"kind": "answer", "understanding": "A question.", "answer": "It adds two numbers."})
    q = ide.start_mission(PROJECT, "what does add do?", model="gemini:gemini-2.5-flash", background=False)
    q = ide.get_mission(q["id"])
    check("question answered with no changes", q["status"] == "done" and q["result"] == "It adds two numbers." and not q["changes"])

    REPLIES["plan"] = "this is not json"
    bad = ide.start_mission(PROJECT, "anything", model="gemini:gemini-2.5-flash", background=True)
    for _ in range(50):
        if ide.get_mission(bad["id"])["status"] not in ("planning", "working"):
            break
        time.sleep(0.05)
    bad = ide.get_mission(bad["id"])
    check("bad model output fails the mission, doesn't hang", bad["status"] == "failed" and "JSON" in bad["error"], bad)

    print("every message plans; /ultra is an alias")
    REPLIES["plan"] = json.dumps({"kind": "change", "understanding": "Handle negatives.", "steps": ["Edit add"],
                                  "read": ["src/app.py"], "change": ["src/app.py"], "questions": []})
    u = ide.start_mission(PROJECT, "make add handle negatives", model="gemini:gemini-2.5-flash", background=False)
    u = ide.get_mission(u["id"])
    check("a plain message plans like /ultra did", u["status"] == "awaiting_plan" and CALLS[-1]["system"] is ide.PLAN_SYSTEM, u["status"])
    u2 = ide.start_mission(PROJECT, "/ultra make add handle negatives", model="gemini:gemini-2.5-flash", background=False)
    u2 = ide.get_mission(u2["id"])
    check("/ultra still works and is stripped", u2["status"] == "awaiting_plan"
          and u2["conversation"][0] == {"role": "owner", "text": "make add handle negatives"}
          and "/ultra" not in CALLS[-1]["prompt"])
    ide.stop_mission(u["id"]); ide.stop_mission(u2["id"])
    # A conversation saved by the chat-only version becomes a normal mission on its next message.
    old = dict(u2, id="oldchat00001", kind="chat", engine="jarvis", status="done", plan=None,
               conversation=[{"role": "owner", "text": "what is add?"}, {"role": "jarvis", "text": "It adds."}])
    ide._save(old)
    ide.follow_up("oldchat00001", "now make it handle negatives", engine="jarvis", model="gemini:gemini-2.5-pro", background=False)
    old = ide.get_mission("oldchat00001")
    check("an old chat continues as a mission", old["kind"] == "ultra" and old["status"] == "awaiting_plan"
          and old["model"] == "gemini:gemini-2.5-pro" and "what is add?" in CALLS[-1]["prompt"], old["status"])
    ide.stop_mission("oldchat00001")
    code, r = post("/ide/missions", {"project": PROJECT, "prompt": "/ultra   "})
    check("/ultra with nothing after it refused", code == 400, r)
    check("/ULTRA: works too", ide.split_ultra("  /ULTRA: fix it") == (True, "fix it") and ide.split_ultra("/ultrafast") == (False, "/ultrafast"))

    code, r = post("/ide/missions", {"project": PROJECT, "prompt": "   "})
    check("empty prompt refused", code == 400)
    check("missions persisted", os.path.exists(ide.MISSIONS_FILE))
    ide._MISSIONS.clear(); ide._loaded = False
    check("missions reload from disk", ide.get_mission(q["id"])["result"] == "It adds two numbers.")

    print("cli engines")
    ide._antigravity = lambda: None
    ide._code_agent = lambda: None
    eng = ide.engines()
    by_id = {e["id"]: e for e in eng["engines"]}
    check("jarvis available with a Gemini key", by_id["jarvis"]["available"])
    check("missing Claude agent explained", not by_id["claude"]["available"] and by_id["claude"]["reason"])

    class FakeAgent:
        @staticmethod
        def describe_availability():
            return True, "ok"

        @staticmethod
        def run_coding_task(project, agent_id, task, **kw):
            with open(os.path.join(ROOT, "built.py"), "w", encoding="utf-8") as f:
                f.write("print('built')\n")
            return {"status": "ok", "summary": "Built it.", "files_changed": ["built.py"]}

    ide._code_agent = lambda: FakeAgent
    check("claude engine available when the module is", {e["id"]: e for e in ide.engines()["engines"]}["claude"]["available"])
    c = ide.start_mission(PROJECT, "build it", engine="claude", background=False)
    c = ide.get_mission(c["id"])
    check("CLI engine changes show as applied diffs",
          c["status"] == "done" and [x["path"] for x in c["changes"]] == ["built.py"] and c["changes"][0]["status"] == "applied",
          c)
    ide.decide_change(c["id"], c["changes"][0]["id"], "revert")
    check("CLI engine change can be reverted", not os.path.exists(os.path.join(ROOT, "built.py")))

    print("terminal")
    code, job = post("/ide/terminal", {"project": PROJECT, "command": f"\"{sys.executable}\" -c \"import os;print(os.getcwd())\""})
    check("command starts", code == 200, job)
    for _ in range(100):
        code, j = get(f"/ide/terminal/{job['id']}")
        if not j["running"]:
            break
        time.sleep(0.05)
    check("runs in the project folder", os.path.realpath(j["output"].strip()) == os.path.realpath(ROOT), j["output"])
    check("exit code reported", j["exit_code"] == 0, j)
    code, r = post("/ide/terminal", {"project": PROJECT, "command": "shutdown now"})
    check("denylisted command refused", code == 400 and "Refused" in r["error"], r)
    code, job = post("/ide/terminal", {"project": PROJECT, "command": f"\"{sys.executable}\" -c \"import time;time.sleep(30)\""})
    post(f"/ide/terminal/{job['id']}/stop")
    for _ in range(100):
        code, j = get(f"/ide/terminal/{job['id']}")
        if not j["running"]:
            break
        time.sleep(0.05)
    check("stop ends a running command", not j["running"], j)

    print("folders from anywhere on the PC")
    outside = os.path.join(TMP, "elsewhere", "My App")
    os.makedirs(os.path.join(outside, "lib"))
    with open(os.path.join(outside, "lib", "util.py"), "w", encoding="utf-8") as f:
        f.write("X = 1\n")
    code, r = get("/ide/tree?project=" + urllib.parse.quote(outside))
    check("a folder that wasn't opened can't be read", code == 400 and "Open that folder" in r["error"], r)
    code, r = post("/ide/open", {"path": os.path.join(outside, "lib", "util.py")})
    check("opening a file opens its folder", code == 200 and r["project"] == os.path.join(os.path.realpath(outside), "lib") and r["file"] == "util.py", r)
    code, r = post("/ide/close", {"path": r["project"]})
    code, r = post("/ide/open", {"path": outside})
    ext = r["project"]
    check("open a folder where it is", code == 200 and ext == os.path.realpath(outside) and r["file"] == "", r)
    code, r = get("/ide/projects")
    listed = [p for p in r["projects"] if p["external"]]
    check("it is listed under its folder name", [(p["name"], p["label"]) for p in listed] == [(ext, "My App")], listed)
    code, r = post("/ide/open", {"path": os.path.join(outside, "lib", "util.py")})
    check("a file inside an open folder uses that folder", r == {"project": ext, "file": "lib/util.py"}, r)
    code, r = get("/ide/tree?project=" + urllib.parse.quote(ext))
    check("its tree reads", code == 200 and any(e["path"] == "lib/util.py" for e in r["entries"]) and r["root"] == ext, r)
    code, r = post("/ide/file", {"project": ext, "path": "lib/util.py", "content": "X = 2\n", "hash": ide._hash("X = 1\n")})
    check("and saves in place", code == 200 and open(os.path.join(outside, "lib", "util.py")).read() == "X = 2\n", r)
    code, r = post("/ide/entry", {"op": "create", "project": ext, "path": "../../escape.txt", "kind": "file"})
    check("still can't step outside it", code == 400 and not os.path.exists(os.path.join(TMP, "escape.txt")), r)
    code, r = post("/ide/open", {"path": os.path.join(ROOT, "src")})
    check("a folder inside one of Jarvis's projects opens that project", r == {"project": PROJECT, "file": ""}, r)
    code, r = post("/ide/open", {"path": "relative/path"})
    check("a relative path is refused", code == 400, r)
    code, r = post("/ide/open", {"path": os.path.join(TMP, "nope")})
    check("a missing path is refused", code == 400 and "does not exist" in r["error"], r)
    REPLIES["plan"] = json.dumps({"kind": "answer", "understanding": "A question.", "answer": "X is 2."})
    c2 = ide.start_mission(ext, "what is X?", background=False)
    check("missions work on an opened folder", ide.get_mission(c2["id"])["result"] == "X is 2." and "lib/util.py" in CALLS[-1]["prompt"])

    code, r = get("/ide/browse?path=" + urllib.parse.quote(os.path.dirname(outside)))
    check("the page's folder browser lists folders", code == 200 and r["dirs"] == ["My App"] and r["parent"], r)
    code, r = post("/ide/pick", {"kind": "folder"})
    check("no desktop window: the page falls back to its own browser", code == 200 and r["available"] is False, r)

    class FakeWindow:
        def create_file_dialog(self, kind, allow_multiple=False):
            return (outside,)
    fake_webview = type(sys)("webview")
    fake_webview.windows = [FakeWindow()]
    fake_webview.FOLDER_DIALOG, fake_webview.OPEN_DIALOG = 20, 10
    real_webview = sys.modules.get("webview")
    sys.modules["webview"] = fake_webview
    try:
        code, r = post("/ide/pick", {"kind": "folder"})
    finally:
        if real_webview is not None:
            sys.modules["webview"] = real_webview
        else:
            sys.modules.pop("webview", None)
    check("the desktop window's own dialog is used when there is one", r == {"available": True, "paths": [outside]}, r)

    print("copying files in")
    resp = client.post("/ide/upload", environ_base=LOCAL, content_type="multipart/form-data", data={
        "project": ext, "dir": "lib",
        "files": [(io.BytesIO(b"hello"), "a.txt"), (io.BytesIO(b"deep"), "b.txt")],
        "paths": ["a.txt", "assets/img/b.txt"]})
    r = resp.get_json()
    check("dropped files and a dropped folder's layout are saved",
          resp.status_code == 200 and sorted(r["saved"]) == ["lib/a.txt", "lib/assets/img/b.txt"]
          and open(os.path.join(outside, "lib", "assets", "img", "b.txt")).read() == "deep", r)
    resp = client.post("/ide/upload", environ_base=LOCAL, content_type="multipart/form-data", data={
        "project": ext, "files": [(io.BytesIO(b"new"), "a.txt")], "paths": ["lib/a.txt"]})
    r = resp.get_json()
    check("an existing file isn't replaced without asking",
          r["exists"] == ["lib/a.txt"] and open(os.path.join(outside, "lib", "a.txt")).read() == "hello", r)
    resp = client.post("/ide/upload", environ_base=LOCAL, content_type="multipart/form-data", data={
        "project": ext, "overwrite": "1", "files": [(io.BytesIO(b"new"), "a.txt")], "paths": ["lib/a.txt"]})
    check("...and is when the owner says so", open(os.path.join(outside, "lib", "a.txt")).read() == "new")
    resp = client.post("/ide/upload", environ_base=LOCAL, content_type="multipart/form-data", data={
        "project": ext, "files": [(io.BytesIO(b"x"), "x.txt")], "paths": ["../../../pwned.txt"]})
    check("an upload can't land outside the folder", resp.status_code == 400 and not os.path.exists(os.path.join(TMP, "pwned.txt")))

    source = os.path.join(TMP, "source")
    os.makedirs(os.path.join(source, "pkg"))
    with open(os.path.join(source, "pkg", "m.py"), "w") as f:
        f.write("pass\n")
    with open(os.path.join(source, "notes.md"), "w") as f:
        f.write("n\n")
    code, r = post("/ide/import", {"project": PROJECT, "dir": "", "paths": [os.path.join(source, "pkg"), os.path.join(source, "notes.md")]})
    check("import copies a folder and a file into the project",
          code == 200 and sorted(r["copied"]) == ["notes.md", "pkg"] and os.path.isfile(os.path.join(ROOT, "pkg", "m.py"))
          and os.path.isfile(os.path.join(source, "notes.md")), r)
    code, r = post("/ide/import", {"project": PROJECT, "paths": [os.path.join(source, "notes.md")]})
    check("import doesn't replace without asking", r["exists"] == ["notes.md"] and not r["copied"], r)

    code, r = post("/ide/close", {"path": ext})
    code, r = get("/ide/projects")
    check("closing takes it off the list and leaves the files", not any(p["external"] for p in r["projects"])
          and os.path.isfile(os.path.join(outside, "lib", "util.py")))

    print("access")
    code, r = post("/ide/file", {"project": PROJECT, "path": "x.txt", "content": "x"}, remote="192.168.1.20")
    check("another machine is refused", code == 403 and not os.path.exists(os.path.join(ROOT, "x.txt")), r)
    code, r = post("/ide/entry", {"op": "delete", "project": PROJECT, "path": "src"})
    check("delete folder", code == 200 and not os.path.exists(os.path.join(ROOT, "src")), r)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {len(failures)} failed")
if failures:
    print("Failed:", *failures, sep="\n  ")
    sys.exit(1)
