"""The whole chain, minus the Antigravity CLI itself.

command_gate.py is unit-tested separately; what this checks is the part that
only breaks in the joins: the hook script reading the CLI's own payload shape
off stdin, reaching a running Jarvis over HTTP, blocking while the command sits
on the page, and printing back exactly the JSON the CLI expects.

The CLI is never launched and no tokens are spent — the hook is run directly
with a payload of the shape `agy` sends. Nothing is written to the real
database.

    python scripts/test_command_gate_end_to_end.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 5099
BASE = f"http://127.0.0.1:{PORT}"
FAILED = []


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


import command_gate
command_gate.DB_PATH = os.path.join(
    tempfile.mkdtemp(prefix="jarvis_e2e_"), "test.db")

import jarvis
# The routes read the log through jarvis's own connection, so both have to be
# pointed at the scratch database or the test would write to the real one.
jarvis.DB_PATH = command_gate.DB_PATH

server = threading.Thread(
    target=lambda: jarvis.app.run(host="127.0.0.1", port=PORT,
                                  use_reloader=False, threaded=True),
    daemon=True)
server.start()

for _ in range(60):
    try:
        get("/commands/pending")
        break
    except Exception:
        time.sleep(0.25)
else:
    print("FAIL  the server never came up")
    sys.exit(1)
print("PASS  the server is up")


# ---------------------------------------------------------------------------
print("\n--- the hook asking, and being answered ---")

# The payload shape the CLI sends a PreToolUse hook, from its own docs.
payload = {
    "conversationId": "test-conversation",
    "workspacePaths": [os.path.join(os.getcwd(), "Let Jarvis Handle It", "Zz E2E")],
    "modelName": "auto",
    "stepIdx": 4,
    "toolCall": {"name": "run_command", "args": {"CommandLine": "npm install"}},
}

env = os.environ.copy()
env["JARVIS_GATE_URL"] = BASE + "/commands/ask"
env["JARVIS_PROJECT"] = "Zz E2E"
env["JARVIS_PLAN_ID"] = "42"

hook = subprocess.Popen(
    [sys.executable, os.path.join("scripts", "agy_command_gate.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env=env)
hook.stdin.write(json.dumps(payload))
hook.stdin.close()

waiting = []
for _ in range(80):
    waiting = get("/commands/pending")["pending"]
    if waiting:
        break
    time.sleep(0.25)

check("the command the CLI wanted to run reaches the page", len(waiting) == 1)
check("with the command line intact", waiting and waiting[0]["command"] == "npm install")
check("filed under the project it was asked in", waiting and waiting[0]["project"] == "Zz E2E")
check("and the pipeline that asked", waiting and waiting[0]["plan_id"] == "42")
check("the hook is still waiting for an answer", hook.poll() is None)

post("/commands/decide", {
    "request_id": waiting[0]["request_id"],
    "decision": "allow",
    "reason": "Fine for this build.",
})

out, err = hook.communicate(timeout=30)
answer = json.loads(out.strip() or "{}")
check("the hook answers the CLI in the shape it expects",
      answer.get("decision") == "allow" and isinstance(answer.get("reason"), str))
check("the hook exits cleanly", hook.returncode == 0)
check("nothing is left waiting", not get("/commands/pending")["pending"])
check("the decision is in the log",
      any(r["command"] == "npm install" and r["decision"] == "allowed"
          for r in get("/commands/log")["commands"]))


# ---------------------------------------------------------------------------
print("\n--- a command that is never allowed ---")

payload["toolCall"]["args"]["CommandLine"] = "rm -rf /"
hook = subprocess.Popen(
    [sys.executable, os.path.join("scripts", "agy_command_gate.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env=env)
out, err = hook.communicate(json.dumps(payload), timeout=30)
answer = json.loads(out.strip() or "{}")
check("it is refused without anyone being asked", answer.get("decision") == "deny")
check("nothing was parked for it", not get("/commands/pending")["pending"])


# ---------------------------------------------------------------------------
print("\n--- with Jarvis not running ---")

env_down = env.copy()
env_down["JARVIS_GATE_URL"] = "http://127.0.0.1:5098/commands/ask"   # nothing there
hook = subprocess.Popen(
    [sys.executable, os.path.join("scripts", "agy_command_gate.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env=env_down)
payload["toolCall"]["args"]["CommandLine"] = "npm run build"
out, err = hook.communicate(json.dumps(payload), timeout=30)
answer = json.loads(out.strip() or "{}")
check("it fails closed rather than running unreviewed", answer.get("decision") == "deny")
check("and says why", "not running" in (answer.get("reason") or "").lower())


# ---------------------------------------------------------------------------
print("\n--- the pages and their data ---")

for path in ("/commands.html", "/library.html"):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        body = r.read().decode("utf-8")
    check(f"{path} is served", r.status == 200 and "<title>" in body)

library = get("/api/library")
check("the library answers with sections and unfiled work",
      "sections" in library and "unfiled" in library)
check("every section carries its pipelines and its files",
      all("pipelines" in s and "files" in s for s in library["sections"]))
check("the totals are there", "pipelines" in library.get("totals", {}))

rules = get("/commands/rules")
check("the denylist is readable by the page", len(rules.get("denylist") or []) > 5)


print()
if FAILED:
    print(f"{len(FAILED)} check(s) failed:")
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
