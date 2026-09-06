"""Checks on the Antigravity CLI integration.

The parts worth guarding are the ones that fail quietly: a tool that is offered
when the binary is missing, a build pointed at a folder nobody chose, and a
refused command being reported as a success. Nothing here runs `agy` or spends
a token — the subprocess is stubbed, except in the optional live check at the
bottom which only runs when you ask for it.

    python scripts/test_antigravity.py          # fast, offline
    python scripts/test_antigravity.py --live   # also runs one real build
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import coordinator
from agents import tool_executor
from connectors import antigravity

FAILED = []
PROJECT = "Zz Test Antigravity"

# Running a check must not rewrite the real CLI's list of trusted folders — the
# test project is not somewhere anything should be allowed to work afterwards.
import tempfile
antigravity._CLI_SETTINGS = os.path.join(
    tempfile.mkdtemp(prefix="jarvis_agy_test_"), "settings.json")


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def stub_run(payload=None, stderr="", returncode=0, capture=None):
    """Replace the subprocess call, keeping the command line for inspection."""
    def fake(cmd, **kwargs):
        if capture is not None:
            capture.append({"cmd": cmd, "kwargs": kwargs})
        return FakeCompleted(payload or "", stderr, returncode)
    return fake


real_subprocess_run = subprocess.run

# ---- 1. availability decides whether the tool is offered at all ------------

check("the installed binary is found", antigravity.find_binary() is not None)
check("so the tool is available", antigravity.is_available() is True)

os.environ["JARVIS_AGY_DISABLED"] = "1"
check("the kill switch takes it away", antigravity.is_available() is False)
decls, handlers, _ = tool_executor.get_tools_for_execution_agent([], PROJECT)
check("and then no agent is offered it",
      "delegate_build" not in [d["name"] for d in decls])
del os.environ["JARVIS_AGY_DISABLED"]

decls, handlers, _ = tool_executor.get_tools_for_execution_agent([], PROJECT)
check("with it installed, execution agents get it",
      "delegate_build" in [d["name"] for d in decls])
check("and the always-on tools are still there",
      {"write_file", "read_file"} <= {d["name"] for d in decls})

orig_find = antigravity.find_binary
antigravity.find_binary = lambda: None
try:
    missing = antigravity.run("build something", PROJECT)
    check("with no binary, a call fails honestly rather than hanging",
          missing["status"] == "error" and "not installed" in missing["error"])
finally:
    antigravity.find_binary = orig_find

# ---- 2. what the subprocess is actually asked to do -----------------------

calls = []
subprocess.run = stub_run('{"response": "Built it."}', capture=calls)
try:
    result = antigravity.run("Build the landing page.", PROJECT, timeout=600)
finally:
    subprocess.run = real_subprocess_run

cmd = calls[0]["cmd"]
check("the prompt is passed for a single non-interactive run",
      "-p" in cmd and "Build the landing page." in cmd)
# The sandbox is off, and that is a decision rather than an oversight: with it
# on, every command needs a second permission to leave the sandbox that headless
# mode refuses, so an approved command still never runs. The review that
# replaces it is the Commands page, which sees the command before it happens.
check("the sandbox is not asked for", "--sandbox" not in cmd)
check("permissions are never skipped wholesale",
      "--dangerously-skip-permissions" not in cmd)
# Headless mode cannot prompt, so without this the CLI reports SUCCESS and
# writes nothing at all. accept-edits approves edits only — commands still go
# through the hook to the Commands page.
check("edits are approved so the run can actually do something",
      "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "accept-edits")
check("output is asked for as json",
      "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json")
check("the CLI gets its own timeout", "--print-timeout" in cmd)
check("there is a backstop timeout on the process too",
      calls[0]["kwargs"].get("timeout", 0) > 600)

workspace = calls[0]["kwargs"].get("cwd", "")
check("it works in the project's own folder", workspace.endswith(PROJECT))
check("never in Jarvis's source",
      os.path.abspath(workspace) != os.path.abspath(antigravity.BASE_DIR))
check("the workspace is added explicitly too",
      "--add-dir" in cmd and workspace in cmd)
check("the written answer is handed back", result["output"] == "Built it.")
check("and it reads as a success", result["status"] == "success")

# ---- 3. the failure modes that would otherwise read as success ------------

subprocess.run = stub_run('{"response": "Done."}',
                          stderr="Tool `run_command` requires approval; permission denied for command(npm).")
try:
    refused = antigravity.run("Install the dependencies.", PROJECT)
finally:
    subprocess.run = real_subprocess_run
check("a refused command is surfaced even though the CLI exits 0",
      refused.get("refused") and refused["status"] == "success")
check("with an instruction not to report it as done",
      "did NOT happen" in refused.get("note", ""))

subprocess.run = stub_run("", stderr="Error: not authenticated, please sign in again.")
try:
    unauth = antigravity.run("Build it.", PROJECT)
finally:
    subprocess.run = real_subprocess_run
check("a lapsed session is reported rather than retried",
      unauth.get("needs_sign_in") is True)


def raise_timeout(cmd, **kwargs):
    raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))


subprocess.run = raise_timeout
try:
    slow = antigravity.run("Build forever.", PROJECT, timeout=120)
finally:
    subprocess.run = real_subprocess_run
check("a run that overruns is stopped, not left hanging",
      slow["status"] == "error" and slow.get("timed_out") is True)

subprocess.run = stub_run("not json at all")
try:
    plain = antigravity.run("Say something.", PROJECT)
finally:
    subprocess.run = real_subprocess_run
check("text output instead of json is still returned", plain["output"] == "not json at all")

check("an empty instruction is refused before spawning anything",
      antigravity.run("", PROJECT)["status"] == "error")

# ---- 4. Jarvis's own tool ------------------------------------------------

check("Jarvis is offered the tool",
      "delegate_build" in [t["name"] for t in coordinator.TOOLS])
check("and it is wired to an implementation", "delegate_build" in coordinator.TOOL_IMPL)

coordinator.set_active_section(None)
outside = coordinator.perform_tool_action(None, "delegate_build", instruction="Build me a site.")
check("outside a section it refuses to guess a project",
      outside["status"] == "error" and "No project chosen" in outside["error"])

calls = []
subprocess.run = stub_run('{"response": "ok"}', capture=calls)
coordinator.set_active_section({"id": "sec1", "name": "Test", "folder": PROJECT})
try:
    coordinator.perform_tool_action(None, "delegate_build", instruction="Build the page.")
finally:
    subprocess.run = real_subprocess_run
    coordinator.set_active_section(None)
check("inside a section it uses that section's folder",
      calls and calls[0]["kwargs"]["cwd"].endswith(PROJECT))

calls = []
subprocess.run = stub_run('{"response": "ok"}', capture=calls)
try:
    coordinator.perform_tool_action(None, "delegate_build",
                                    instruction="Build it.", project=PROJECT,
                                    timeout_minutes=99)
finally:
    subprocess.run = real_subprocess_run
# The clamp is on the job itself (30 minutes). What the CLI is told is that
# plus the allowance for time parked at the Commands page waiting for the user,
# which is not time the build spent working and must not count against it.
expected = (30 * 60 + antigravity._APPROVAL_ALLOWANCE) // 60
check("an absurd timeout is clamped rather than honoured",
      f"{expected}m" in calls[0]["cmd"])

# ---- 5. optional: one real build -----------------------------------------

if "--live" in sys.argv:
    print("\n--- live run (this spends tokens and takes a minute) ---")
    live = antigravity.run(
        "Create a file named agy_ok.txt in this folder containing exactly the word OK. "
        "Do nothing else.",
        PROJECT, timeout=180,
    )
    print("status:", live.get("status"))
    print("output:", (live.get("output") or "")[:400])
    if live.get("refused"):
        print("refused:", live["refused"])
    if live.get("needs_sign_in"):
        print("NEEDS SIGN-IN — run `agy` once in a terminal.")
    made = os.path.join(antigravity.project_dir(PROJECT), "agy_ok.txt")
    check("the live run really wrote a file", os.path.exists(made))
    if os.path.exists(made):
        print("file contents:", open(made, encoding="utf-8").read().strip()[:100])

shutil.rmtree(os.path.join(antigravity.BASE_DIR, "Let Jarvis Handle It", PROJECT),
              ignore_errors=True)

if FAILED:
    print(f"\n{len(FAILED)} check(s) failed.")
    sys.exit(1)
print("\nAll checks passed.")
