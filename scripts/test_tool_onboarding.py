"""Connecting an MCP server gives agents its tools — after a review, with risky ones held in the Control room.

A server's tools are written to the tool catalogue the moment they are listed.
Tools that only read or change things wait for a review on the Commands page,
which approves itself if nobody answers; tools that cost money or are
destructive wait in the Control room with no time limit. Approved read-only
tools go to every agent, the rest only to agents whose plan names the server,
and every call to a risky tool on "always ask" is held until the user answers.

Uses the real MCP test server (scripts/mcp_test_server.py) over stdio; no model is called.
"""
import asyncio, json, os, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("GEMINI_API_KEY", None)

from connectors import tool_catalog, mcp_client
from agents import tool_onboarding, tool_executor
import control_room


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_onboarding_")
tool_catalog.CATALOG_PATH = os.path.join(tmp, "tool_catalog.json")
mcp_client.REGISTRY_PATH = os.path.join(tmp, "mcp_registry.json")
with open(mcp_client.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({"jarvis_test": {"enabled": True, "command": sys.executable,
                               "args": [os.path.join(ROOT, "scripts", "mcp_test_server.py")]}}, f)
control_room.POLL_SECONDS = 0.01
# Research runs inline here, and with no Gemini key it fails at once, so the review
# opens with the server's own descriptions (test_tool_research.py covers research).
tool_onboarding.RESEARCH_IN_BACKGROUND = False
notes = []
tool_onboarding.set_notifier(notes.append)
control_room.set_notifier(notes.append)


def names(tools_needed):
    decls, handlers, unavailable = tool_executor.get_tools_for_execution_agent(tools_needed, "__onboarding_test__")
    return {d["name"] for d in decls}, handlers, unavailable


# ---- 1. risk labels ---------------------------------------------------------------------
c = tool_catalog.classify_risk
check("a reading verb is read", c("search_repositories", "Search GitHub") == "read")
check("an unclear verb is write", c("move_file", "Move a file") == "write")
check("deleting is destructive", c("delete_file") == "destructive")
check("sending is destructive", c("send_email") == "destructive")
check("buying costs money", c("create_order") == "costs_money")
check("listing orders is only a read", c("list_orders") == "read")
check("a description that mentions credits costs money", c("generate_image", "Costs 2 credits.") == "costs_money")
check("a server's destructiveHint is believed", c("tidy_up", "", {"destructiveHint": True}) == "destructive")
check("readOnlyHint never overrules a destructive name", c("delete_all", "", {"readOnlyHint": True}) == "destructive")

# ---- 2. first sight of a server opens a review, binds nothing ----------------------------
got, _, unavailable = names(["jarvis_test"])
check("no tool of a just-connected server is bound before review", not any(n.startswith("mcp_") for n in got))
check("the agent is told its tools are waiting for review",
      any("waiting for review" in u for u in unavailable))
entry = tool_catalog.get("jarvis_test")
check("the catalogue has all three tools", set(entry["tools"]) == {"echo", "add", "delete_note"})
check("echo is read, add is write, delete_note is destructive",
      [entry["tools"][t]["risk"] for t in ("echo", "add", "delete_note")] == ["read", "write", "destructive"])
pending = tool_onboarding.pending()
check("one review waits on the Commands page", len(pending) == 1 and pending[0]["service"] == "jarvis_test")
check("the review lists echo and add, not the destructive tool",
      {t["name"] for t in pending[0]["tools"]} == {"echo", "add"})
check("the review says one tool waits in the Control room", pending[0]["held_in_control_room"] == 1)
check("delete_note waits in the Control room", [t["tool"] for t in control_room.held_tools()] == ["delete_note"])
check("the user was told where to look", any("Commands page" in n and "Control room" in n for n in notes))

# ---- 3. an unanswered review approves itself, except the risky tool ----------------------
tool_catalog.update(lambda cat: cat["jarvis_test"]["review"].update(deadline=time.time() - 1))
approved = tool_onboarding.sweep()
check("the expired review approved itself", approved == ["jarvis_test"])
entry = tool_catalog.get("jarvis_test")
check("echo and add are enabled", entry["tools"]["echo"]["status"] == entry["tools"]["add"]["status"] == "enabled")
check("delete_note is still held — no time limit in the Control room",
      entry["tools"]["delete_note"]["status"] == "held")
check("the approval says who decided", entry["decided"]["by"] == "timeout")

# ---- 4. who gets what ---------------------------------------------------------------------
got, handlers, _ = names([])
check("every agent gets the approved read-only tool", "mcp_jarvis_test_echo" in got)
check("an agent that didn't ask for the server doesn't get the write tool", "mcp_jarvis_test_add" not in got)
got_named, handlers_named, _ = names(["jarvis_test"])
check("an agent whose plan names the server gets the write tool too", "mcp_jarvis_test_add" in got_named)
check("nobody gets the held destructive tool", "mcp_jarvis_test_delete_note" not in got_named)
result = tool_executor.run_tool(handlers, "__onboarding_test__", "a1", "mcp_jarvis_test_echo", {"message": "hi"})
check("the approved tool really runs on the server", result.get("content") == "echoed: hi")

# ---- 5. seeing the same tools again changes nothing ----------------------------------------
before = len(notes)
tool_onboarding.observe_mcp("jarvis_test", mcp_client.ensure_server_running("jarvis_test")["tools"])
check("an unchanged tool list opens no new review", not tool_onboarding.pending() and len(notes) == before)

# ---- 6. the Control room decides the risky tool -------------------------------------------
check("a read tool can't be decided in the Control room",
      "error" in control_room.decide_tool("jarvis_test", "echo", "always_allow"))
check("a risky tool is enabled as always ask",
      control_room.decide_tool("jarvis_test", "delete_note", "always_ask")["status"] == "ok")
got_named, handlers_named, _ = names(["jarvis_test"])
check("once allowed, the agent that asked for the server gets it", "mcp_jarvis_test_delete_note" in got_named)
decl = [d for d in tool_executor.get_tools_for_execution_agent(["jarvis_test"], "x")[0]
        if d["name"] == "mcp_jarvis_test_delete_note"][0]
check("its description warns that each call waits in the Control room", "Control room" in decl["description"])


async def held_call(answer, reason=""):
    task = asyncio.create_task(control_room.check_call(
        "mcp_jarvis_test_delete_note", {"title": "old"}, agent_id="a1", role="Cleaner", kind="execution"))
    while not control_room.pending_calls():
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.2)
    still = control_room.pending_calls()
    check("the call is still waiting — nothing answers it by itself", len(still) == 1 and "deadline" not in still[0])
    check("the held call shows what it would do", '"old"' in still[0]["args"] and still[0]["role"] == "Cleaner")
    control_room.decide_call(still[0]["request_id"], answer, reason)
    return await task


check("an allowed call runs", asyncio.run(held_call("allow")) is None)
denied = asyncio.run(held_call("deny", "keep it"))
check("a denied call tells the agent it did not happen",
      denied["status"] == "error" and "did NOT happen" in denied["error"] and "keep it" in denied["error"])
check("answered calls are logged", [c["decision"] for c in control_room.call_log()] == ["deny", "allow"])
check("a call to a read tool is never held",
      asyncio.run(control_room.check_call("mcp_jarvis_test_echo", {"message": "x"})) is None)

control_room.decide_tool("jarvis_test", "delete_note", "always_allow")
check("always allow lets calls through at once",
      asyncio.run(control_room.check_call("mcp_jarvis_test_delete_note", {"title": "x"})) is None)
control_room.decide_tool("jarvis_test", "delete_note", "block")
blocked = asyncio.run(control_room.check_call("mcp_jarvis_test_delete_note", {"title": "x"}))
check("a blocked tool is refused", blocked and "blocked" in blocked["error"])
check("and no agent gets it", "mcp_jarvis_test_delete_note" not in names(["jarvis_test"])[0])

# ---- 7. the user's review choices ---------------------------------------------------------
fake = [{"name": "list_pages", "description": "List pages"},
        {"name": "update_page", "description": "Update a page"},
        {"name": "charge_card", "description": "Charge a card"}]


def fresh(service):
    tool_catalog.record_inventory(service, "mcp", fake, tool_executor._mcp_function_name)
    tool_onboarding._open_review(service)


fresh("notes_a")
tool_onboarding.decide("notes_a", "approve_read")
t = tool_catalog.get("notes_a")["tools"]
check("approve read-only enables only the read tool",
      (t["list_pages"]["status"], t["update_page"]["status"]) == ("enabled", "disabled"))
check("and leaves the money tool to the Control room", t["charge_card"]["status"] == "held")

fresh("notes_b")
check("pick needs at least one tool", "error" in tool_onboarding.decide("notes_b", "pick", tools=[]))
tool_onboarding.decide("notes_b", "pick", tools=["update_page", "charge_card"])
t = tool_catalog.get("notes_b")["tools"]
check("pick enables the ticked tool and not the other",
      (t["update_page"]["status"], t["list_pages"]["status"]) == ("enabled", "disabled"))
check("picking a money tool on the Commands page doesn't enable it", t["charge_card"]["status"] == "held")

fresh("notes_c")
tool_onboarding.decide("notes_c", "reject")
t = tool_catalog.get("notes_c")["tools"]
check("reject turns everything off, the held tool too", {v["status"] for v in t.values()} == {"disabled"})
check("a review can't be answered twice", "error" in tool_onboarding.decide("notes_c", "approve"))

changed = fake[:2] + [{"name": "charge_card", "description": "Charge a card"},
                      {"name": "get_page", "description": "Get one page"}]
tool_catalog.record_inventory("notes_a", "mcp", changed, tool_executor._mcp_function_name)
tool_onboarding._open_review("notes_a")
t = tool_catalog.get("notes_a")["tools"]
check("a changed tool list keeps earlier decisions", t["list_pages"]["status"] == "enabled")
check("and puts only the new tool up for review",
      [x["name"] for x in tool_onboarding.pending()[0]["tools"]] == ["get_page"])

mcp_client.shutdown_all()
print("\nAll tool onboarding checks passed.")
