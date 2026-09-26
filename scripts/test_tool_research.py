"""Before a new service's tools go up for review, Jarvis researches them and writes a card for each.

The card (what it does, when to use it, example arguments, limits, what it goes
with) becomes the description agents see. The researcher can describe only the
tools the server really listed, and can raise a tool's risk but never lower it.
If research fails, the review opens anyway with the server's own descriptions.
The model is stubbed; no network is used.
"""
import json, os, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("GEMINI_API_KEY", None)

from connectors import tool_catalog, mcp_client
from agents import tool_onboarding, tool_researcher, tool_executor
import control_room


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_research_")
tool_catalog.CATALOG_PATH = os.path.join(tmp, "tool_catalog.json")
mcp_client.REGISTRY_PATH = os.path.join(tmp, "mcp_registry.json")
with open(mcp_client.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({"notes": {"enabled": True, "command": "unused"}}, f)
notes = []
tool_onboarding.set_notifier(notes.append)
tool_onboarding.RESEARCH_IN_BACKGROUND = False

TOOLS = [
    {"name": "list_pages", "description": "List pages", "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "update_page", "description": "Update a page", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "delete_page", "description": "Delete a page", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
]

ANSWER = {
    "summary": "A notes app. Jarvis can read and edit pages in it.",
    "use_for": ["keeping research notes", "drafting documents"],
    "tools": {
        "list_pages": {"what": "Lists the pages in the workspace.", "when": "Before reading or editing a page, to find its id.",
                       "example_args": {"limit": 20}, "risk": "read", "limits": "At most 100 per call.", "goes_with": "update_page"},
        "update_page": {"what": "Replaces a page's text.", "when": "To publish an edited page to everyone.",
                        "example_args": {"id": "abc"}, "risk": "destructive", "limits": "", "goes_with": "list_pages"},
        "delete_page": {"what": "Deletes a page.", "when": "Only when asked.", "example_args": "not a dict", "risk": "read"},
        "invented_tool": {"what": "Something the server never listed.", "risk": "read"},
    },
    "sources": ["https://notes.example/docs", "notes docs page", "javascript:alert(1)"],
}
prompts = []


def answering(answer):
    def _run(prompt):
        prompts.append(prompt)
        return "```json\n" + json.dumps(answer) + "\n```"
    return _run


# ---- 1. checking what the researcher wrote ------------------------------------------------
entry, _ = tool_catalog.record_inventory("scratch", "mcp", TOOLS, tool_executor._mcp_function_name)
checked = tool_researcher.validate(ANSWER, entry["tools"])
check("a tool the server never listed is dropped", "invented_tool" not in checked["cards"])
check("risk can go up: update_page becomes destructive", checked["cards"]["update_page"]["risk"] == "destructive")
check("risk never goes down: delete_page stays destructive", checked["cards"]["delete_page"]["risk"] == "destructive")
check("example arguments that aren't an object are dropped", checked["cards"]["delete_page"]["example_args"] == {})
check("only real links are kept as sources", checked["sources"] == ["https://notes.example/docs"])
desc = tool_researcher.card_description(checked["cards"]["list_pages"], "List pages")
check("the card becomes a description an agent can act on",
      "When to use: Before reading" in desc and '"limit": 20' in desc and "At most 100" in desc)
try:
    tool_researcher._parse_json("no json here")
    check("text that isn't JSON is a failed research", False)
except tool_researcher.ResearchFailed:
    check("text that isn't JSON is a failed research", True)

# ---- 2. research runs before the review opens --------------------------------------------
tool_researcher._run_model = answering(ANSWER)
tool_catalog.record_inventory("notes", "mcp", TOOLS, tool_executor._mcp_function_name)
tool_onboarding.start_research("notes")
entry = tool_catalog.get("notes")
check("the prompt lists the real tools and their schemas",
      "update_page" in prompts[-1] and '"limit"' in prompts[-1] and "Do not call the service's own tools" in prompts[-1])
check("research is recorded as done, with its sources",
      entry["research"]["status"] == "done" and entry["research"]["sources"] == ["https://notes.example/docs"])
check("the service summary and uses are kept", entry["summary"].startswith("A notes app") and len(entry["use_for"]) == 2)
check("the tool's description is now the researched one", "When to use" in entry["tools"]["list_pages"]["description"])
check("the server's own words are kept alongside", entry["tools"]["list_pages"]["server_description"] == "List pages")
check("a tool research found destructive moves to the Control room",
      entry["tools"]["update_page"]["status"] == "held" and entry["tools"]["update_page"]["risk"] == "destructive")
review = tool_onboarding.pending()
check("then the review opens with only the tools still to decide",
      len(review) == 1 and [t["name"] for t in review[0]["tools"]] == ["list_pages"])
check("the review carries the summary and marks the tool researched",
      review[0]["summary"].startswith("A notes app") and review[0]["tools"][0]["researched"])
check("the Control room shows the researched text for held tools",
      any("publish an edited page" in t["description"] for t in control_room.held_tools()))

tool_onboarding.decide("notes", "approve")
decls, _, _ = tool_executor.get_tools_for_execution_agent([], "__research_test__")
listing = [d for d in decls if d["name"] == "mcp_notes_list_pages"]
check("agents see the researched description", listing and "When to use" in listing[0]["description"])

# ---- 3. an unchanged tool keeps its card; only new tools are researched --------------------
prompts.clear()
tool_researcher._run_model = answering({"summary": "", "tools": {"get_page": {"what": "Reads one page.", "risk": "read"}}})
tool_catalog.record_inventory("notes", "mcp", TOOLS + [{"name": "get_page", "description": "Get a page"}],
                              tool_executor._mcp_function_name)
tool_onboarding.start_research("notes")
entry = tool_catalog.get("notes")
check("only the new tool is sent to research", "get_page" in prompts[-1] and "- list_pages" not in prompts[-1])
check("the earlier card and decision are kept",
      entry["tools"]["list_pages"]["status"] == "enabled" and "When to use" in entry["tools"]["list_pages"]["description"])
check("an empty summary doesn't wipe the old one", entry["summary"].startswith("A notes app"))
tool_onboarding.decide("notes", "approve")

# ---- 4. failed research still opens the review ---------------------------------------------
def broken(prompt):
    raise tool_researcher.ResearchFailed("GEMINI_API_KEY is not set")


tool_researcher._run_model = broken
tool_catalog.record_inventory("wiki", "mcp", TOOLS[:1], tool_executor._mcp_function_name)
notes.clear()
tool_onboarding.start_research("wiki")
entry = tool_catalog.get("wiki")
check("a failed research is recorded with its reason",
      entry["research"]["status"] == "failed" and "GEMINI_API_KEY" in entry["research"]["error"])
check("the review opens anyway, with the server's own description",
      tool_onboarding.pending()[-1]["service"] == "wiki" and tool_onboarding.pending()[-1]["tools"][0]["description"] == "List pages")
check("and the user is told research failed", any("could not research" in n for n in notes))
tool_onboarding.decide("wiki", "approve")

# ---- 5. in the background: researching, no review and no timer until it's done -------------
release = threading.Event()


def slow(prompt):
    release.wait(10)
    return json.dumps({"summary": "Slow docs.", "tools": {}})


tool_researcher._run_model = slow
tool_onboarding.RESEARCH_IN_BACKGROUND = True
tool_catalog.record_inventory("slowdocs", "mcp", TOOLS[:1], tool_executor._mcp_function_name)
tool_onboarding.start_research("slowdocs")
check("while it researches, the service shows as researching",
      [r["service"] for r in tool_onboarding.researching()] == ["slowdocs"])
check("and no review is open yet, so nothing can approve itself", not tool_onboarding.pending())
release.set()
for _ in range(200):
    if tool_onboarding.pending():
        break
    time.sleep(0.02)
check("when research ends the review opens", [p["service"] for p in tool_onboarding.pending()] == ["slowdocs"])
tool_onboarding.decide("slowdocs", "approve")
tool_onboarding.RESEARCH_IN_BACKGROUND = False

# ---- 6. research cut off by a restart starts again -----------------------------------------
tool_researcher._run_model = answering({"summary": "Restarted.", "tools": {}})
tool_catalog.record_inventory("stranded", "mcp", TOOLS[:1], tool_executor._mcp_function_name)
tool_catalog.update(lambda cat: cat["stranded"].update(state="researching", research={"status": "running"}))
tool_onboarding.sweep()
check("a service left researching by a restart is researched again and reviewed",
      tool_catalog.get("stranded")["research"]["status"] == "done"
      and any(p["service"] == "stranded" for p in tool_onboarding.pending()))

# ---- 7. the research loop itself, with a stand-in Gemini --------------------------------
import importlib
from google import genai
from agents import tool_executor as te

tool_researcher = importlib.reload(tool_researcher)       # the real _run_model again
searched = []


class FakeChat:
    def __init__(self):
        self.turn = 0

    def send_message(self, message):
        self.turn += 1
        if self.turn == 1:
            call = type("Call", (), {"name": "web_search", "args": {"query": "notes app api docs"}})()
            return type("Response", (), {"function_calls": [call], "text": None})()
        sent_back = [getattr(p, "function_response", None) for p in message]
        return type("Response", (), {"function_calls": None,
                                     "text": json.dumps({"summary": f"after {len(sent_back)} result(s)", "tools": {}})})()


class FakeClient:
    def __init__(self, api_key=None):
        self.chats = type("Chats", (), {"create": lambda _self, model, config: FakeChat()})()


real_client, real_search = genai.Client, te.web_search_impl
genai.Client = FakeClient
te.web_search_impl = lambda project_name, agent_id, query: searched.append(query) or {"status": "ok", "summary": "docs"}
os.environ["GEMINI_API_KEY"] = "stand-in"
try:
    out = tool_researcher.research("notes", "mcp", {"list_pages": {"risk": "read", "description": "List pages"}})
finally:
    genai.Client, te.web_search_impl = real_client, real_search
    os.environ.pop("GEMINI_API_KEY", None)
check("the researcher really searches, gets the result back, and answers",
      searched == ["notes app api docs"] and out["summary"] == "after 1 result(s)")

print("\nAll tool research checks passed.")
