"""No cap on agents' tool calls — every few rounds of them the user decides, on the Commands page, whether they keep going.

Agents used to be cut off after six rounds of tool calls and lose everything they had found.
Now they pause for a review beside the other approvals: continue for another round,
or stop and give a final answer with what they have. Like every approval there, it
carries an optional reason to the agent, is logged, and an unanswered review times
out — as a stop, so the pipeline finishes with what it found. Checked without a real model.
"""
import asyncio, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

from agents import tool_review, research_agent, execution_agent

PROJECT = "__tool_review_test__"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def reply(text=None, calls=None):
    return type("Response", (), {"text": text, "function_calls": calls})()


def call(name, **args):
    return type("Call", (), {"name": name, "args": args})()


def texts(message):
    return [getattr(p, "text", None) or "" for p in (message if isinstance(message, list) else [])]


tool_review.POLL_SECONDS = 0.01

# ---- 1. the review queue ----------------------------------------------------------------
check("an unknown review can't be answered", "error" in tool_review.decide("nope", "continue"))
check("only continue or stop are answers", "error" in tool_review.decide("nope", "maybe"))


async def answer_reviews(decisions, seen):
    """Stands in for the user on the Commands page: (decision, reason) per review."""
    while decisions:
        waiting = tool_review.pending()
        if waiting:
            seen.append(waiting[0])
            decision, reason = decisions.pop(0)
            tool_review.decide(waiting[0]["request_id"], decision, reason)
        await asyncio.sleep(0.005)


events = []


def logger(event):
    event["plan_id"] = "42"          # what the pipeline's own logger does
    events.append(event)


async def one_checkpoint(decisions):
    seen = []
    user = asyncio.create_task(answer_reviews(list(decisions), seen))
    review = await tool_review.checkpoint(
        "analyst_cycle1_adv_1", "Analyst", "research", "Find the companies.",
        [{"tool": "web_search", "args": {"query": f"q{i}"}} for i in range(7)], logger)
    user.cancel()
    return review, (seen[0] if seen else None)


review, shown = asyncio.run(one_checkpoint([("continue", "keep checking Canada")]))
check("continue lets the agent keep going, with the user's reason",
      review == {"decision": "continue", "reason": "keep checking Canada"})
check("the review shows the agent, its pipeline, its brief, its latest calls and a countdown",
      shown["agent_id"] == "analyst_cycle1_adv_1" and shown["plan_id"] == "42" and shown["kind"] == "research"
      and shown["brief"] == "Find the companies." and shown["tool_calls"] == 7
      and shown["recent_calls"] == [f"web_search: q{i}" for i in range(2, 7)]
      and 0 < shown["expires_in"] <= tool_review.WAIT_SECONDS)
check("an answered review leaves the queue", tool_review.pending() == [])
check("waiting and the answer are both logged in the pipeline's events",
      [e["event_type"] for e in events[-2:]] == ["tool_review_waiting", "tool_review_resolved"])

review, _ = asyncio.run(one_checkpoint([("stop", "enough")]))
check("stop tells the agent to stop", review["decision"] == "stop")

tool_review.WAIT_SECONDS = 0
review, _ = asyncio.run(one_checkpoint([]))
tool_review.WAIT_SECONDS = 600
check("an unanswered review times out as a stop, like the other approvals",
      review["decision"] == "timeout" and tool_review.pending() == [])
check("answered and timed-out reviews are kept in the log, newest first",
      [r["decision"] for r in tool_review.log()[:3]] == ["timeout", "stop", "continue"])

check("a stop passes on the user's reason", 'They said: "enough"' in tool_review.stop_message({"decision": "stop", "reason": "enough"}))
check("a timeout says nobody answered", "Nobody answered" in tool_review.stop_message({"decision": "timeout", "reason": ""}))

announced = []
tool_review.set_notifier(announced.append)
asyncio.run(one_checkpoint([("continue", "")]))
check("a new review is announced, pointing at the Commands page",
      announced and "has made 7 tool calls" in announced[0] and "Commands page" in announced[0])
tool_review.set_notifier(None)


# ---- 2. research agents -------------------------------------------------------------------
DECLARATIONS = [{"name": "web_search", "description": "Search the web.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]


class SearchForever:
    """Keeps calling tools until told to stop, then answers."""
    def __init__(self, answer_after=None, obey_stop=True):
        self.sent, self.calls, self.answer_after, self.obey_stop = [], 0, answer_after, obey_stop

    def send_message(self, message, config=None):
        self.sent.append(message)
        stopped = any(tool_review.STOP_INSTRUCTION in t for t in texts(message))
        if (stopped and self.obey_stop) or (self.answer_after is not None and self.calls >= self.answer_after):
            return reply(json.dumps({"status": "ok", "confidence": 0.7, "findings": {"found": self.calls},
                                     "sources": [], "summary": "done"}))
        self.calls += 1
        return reply(calls=[call("web_search", query=f"q{self.calls}")])


def use_chat(chat):
    genai.Client = lambda *a, **k: type("Client", (), {
        "chats": type("Chats", (), {"create": staticmethod(lambda **kw: chat)})()})()


research_agent.get_tools_for_execution_agent = lambda *a, **k: (DECLARATIONS, {"web_search": None}, [])
research_agent.run_tool = lambda handlers, project, agent, name, args: {"status": "ok", "results": []}
RESEARCH_CFG = {"agent_id": "analyst_cycle1_adv_1", "role": "Analyst", "brief": "Research.", "tools_needed": ["web_search"]}

check("research agents no longer have a tool-call cap", not hasattr(research_agent, "MAX_TOOL_TURNS"))

tool_review.REVIEW_EVERY = 100
chat = SearchForever(answer_after=10)
use_chat(chat)
result = asyncio.run(research_agent.run_research_agent(RESEARCH_CFG, project_name=PROJECT))
check("an agent can make more than six tool calls and still answer",
      result["status"] == "ok" and result["tool_calls_made"] == 10 and "stopped_by_user_after_tool_calls" not in result)


async def research_with_reviews(decisions, chat):
    seen = []
    use_chat(chat)
    user = asyncio.create_task(answer_reviews(list(decisions), seen))
    out = await research_agent.run_research_agent(RESEARCH_CFG, project_name=PROJECT, event_logger=logger)
    user.cancel()
    return out, seen


tool_review.REVIEW_EVERY = 3
chat = SearchForever()
result, seen = asyncio.run(research_with_reviews([("continue", "look at Canada too"), ("stop", "that's enough")], chat))
check("the agent pauses for a review every few rounds", [s["tool_calls"] for s in seen] == [3, 6])
check("a reason given with Continue reaches the agent",
      any('They said: "look at Canada too"' in t for m in chat.sent for t in texts(m)))
check("after stop it answers with what it found, and nothing is lost",
      result["status"] == "ok" and result["findings"] == {"found": 6}
      and result["stopped_by_user_after_tool_calls"] == 6 and result["tool_calls_made"] == 6)
check("the last tool results go back with the stop message and its reason",
      len(chat.sent[-1]) == 2 and 'They said: "that\'s enough"' in texts(chat.sent[-1])[-1])
check("nothing reports a tool-call cap any more",
      not any("Exceeded max tool-call turns" in json.dumps(e.get("data"), default=str) for e in events))

chat = SearchForever(obey_stop=False)
result, _ = asyncio.run(research_with_reviews([("stop", "")], chat))
check("an agent that ignores the stop gets no more tools and is marked partial",
      result["status"] == "partial" and "Stopped by the user" in result["blocked_reason"] and chat.calls == 5)


class SearchInBursts(SearchForever):
    """Asks for several searches at once each round, as models often do."""
    def send_message(self, message, config=None):
        self.sent.append(message)
        if any(tool_review.STOP_INSTRUCTION in t for t in texts(message)):
            return reply(json.dumps({"status": "ok", "confidence": 0.7, "findings": {"found": self.calls},
                                     "sources": [], "summary": "done"}))
        self.calls += 4
        return reply(calls=[call("web_search", query=f"q{self.calls}-{i}") for i in range(4)])


# Counting calls paused an agent that asked for six searches at once straight after
# its first round. The old cap, and the review now, count rounds.
chat = SearchInBursts()
result, seen = asyncio.run(research_with_reviews([("stop", "")], chat))
check("several calls in one round count as one round: the review comes after 3 rounds, not after the first",
      [s["tool_calls"] for s in seen] == [12] and result["stopped_by_user_after_tool_calls"] == 12)


# ---- 3. execution agents -------------------------------------------------------------------
execution_agent.get_tools_for_execution_agent = lambda *a, **k: (DECLARATIONS, {"web_search": None}, [])
execution_agent.run_tool = lambda handlers, project, agent, name, args: {
    "status": "ok", "action": "write_file", "path": f"Deliverables/{args.get('query')}.md"}
EXEC_CFG = {"agent_id": "writer_exec_1", "role": "Writer", "brief": "Write it.", "tools_needed": ["web_search"], "output_spec": {}}

check("execution agents no longer have a tool-call cap", not hasattr(execution_agent, "MAX_TOOL_TURNS"))


async def execute_with_reviews(decisions, chat):
    seen = []
    use_chat(chat)
    user = asyncio.create_task(answer_reviews(list(decisions), seen))
    out = await execution_agent.run_execution_agent(EXEC_CFG, {}, project_name=PROJECT, event_logger=logger)
    user.cancel()
    return out, seen


tool_review.REVIEW_EVERY = 2
result, seen = asyncio.run(execute_with_reviews([("stop", "")], SearchForever()))
check("an execution agent is reviewed the same way", seen and seen[0]["kind"] == "execution")
check("stopping it keeps the files it already wrote",
      result["stopped_by_user_after_tool_calls"] == 2 and len(result["artifacts"]) == 2)
result, seen = asyncio.run(execute_with_reviews([("stop", "")], SearchInBursts()))
check("an execution agent also counts rounds, not calls",
      [s["tool_calls"] for s in seen] == [8] and result["stopped_by_user_after_tool_calls"] == 8)


# ---- 4. the Commands page ---------------------------------------------------------------------
import jarvis

client = jarvis.app.test_client()
pending = client.get("/tool_reviews/pending").get_json()
check("waiting reviews can be listed, with the wait", pending["pending"] == [] and pending["wait_seconds"] == 600)
check("answered reviews can be listed", len(client.get("/tool_reviews/log").get_json()["log"]) >= 3)
check("answering needs a request id", client.post("/tool_reviews/decide", json={"decision": "stop"}).status_code == 400)
check("a bad answer is refused", client.post("/tool_reviews/decide", json={"request_id": "x", "decision": "later"}).status_code == 400)
check("the command centre's state no longer carries reviews", "tool_reviews" not in client.get("/state").get_json())

page = open(os.path.join(ROOT, "commands.html"), encoding="utf-8").read()
check("the Commands page shows reviews with continue, stop, a reason and a countdown",
      'id="tool-reviews"' in page and "/tool_reviews/decide" in page and "/tool_reviews/log" in page
      and "decideToolReview('${item.request_id}','continue')" in page
      and "decideToolReview('${item.request_id}','stop')" in page
      and 'id="review-why-${item.request_id}"' in page and "Stopped automatically in" in page)
centre = open(os.path.join(ROOT, "command_center.html"), encoding="utf-8").read()
check("the command centre no longer has its own review card", "tool-review" not in centre and "tool_reviews" not in centre)

print("\nAll tool review checks passed.")
