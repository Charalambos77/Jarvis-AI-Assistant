"""The Control room owns connected services and spending, and the Brain plans with what is connected.

  * APIs, MCP servers and reviews of their new tools live on the Control room page;
    the old APIs/MCPs button now opens it.
  * Tools that cost money obey spending limits per service and per pipeline: a call
    that would go over is held for the user even when its tool is on "always allow",
    and so is a money call whose amount can't be read while a limit is set.
  * The Brain and the agents are told which services are connected and approved,
    and a service can be named loosely ("notes_api", "notes_mcp") in tools_needed.

No model or network is used.
"""
import asyncio, json, os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from connectors import tool_catalog, mcp_client
from agents import tool_onboarding, tool_executor
import control_room


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_step3_")
tool_catalog.CATALOG_PATH = os.path.join(tmp, "tool_catalog.json")
control_room.SPENDING_PATH = os.path.join(tmp, "spending.json")
mcp_client.REGISTRY_PATH = os.path.join(tmp, "mcp_registry.json")
with open(mcp_client.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({"shop": {"enabled": True, "command": "unused"}, "notes": {"enabled": True, "command": "unused"},
               "offline": {"enabled": False, "command": "unused"}}, f)
control_room.POLL_SECONDS = 0.01

fn = tool_executor._mcp_function_name
tool_catalog.record_inventory("shop", "mcp", [
    {"name": "buy_item", "description": "Buy an item"},
    {"name": "list_items", "description": "List items"}], fn)
tool_catalog.record_inventory("notes", "mcp", [
    {"name": "list_pages", "description": "List pages"},
    {"name": "update_page", "description": "Update a page"}], fn)
tool_catalog.record_inventory("offline", "mcp", [{"name": "get_thing", "description": "Get a thing"}], fn)
for svc in ("shop", "notes", "offline"):
    tool_onboarding._open_review(svc)
    tool_onboarding.decide(svc, "approve")
tool_catalog.update(lambda c: c["notes"].update(summary="A notes app.", use_for=["research notes"]))

# ---- 1. reading what a call would spend ---------------------------------------------------
amt = control_room.call_amount
check("an amount argument is read", amt({"item": "x", "amount": 5}) == 5.0)
check("a price written as text is read", amt({"price": "$1,250.50"}) == 1250.5)
check("an amount one level down is read", amt({"order": {"total": 3}}) == 3.0)
check("no amount means None", amt({"item": "x", "quantity": "two"}) is None)
check("true/false is not an amount", amt({"amount": True}) is None)

# ---- 2. limits -------------------------------------------------------------------------
check("a limit must be a number", "error" in control_room.set_limit("service", "shop", "lots"))
check("a limit can't be negative", "error" in control_room.set_limit("service", "shop", -1))
check("a service limit needs a service", "error" in control_room.set_limit("service", "", 5))
check("the page lists services with money tools", [s["service"] for s in control_room.spending()["services"]] == ["shop"])

control_room.decide_tool("shop", "buy_item", "always_allow")
plan_events = []


def logger(event):
    event["plan_id"] = "p9"                  # what the pipeline's logger does
    plan_events.append(event)


def call(args, log=logger):
    return asyncio.run(control_room.check_call("mcp_shop_buy_item", args, agent_id="a1", role="Buyer", event_logger=log))


check("with no limit, an always-allowed money call runs", call({"amount": 40}) is None)
check("and what it spent is counted, per service and per pipeline",
      control_room.spending()["services"][0]["spent"] == 40 and control_room.spending()["pipelines"] == {"p9": 40})
check("with no limit, a money call with no amount still runs", call({"item": "pen"}) is None)

control_room.set_limit("service", "shop", 50)


async def held(args, answer):
    task = asyncio.create_task(control_room.check_call("mcp_shop_buy_item", args, agent_id="a1", role="Buyer",
                                                       event_logger=logger))
    while not control_room.pending_calls():
        await asyncio.sleep(0.005)
    item = control_room.pending_calls()[0]
    control_room.decide_call(item["request_id"], answer)
    return item, await task


check("under the limit, an always-allowed call still runs", call({"amount": 5}) is None)
item, result = asyncio.run(held({"amount": 10}, "deny"))
check("over the limit it is held even on always allow", "over" in item["limit_reason"] and item["amount"] == 10)
check("a denied call spends nothing", result["status"] == "error" and control_room.spending()["services"][0]["spent"] == 45)
item, result = asyncio.run(held({"item": "pen"}, "allow"))
check("with a limit set, a call with no readable amount is held", "can't tell how much" in item["limit_reason"])
item, result = asyncio.run(held({"amount": 10}, "allow"))
check("allowing an over-limit call once lets it run and counts it",
      result is None and control_room.spending()["services"][0]["spent"] == 55)

control_room.set_limit("service", "shop", "")
control_room.set_limit("pipeline", "", 60)
check("clearing a service limit works", control_room.spending()["services"][0]["limit"] is None)
item, _ = asyncio.run(held({"amount": 10}, "deny"))
check("a pipeline limit holds calls from that pipeline", "pipeline p9" in item["limit_reason"])
check("a call from outside any pipeline isn't held by the pipeline limit", call({"amount": 10}, log=None) is None)
check("the event says which pipeline asked", any(e["event_type"] == "control_room_check" for e in plan_events))

# ---- 3. what the Brain and agents are told ----------------------------------------------
note = tool_onboarding.connected_services_note()
check("connected, approved services are listed", "- notes: A notes app. Good for: research notes." in note)
check("with each tool's risk in plain words", "list_pages (reads)" in note and "update_page (changes things)" in note)
check("a money tool says it asks first", "buy_item (costs money, asks first)" in note)
check("a switched-off server is left out", "offline" not in note)
tool_catalog.update(lambda c: [t.update(status="disabled") for t in c["notes"]["tools"].values()])
check("a server with nothing approved is left out", "- notes" not in tool_onboarding.connected_services_note())
tool_catalog.update(lambda c: [t.update(status="enabled") for t in c["notes"]["tools"].values()])

from agents import brain
sent = {}


class FakeModels:
    def generate_content(self, model, contents, config=None):
        sent.setdefault("prompts", []).append(contents)
        return type("R", (), {"text": json.dumps({"cycles": [], "execution_agents": []})})()


class FakeClient:
    def __init__(self, api_key=None):
        self.models = FakeModels()


brain.genai.Client = FakeClient
try:
    brain.build_agent_plan("Write a report")
except Exception:
    pass                                     # only the prompt matters here
try:
    brain.plan_execution_agents("Write a report", {"x": 1}, [], None)
except Exception:
    pass
check("the Brain's first plan is told about connected services",
      any("CONNECTED SERVICES" in p and "- notes" in p for p in sent["prompts"][:1]))
check("so is its execution plan", len(sent["prompts"]) > 1 and "CONNECTED SERVICES" in sent["prompts"][1])

for p in ("agents/research_agent.py", "agents/execution_agent.py"):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        check(f"{p} puts connected services in the agent's prompt", "{connected_note}" in f.read())

# ---- 4. naming a service loosely --------------------------------------------------------
r = lambda name: tool_executor._resolve_tool_key(name, allow_llm=False)
check("a server by its exact name", r("notes") == "mcp:notes")
check("a server with _api on the end", r("notes_api") == "mcp:notes")
check("a server with _mcp on the end", r("notes_mcp") == "mcp:notes")
check("a server by one of its tools' names", r("mcp_notes_update_page") == "mcp:notes")

# ---- 5. the pages ----------------------------------------------------------------------
read = lambda name: open(os.path.join(ROOT, name), encoding="utf-8").read()
room, commands, nav = read("control_room.html"), read("commands.html"), read("nav_ui.js")
for part in ('id="onboarding"', 'id="limits"', 'id="mcps"', 'id="apis"', 'id="always-on"', "provider_comparison.html"):
    check(f"the Control room has {part}", part in room)
check("new-tool reviews left the Commands page", "onboarding" not in commands)
check("the shared nav's APIs/MCPs button opens the Control room",
      '"apis/mcps"' in nav and 'href: "provider_comparison.html"' not in nav)
for page in ("plan.html", "execution.html", "command_center.html"):
    check(f"{page} sends APIs/MCPs to the Control room",
          "control_room.html" in read(page) and "window.location.href = 'provider_comparison.html" not in read(page))

print("\nAll Control room step 3 checks passed.")
