"""What went wrong in pipeline 7's Cycle 2, checked without a real model.

A review that was "mostly positive, with some complaints" was treated as a
conflict. The rerun threw away four agents' findings, let the Brain swap the ad
and sales-process agents for ones that duplicated Cycle 3, and overwrote the
first attempt's files. The lead answered without calling a tool, at confidence
1.0, citing searches another agent had run a cycle earlier.
"""
import asyncio, inspect, json, os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

import multi_agent_coordinator as mac
from agents import research_agent, synthesis

PROJECT = "__cycle_integrity_test__"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def reply(text=None, calls=None):
    return type("Response", (), {"text": text, "function_calls": calls})()


class _Models:
    """Hands back scripted replies in order and records every prompt."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.contents = []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.contents.append(contents)
        return reply(self.replies.pop(0))


def use_models(replies):
    models = _Models(replies)
    genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


# ---- 1. what counts as a conflict ---------------------------------------------------
prompt = synthesis._detect_conflicts_prompt("{}")
check("mixed evidence is not a conflict", "mixed evidence" in prompt and "NOT conflicts" in prompt)
check("each conflict says whether it needs new research", '"needs_new_research"' in prompt)

blocking, reconcilable = synthesis.split_conflicts([
    {"description": "fonts", "needs_new_research": True},
    {"description": "reviews", "needs_new_research": False},
    {"description": "unflagged"},
    {"description": "as text", "needs_new_research": "true"},
    "not a conflict",
])
check("only conflicts needing new research block",
      [c["description"] for c in blocking] == ["fonts", "as text"]
      and [c["description"] for c in reconcilable] == ["reviews", "unflagged"])

reviews = {"description": "Reviews are mostly positive but some mention scope creep",
           "agents_involved": ["lead"], "needs_new_research": False}
models = use_models([json.dumps({"has_conflicts": True, "conflicts": [reviews]}),
                     json.dumps({"customer_experience": "mixed"})])
result = asyncio.run(synthesis.run_synthesis_agent([{"agent_id": "lead", "findings": {}}]))
check("a disagreement the findings can settle does not rerun the cycle",
      result["status"] == "ok" and result["has_conflicts"] is False)
check("it is written into the blueprint with both sides",
      "DISAGREEMENTS" in models.contents[1] and "scope creep" in models.contents[1]
      and result["reconciled_conflicts"] == [reviews])

fonts = {"description": "Georgia or Lato", "agents_involved": ["a1", "a2"], "needs_new_research": True}
models = use_models([json.dumps({"has_conflicts": True, "conflicts": [fonts, reviews]})])
result = asyncio.run(synthesis.run_synthesis_agent([{"agent_id": "lead", "findings": {}}]))
check("a conflict that needs checking still reruns",
      result["has_conflicts"] is True and result["conflicts"] == [fonts] and len(models.contents) == 1)


# ---- 2. the rerun keeps the team and the work -----------------------------------------
cycles = [{"cycle_id": 1, "lead_specialist": {"agent_id": "c1"}},
          {"cycle_id": 2, "lead_specialist": {"agent_id": "c2"}}]
check("the re-planned cycle is picked by id, not position", mac.pick_cycle(cycles, 2)["lead_specialist"]["agent_id"] == "c2")
check("a lone re-planned cycle is accepted", mac.pick_cycle([{"cycle_id": 9}], 2) == {"cycle_id": 9})
check("no match among several is no match", mac.pick_cycle(cycles, 3) is None)
check("the rejection path no longer takes the first cycle",
      "updated_cycles[0]" not in inspect.getsource(mac.run_full_pipeline))


def fresh_cycle():
    return {
        "cycle_id": 2,
        "lead_specialist": {"agent_id": "strategist_lead", "role": "Strategist", "brief": "Lead brief."},
        "advisory_agents": [
            {"agent_id": "ads_adv_1", "role": "Ad Analyst", "brief": "Ads brief."},
            {"agent_id": "cx_adv_2", "role": "CX Researcher", "brief": "CX brief."},
            {"agent_id": "sales_adv_3", "role": "Sales Analyst", "brief": "Sales brief."},
        ],
    }


cycle = fresh_cycle()
brain_replan = {"cycle_id": 2,
                "lead_specialist": {"agent_id": "strategist_lead", "role": "Strategist", "brief": "Settle the reviews."},
                "advisory_agents": [{"agent_id": "seo_adv_2", "role": "SEO Analyst", "brief": "Cycle 3 work."}]}
rerun = mac.rebrief_for_conflict(cycle, brain_replan, ["strategist_lead"], "reviews disagree")
check("only the agents the conflict names run again", rerun == ["strategist_lead"])
check("the team stays the same: nobody dropped, nobody added",
      [a["agent_id"] for a in cycle["advisory_agents"]] == ["ads_adv_1", "cx_adv_2", "sales_adv_3"])
check("an involved agent takes its new brief from the re-plan", cycle["lead_specialist"]["brief"] == "Settle the reviews.")
check("agents not involved keep their briefs", cycle["advisory_agents"][0]["brief"] == "Ads brief.")

cycle = fresh_cycle()
rerun = mac.rebrief_for_conflict(cycle, None, ["cx_adv_2"], "reviews disagree")
check("with no re-plan, the involved agent gets the conflict added to its brief",
      rerun == ["cx_adv_2"] and "reviews disagree" in cycle["advisory_agents"][1]["brief"]
      and cycle["advisory_agents"][1]["brief"].startswith("CX brief."))
mac.rebrief_for_conflict(cycle, None, ["cx_adv_2"], "ratings disagree")
second = cycle["advisory_agents"][1]["brief"]
check("a second conflict replaces the note instead of stacking another under it",
      second.count("CONFLICTED") == 1 and "ratings disagree" in second
      and "reviews disagree" not in second and second.startswith("CX brief."))

cycle = fresh_cycle()
rerun = mac.rebrief_for_conflict(cycle, None, ["someone_else"], "x")
check("a conflict naming nobody in the cycle reruns everyone", len(rerun) == 4)

source = inspect.getsource(mac.run_full_pipeline)
check("the cycle loop only runs agents whose findings were not kept",
      "agents_to_run = [a for a in all_agents if a.get(\"agent_id\") not in kept_results]" in source
      and "kept_results = {aid: r for aid, r in results_by_id.items() if aid not in rerun_ids}" in source)
check("a rejected cycle runs in full again", "kept_results = {}  # a rejected cycle" in source)

agents_dir = os.path.join(mac.BASE_DIR, "Let Jarvis Handle It", PROJECT, "Implementation plan", "Agents")
shutil.rmtree(os.path.join(mac.BASE_DIR, "Let Jarvis Handle It", PROJECT), ignore_errors=True)
try:
    mac.save_research_findings_file("77", "cx_adv_2", {"confidence": 0.4, "sources": []}, PROJECT)
    mac.save_research_findings_file("77", "cx_adv_2", {"confidence": 0.9, "sources": ["web_search: a"],
                                                       "unverified_sources": ["web_search: b"]}, PROJECT, attempt=1)
    first = open(os.path.join(agents_dir, "research_cx_adv_2_77_attempt1.md"), encoding="utf-8").read()
    latest = open(os.path.join(agents_dir, "research_cx_adv_2_77.md"), encoding="utf-8").read()
    check("a rerun keeps the first attempt's findings file", "0.4" in first)
    check("the latest findings keep the usual name", "0.9" in latest)
    check("unverified sources are shown separately", "Unverified sources" in latest and "web_search: b" in latest)
finally:
    shutil.rmtree(os.path.join(mac.BASE_DIR, "Let Jarvis Handle It", PROJECT), ignore_errors=True)


# ---- 3. an agent's sources are the tool calls it made ---------------------------------
sources, unverified, invented = research_agent.verify_sources(
    ["web_search: LeewayHertz reviews", "web_search: top AI companies Gartner", "https://seen.example/page", "https://never.example"],
    [{"tool": "web_search", "args": {"query": "LeewayHertz reviews"}}],
    ['{"results": [{"url": "https://seen.example/page"}]}'],
)
check("real calls and URLs the tools returned are sources",
      sources == ["web_search: LeewayHertz reviews", "https://seen.example/page"])
check("searches the agent never ran are unverified", unverified == ["web_search: top AI companies Gartner"])
check("a link no tool returned is not kept, not even as unverified", invented == ["https://never.example"])

final = mac.merge_review_sources({"sources": ["web_search: a", "web_search: invented"]},
                                 {"sources": ["web_search: a"]}, [{"sources": ["inspect_website: u"]}])
check("a lead review cites only its agents' real sources",
      final["sources"] == ["web_search: a", "inspect_website: u"] and final["unverified_sources"] == ["web_search: invented"])

DECLARATIONS = [{"name": "web_search", "description": "Search the web.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]
research_agent.get_tools_for_execution_agent = lambda *a, **k: (DECLARATIONS, {"web_search": None}, [])
research_agent.run_tool = lambda handlers, project, agent, name, args: {"results": [{"url": "https://seen.example/page"}]}


def run_agent_with(replies):
    chat = type("Chat", (), {})()
    chat.sent, chat.replies = [], list(replies)
    chat.send_message = lambda m: (chat.sent.append(m), chat.replies.pop(0))[1]
    genai.Client = lambda *a, **k: type("Client", (), {
        "chats": type("Chats", (), {"create": staticmethod(lambda **kw: chat)})()})()
    cfg = {"agent_id": "strategist_lead", "role": "Strategist", "brief": "Research.", "tools_needed": ["web_search"]}
    return asyncio.run(research_agent.run_research_agent(cfg, project_name=PROJECT)), chat


search = type("Call", (), {"name": "web_search", "args": {"query": "LeewayHertz reviews"}})()
answer = json.dumps({"confidence": 1.0, "findings": {"x": 1},
                     "sources": ["web_search: LeewayHertz reviews", "web_search: copied from cycle 1"]})
result, chat = run_agent_with([reply('{"confidence": 1.0, "findings": {}}'), reply(calls=[search]), reply(answer)])
check("an agent that answers without tools is sent back to use them", chat.sent[1] == research_agent.NO_TOOLS_NUDGE)
check("after searching, its real search is its source", result["sources"] == ["web_search: LeewayHertz reviews"])
check("a search it never ran is flagged, not cited", result["unverified_sources"] == ["web_search: copied from cycle 1"])
check("its tool calls are counted", result["tool_calls_made"] == 1 and "no_tool_calls" not in result)

stubborn = json.dumps({"confidence": 1.0, "findings": {}, "sources": ["web_search: anything"]})
result, chat = run_agent_with([reply(stubborn), reply(stubborn)])
check("an agent that still uses no tools is marked and its confidence capped",
      result["no_tool_calls"] is True and result["confidence"] == 0.5 and result["sources"] == [])
check("research agents must search before calling something unavailable",
      "searches_tried" in inspect.getsource(research_agent.run_research_agent))

print("\nAll cycle integrity checks passed.")
