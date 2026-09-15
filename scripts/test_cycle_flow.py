"""Research runs as an ordered flow of as many cycles as the task needs.

The Brain always planned three cycles, and wrote every brief before any research
existed. In pipeline 8 an advisor was handed five countries while the lead of the
same cycle was still choosing them. Now there is no fixed number of cycles, each
cycle lists the earlier cycles it builds on, and just before a cycle starts its
briefs are rewritten from what the approved cycles actually found.
Checked without a real model.
"""
import inspect, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

from agents import brain
import multi_agent_coordinator as mac


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


class _Models:
    def __init__(self, reply):
        self.reply, self.contents = reply, []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.contents.append(contents)
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("Response", (), {"text": self.reply})()


def use_model(reply):
    models = _Models(reply)
    genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


# ---- 1. the Brain plans a flow, not three cycles -------------------------------------------
prompt = brain.BRAIN_SYSTEM_PROMPT
check("there is no fixed number of cycles", "at least 3" not in prompt and "AS MANY RESEARCH CYCLES AS THE TASK NEEDS" in prompt)
check("cycles are an ordered flow with dependencies", "ORDERED FLOW" in prompt and "depends_on" in prompt)
check("briefs may not pre-fill what a cycle is meant to find", "names or assumes a result some cycle is meant to find" in prompt)


def agent(aid, brief="Brief.", role="Role", tools=("google_search",)):
    return {"agent_id": aid, "role": role, "brief": brief, "tools_needed": list(tools), "memory_query": "q"}


def cycle(cid, depends_on="unset"):
    c = {"cycle_id": cid, "domain": f"Step {cid}", "goal": f"Goal {cid}",
         "lead_specialist": agent(f"lead_cycle{cid}_lead"), "advisory_agents": [agent(f"adv_cycle{cid}_adv_1")]}
    if depends_on != "unset":
        c["depends_on"] = depends_on
    return c


plan = brain.order_cycles({"cycles": [cycle(3, [1, 2, 3, 7]), cycle(1, [2]), cycle(2), cycle(4, "not a list")]})
check("cycles are put in order", [c["cycle_id"] for c in plan["cycles"]] == [1, 2, 3, 4])
check("a cycle may only depend on earlier cycles that exist",
      plan["cycles"][0]["depends_on"] == [] and plan["cycles"][2]["depends_on"] == [1, 2])
check("a cycle that doesn't say builds on the one before it",
      plan["cycles"][1]["depends_on"] == [1] and plan["cycles"][3]["depends_on"] == [3])

eight = {"task_summary": "x", "task_type": "research", "cycles": [cycle(i) for i in range(8, 0, -1)], "execution_agents": []}
use_model(json.dumps(eight))
planned = brain.build_agent_plan("Research my competitors deeply.")
check("the Brain's plan can have as many cycles as it needs, in flow order",
      [c["cycle_id"] for c in planned["cycles"]] == list(range(1, 9))
      and all(c["depends_on"] == ([] if c["cycle_id"] == 1 else [c["cycle_id"] - 1]) for c in planned["cycles"]))

one = {"task_summary": "x", "task_type": "research", "cycles": [cycle(1, [])], "execution_agents": []}
use_model(json.dumps(one))
check("a one-cycle plan is fine too", len(brain.build_agent_plan("A small task.")["cycles"]) == 1)


# ---- 2. each cycle's briefs are rewritten from what earlier cycles found --------------------
approved = [{"business": "AI Solutions & Marketing studio"},
            {"chosen_countries": ["United States", "United Kingdom", "Canada", "Australia", "Ireland"]}]
next_cycle = {"cycle_id": 3, "depends_on": [2], "domain": "Competitor identification", "goal": "Find 20 per country",
              "lead_specialist": agent("competitor_finder_cycle3_lead", "Find competitors in Singapore and the US.", "Competitor Finder"),
              "advisory_agents": [agent("visibility_checker_cycle3_adv_1", "Check visibility.", "Visibility Checker", ("inspect_website",))]}

models = use_model(json.dumps({"agents": [
    {"agent_id": "competitor_finder_cycle3_lead", "brief": "Find 20 competitors in each of the United States, United Kingdom, Canada, Australia and Ireland."},
    {"agent_id": "someone_new_cycle3_adv_9", "brief": "An agent that wasn't in the cycle."},
]}))
brain.refresh_cycle_briefs("THE TASK", next_cycle, approved)
sent = models.contents[0]
check("the rewrite sees the approved results and the cycle's own agents",
      "Ireland" in sent and "competitor_finder_cycle3_lead" in sent and "Cycle 2" in sent)
check("a brief that guessed is rewritten with the real results",
      next_cycle["lead_specialist"]["brief"].startswith("Find 20 competitors in each of the United States"))
check("agents keep their ids, roles and tools, and nobody is added",
      next_cycle["lead_specialist"]["role"] == "Competitor Finder"
      and [a["agent_id"] for a in next_cycle["advisory_agents"]] == ["visibility_checker_cycle3_adv_1"]
      and next_cycle["advisory_agents"][0]["tools_needed"] == ["inspect_website"])
check("an agent the model didn't rewrite keeps its brief", next_cycle["advisory_agents"][0]["brief"] == "Check visibility.")

use_model(RuntimeError("model unavailable"))
before = json.dumps(next_cycle, sort_keys=True)
brain.refresh_cycle_briefs("THE TASK", next_cycle, approved)
check("if the rewrite fails, the cycle stays as planned", json.dumps(next_cycle, sort_keys=True) == before)

models = use_model('{"agents": []}')
brain.refresh_cycle_briefs("THE TASK", next_cycle, [])
check("the first cycle has nothing earlier to learn from, so no model call", models.contents == [])


# ---- 3. the pipeline runs the flow ---------------------------------------------------------
source = inspect.getsource(mac.run_full_pipeline)
check("the pipeline no longer demands three cycles",
      "at least 3" not in source and "len(cycles) < 3" not in source and "if not cycles:" in source)
check("each cycle's briefs are refreshed before its agents start",
      "refresh_cycle_briefs(" in source
      and source.index("refresh_cycle_briefs(") < source.index("research_output = await run_research_phase_for_cycle("))
check("resuming a plan has no ceiling on how many cycles it rebuilds", "range(1, 10)" not in source)

print("\nAll cycle flow checks passed.")
