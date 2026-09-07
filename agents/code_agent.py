"""
Code Agent — delegates real software work to the Claude Agent SDK.

Why this exists: execution agents can already write files (agents/tool_executor.py),
but writing a file is not the same as building working software. Producing code that
runs takes a loop — write it, execute it, read the traceback, fix it, run it again —
and Gemini function-calling inside execution_agent.py caps out long before that loop
finishes. The Claude Agent SDK is that loop, packaged as a library, so this module
hands a coding task to it and reports back what actually landed on disk.

ENTIRELY OPTIONAL. Jarvis runs exactly as before without it. The SDK is not in the
base requirements and no key is assumed:

  - `claude-agent-sdk` not installed  -> is_available() is False
  - ANTHROPIC_API_KEY not set         -> is_available() is False

In either case nothing raises, nothing is bound, and the pipeline continues on the
same path it took before this module existed. When it IS available, the Brain is told
so (agents/brain.py) and execution agents get a real `code_project` tool.

Scope is deliberately narrow: this is wired into the multi-agent PIPELINE only.
The voice/chat path in coordinator.py never touches it.
"""
import asyncio
import concurrent.futures
import os
import time

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How long a single coding task may run before we give up on it, in seconds.
# A real build loop is slow — minutes, not seconds — so this is generous by
# default and overridable for people who want a tighter leash.
DEFAULT_TIMEOUT = int(os.getenv("JARVIS_CODE_AGENT_TIMEOUT", "900"))

# The model the coding agent runs on. Left unset by default so the SDK picks
# its own default rather than us pinning a version that ages badly.
CODE_AGENT_MODEL = os.getenv("JARVIS_CODE_AGENT_MODEL") or None

_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# The full transcript of a coding run is long, and it is fed straight back to the
# calling execution agent as a function response. Cap it so one coding task can't
# blow that agent's context window. The tail is what's kept — the agent's own
# closing summary of what it built lives at the end.
MAX_SUMMARY_CHARS = 8000
MAX_FILES_REPORTED = 200

_availability_cache: dict | None = None


# ---------------------------------------------------------------------------
# Availability — never raises, never required
# ---------------------------------------------------------------------------

def describe_availability(refresh: bool = False) -> tuple[bool, str]:
    """
    (available, human_readable_reason).

    Cached, because this is consulted on every plan and every execution agent's
    tool binding, and the answer only changes when the user installs the SDK or
    edits .env — both of which mean an app restart anyway. Pass refresh=True to
    re-check within a single run.
    """
    global _availability_cache
    if _availability_cache is not None and not refresh:
        return _availability_cache["available"], _availability_cache["reason"]

    available, reason = False, ""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        reason = (
            "the claude-agent-sdk package is not installed "
            "(optional — `pip install claude-agent-sdk` to enable real coding tasks)"
        )
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            reason = (
                "claude-agent-sdk is installed but ANTHROPIC_API_KEY is not set in .env "
                "(optional — add a key from the Anthropic Console to enable real coding tasks)"
            )
        else:
            available, reason = True, "claude-agent-sdk installed and ANTHROPIC_API_KEY present"

    _availability_cache = {"available": available, "reason": reason}
    return available, reason


def is_available(refresh: bool = False) -> bool:
    return describe_availability(refresh=refresh)[0]


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def _workspace_dir(project_name: str, subdirectory: str | None = None) -> str:
    """
    Resolve the directory the coding agent is allowed to work in: the project's
    own Deliverables folder, optionally a subfolder of it. Same root the
    always-on write_file tool uses, so code the agent produces shows up in
    list_deliverables and in the UI alongside every other artifact.

    Traversal is blocked here rather than trusted to the agent — the agent
    decides what to build, this decides where it is allowed to build it.
    """
    root = os.path.abspath(os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Deliverables"))
    target = root
    if subdirectory:
        candidate = os.path.abspath(os.path.join(root, subdirectory.lstrip("/\\")))
        if not (candidate == root or candidate.startswith(root + os.sep)):
            raise ValueError(f"Subdirectory '{subdirectory}' escapes the project's Deliverables directory.")
        target = candidate
    os.makedirs(target, exist_ok=True)
    return target


def _snapshot(root: str) -> dict[str, float]:
    """path -> mtime, for every file under root. Diffed after the run so we can
    report what the agent actually touched instead of taking its word for it."""
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__", ".venv", "venv")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                seen[p] = os.path.getmtime(p)
            except OSError:
                continue
    return seen


def _changed_files(before: dict[str, float], after: dict[str, float], root: str) -> list[str]:
    changed = [p for p, m in after.items() if before.get(p) != m]
    return sorted(os.path.relpath(p, root).replace(os.sep, "/") for p in changed)


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the coding agent for the Jarvis multi-agent system.

You have been handed one concrete software task by an execution agent that cannot
write working code itself. Build it for real in the working directory:

- Write the actual files. Run them. Read the errors. Fix them. Repeat until they work.
- Verify before you report. A script you never executed is not finished work.
- Stay inside the working directory. Do not touch anything outside it.
- Do not install system-wide packages or run destructive commands.

When you are done, end with a short plain-text summary: what you built, which files
it lives in, how to run it, and anything you could NOT get working. Never claim
something works if you did not see it work — a stated limitation is far more useful
to the pipeline than a false success.
"""


def _extract_text(message) -> str:
    """Pull display text out of an SDK message without depending on its concrete
    type — the SDK's message classes are richer than we need, and defensive
    duck-typing here keeps a shape change from taking the pipeline down."""
    try:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [getattr(b, "text", "") for b in content]
            return "".join(p for p in parts if isinstance(p, str))
        result = getattr(message, "result", None)
        if isinstance(result, str):
            return result
    except Exception:
        pass
    return ""


async def _run_query(task: str, workspace: str, event_logger, agent_id: str) -> str:
    from claude_agent_sdk import query, ClaudeAgentOptions

    option_kwargs = dict(
        system_prompt=SYSTEM_PROMPT,
        cwd=workspace,
        allowed_tools=list(_TOOLS),
        permission_mode="acceptEdits",
    )
    if CODE_AGENT_MODEL:
        option_kwargs["model"] = CODE_AGENT_MODEL

    options = ClaudeAgentOptions(**option_kwargs)

    transcript = []
    async for message in query(prompt=task, options=options):
        text = _extract_text(message)
        if not text.strip():
            continue
        transcript.append(text)
        if event_logger:
            # Same narrative channel every other agent reports on, so coding
            # progress streams into execution.html / agent_talk.task_log.html
            # live rather than landing as one silent blob at the end.
            event_logger({
                "event_type": "narrative",
                "source": agent_id,
                "data": {
                    "phase": "execution",
                    "message": text[:600],
                    "icon": "💻",
                },
            })
    return "\n".join(transcript).strip()


def run_coding_task(
    project_name: str,
    agent_id: str,
    task: str,
    subdirectory: str | None = None,
    event_logger=None,
    timeout: int | None = None,
) -> dict:
    """
    Run one coding task to completion and report what changed on disk.

    Synchronous by design: execution_agent.py calls its tools synchronously from
    inside an already-running event loop, so the SDK's async work happens on a
    dedicated thread with its own loop. Calling asyncio.run() inline would raise.

    Always returns a dict — an unavailable SDK, a timeout, or a crash inside the
    coding agent all come back as {"status": "error", ...} and leave the calling
    pipeline free to carry on.
    """
    available, reason = describe_availability()
    if not available:
        return {"status": "error", "action": "code_project", "error": f"Coding agent unavailable: {reason}"}

    if not (task or "").strip():
        return {"status": "error", "action": "code_project", "error": "No task description given to the coding agent."}

    try:
        workspace = _workspace_dir(project_name, subdirectory)
    except ValueError as e:
        return {"status": "error", "action": "code_project", "error": str(e)}

    before = _snapshot(workspace)
    started = time.time()

    if event_logger:
        event_logger({
            "event_type": "narrative",
            "source": agent_id,
            "data": {
                "phase": "execution",
                "message": f"Handing a coding task to the Claude coding agent in {os.path.relpath(workspace, BASE_DIR)}...",
                "icon": "💻",
            },
        })

    def _worker() -> str:
        return asyncio.run(
            asyncio.wait_for(
                _run_query(task, workspace, event_logger, agent_id),
                timeout=timeout or DEFAULT_TIMEOUT,
            )
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            summary = pool.submit(_worker).result()
        status, error = "ok", None
    except asyncio.TimeoutError:
        summary, status = "", "error"
        error = f"Coding agent exceeded its {timeout or DEFAULT_TIMEOUT}s time limit."
    except Exception as e:
        summary, status = "", "error"
        error = f"Coding agent failed: {e}"

    # Files the agent wrote before timing out or crashing are real and already on
    # disk — report them either way rather than losing the partial work.
    files = _changed_files(before, _snapshot(workspace), workspace)
    total_files = len(files)
    if total_files > MAX_FILES_REPORTED:
        files = files[:MAX_FILES_REPORTED]
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = "[...earlier progress truncated...]\n" + summary[-MAX_SUMMARY_CHARS:]
    rel_workspace = os.path.relpath(workspace, BASE_DIR).replace(os.sep, "/")

    result = {
        "status": status,
        "action": "code_project",
        "path": rel_workspace,
        "files_changed": files,
        "duration_seconds": round(time.time() - started, 1),
    }
    if total_files > MAX_FILES_REPORTED:
        result["files_changed_total"] = total_files
    if summary:
        result["summary"] = summary
    if error:
        result["error"] = error
        if files:
            result["note"] = "Some files were written before the failure — inspect them before retrying."

    if event_logger:
        # Closing narrative only — the caller (execution_agent.py) emits the
        # tool_result event for this call, and emitting it here too would show
        # the same result twice in the task log.
        done = f"Coding agent finished: {total_files} file(s) changed." if status == "ok" else f"Coding agent failed: {error}"
        event_logger({
            "event_type": "narrative",
            "source": agent_id,
            "data": {"phase": "execution", "message": done, "icon": "💻"},
        })

    return result
