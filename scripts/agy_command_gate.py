"""
The bridge between the Antigravity CLI's permission prompt and Jarvis.

`agy` runs this script before every terminal command it wants to execute (a
PreToolUse hook, wired up per project by connectors/antigravity.py). The
command line arrives as JSON on stdin; a decision has to go back as JSON on
stdout:

    {"decision": "allow" | "deny", "reason": "..."}

All this file does is carry the question to the running Jarvis and carry the
answer back. The actual policy — the hard denylist, the "always allow" rules,
the waiting room the Commands page reads — lives in command_gate.py, so it can
be changed without touching anything `agy` is configured with.

It deliberately has no third-party imports: it runs as a bare subprocess of the
CLI, not inside Jarvis, and must work whatever interpreter picks it up.

Fails closed. If Jarvis is not running, or answers with anything unexpected, the
command is denied and `agy` is told why — a build that could not ask is a build
that must not run commands.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PORT = os.getenv("JARVIS_PORT", "5000")
ENDPOINT = os.getenv("JARVIS_GATE_URL", f"http://127.0.0.1:{PORT}/commands/ask")

# Longer than the gate's own wait (10 minutes), so the timeout that decides the
# outcome is always Jarvis's — the one that logs the command as timed out and
# tells the agent it was denied. This one is only a backstop.
REQUEST_TIMEOUT = int(os.getenv("JARVIS_GATE_HTTP_TIMEOUT", "900"))


def respond(decision: str, reason: str, overrides=None) -> None:
    """Hand the decision back to the CLI and stop.

    `permissionOverrides` is what makes an approval actually take effect.
    Saying "allow" is not enough on its own: headless mode has nobody to
    prompt, so it auto-denies a terminal command anyway and tells you to add an
    allow-rule of the form `command(<target>)`. A hook may hand those back as a
    temporary grant, so Jarvis sends one for exactly the command that was just
    approved — nothing wider, and only for this call.
    """
    answer = {"decision": decision, "reason": reason}
    if overrides:
        answer["permissionOverrides"] = overrides
    json.dump(answer, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(0)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        respond("deny", "Jarvis could not read what command was being requested.")

    tool_call = payload.get("toolCall") or {}
    args = tool_call.get("args") or {}
    # protojson gives CommandLine; be forgiving about the exact spelling so a
    # rename upstream degrades into a review rather than into silent approval.
    command = ""
    for key in ("CommandLine", "commandLine", "command", "Command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            command = value.strip()
            break

    if not command:
        respond("deny", "Jarvis could not tell which command this was, so it was refused.")

    workspaces = payload.get("workspacePaths") or []
    request = {
        "command": command,
        "tool": tool_call.get("name") or "run_command",
        "conversation_id": payload.get("conversationId") or "",
        "cwd": workspaces[0] if workspaces else os.getcwd(),
        # Which pipeline asked. Jarvis puts these in the environment when it
        # launches `agy`, so the Commands page can show the command next to the
        # run that wanted it rather than on its own.
        "project": os.getenv("JARVIS_PROJECT", ""),
        "plan_id": os.getenv("JARVIS_PLAN_ID", ""),
        "section_id": os.getenv("JARVIS_SECTION_ID", ""),
    }

    body = json.dumps(request).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.URLError:
        respond("deny", (
            "Jarvis is not running, so this command could not be reviewed and was refused. "
            "It did NOT run — say so in your final answer instead of reporting it as done."
        ))
    except Exception as e:
        respond("deny", f"Jarvis could not review this command ({e}), so it was refused.")

    decision = (answer.get("decision") or "").strip().lower()
    reason = answer.get("reason") or ""
    if decision == "allow":
        overrides = answer.get("overrides") or [f"command({command})"]
        respond("allow", reason or "Approved by the user.", overrides)
    respond("deny", reason or "The user did not approve this command.")


if __name__ == "__main__":
    main()
