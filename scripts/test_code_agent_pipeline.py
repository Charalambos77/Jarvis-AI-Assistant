"""Checks the coding agent from the pipeline's side: an execution agent that is
given `code_project` in its brief gets the tool, calls it, and reports what it
built — and while it runs, the other execution agents keep working.

Gemini is replaced by a scripted fake chat, and the Claude CLI by a small fake
script, so nothing here spends a token unless you ask for the live run.

    python scripts/test_code_agent_pipeline.py          # fast, offline
    python scripts/test_code_agent_pipeline.py --live   # real Claude CLI build
"""
import asyncio
import os
import shutil
import stat
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import code_agent, tool_executor, execution_agent

FAILED = []
PROJECT = "Zz Test Code Agent Pipeline"
LIVE = "--live" in sys.argv

for var in ("JARVIS_CODE_AGENT_BACKEND", "JARVIS_CLAUDE_CLI", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)
os.environ["JARVIS_CODE_AGENT_BACKEND"] = "cli"

# Both the coding agent and write_file build under BASE_DIR; keep that out of
# the real "Let Jarvis Handle It" folder.
TMP_BASE = tempfile.mkdtemp(prefix="jarvis_code_pipeline_test_")
code_agent.BASE_DIR = TMP_BASE
tool_executor.BASE_DIR = TMP_BASE


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def fake_cli(body):
    folder = tempfile.mkdtemp(prefix="jarvis_fake_claude_")
    script = os.path.join(folder, "fake_claude.py")
    with open(script, "w") as f:
        f.write(body)
    if os.name == "nt":
        path = os.path.join(folder, "claude.cmd")
        with open(path, "w") as f:
            f.write(f'@"{sys.executable}" "{script}" %*\n')
    else:
        path = os.path.join(folder, "claude")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    os.environ["JARVIS_CLAUDE_CLI"] = path


# ---- a scripted stand-in for the Gemini chat --------------------------------

class FakeChat:
    """Replays one agent's turns: each turn is either a tool call or final text."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.tool_responses = []

    def send_message(self, message):
        if isinstance(message, list):
            self.tool_responses.append(message)
        kind, payload = self.turns.pop(0)
        if kind == "call":
            name, args = payload
            return SimpleNamespace(function_calls=[SimpleNamespace(name=name, args=args)], text=None)
        return SimpleNamespace(function_calls=None, text=payload)


SCRIPTS = {}
CONFIGS = {}


class FakeClient:
    def __init__(self, api_key=None):
        self.chats = self

    def create(self, model, config):
        CONFIGS[len(CONFIGS)] = config
        for agent_id, chat in SCRIPTS.items():
            if agent_id in config.system_instruction and not getattr(chat, "_used", False):
                chat._used = True
                return chat
        raise AssertionError("no scripted chat for this agent")


execution_agent.genai = SimpleNamespace(Client=FakeClient)

FINAL = '{"status": "ok", "summary": "done"}'


def agent_cfg(agent_id, tools):
    return {"agent_id": agent_id, "role": "Builder", "brief": "Build the thing.",
            "output_spec": {"required_keys": ["summary"]}, "tools_needed": tools}


# ---- 1. the tool is offered only when a backend is usable -------------------

fake_cli("import sys\nsys.stdin.read()\n")
code_agent.describe_availability(refresh=True)
decls, _, unavailable = tool_executor.get_tools_for_execution_agent(["code_project"], PROJECT)
check("with the CLI present, an agent asking for code_project gets it",
      "code_project" in [d["name"] for d in decls] and not unavailable)

os.environ["JARVIS_CLAUDE_CLI"] = os.path.join(TMP_BASE, "no-such-claude")
code_agent._STATUS_CHECKS["sdk"] = lambda: (False, "the claude-agent-sdk package is not installed")
code_agent.describe_availability(refresh=True)
decls, _, unavailable = tool_executor.get_tools_for_execution_agent(["code_project"], PROJECT)
check("with no CLI and no SDK, code_project is not offered",
      "code_project" not in [d["name"] for d in decls])
check("and the agent is told why, and to fall back to write_file",
      len(unavailable) == 1 and "not found on PATH" in unavailable[0]
      and "write_file" in unavailable[0])
check("the Brain is not told about a coding agent it can't use",
      "REAL CODING AGENT" not in __import__("agents.brain", fromlist=["x"]).get_brain_system_prompt())

# ---- 2. an execution agent builds through the CLI, others keep running -----

fake_cli(r'''
import json, sys, time
sys.stdin.read()
open("app.py", "w").write("print('ok')\n")
time.sleep(3)
print(json.dumps({"type": "result", "is_error": False, "result": "Built app.py and ran it."}))
''')
code_agent.describe_availability(refresh=True)
check("the Brain is told about the coding agent once the CLI is there",
      "REAL CODING AGENT" in __import__("agents.brain", fromlist=["x"]).get_brain_system_prompt())

SCRIPTS["coder_1"] = FakeChat([
    ("call", ("code_project", {"task": "Build app.py that prints ok", "subdirectory": "app"})),
    ("text", FINAL),
])
SCRIPTS["writer_1"] = FakeChat([
    ("call", ("write_file", {"relative_path": "notes.md", "content": "hello"})),
    ("text", FINAL),
])

finished = {}
events = []


async def timed(cfg):
    r = await execution_agent.run_execution_agent(cfg, {}, event_logger=events.append,
                                                  project_name=PROJECT)
    finished[cfg["agent_id"]] = time.time()
    return r


async def main():
    return await asyncio.gather(timed(agent_cfg("coder_1", ["code_project"])),
                                timed(agent_cfg("writer_1", [])))

start = time.time()
coder, writer = asyncio.run(main())

check("the coding agent's execution agent finishes ok", coder.get("status") == "ok")
check("its built folder is reported as an artifact",
      any(a.get("tool") == "code_project" for a in coder.get("artifacts", [])))
tool_result = next((e for e in events if e["event_type"] == "tool_result"
                    and e["data"]["tool"] == "code_project"), None)
check("the tool result reaches the task log with the files it built",
      tool_result is not None and tool_result["data"]["result"].get("files_changed") == ["app.py"])
check("and says it ran on the CLI backend",
      tool_result is not None and tool_result["data"]["result"].get("backend") == "cli")
check("the Gemini agent is handed the CLI's summary",
      any("Built app.py" in str(part.function_response.response.get("summary"))
          for turn in SCRIPTS["coder_1"].tool_responses for part in turn))
check("the other execution agent is not held up by the coding task",
      writer.get("status") == "ok" and finished["writer_1"] - start < 2.5)

# ---- 3. optional live run through the real CLI ------------------------------

if LIVE:
    os.environ.pop("JARVIS_CLAUDE_CLI", None)
    code_agent.describe_availability(refresh=True)
    if code_agent.selected_backend() != "cli":
        check("live: the real Claude CLI is on PATH", False)
    else:
        SCRIPTS.clear()
        SCRIPTS["coder_live"] = FakeChat([
            ("call", ("code_project", {
                "task": "Create fizzbuzz.py printing FizzBuzz for 1..15, and run it to confirm "
                        "the output is correct.",
                "subdirectory": "live"})),
            ("text", FINAL),
        ])
        events.clear()
        live = asyncio.run(execution_agent.run_execution_agent(
            agent_cfg("coder_live", ["code_project"]), {}, event_logger=events.append,
            project_name=PROJECT))
        res = next((e["data"]["result"] for e in events if e["event_type"] == "tool_result"), {})
        print({k: res.get(k) for k in ("status", "backend", "files_changed", "duration_seconds")})
        print((res.get("summary") or res.get("error") or "")[:600])
        check("live: the real CLI built fizzbuzz.py through an execution agent",
              live.get("status") == "ok" and res.get("status") == "ok"
              and "fizzbuzz.py" in res.get("files_changed", []))

shutil.rmtree(TMP_BASE, ignore_errors=True)

print()
print(f"{len(FAILED)} failed" if FAILED else "all passed")
sys.exit(1 if FAILED else 0)
