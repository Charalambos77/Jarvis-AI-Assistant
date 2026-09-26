"""Connected services are checked again at startup, and a pipeline waits for their reviews.

  * At startup every switched-on MCP server and connected API is looked at again.
    Only new or changed tools go up for review; the rest keep their decisions.
  * After the Plugging Gate, execution waits while a service the agents need
    still has tools being researched or reviewed. The wait ends when the user
    answers or when the review approves itself. Tools held in the Control room
    for money or destruction never hold a pipeline up.
  * The Plugging Gate shows where each service's tools are, and the Control
    room lists a service saved under two spellings once.

Uses the real MCP test server and a small local HTTP API; no model is called.
"""
import asyncio, json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("GEMINI_API_KEY", "dummy")      # multi_agent_coordinator builds a client at import

from connectors import tool_catalog, api_connector, mcp_client
from agents import tool_onboarding, tool_executor
import control_room


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_step5_")
tool_catalog.CATALOG_PATH = os.path.join(tmp, "tool_catalog.json")
control_room.SPENDING_PATH = os.path.join(tmp, "spending.json")
api_connector.REGISTRY_PATH = os.path.join(tmp, "api_registry.json")
mcp_client.REGISTRY_PATH = os.path.join(tmp, "mcp_registry.json")
with open(mcp_client.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({"jarvis_test": {"enabled": True, "command": sys.executable,
                               "args": [os.path.join(ROOT, "scripts", "mcp_test_server.py")]},
               "switched_off": {"enabled": False, "command": "unused"}}, f)
with open(api_connector.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({}, f)
tool_onboarding.RESEARCH_IN_BACKGROUND = False
# No web search: the model key above is a dummy.
from connectors import openapi_tools
openapi_tools._search_for_spec = lambda service: None
notes = []
tool_onboarding.set_notifier(notes.append)

SPEC = {"openapi": "3.0.0", "info": {"title": "Notes API"}, "servers": [{"url": "/v1"}],
        "paths": {"/notes": {"get": {"operationId": "listNotes", "summary": "List notes"}}}}


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        code, data = (200, SPEC) if self.path == "/openapi.json" else (404, {})
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
threading.Thread(target=server.serve_forever, daemon=True).start()
api_connector.register_service("notes_api", {"status": "up", "base_url": f"http://127.0.0.1:{server.server_address[1]}"})

# ---- 1. the first startup finds everything ---------------------------------------------------
found = tool_onboarding.check_for_changes()
check("the switched-on server is checked, the switched-off one isn't", found["mcp"] == ["jarvis_test"])
check("the connected API is discovered", found["api"] == ["notes_api"])
check("both count as changed the first time", found["changed"] == ["jarvis_test", "notes_api"])
check("both wait for review", sorted(p["service"] for p in tool_onboarding.pending()) == ["jarvis_test", "notes_api"])
for svc in ("jarvis_test", "notes_api"):
    tool_onboarding.decide(svc, "approve")

# ---- 2. a startup with nothing new changes nothing ------------------------------------------
before = len(notes)
found = tool_onboarding.check_for_changes()
check("the next startup looks at both again", found["mcp"] == ["jarvis_test"] and found["api"] == ["notes_api"])
check("and finds nothing changed", found["changed"] == [] and not tool_onboarding.pending() and len(notes) == before)

# ---- 3. a server that gained a tool while Jarvis was closed ---------------------------------
# Pretend the catalogue last saw jarvis_test without its "add" tool.
tool_catalog.update(lambda c: (c["jarvis_test"]["tools"].pop("add"), c["jarvis_test"].update(tools_hash="old")))
SPEC["paths"]["/notes/{id}"] = {"delete": {"operationId": "deleteNote", "summary": "Delete a note",
                                           "parameters": [{"name": "id", "in": "path", "required": True}]},
                                "put": {"operationId": "renameNote", "summary": "Rename a note"}}
found = tool_onboarding.check_for_changes()
check("both changes are noticed at startup", found["changed"] == ["jarvis_test", "notes_api"])
t = tool_catalog.get("jarvis_test")["tools"]
check("the new MCP tool waits for review", t["add"]["status"] == "pending")
check("the tools already decided keep their decision", t["echo"]["status"] == "enabled")
t = tool_catalog.get("notes_api")["tools"]
check("the new API operation waits for review", t["rename_note"]["status"] == "pending")
check("the new destructive operation waits in the Control room", t["delete_note"]["status"] == "held")
check("the old API operation stays approved", t["list_notes"]["status"] == "enabled")

# ---- 4. what the gate is told ----------------------------------------------------------------
state = tool_onboarding.tools_state("notes_api")
check("tools_state says the review is open, with counts and time left",
      state["state"] == "awaiting_review" and state["pending"] == 1 and state["held"] == 1
      and 0 < state["expires_in"] <= tool_onboarding.WAIT_SECONDS)
check("the gate's row for an API carries it", tool_executor.describe_connectable("api:notes_api")["review"]["pending"] == 1)
check("and so does an MCP server's", tool_executor.describe_connectable("mcp:jarvis_test")["review"]["state"] == "awaiting_review")
check("both are waited on", tool_onboarding.waiting_on_review(["jarvis_test", "notes_api", "google_search"])
      == ["jarvis_test", "notes_api"])

check("a model error shows as one readable line, not its JSON body",
      tool_onboarding._short_error(Exception("400 INVALID_ARGUMENT. {'error': {'message': 'API key not valid.'}}"))
      == "400 INVALID_ARGUMENT: API key not valid.")

# ---- 5. execution waits for the review -------------------------------------------------------
import multi_agent_coordinator as mac
mac.REVIEW_POLL_SECONDS = 0.02
events = []


async def run_wait(answer):
    task = asyncio.create_task(mac.wait_for_tool_reviews(["mcp:jarvis_test", "api:notes_api", "arxiv_api"],
                                                         events.append))
    await asyncio.sleep(0.2)
    still_waiting = not task.done()
    answer()
    return still_waiting, await asyncio.wait_for(task, 5)


def user_answers():
    tool_onboarding.decide("jarvis_test", "approve")
    tool_onboarding.decide("notes_api", "reject")


waiting, waited = asyncio.run(run_wait(user_answers))
check("execution does not start while tools are in review", waiting)
check("it goes ahead once the user answers, rejected or not", waited == ["jarvis_test", "notes_api"])
messages = [e["data"]["message"] for e in events]
check("the pipeline says what it is waiting for, and where", "Control room" in messages[0] and "jarvis_test" in messages[0])
check("it says so once, not every poll", len(messages) == 2 and "starting the agents" in messages[1])

# A review nobody answers approves itself, so the wait always ends.
tool_catalog.update(lambda c: (c["jarvis_test"]["tools"].pop("add"), c["jarvis_test"].update(tools_hash="old2")))
tool_onboarding.observe_mcp("jarvis_test", mcp_client.ensure_server_running("jarvis_test")["tools"])
check("a new review is open", tool_onboarding.waiting_on_review(["jarvis_test"]) == ["jarvis_test"])
waiting, waited = asyncio.run(run_wait(
    lambda: tool_catalog.update(lambda c: c["jarvis_test"]["review"].update(deadline=time.time() - 1))))
check("an unanswered review approves itself and execution starts", waiting and waited == ["jarvis_test"]
      and tool_catalog.get("jarvis_test")["tools"]["add"]["status"] == "enabled")

check("a tool held in the Control room never holds up a pipeline",
      tool_catalog.get("jarvis_test")["tools"]["delete_note"]["status"] == "held"
      and asyncio.run(mac.wait_for_tool_reviews(["mcp:jarvis_test"])) == [])
check("nothing to wait on returns at once", asyncio.run(mac.wait_for_tool_reviews([])) == [])

mcp_client.shutdown_all()

# ---- 6. the pages ----------------------------------------------------------------------------
with open(os.path.join(ROOT, "plan.html"), encoding="utf-8") as f:
    plan_html = f.read()
check("the Plugging Gate shows each service's review state", plan_html.count("${toolReviewNote(r)}") == 2)

try:
    tool_onboarding.start_startup_check = lambda: None      # no second check racing this one
    import jarvis
except ImportError as e:
    print(f"SKIP  Control room routes (jarvis needs desktop modules here: {e})")
else:
    with open(api_connector.REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump({"Mini API": {"status": "up"}, "mini_api": {"status": "up"}}, f)
    client = jarvis.app.test_client()
    rows = [a for a in client.get("/api/tools/overview").get_json()["apis"] if "mini" in a["service"].lower()]
    check("a service saved under two spellings is listed once", [a["service"] for a in rows] == ["mini_api"])
    client.post("/api/tools/disconnect", json={"service_name": "mini_api"})
    reg = api_connector.load_registry()
    check("disconnecting it disconnects both spellings",
          reg["Mini API"]["status"] == reg["mini_api"]["status"] == "unknown")

server.shutdown()
print("\nAll startup and gate checks passed.")
