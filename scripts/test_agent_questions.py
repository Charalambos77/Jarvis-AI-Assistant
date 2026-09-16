"""An agent asking the user a question, and waiting for the answer.

Agents could ask for a tool but never for a fact only the user has, so they guessed:
pipeline 9 redefined "just starting to incorporate AI" to fit a list it had already
picked, and counted Malaysia as English-speaking. Now they ask, as often as they need.
"""
import asyncio, inspect, json, os, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agents import agent_questions as aq
from agents import research_agent, execution_agent

AGENT = {"agent_id": "economic_ai_trend_analyst_cycle1_lead", "role": "Economic & AI Trend Analyst",
         "brief": "Pick the countries."}
QUESTION = "Should the four countries already chosen stay in the final research, or only in the logs?"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def ask_in_background(**kwargs):
    """Run one ask_user call in its own loop, the way an agent would, and hand back its result."""
    box = {}
    args = {"question": QUESTION, "why": "Their note could mean either.", **kwargs}
    thread = threading.Thread(target=lambda: box.update(
        result=asyncio.run(aq.handle(args, agent_config=AGENT, kind="research"))), daemon=True)
    thread.start()
    for _ in range(200):                 # wait for it to reach the user
        if aq.pending():
            break
        time.sleep(0.02)
    return thread, box


# ---- 1. the question reaches the user ----------------------------------------------------
said = []
aq.set_notifier(said.append)
thread, box = ask_in_background(options=["Keep them in the research", "Logs only"])
waiting = aq.pending()
check("the question is waiting for the user", len(waiting) == 1 and waiting[0]["question"] == QUESTION)
item = waiting[0]
check("with who is asking, and why", item["role"] == AGENT["role"] and item["why"] == "Their note could mean either.")
check("and the answers it thinks are likely", item["options"] == ["Keep them in the research", "Logs only"])
check("it says how long the agent will wait", item["expires_in"] > 0 and item["waited"] >= 0)
check("the user is told a question is waiting", said and QUESTION in said[0])

# ---- 2. the answer goes back into that agent's own run ------------------------------------
check("answering an unknown question is refused", "error" in aq.answer("no_such_id", "yes"))
check("an empty answer is refused", "error" in aq.answer(item["request_id"], "   "))
check("answering works", aq.answer(item["request_id"], "Keep them in the research") == {
    "status": "ok", "request_id": item["request_id"], "agent_id": AGENT["agent_id"]})
thread.join(timeout=5)
result = box["result"]
check("the agent is given the user's own words",
      result["status"] == "answered" and result["answer"] == "Keep them in the research")
check("and told to follow them and say it asked", "Follow it exactly" in result["message"])
check("the question is not left waiting afterwards", aq.pending() == [])
check("it is kept in the log with the answer",
      aq.log()[0]["question"] == QUESTION and aq.log()[0]["answer"] == "Keep them in the research")

# ---- 3. the user can leave it to the agent ------------------------------------------------
thread, box = ask_in_background()
aq.answer(aq.pending()[0]["request_id"], skipped=True)
thread.join(timeout=5)
check("a skipped question tells the agent to decide and say so",
      box["result"]["status"] == "skipped" and "which way you decided" in box["result"]["message"])

# ---- 4. nobody answers ---------------------------------------------------------------------
patience, aq.WAIT_SECONDS = aq.WAIT_SECONDS, 0
try:
    thread = threading.Thread(target=lambda: None)
    unanswered = asyncio.run(aq.handle({"question": QUESTION}, agent_config=AGENT, kind="research"))
finally:
    aq.WAIT_SECONDS = patience
check("with no answer the agent carries on, and must say what it assumed",
      unanswered["status"] == "no_answer" and "which assumption you made" in unanswered["message"])
check("a question with no text is refused before it reaches the user",
      asyncio.run(aq.handle({"question": "  "}, agent_config=AGENT, kind="research"))["status"] == "error"
      and aq.pending() == [])

# ---- 5. every agent has it, and is told when to use it --------------------------------------
for module, kind in ((research_agent, "research"), (execution_agent, "execution")):
    source = inspect.getsource(module)
    check(f"a {kind} agent is given ask_user", "agent_questions.DECLARATION" in source)
    check(f"a {kind} agent's ask_user call reaches the user",
          f'agent_questions.handle(\n                        tool_args, agent_config=agent_config, kind="{kind}"' in source)
    check(f"a {kind} agent is told to ask instead of guessing",
          "ask_user" in source and "as many questions as you need" in source)
check("the description tells it to ask one at a time, as many as it needs",
      "one question at a time" in aq.DECLARATION["description"] and "as many as you need" in aq.DECLARATION["description"])

# ---- 6. the pop-up and its routes -------------------------------------------------------------
import jarvis
client = jarvis.app.test_client()
check("the page asks Jarvis what is waiting", client.get("/questions/pending").get_json()["pending"] == [])
check("answering without saying which question is refused", client.post("/questions/answer", json={}).status_code == 400)
check("answering a question nobody is waiting on is refused",
      client.post("/questions/answer", json={"request_id": "nope", "answer": "x"}).status_code == 400)

thread, box = ask_in_background()
posted = client.post("/questions/answer", json={"request_id": aq.pending()[0]["request_id"], "answer": "Logs only"})
thread.join(timeout=5)
check("an answer sent from the pop-up reaches the agent",
      posted.status_code == 200 and box["result"]["answer"] == "Logs only")
check("the log is readable from the page", any(e["answer"] == "Logs only" for e in client.get("/questions/log").get_json()["log"]))

script = client.get("/questions_ui.js")
check("the pop-up script is served", script.status_code == 200 and b"questions/answer" in script.data)
pages = [p for p in os.listdir(ROOT) if p.endswith(".html")]
with_nav = [p for p in pages if 'src="nav_ui.js"' in open(os.path.join(ROOT, p), encoding="utf-8").read()]
missing = [p for p in with_nav if 'src="questions_ui.js"' not in open(os.path.join(ROOT, p), encoding="utf-8").read()]
check(f"every page loads it, so the question finds the user wherever they are ({len(with_nav)} pages)", not missing)

print("\nAll agent question checks passed.")
