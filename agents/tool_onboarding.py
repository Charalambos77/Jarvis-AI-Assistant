"""
New tools from a connected service, reviewed before agents use them.

An MCP server's tools used to reach an agent only when the Brain happened to
name the server in that agent's tools_needed, and then every tool on it arrived
at once — the ones that read beside the ones that delete. Now, the moment a
server's tool list is seen (switched on at the Connected panel, or started for
an agent), it is written into the tool catalogue and:

  * a Tool Researcher first reads up on the service and writes a card for each
    new tool — what it does, when to use it, example arguments, limits, and a
    risk label it may raise but never lower (see tool_researcher.py). If the
    research fails, the review opens anyway with the server's own descriptions;
  * tools that cost money or are destructive go to the Control room,
    where they wait for the user with no time limit (see control_room.py);
  * every other new tool waits for a review in the Control room. The user can
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

# Same patience as the approvals on the Commands page.
WAIT_SECONDS = int(os.getenv("JARVIS_COMMAND_WAIT", "600"))
SWEEP_SECONDS = 5.0
DECISIONS = ("approve", "approve_read", "pick", "reject")

# Research runs on its own thread so connecting a server, or an agent that
# starts one, never waits for it. Tests turn this off to run it inline.
RESEARCH_IN_BACKGROUND = True
_researching: set[str] = set()      # services with a research thread running now
_rerun: set[str] = set()            # tool list changed again mid-research
_research_lock = threading.Lock()

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
        start_research(server)
    sweep()
    return tool_catalog.get(server) or entry


def observe_api(service: str, spec_url: str | None = None) -> dict:
    """Read a connected API's published spec and record each operation as a tool.

    Tries the spec URL given here, then the one saved for the service, a known
    spec, the usual paths on its base URL and a web search (openapi_tools.find_spec).
    If none is found the API stays connected with no tools, and the Control room
    asks for the spec's address. A spec that can't be reached this time never
    wipes tools that were already found.
    """
    from connectors import openapi_tools
    from connectors.api_connector import get_service_config
    from agents.tool_executor import _api_function_name

    config = get_service_config(service) or {}
    spec_url = spec_url or config.get("spec_url")
    try:
        found_url, spec = openapi_tools.find_spec(service, spec_url, config.get("base_url"))
        ops = openapi_tools.operations(spec, found_url)
        if not ops:
            raise openapi_tools.SpecNotFound(f"The spec at {found_url} lists no operations.")
    except openapi_tools.SpecNotFound as e:
        told = {}

        def _no_spec(catalog):
            entry = catalog.setdefault(service, {"kind": "api", "tools": {}, "review": None})
            entry["spec_error"] = str(e)
            entry["spec_checked_at"] = tool_catalog.now_iso()
            if not entry.get("tools"):
                told["new"] = entry.get("state") != "no_spec"
                entry["state"] = "no_spec"

        tool_catalog.update(_no_spec)
        if told.get("new"):
            _notify(f"{service} is connected, but Jarvis found no API description for it, so agents can't "
                    "use it yet. Paste the address of its OpenAPI or Swagger spec in the Control room.")
        return tool_catalog.get(service) or {}

    entry, changed = tool_catalog.record_inventory(service, "api", ops, _api_function_name)

    def _spec(catalog):
        current = catalog.get(service)
        if current:
            current["spec_url"] = found_url
            current["spec_title"] = str((spec.get("info") or {}).get("title") or spec.get("title") or "")[:120]
            current["spec_error"] = None
            current["spec_checked_at"] = tool_catalog.now_iso()
            if current.get("state") == "no_spec":
                current["state"] = "ready"

    tool_catalog.update(_spec)
    if changed:
        start_research(service)
    sweep()
    return tool_catalog.get(service) or entry


def discover_connected_apis() -> list[str]:
    """Read the spec of every connected API that has no tools of its own yet.

    Skips services Jarvis already has a built-in handler for (arXiv, web search,
    Google Docs...), MCP servers, and APIs already in the catalogue. Returns the
    services it looked at.
    """
    from connectors.api_connector import get_all_configured_services
    from agents.tool_executor import (_resolve_tool_key, REGISTRY_TOOLS, ALWAYS_ON_TOOLS,
                                      MCP_KEY_PREFIX)

    catalog = tool_catalog.load()
    looked = []
    for name in get_all_configured_services():
        service = name.lower().replace("-", "_").replace(" ", "_")
        # A service with no spec last time is tried again: the web may answer now.
        if service in looked or (service in catalog and catalog[service].get("state") != "no_spec"):
            continue
        key = _resolve_tool_key(service, allow_llm=False)
        if key in REGISTRY_TOOLS or key in ALWAYS_ON_TOOLS or key.startswith(MCP_KEY_PREFIX):
            continue
        looked.append(service)
        try:
            observe_api(service)
        except Exception as e:
            print(f"[Tool onboarding] Could not read {service}'s API description: {e}")
    return looked


def start_api_discovery() -> None:
    """discover_connected_apis on its own thread, so startup never waits on the web."""
    threading.Thread(target=discover_connected_apis, name="api-discovery", daemon=True).start()


def start_research(server: str) -> None:
    """Research a service's new tools, then open their review."""
    with _research_lock:
        if server in _researching:
            _rerun.add(server)      # the running research picks up the new list when it ends
            return
        _researching.add(server)

    def _mark(catalog):
        entry = catalog.get(server)
        if entry:
            entry["state"] = "researching"
            entry["review"] = None
            entry["research"] = {"status": "running", "started_at": tool_catalog.now_iso()}

    tool_catalog.update(_mark)
    if RESEARCH_IN_BACKGROUND:
        threading.Thread(target=_research_loop, args=(server,), name=f"tool-research-{server}",
                         daemon=True).start()
    else:
        _research_loop(server)


def _research_loop(server: str) -> None:
    try:
        while True:
            _research_then_review(server)
            with _research_lock:
                if server not in _rerun:
                    break
                _rerun.discard(server)
    finally:
        with _research_lock:
            _researching.discard(server)


def _research_then_review(server: str) -> None:
    from agents import tool_researcher

    entry = tool_catalog.get(server) or {}
    # Only tools nobody has decided on and nobody has researched: an unchanged
    # tool keeps its card, like it keeps its decision.
    todo = {n: t for n, t in (entry.get("tools") or {}).items()
            if t.get("status") in ("pending", "held") and not t.get("card")}
    try:
        found = tool_researcher.research(server, entry.get("kind", "mcp"), todo)
        error = None
    except Exception as e:
        found, error = None, str(e)
        print(f"[Tool onboarding] Research of {server} failed: {e}")

    def _apply(catalog):
        current = catalog.get(server)
        if not current:
            return
        if found is None:
            current["research"] = {"status": "failed", "error": error, "at": tool_catalog.now_iso()}
            return
        for name, card in found["cards"].items():
            tool = current["tools"].get(name)
            if not tool or tool.get("status") not in ("pending", "held"):
                continue
            tool["card"] = card
            tool["description"] = tool_researcher.card_description(
                card, tool.get("server_description") or tool.get("description") or name)
            if card["risk"] != tool.get("risk"):
                tool["risk"] = card["risk"]
                if card["risk"] in tool_catalog.RISKY and tool.get("status") == "pending":
                    tool["status"] = "held"     # found to cost money or destroy: Control room
        if found["summary"]:
            current["summary"] = found["summary"]
        if found["use_for"]:
            current["use_for"] = found["use_for"]
        current["research"] = {"status": "done", "at": tool_catalog.now_iso(),
                               "researched": sorted(found["cards"]), "sources": found["sources"]}

    tool_catalog.update(_apply)
    _open_review(server)


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
    research = (tool_catalog.get(server) or {}).get("research") or {}
    parts = []
    if opened.get("pending"):
        parts.append(f"{opened['pending']} new tool(s) to review in the Control room "
                     f"(approved automatically in {WAIT_SECONDS // 60} minutes if you don't answer)")
    if opened.get("held"):
        parts.append(f"{opened['held']} that cost money or are destructive, waiting in the Control room")
    if parts:
        note = ""
        if research.get("status") == "failed":
            note = f" Jarvis could not research it ({research.get('error')}), so the tools carry the server's own descriptions."
        _notify(f"{server} is connected: " + "; ".join(parts) + "." + note)


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
    """Approve every review whose wait has run out. Returns the services approved.

    Also restarts research that a restart of Jarvis cut off, so a service can't
    sit in "researching" forever with no review ever opening.
    """
    for name, entry in tool_catalog.load().items():
        if entry.get("state") == "researching":
            with _research_lock:
                running = name in _researching
            if not running:
                start_research(name)
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
            "summary": entry.get("summary", ""),
            "use_for": entry.get("use_for", []),
            "research": entry.get("research") or {},
            "tools": [
                {"name": n, "risk": t.get("risk"), "description": (t.get("description") or "")[:900],
                 "researched": bool(t.get("card"))}
                for n, t in entry["tools"].items() if t.get("status") == "pending"
            ],
        })
    items.sort(key=lambda i: i.get("asked_ts") or 0)
    return items


def researching() -> list[dict]:
    """Services whose new tools are being researched, before their review opens."""
    return [{"service": name, "started_at": (entry.get("research") or {}).get("started_at"),
             "tools": sum(1 for t in entry["tools"].values() if t.get("status") in ("pending", "held"))}
            for name, entry in tool_catalog.load().items() if entry.get("state") == "researching"]


RISK_WORDS = {"read": "reads", "write": "changes things", "destructive": "destructive, asks first",
              "costs_money": "costs money, asks first"}


def connected_services_note(max_tools: int = 12) -> str:
    """What the Brain and the agents are told about connected services.

    Only switched-on servers and connected APIs with at least one approved tool:
    planning around a service nobody can use yet would only produce a blocked agent.
    """
    try:
        from connectors.mcp_client import enabled_servers
        on = set(enabled_servers())
    except Exception:
        on = set()
    try:
        from connectors.api_connector import get_service_status
    except Exception:
        get_service_status = lambda _name: "unknown"
    lines = []
    for name, entry in sorted(tool_catalog.load().items()):
        if entry.get("kind") == "api":
            if get_service_status(name) == "unknown":
                continue
        elif name not in on:
            continue
        tools = [(n, t) for n, t in (entry.get("tools") or {}).items() if t.get("status") == "enabled"]
        if not tools:
            continue
        shown = ", ".join(f"{n} ({RISK_WORDS.get(t.get('risk'), t.get('risk'))})" for n, t in tools[:max_tools])
        more = f", and {len(tools) - max_tools} more" if len(tools) > max_tools else ""
        about = entry.get("summary") or ""
        use_for = f" Good for: {'; '.join(entry['use_for'])}." if entry.get("use_for") else ""
        lines.append(f"- {name}: {about}{use_for} Tools: {shown}{more}.")
    if not lines:
        return ""
    return ("CONNECTED SERVICES (set up and approved by the user; name one in tools_needed exactly as written "
            "to give an agent all its approved tools):\n" + "\n".join(lines) + "\n")


def log(limit: int = 20) -> list[dict]:
    """The latest answered reviews, newest first."""
    out = []
    for name, entry in tool_catalog.load().items():
        decided = entry.get("decided")
        if decided:
            out.append({"service": name, **decided})
    out.sort(key=lambda d: d.get("at") or "", reverse=True)
    return out[:limit]
