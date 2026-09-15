"""Agents ask the user for tools they need, on the Commands page, instead of making do.

Pipeline 8's AI Industry Analyst decided on its own that "no tool calls are
appropriate" and wrote from memory. Now any agent can call request_tool; the user
approves or rejects it, and an approved tool Jarvis has is added mid-run.
Checked without a real model.
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

from agents import tool_requests, tool_executor, research_agent, execution_agent

PROJECT = "__tool_requests_test__"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def reply(text=None, calls=None):
    return type("Response", (), {"text": text, "function_calls": calls})()


def call(name, **args):
    return type("Call", (), {"name": name, "args": args})()


def decl(name):
    return {"name": name, "description": name,
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}


WEB, INSPECT = decl("web_search"), decl("inspect_website")


def fake_get_tools(tools_needed, project_name, context=None):
    """What Jarvis would bind for one requested name."""
    name = tools_needed[0]
    if name == "inspect_website":
        return [WEB, INSPECT], {"web_search": print, "inspect_website": print}, []
    if name == "web_search":
        return [WEB], {"web_search": print}, []
    if name == "google_docs":
        return [WEB], {"web_search": print}, ["google_docs (service 'google_docs_api' not yet configured at the Plugging Gate)"]
    return [WEB], {"web_search": print}, [f"{name} (no real connector implemented yet — do not claim to have used it)"]


tool_executor.get_tools_for_execution_agent = fake_get_tools
tool_requests.POLL_SECONDS = 0.01

events = []


def logger(event):
    event["plan_id"] = "8"
    events.append(event)


async def answer_requests(decisions, seen):
    """Stands in for the user on the Commands page."""
    while decisions:
        waiting = tool_requests.pending()
        if waiting:
            seen.append(waiting[0])
            decision, reason = decisions.pop(0)
            tool_requests.decide(waiting[0]["request_id"], decision, reason)
        await asyncio.sleep(0.005)


async def request(tool, decisions, agent_config=None):
    seen = []
    agent_config = agent_config if agent_config is not None else {
        "agent_id": "ai_industry_analyst_cycle1_adv_1", "role": "AI Industry Analyst", "brief": "Refine the niche."}
    declarations, handlers = [WEB], {"web_search": print}
    user = asyncio.create_task(answer_requests(list(decisions), seen))
    result, changed = await tool_requests.handle(
        {"tool": tool, "why": "to see what buyers search for"}, agent_config=agent_config, kind="research",
        project_name=PROJECT, declarations=declarations, handlers=handlers, event_logger=logger)
    user.cancel()
    return result, changed, seen, declarations, handlers, agent_config


# ---- 1. the queue ---------------------------------------------------------------------------
check("an unknown request can't be answered", "error" in tool_requests.decide("nope", "approve"))
check("only approve or reject are answers", "error" in tool_requests.decide("nope", "maybe"))

# ---- 2. what an answer does -------------------------------------------------------------------
result, changed, seen, declarations, handlers, _ = asyncio.run(request("inspect_website", [("approve", "")]))
check("the request reaches the Commands page with the agent, pipeline, tool and reason",
      seen and seen[0]["tool"] == "inspect_website" and seen[0]["plan_id"] == "8"
      and seen[0]["role"] == "AI Industry Analyst" and seen[0]["why"] == "to see what buyers search for"
      and seen[0]["availability"] == "ready")
check("approving a tool Jarvis has gives it to the agent straight away",
      result["status"] == "approved" and changed is True
      and [d["name"] for d in declarations] == ["web_search", "inspect_website"] and "inspect_website" in handlers)

result, changed, *_ = asyncio.run(request("inspect_website", [("reject", "Search is enough here")]))
check("rejecting tells the agent why and to carry on without it",
      result["status"] == "rejected" and "Search is enough here" in result["message"] and changed is False)

result, changed, seen, _, _, config = asyncio.run(request("crunchbase", [("approve", "")]))
check("a tool Jarvis has no connection for is marked as such",
      seen[0]["availability"] == "no_connector" and "no real connector" in seen[0]["availability_detail"])
check("approving it adds it to the pipeline's tool list, and the agent knows it can't use it yet",
      result["status"] == "approved_not_connected" and changed is False and config["tools_needed"] == ["crunchbase"])

_, _, seen, *_ = asyncio.run(request("google_docs", [("approve", "")]))
check("a tool that only needs connecting is marked as needing setup", seen[0]["availability"] == "needs_setup")

before = len(tool_requests.log())
result, changed, seen, *_ = asyncio.run(request("web_search", []))
check("asking for a tool it already has is answered without bothering the user",
      result["status"] == "already_available" and not seen and len(tool_requests.log()) == before)

tool_requests.WAIT_SECONDS = 0
result, *_ = asyncio.run(request("semrush", []))
tool_requests.WAIT_SECONDS = 600
check("an unanswered request times out as turned down",
      result["status"] == "rejected" and "Nobody answered" in result["message"]
      and tool_requests.log()[0]["decision"] == "timeout" and tool_requests.pending() == [])
check("answered requests are logged, newest first",
      [e["tool"] for e in tool_requests.log()[:3]] == ["semrush", "google_docs", "crunchbase"])
check("waiting and the answer are both in the pipeline's events",
      {"tool_request_waiting", "tool_request_resolved"} <= {e["event_type"] for e in events})


# ---- 3. a research agent asks, gets the tool, and uses it --------------------------------------
class AsksThenInspects:
    def __init__(self):
        self.sent, self.configs = [], []

    def send_message(self, message, config=None):
        self.sent.append(message)
        self.configs.append(config)
        turn = len(self.sent)
        if turn == 1:
            return reply(calls=[call("request_tool", tool="inspect_website", why="to read competitor sites")])
        if turn == 2:
            return reply(calls=[call("inspect_website", url="https://competitor.example")])
        return reply(json.dumps({"status": "ok", "confidence": 0.8, "findings": {"inspected": True},
                                 "sources": ["inspect_website: https://competitor.example"]}))


def use_chat(chat):
    genai.Client = lambda *a, **k: type("Client", (), {
        "chats": type("Chats", (), {"create": staticmethod(lambda **kw: chat)})()})()


research_agent.get_tools_for_execution_agent = lambda *a, **k: ([WEB], {"web_search": print}, [])
research_agent.run_tool = lambda handlers, project, agent, name, args: (
    {"status": "ok", "url": args.get("url")} if name in handlers else {"status": "error", "error": "unbound"})


async def run_research(decisions):
    seen, chat = [], AsksThenInspects()
    use_chat(chat)
    user = asyncio.create_task(answer_requests(list(decisions), seen))
    cfg = {"agent_id": "ai_industry_analyst_cycle1_adv_1", "role": "AI Industry Analyst",
           "brief": "Refine the niche.", "tools_needed": ["web_search"]}
    out = await research_agent.run_research_agent(cfg, project_name=PROJECT, event_logger=logger)
    user.cancel()
    return out, chat, cfg


result, chat, cfg = asyncio.run(run_research([("approve", "")]))
added = chat.configs[2]
check("once approved, the agent's next message carries the new tool",
      chat.configs[1] is not None and added is not None
      and "inspect_website" in [f.name for f in added.tools[0].function_declarations])
check("the agent then really uses it, and it counts as a source",
      result["status"] == "ok" and result["sources"] == ["inspect_website: https://competitor.example"]
      and result["tool_calls_made"] == 1)

prompt = next(e["data"]["content"] for e in events
              if e.get("event_type") == "thinking" and e.get("data", {}).get("thinking_type") == "system_prompt")
check("research agents are told they can ask for tools", "call request_tool" in prompt)
check("the no-tools nudge points at request_tool", "request_tool" in research_agent.NO_TOOLS_NUDGE)


# ---- 4. execution agents can ask too ---------------------------------------------------------------
class AsksThenAnswers:
    def __init__(self):
        self.sent = []

    def send_message(self, message, config=None):
        self.sent.append(message)
        if len(self.sent) == 1:
            return reply(calls=[call("request_tool", tool="semrush", why="keyword volumes")])
        return reply(json.dumps({"status": "ok", "summary": "written without keyword volumes"}))


execution_agent.get_tools_for_execution_agent = lambda *a, **k: ([WEB], {"web_search": print}, [])


async def run_execution(decisions):
    seen, chat = [], AsksThenAnswers()
    use_chat(chat)
    user = asyncio.create_task(answer_requests(list(decisions), seen))
    cfg = {"agent_id": "report_writer_exec_1", "role": "Report Writer", "brief": "Write it.",
           "tools_needed": [], "output_spec": {}}
    out = await execution_agent.run_execution_agent(cfg, {}, project_name=PROJECT, event_logger=logger)
    user.cancel()
    return out, chat, seen


result, chat, seen = asyncio.run(run_execution([("reject", "")]))
told = chat.sent[1][0].function_response.response
check("an execution agent's request goes to the same page", seen and seen[0]["kind"] == "execution")
check("it is told the answer and carries on", told["status"] == "rejected" and result["status"] == "ok")


# ---- 5. the Commands page ---------------------------------------------------------------------------
import jarvis

client = jarvis.app.test_client()
check("pending tool requests can be listed", client.get("/tool_requests/pending").get_json()["pending"] == [])
check("answered tool requests can be listed", len(client.get("/tool_requests/log").get_json()["log"]) >= 3)
check("answering needs a request id", client.post("/tool_requests/decide", json={"decision": "approve"}).status_code == 400)
check("a bad answer is refused", client.post("/tool_requests/decide", json={"request_id": "x", "decision": "later"}).status_code == 400)

page = open(os.path.join(ROOT, "commands.html"), encoding="utf-8").read()
check("the Commands page shows tool requests with approve and reject",
      'id="tool-requests"' in page and "/tool_requests/decide" in page and "/tool_requests/pending" in page)

print("\nAll tool request checks passed.")
