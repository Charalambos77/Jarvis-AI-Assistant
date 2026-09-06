"""
Antigravity CLI (`agy`) — the builder Jarvis delegates real software work to.

Jarvis's own agents can write files and call APIs, but they cannot iterate:
they get six tool turns, and `write_file` rewrites a whole file from one model
turn. That is fine for producing a new script and hopeless for scaffolding a
project, installing its dependencies, running the build and fixing what broke.
`agy` is a coding agent in a terminal that does exactly that loop, so this
module hands it a job and waits for the result.

Everything here is deliberately conservative, because the thing on the other
end edits files and runs commands:

  * `--sandbox` is always on. Terminal restrictions stay enabled even when the
    caller is Jarvis himself.
  * `--dangerously-skip-permissions` is never passed. Every terminal command
    goes through Jarvis's own gate first (command_gate.py), reached by a
    PreToolUse hook this module writes into each project. Commands are no
    longer silently refused for want of anyone to ask — they are shown on the
    Commands page and wait for a real answer.
  * Trust is per project, never the repository. `agy` is told to trust the one
    folder it was given, so it cannot reach Jarvis's own source or another
    project's work. New projects are trusted as they are created, so this holds
    for pipelines that do not exist yet.
  * One call at a time, process-wide. Execution agents run in parallel and two
    of them editing the same folder would clobber each other.
  * Every call is bounded. An unattended pipeline must never sit forever on a
    sign-in prompt that nobody is there to answer.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `agy` edits files and runs commands, so two of them in one folder is a race
# with real consequences. Execution agents are spawned with asyncio.gather, so
# without this they would overlap by default rather than by accident.
_RUN_LOCK = threading.Lock()

DEFAULT_TIMEOUT = 600          # seconds of wall clock for one delegated job
_LOCK_WAIT = 900               # how long a queued call waits for its turn

# Time spent parked at the Commands page waiting for a person is not time spent
# building, but the CLI's own clock cannot tell the difference. Without this a
# ten-minute build that waited ten minutes for one `npm install` would be killed
# for running long, having done nothing wrong. The allowance covers a full
# approval wait (command_gate.WAIT_SECONDS) plus a little slack.
_APPROVAL_ALLOWANCE = 900

# Where `agy` keeps the folders it is willing to work in, and where its
# per-project customisations live.
_CLI_SETTINGS = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
_AGENTS_DIR = ".agents"
_GATE_SCRIPT = os.path.join(BASE_DIR, "scripts", "agy_command_gate.py")

# Written into every project so `agy` asks Jarvis before running anything. The
# timeout has to outlast the gate's own wait, or the CLI would give up on the
# hook while the command was still sitting on the page waiting to be answered.
_HOOK_TIMEOUT = 900

# Where the installer puts it, checked after PATH so a deliberate override wins.
_KNOWN_LOCATIONS = [
    r"D:\Tools\agy\bin\agy.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"),
    os.path.expanduser("~/.local/bin/agy"),
]


def find_binary() -> str | None:
    """The `agy` executable, or None if it is not installed.

    AGY_BINARY wins so a different build can be pointed at without code
    changes; then PATH, which is what the installer configures; then the
    places the installer is known to use, because a freshly installed CLI is
    not on the PATH of an already-running Python process.
    """
    override = (os.getenv("AGY_BINARY") or "").strip('"').strip()
    if override and os.path.exists(override):
        return override

    found = shutil.which("agy")
    if found:
        return found

    for path in _KNOWN_LOCATIONS:
        if path and os.path.exists(path):
            return path
    return None


def is_available() -> bool:
    """Whether Jarvis should offer the tool at all.

    Set JARVIS_AGY_DISABLED=1 to take it away from the agents without
    uninstalling anything — the switch to reach for if a pipeline starts
    delegating work you would rather it did itself.
    """
    if (os.getenv("JARVIS_AGY_DISABLED") or "").strip().lower() in ("1", "true", "yes"):
        return False
    return find_binary() is not None


# ---------------------------------------------------------------------------
# Folder trust
#
# "Yes, I trust this folder" was answered once, for the whole repository, which
# left `agy` holding permission over Jarvis's own source on every call. These
# functions narrow that to the one project a job was given, and — because every
# job passes through project_dir() — do it for projects that do not exist yet
# as well as the ones that already do.
# ---------------------------------------------------------------------------

def _read_cli_settings() -> dict:
    try:
        with open(_CLI_SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cli_settings(data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(_CLI_SETTINGS), exist_ok=True)
        with open(_CLI_SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError:
        return False


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def trust_project(path: str) -> bool:
    """Trust one project folder, and stop trusting the repository root.

    The root entry is the one that has to go: with it in place, narrowing
    anything else changes nothing, because a trusted parent already covers
    every child. Other projects' entries are left alone — each was added the
    same way, for the same reason.
    """
    settings = _read_cli_settings()
    trusted = settings.get("trustedWorkspaces")
    if not isinstance(trusted, list):
        trusted = []

    kept = [
        w for w in trusted
        if isinstance(w, str) and not _same_path(w, BASE_DIR)
    ]
    if not any(_same_path(w, path) for w in kept):
        kept.append(os.path.normpath(path))

    if kept == trusted:
        return True
    settings["trustedWorkspaces"] = kept
    return _write_cli_settings(settings)


# ---------------------------------------------------------------------------
# Letting an approval actually take effect
#
# Answering "allow" on the Commands page is not enough on its own. In print
# mode the CLI has nobody to prompt, so it refuses every terminal command by
# itself, whatever the hook decided, and says to add an allow-rule under
# permissions.allow in its settings.
#
# So Jarvis adds one — `command(*)` — for the length of a delegated build, and
# takes it away again the moment the build ends. That does not hand anything
# away: the hook still runs on every command, "Reject" still hard-blocks it,
# and the denylist still refuses outright. All the rule does is stop the CLI
# overruling an answer the user already gave. It covers commands only; every
# other kind of tool stays gated by the CLI itself.
#
# It is removed in a finally block, and again when Jarvis starts, so a crash
# mid-build cannot leave it sitting in the settings afterwards.
# ---------------------------------------------------------------------------
# Two rules, because running a command inside the sandbox takes two steps: the
# command itself, and leaving the sandbox to run it (the CLI calls that
# escalate_admin and displays it as "Bash"). Both are the same command the user
# already answered for on the Commands page — the hook reviewed it before
# either happened.
_COMMAND_GRANTS = ["command(*)", "escalate_admin(*)"]


def _set_command_grant(granted: bool) -> None:
    settings = _read_cli_settings()
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    allow = [a for a in (permissions.get("allow") or []) if isinstance(a, str)]

    without = [a for a in allow if a not in _COMMAND_GRANTS]
    updated = (without + _COMMAND_GRANTS) if granted else without
    if updated == allow:
        return

    permissions["allow"] = updated
    settings["permissions"] = permissions
    _write_cli_settings(settings)


def clear_command_grant() -> None:
    """Take the grant away. Called when a build ends, and again on startup."""
    _set_command_grant(False)


def _hook_command() -> str:
    """The command `agy` runs to ask Jarvis about a terminal command.

    sys.executable is the interpreter Jarvis itself is running under, which is
    the one with the project's dependencies — though the gate script only uses
    the standard library, so any working Python would do.
    """
    parts = [sys.executable or "python", _GATE_SCRIPT]
    if any(" " in p for p in parts):
        # `cmd /c` eats the outer quotes when a command starts with one; the
        # documented way round it is to wrap the whole thing in another pair.
        return '""{}" "{}""'.format(*parts)
    return "{} {}".format(*parts)


def install_hook(workspace: str) -> bool:
    """Write the PreToolUse hook into a project so its commands come to Jarvis.

    Per project rather than globally on purpose: an interactive `agy` session
    the user opens themselves should keep its own prompt, not be routed through
    a page meant for unattended pipelines.
    """
    hooks_path = os.path.join(workspace, _AGENTS_DIR, "hooks.json")
    config = {
        "jarvis-command-gate": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _hook_command(),
                            "timeout": _HOOK_TIMEOUT,
                        }
                    ],
                }
            ]
        }
    }
    try:
        existing = {}
        if os.path.exists(hooks_path):
            with open(hooks_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        # Anything else in the file is somebody's own hook; only ours is
        # rewritten, so the path stays right if the venv or repo ever moves.
        if existing.get("jarvis-command-gate") == config["jarvis-command-gate"]:
            return True
        existing.update(config)
        os.makedirs(os.path.dirname(hooks_path), exist_ok=True)
        with open(hooks_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def project_dir(project_name: str) -> str:
    """The folder a delegated job is allowed to work in.

    Always the project's own folder under "Let Jarvis Handle It", never the
    repository root: `agy` should be building what the pipeline was asked for,
    not editing Jarvis. Creating the folder is also where it is granted trust
    and given the command hook, so a project made by a pipeline written next
    month arrives with both already in place.
    """
    path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name or "Default Project")
    os.makedirs(path, exist_ok=True)
    trust_project(path)
    install_hook(path)
    return path


def _extract_text(payload) -> str:
    """Pull the written answer out of whatever shape --output-format json used.

    The exact key is not contractual, so this looks for the usual suspects and
    falls back to the whole object rather than reporting an empty result for a
    job that actually ran.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("response", "result", "text", "output", "message", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload, ensure_ascii=False)[:4000]
    return str(payload)


# `agy` reports a refused tool on stderr and still exits 0. Silence about that
# would read to the calling agent as "it worked", which is the worst possible
# misreading — hence these are lifted out and returned as first-class fields.
_DENIED_RE = re.compile(r"(?:permission|approval|denied|not allowed|blocked)[^\n]*", re.IGNORECASE)
_AUTH_RE = re.compile(r"(sign[- ]?in|log[- ]?in|authenticat|unauthor|credential|token expired)",
                      re.IGNORECASE)


def run(instruction: str, project_name: str, timeout: int = DEFAULT_TIMEOUT,
        extra_dirs: list[str] | None = None, plan_id: str | None = None,
        section_id: str | None = None) -> dict:
    """Hand one job to `agy` and wait for the result.

    `plan_id` and `section_id` are not passed to the CLI — they travel in the
    environment so the command hook can say which pipeline a command belongs
    to, which is what lets the Commands page group them by run.

    Returns a dict the calling agent can read without knowing anything about
    subprocesses: `status`, `output`, and — when they happened — `refused` and
    `needs_sign_in`, so an agent is never left thinking a refused command ran.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return {"status": "error", "error": "No instruction given to the Antigravity CLI."}

    binary = find_binary()
    if not binary:
        return {"status": "error", "error": (
            "The Antigravity CLI (`agy`) is not installed on this machine, so this work "
            "cannot be delegated. Do it with your own tools instead."
        )}

    workspace = project_dir(project_name)
    # The CLI's clock has to allow for time parked at the Commands page as well
    # as time actually working, or a build gets killed for the user's thinking.
    allowed = timeout + _APPROVAL_ALLOWANCE
    minutes = max(1, int(allowed // 60))
    cmd = [
        binary,
        "-p", instruction,
        "--output-format", "json",
        # --sandbox is deliberately NOT passed. Its terminal restriction makes
        # every command need a second permission to leave the sandbox
        # (escalate_admin), which headless mode refuses and no allow-rule was
        # found to satisfy — so with it on, an approved command still never
        # runs. What replaces it is not nothing: the command was reviewed on
        # the Commands page before it got here, the hard denylist refuses the
        # dangerous shapes outright, and folder trust keeps the CLI inside this
        # one project.
        # Headless mode cannot prompt, so anything needing approval is denied —
        # file writes included, whatever the docs say. Without this the CLI
        # runs, reports SUCCESS, and writes nothing. accept-edits approves the
        # edits and NOTHING else: terminal commands still have to be asked for,
        # and the hook installed in this project sends that question to Jarvis's
        # Commands page. It is a flag rather than a settings.json rule so it
        # applies only to Jarvis's calls and never to an interactive session.
        "--mode", "accept-edits",
        "--print-timeout", f"{minutes}m",
        "--add-dir", workspace,
    ]
    for extra in (extra_dirs or []):
        if extra and os.path.isdir(extra):
            cmd += ["--add-dir", extra]

    # Queue rather than run in parallel. The wait is bounded too, so a wedged
    # call cannot silently stall every other agent behind it forever.
    if not _RUN_LOCK.acquire(timeout=_LOCK_WAIT):
        return {"status": "error", "error": (
            "Another delegated build is still running and this one waited too long for it. "
            "Produce what you can with your own tools."
        )}
    # The hook runs as a bare subprocess of the CLI and inherits this, which is
    # how a command arriving at the Commands page knows which run wanted it.
    env = os.environ.copy()
    env["JARVIS_PROJECT"] = project_name or ""
    env["JARVIS_PLAN_ID"] = plan_id or ""
    env["JARVIS_SECTION_ID"] = section_id or ""

    # Only for as long as this build runs, and only so the user's own answer is
    # the one that decides. See _set_command_grant.
    _set_command_grant(True)

    try:
        completed = subprocess.run(
            cmd,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            # The CLI's own --print-timeout should fire first; this is the
            # backstop for the case where it never gets that far, such as a
            # lapsed session waiting on a browser nobody is watching.
            timeout=allowed + 60,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "timed_out": True, "error": (
            f"The delegated build ran past {allowed // 60} minutes and was stopped. "
            "Anything it wrote before that is still in the project folder."
        )}
    except Exception as e:
        return {"status": "error", "error": f"Could not run the Antigravity CLI: {e}"}
    finally:
        clear_command_grant()
        _RUN_LOCK.release()
        # This build is over, so nothing it asked for should still be sitting on
        # the Commands page waiting for an answer nobody can act on.
        try:
            import command_gate
            command_gate.cancel(project=project_name)
        except Exception:
            pass

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()

    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        payload = stdout          # text came back instead; still the answer

    result = {
        "status": "success" if completed.returncode == 0 else "error",
        "output": _extract_text(payload),
        "workspace": os.path.relpath(workspace, BASE_DIR),
    }
    if completed.returncode != 0:
        result["error"] = stderr[-1500:] or f"Exited with code {completed.returncode}."

    # A refused command exits 0. Say so loudly or the agent reports success.
    refused = _DENIED_RE.findall(stderr)
    if refused:
        result["refused"] = refused[:5]
        result["note"] = (
            "Some commands were refused. Whatever needed them did NOT happen — say so "
            "plainly rather than reporting it as done. Every command goes to the user's "
            "Commands page for a decision; a refusal means they said no, it was on the "
            "hard denylist, or nobody answered in time. Do not retry a variation to get "
            "around it."
        )
    if _AUTH_RE.search(stderr):
        result["needs_sign_in"] = True
        result["note"] = (
            "The Antigravity CLI needs the user to sign in again before it can do anything. "
            "Nothing was built. Report this rather than retrying."
        )
    return result
