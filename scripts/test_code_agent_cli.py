"""Checks on the coding agent's Claude CLI backend.

The parts worth guarding are the ones that fail quietly: the wrong backend being
picked, a missing backend switching the coding agent off when the other one would
have worked, a CLI failure being reported as a success, and a run landing outside
the project's Deliverables folder. Nothing here spends a token — the `claude`
binary is replaced by a small fake script, except in the optional live check at
the bottom which only runs when you ask for it.

    python scripts/test_code_agent_cli.py          # fast, offline
    python scripts/test_code_agent_cli.py --live   # also runs one real CLI task
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import code_agent

FAILED = []
PROJECT = "Zz Test Code Agent CLI"

# Workspaces go in a throwaway folder, never the real "Let Jarvis Handle It".
code_agent.BASE_DIR = tempfile.mkdtemp(prefix="jarvis_code_cli_test_")

for var in ("JARVIS_CODE_AGENT_BACKEND", "JARVIS_CLAUDE_CLI", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def fake_cli(body):
    """Write an executable stand-in for `claude` and point the agent at it."""
    folder = tempfile.mkdtemp(prefix="jarvis_fake_claude_")
    script = os.path.join(folder, "fake_claude.py")
    with open(script, "w") as f:
        f.write(body)
    if os.name == "nt":
        # Same shape as the claude.cmd shim npm installs on Windows.
        path = os.path.join(folder, "claude.cmd")
        with open(path, "w") as f:
            f.write(f'@"{sys.executable}" "{script}" %*\n')
    else:
        path = os.path.join(folder, "claude")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    os.environ["JARVIS_CLAUDE_CLI"] = path
    return path


def set_sdk(installed, keyed):
    code_agent._sdk_status = (
        (lambda: (True, "sdk ready")) if installed and keyed
        else (lambda: (False, "sdk not ready")))
    code_agent._STATUS_CHECKS["sdk"] = code_agent._sdk_status


# The fake CLI records how it was called, writes a file into its cwd (the
# workspace) and prints the JSON result the real CLI prints.
GOOD = r'''
import json, os, sys
prompt = sys.stdin.read()
with open("hello.py", "w") as f:
    f.write("print('hi')\n")
with open(os.environ["FAKE_LOG"], "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": prompt}, f)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Built hello.py and ran it."}))
'''
LOG = os.path.join(tempfile.mkdtemp(prefix="jarvis_fake_log_"), "call.json")
os.environ["FAKE_LOG"] = LOG

# ---- 1. which backend is picked --------------------------------------------

fake_cli(GOOD)
set_sdk(installed=False, keyed=False)
check("auto with no SDK falls back to the CLI",
      code_agent.selected_backend(refresh=True) == "cli")
check("and that counts as available", code_agent.is_available(refresh=True) is True)
check("and the reason says the SDK was skipped",
      "preferred sdk backend unavailable" in code_agent.describe_availability(refresh=True)[1])

set_sdk(installed=True, keyed=True)
check("auto prefers the SDK when it is ready",
      code_agent.selected_backend(refresh=True) == "sdk")

os.environ["JARVIS_CODE_AGENT_BACKEND"] = "cli"
check("JARVIS_CODE_AGENT_BACKEND=cli picks the CLI even with the SDK ready",
      code_agent.selected_backend(refresh=True) == "cli")

os.environ["JARVIS_CLAUDE_CLI"] = "/nonexistent/claude"
check("cli preferred but missing falls back to the SDK",
      code_agent.selected_backend(refresh=True) == "sdk")

set_sdk(installed=False, keyed=False)
available, reason = code_agent.describe_availability(refresh=True)
check("with neither backend, the coding agent is off", available is False)
check("and the reason names both ways to turn it on",
      "claude-agent-sdk" in reason and "Claude CLI" in reason)
off = code_agent.run_coding_task(PROJECT, "exec_1", "build it")
check("and a call fails honestly rather than hanging",
      off["status"] == "error" and "unavailable" in off["error"])

os.environ["JARVIS_CODE_AGENT_BACKEND"] = "nonsense"
check("an unknown backend name is treated as auto",
      code_agent._backend_preference() == "auto")
os.environ["JARVIS_CODE_AGENT_BACKEND"] = "cli"

# The tool binding reads the same answer.
from agents import tool_executor
fake_cli(GOOD)
code_agent.describe_availability(refresh=True)
check("tool_executor sees the CLI backend as available",
      tool_executor.code_agent_status()[0] is True)

# ---- 2. a successful run ----------------------------------------------------

result = code_agent.run_coding_task(PROJECT, "exec_1", "Write hello.py\nthat prints hi.",
                                    subdirectory="app")
check("a good run reports ok", result["status"] == "ok")
check("it says which backend ran", result.get("backend") == "cli")
check("the CLI's own summary is passed through",
      result.get("summary") == "Built hello.py and ran it.")
check("the file it wrote is reported", result.get("files_changed") == ["hello.py"])

with open(LOG) as f:
    call = json.load(f)
expected_ws = os.path.join(code_agent.BASE_DIR, "Let Jarvis Handle It", PROJECT, "Deliverables", "app")
check("it ran inside the project's Deliverables subfolder",
      os.path.realpath(call["cwd"]) == os.path.realpath(expected_ws))
argv = call["argv"]
check("it runs headless with -p", "-p" in argv)
check("with JSON output", argv[argv.index("--output-format") + 1] == "json")
check("with edits accepted", argv[argv.index("--permission-mode") + 1] == "acceptEdits")
tools = argv[argv.index("--allowedTools") + 1].split(",")
check("with Read, Edit, Write and Bash allowed", {"Read", "Edit", "Write", "Bash"} <= set(tools))
check("the whole multi-line task arrives on stdin, not argv",
      "Write hello.py\nthat prints hi." in call["stdin"] and not any("hello.py" in a for a in argv))
check("along with the coding agent's instructions",
      "coding agent for the Jarvis" in call["stdin"])

events = []
code_agent.run_coding_task(PROJECT, "exec_1", "again", event_logger=events.append)
check("progress narratives name the backend",
      any("(cli)" in e["data"]["message"] for e in events))

# ---- 3. failures are failures ----------------------------------------------

fake_cli(r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                  "result": "Ran out of turns"}))
sys.exit(1)
''')
bad = code_agent.run_coding_task(PROJECT, "exec_1", "build it")
check("an is_error result is reported as an error",
      bad["status"] == "error" and "Ran out of turns" in bad["error"])

fake_cli(r'''
import sys
sys.stdin.read()
sys.stderr.write("Invalid API key - please run /login\n")
sys.exit(1)
''')
crash = code_agent.run_coding_task(PROJECT, "exec_1", "build it")
check("a CLI crash surfaces its stderr",
      crash["status"] == "error" and "please run /login" in crash["error"])

fake_cli(r'''
import json, sys
sys.stdin.read()
print("warning: something noisy")
print(json.dumps({"type": "result", "is_error": False, "result": "done"}))
''')
noisy = code_agent.run_coding_task(PROJECT, "exec_1", "build it")
check("a warning line before the JSON doesn't break parsing",
      noisy["status"] == "ok" and noisy.get("summary") == "done")

fake_cli(r'''
import sys, time
sys.stdin.read()
open("partial.py", "w").write("x = 1\n")
time.sleep(30)
''')
slow = code_agent.run_coding_task(PROJECT, "exec_1", "build it", subdirectory="slow", timeout=2)
check("a run past its time limit is stopped and reported",
      slow["status"] == "error" and "time limit" in slow["error"])
check("and files written before the timeout are still reported",
      slow.get("files_changed") == ["partial.py"] and "note" in slow)

escape = code_agent.run_coding_task(PROJECT, "exec_1", "build it", subdirectory="../../outside")
check("a subdirectory escaping Deliverables is refused",
      escape["status"] == "error" and "escapes" in escape["error"])

# ---- 4. optional live run ---------------------------------------------------

if "--live" in sys.argv:
    os.environ.pop("JARVIS_CLAUDE_CLI", None)
    if code_agent.selected_backend(refresh=True) != "cli":
        check("live: the real Claude CLI is on PATH", False)
    else:
        live = code_agent.run_coding_task(
            PROJECT, "exec_live",
            "Create add.py with a function add(a, b) returning a + b, and run "
            "`python add.py` with a quick assert to prove it works.",
            subdirectory="live", timeout=300)
        print(json.dumps(live, indent=2)[:2000])
        check("live: the real CLI built something", live["status"] == "ok"
              and "add.py" in live.get("files_changed", []))

shutil.rmtree(code_agent.BASE_DIR, ignore_errors=True)

print()
print(f"{len(FAILED)} failed" if FAILED else "all passed")
sys.exit(1 if FAILED else 0)
