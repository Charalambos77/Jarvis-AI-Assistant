"""
New tools from a connected service, reviewed before agents use them.

An MCP server's tools used to reach an agent only when the Brain happened to
name the server in that agent's tools_needed, and then every tool on it arrived
at once — the ones that read beside the ones that delete. Now, the moment a
server's tool list is seen (switched on at the Connected panel, or started for
an agent), it is written into the tool catalogue and:

  * tools that cost money or are destructive go straight to the Control room,
    where they wait for the user with no time limit (see control_room.py);
  * every other new tool waits for a review on the Commands page. The user can
    approve them all, approve only the read-only ones, pick some, or reject the
    lot. An unanswered review approves itself after WAIT_SECONDS, like the
    other approvals there, so a forgotten review never stalls a pipeline.

Approved read-only tools are then offered to every agent; tools that change
things only to agents whose plan asks for the service (see tool_executor).

The review lives in the catalogue, not in memory, so it survives a restart and
its deadline keeps counting while Jarvis is closed.
"""
import os
import threading
import time

from connectors import tool_catalog

# Same patience as every other approval on the Commands page.
WAIT_SECONDS = int(os.getenv("JARVIS_COMMAND_WAIT", "600"))
SWEEP_SECONDS = 5.0
DECISIONS = ("approve", "approve_read", "pick", "reject")

_notifier = None
_sweeper_started = False
_sweeper_lock = threading.Lock()


def set_notifier(fn) -> None:
    """Called with a one-line message when a review opens or approves itself."""
    global _notifier
    _notifier = fn


def _notify(text: str) -> None:
    if _notifier:
        try:
            _notifier(text)
        except Exception as e:
            print(f"[Tool onboarding] Could not announce: {e}")


def _count(entry: dict, status: str) -> int:
    return sum(1 for t in (entry.get("tools") or {}).values() if t.get("status") == status)


def observe_mcp(server: str, tools: list[dict]) -> dict:
    """Record what a running MCP server offers; open a review if anything is new.

    Cheap when nothing changed (one hash comparison), so it is called wherever a
    live tool list turns up.
    """
    from agents.tool_executor import _mcp_function_name

    entry, changed = tool_catalog.record_inventory(server, "mcp", tools, _mcp_function_name)
    if changed:
        _open_review(server)
    sweep()
    return tool_catalog.get(server) or entry


def _open_review(server: str) -> None:
    now = time.time()
    opened = {}

    def _apply(catalog):
        entry = catalog.get(server)
        if not entry:
            return
        pending = [n for n, t in entry["tools"].items() if t.get("status") == "pending"]
        if pending:
            entry["review"] = {
                "asked_at": tool_catalog.now_iso(),
                "asked_ts": now,
                "deadline": now + WAIT_SECONDS,
            }
            entry["state"] = "awaiting_review"
        else:
            entry["review"] = None
            if entry.get("state") != "rejected":
                entry["state"] = "ready"
        opened["pending"] = len(pending)
        opened["held"] = _count(entry, "held")

    tool_catalog.update(_apply)
    parts = []
    if opened.get("pending"):
        parts.append(f"{opened['pending']} new tool(s) to review on the Commands page "
                     f"(approved automatically in {WAIT_SECONDS // 60} minutes if you don't answer)")
    if opened.get("held"):
        parts.append(f"{opened['held']} that cost money or are destructive, waiting in the Control room")
    if parts:
        _notify(f"{server} is connected: " + "; ".join(parts) + ".")


def decide(server: str, decision: str, tools: list[str] | None = None, reason: str = "",
           by: str = "user") -> dict:
    """Answer one service's review.

    approve       every pending tool is enabled;
    approve_read  pending read-only tools are enabled, the rest disabled;
    pick          the pending tools named in `tools` are enabled, the rest disabled;
    reject        nothing is enabled, and tools held in the Control room are
                  dropped too — rejecting a service means none of it.

    Tools in the Control room are never enabled from here: only the Control room
    decides those.
    """
    decision = (decision or "").strip().lower()
    if decision not in DECISIONS:
        return {"error": f"decision must be one of {', '.join(DECISIONS)}."}
    picked = set(tools or [])
    if decision == "pick" and not picked:
        return {"error": "Pick at least one tool, or reject the review."}
    out = {}

    def _apply(catalog):
        entry = catalog.get(server)
        if not entry or not entry.get("review"):
            out["error"] = f"{server} has no review waiting — it may already have been answered."
            return
        enabled = []
        for name, tool in entry["tools"].items():
            status = tool.get("status")
            if decision == "reject" and status in ("pending", "held"):
                tool["status"] = "disabled"
                continue
            if status != "pending":
                continue
            if decision == "approve" \
                    or (decision == "approve_read" and tool.get("risk") == "read") \
                    or (decision == "pick" and name in picked):
                tool["status"] = "enabled"
                enabled.append(name)
            else:
                tool["status"] = "disabled"
        entry["review"] = None
        entry["state"] = "rejected" if decision == "reject" else "ready"
        entry["decided"] = {"decision": decision, "by": by, "reason": (reason or "").strip(),
                            "enabled": enabled, "at": tool_catalog.now_iso()}
        out.update({"status": "ok", "service": server, "decision": decision, "enabled": enabled})

    tool_catalog.update(_apply)
    return out


def sweep() -> list[str]:
    """Approve every review whose wait has run out. Returns the services approved."""
    now = time.time()
    due = [name for name, entry in tool_catalog.load().items()
           if entry.get("review") and now >= float(entry["review"].get("deadline") or 0)]
    approved = []
    for name in due:
        result = decide(name, "approve", by="timeout")
        if result.get("status") == "ok":
            approved.append(name)
            _notify(f"Nobody answered the review of {name} within {WAIT_SECONDS // 60} minutes, so its "
                    f"{len(result['enabled'])} tool(s) were approved. Anything that costs money or is "
                    "destructive is still waiting in the Control room.")
    return approved


def start_sweeper() -> None:
    """Keep approving expired reviews while nobody is looking at a page."""
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    def _loop():
        while True:
            try:
                sweep()
            except Exception as e:
                print(f"[Tool onboarding] Sweep failed: {e}")
            time.sleep(SWEEP_SECONDS)

    threading.Thread(target=_loop, name="tool-onboarding-sweeper", daemon=True).start()


def pending() -> list[dict]:
    """Every service waiting for a review, oldest first, with the tools to decide on."""
    sweep()
    now = time.time()
    items = []
    for name, entry in tool_catalog.load().items():
        review = entry.get("review")
        if not review:
            continue
        items.append({
            "service": name,
            "kind": entry.get("kind", "mcp"),
            "asked_at": review.get("asked_at"),
            "asked_ts": review.get("asked_ts"),
            "waited": int(now - float(review.get("asked_ts") or now)),
            "expires_in": max(0, int(float(review.get("deadline") or now) - now)),
            "held_in_control_room": _count(entry, "held"),
            "tools": [
                {"name": n, "risk": t.get("risk"), "description": (t.get("description") or "")[:400]}
                for n, t in entry["tools"].items() if t.get("status") == "pending"
            ],
        })
    items.sort(key=lambda i: i.get("asked_ts") or 0)
    return items


def log(limit: int = 20) -> list[dict]:
    """The latest answered reviews, newest first."""
    out = []
    for name, entry in tool_catalog.load().items():
        decided = entry.get("decided")
        if decided:
            out.append({"service": name, **decided})
    out.sort(key=lambda d: d.get("at") or "", reverse=True)
    return out[:limit]
