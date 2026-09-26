"""Jarvis understands every task deeply, then researches it before doing it.

Before planning, the Brain writes down what the task really needs: its goal,
deliverables, constraints, success criteria and the unknowns to research. The
plan is built from that, sized to the task, and never skips research: a plan
with no cycles gets one on the task's unknowns, and a plan that uses an API or
MCP service no cycle covers gets a cycle on how that service works. Execution
and the quality checker see the success criteria. Checked without a real model.
"""
import asyncio, json, os, sys, tempfile

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
    def __init__(self, replies):
        self.replies, self.contents, self.systems = list(replies), [], []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.contents.append(contents)
        self.systems.append(getattr(config, "system_instruction", None))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return type("Response", (), {"text": reply})()


def use_model(*replies):
    models = _Models(replies)
    genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


def agent(aid, tools=("google_search",), brief="Brief."):
    return {"agent_id": aid, "role": "Role", "brief": brief, "tools_needed": list(tools), "memory_query": "q"}


def cycle(cid, brief="Brief."):
    return {"cycle_id": cid, "domain": f"Step {cid}", "goal": "Goal",
            "lead_specialist": agent(f"lead_cycle{cid}_lead", brief=brief),
            "advisory_agents": [agent(f"adv_cycle{cid}_adv_1")]}


def writer(tools=()):
    return {"agent_id": "script_writer_exec_1", "role": "Script Writer", "brief": "Write the script.",
            "tools_needed": list(tools), "output_spec": {"required_keys": ["script"]}}


UNDERSTANDING = {
    "goal": "Get more sign-ups from a 2 minute launch video", "task_type": "content",
    "deliverables": ["A 2 minute video script"], "audience": "Small business owners",
    "constraints": ["2 minutes long", "Friendly tone"],
    "success_criteria": ["Reads in under 2 minutes", "Ends with a sign-up call to action"],
    "unknowns": ["What makes launch videos convert", "What competitors' launch videos say"],
    "assumptions": ["English only"], "services": [],
    "research_depth": "focused", "depth_reason": "A short, clear piece of writing.",
}

# ---- 1. the prompts ---------------------------------------------------------------------------
prompt = brain.BRAIN_SYSTEM_PROMPT
check("every task is researched before it is done", "EVERY TASK IS RESEARCHED BEFORE IT IS DONE" in prompt)
check("research is sized to the task, never under 2 cycles",
      "research_depth" in prompt and "Never plan fewer than 2 cycles" in prompt)
check("services are researched before they are used", "how each service and its tools work" in prompt)
check("the understanding step asks for goal, constraints, success criteria and unknowns",
      all(k in brain.UNDERSTAND_SYSTEM_PROMPT for k in ("goal", "constraints", "success_criteria", "unknowns")))

# ---- 2. understanding the task ----------------------------------------------------------------
use_model(json.dumps({**UNDERSTANDING, "research_depth": "enormous", "constraints": ["2 minutes long", "", 5]}))
u = brain.understand_task("Write my launch video script.")
check("the understanding is cleaned up", u["constraints"] == ["2 minutes long", "5"] and u["goal"].startswith("Get more"))
check("an unknown depth falls back to standard", u["research_depth"] == "standard")

use_model(RuntimeError("model unavailable"))
check("if understanding fails, planning goes ahead from the task alone", brain.understand_task("x") == {})

block = brain.format_understanding(UNDERSTANDING)
check("the understanding reads as a prompt block",
      "Success criteria:" in block and "  - Ends with a sign-up call to action" in block and "Research depth: focused" in block)
check("no understanding, no block", brain.format_understanding({}) == "")

# ---- 3. research is never skipped -------------------------------------------------------------
use_model("{}")  # name resolution may ask the model about unknown services; keep it offline
plan = brain.ensure_research({"cycles": [], "execution_agents": [writer()], "task_understanding": UNDERSTANDING})
check("a plan with no research gets a cycle on the task's unknowns",
      len(plan["cycles"]) == 1 and "What makes launch videos convert" in plan["cycles"][0]["lead_specialist"]["brief"]
      and plan["cycles"][0]["lead_specialist"]["agent_id"] == "task_researcher_cycle1_lead"
      and len(plan["cycles"][0]["advisory_agents"]) == 1)

plan = brain.ensure_research({"cycles": [], "execution_agents": [writer()]})
check("even without an understanding, research is added", len(plan["cycles"]) == 1)

plan = brain.ensure_research({"cycles": [cycle(1), cycle(2)], "execution_agents": [writer(("write_file",))]})
check("a researched plan with no services is left alone", len(plan["cycles"]) == 2 and "research_added" not in plan)

plan = brain.ensure_research({"cycles": [cycle(1), cycle(2)], "execution_agents": [writer(("youtube_api",))]})
check("a service no cycle covers gets its own research cycle, numbered after the others",
      len(plan["cycles"]) == 3 and plan["cycles"][2]["cycle_id"] == 3
      and "youtube_api" in plan["cycles"][2]["goal"]
      and plan["cycles"][2]["lead_specialist"]["agent_id"] == "api_integration_researcher_cycle3_lead")

plan = brain.ensure_research({"cycles": [cycle(1, brief="Learn how the YouTube API uploads work.")],
                              "execution_agents": [writer(("youtube_api",))]})
check("a service a cycle already researches isn't researched twice", len(plan["cycles"]) == 1)

plan = brain.ensure_research({"cycles": [cycle(1)], "execution_agents": [writer()],
                              "recommended_tools": [{"service": "Stripe API"}],
                              "task_understanding": {"services": ["Notion"]}})
check("services the Brain recommends or the understanding names are researched",
      len(plan["cycles"]) == 2 and "Stripe API" in plan["cycles"][1]["goal"] and "Notion" in plan["cycles"][1]["goal"])

plan = brain.ensure_research({"cycles": [cycle(1)], "execution_agents": [writer(("google_search", "arxiv_api"))]})
check("built-in search doesn't count as a service", len(plan["cycles"]) == 1)

check("a failed plan is passed through", brain.ensure_research({"error": "x"}) == {"error": "x"})

# ---- 4. build_agent_plan understands first, then plans from it --------------------------------
plan_reply = json.dumps({"task_summary": "Launch script", "task_type": "content", "cycles": [cycle(1), cycle(2)],
                         "execution_agents": [writer()]})
models = use_model(json.dumps(UNDERSTANDING), plan_reply)
planned = brain.build_agent_plan("Write my launch video script.")
check("the task is understood before it is planned",
      models.systems[0] == brain.UNDERSTAND_SYSTEM_PROMPT and models.systems[1] == brain.BRAIN_SYSTEM_PROMPT)
check("the planner is given the understanding", "JARVIS'S TASK UNDERSTANDING" in models.contents[1]
      and "Ends with a sign-up call to action" in models.contents[1])
check("the plan keeps the understanding and its depth",
      planned["task_understanding"]["goal"] == UNDERSTANDING["goal"] and planned["research_depth"] == "focused")

models = use_model(json.dumps({"cycles": [cycle(2)]}))
brain.build_agent_plan("THE TASK", redirect_note="Dig deeper.", cycle_id=2, approved_blueprints=[{}])
check("a single-cycle re-plan doesn't re-run the understanding", len(models.contents) == 1)

# ---- 5. the pipeline carries it through ------------------------------------------------------
tmp = tempfile.mkdtemp()
mac.BASE_DIR = tmp
mac.DB_PATH = os.path.join(tmp, "test.db")
seen = {}


async def research(agents, *a, **k):
    return {"agent_results": [{"agent_id": x["agent_id"], "status": "ok", "findings": "Found."} for x in agents]}


async def lead_review(*a, **k):
    return [{"finding": "Found."}]


async def synthesis(*a, **k):
    return {"has_conflicts": False, "blueprint": {"findings": "Found."}}


async def brief_check(*a, **k):
    return {"violations": [], "failed_agents": []}


async def master(*a, **k):
    return {"findings": "All found."}


def exec_planning(task, draft, blueprint, **k):
    seen["exec_planning_task"] = task
    return draft


async def execution(agent_plan, blueprint, **k):
    seen["blueprint"] = blueprint
    return {"agent_results": [{"agent_id": "script_writer_exec_1", "status": "ok", "script": "Hello."}]}


async def qa(exec_results, agent_plan, blueprint, **k):
    seen["qa_blueprint"] = blueprint
    return {"all_passed": True, "failed_agents": [], "results": []}


async def deploy(*a, **k):
    return {"status": "ok", "message": "done"}


async def approve(gate_id, data):
    return {"approved": True}


mac.build_agent_plan = lambda *a, **k: {"task_summary": "Launch script", "task_type": "content",
                                        "research_depth": "focused", "task_understanding": UNDERSTANDING,
                                        "cycles": [cycle(1)], "execution_agents": [writer()]}
mac.refresh_cycle_briefs = lambda *a, **k: None
mac.run_research_phase_for_cycle = research
mac.run_lead_review = lead_review
mac.run_synthesis_agent = synthesis
mac.check_research_against_brief = brief_check
mac.run_master_synthesis = master
mac.plan_execution_agents = exec_planning
mac.run_execution_phase = execution
mac.run_quality_checker = qa
mac.run_deployment_agent = deploy

result = asyncio.run(mac.run_full_pipeline("Write my launch video script.", approve, plan_id="p1", project_name="Test"))
check("the pipeline completes", result.get("status") == "complete")
check("execution is planned from the understanding", "Ends with a sign-up call to action" in seen["exec_planning_task"])
check("execution agents and the quality checker see the success criteria",
      seen["blueprint"]["task_understanding"]["success_criteria"] == UNDERSTANDING["success_criteria"]
      and seen["qa_blueprint"]["task_understanding"] == UNDERSTANDING)
saved = open(os.path.join(tmp, "Let Jarvis Handle It", "Test", "Implementation plan", "Agents",
                          "agent_plan_p1.md"), encoding="utf-8").read()
check("the saved plan shows the understanding and the depth",
      "## Task Understanding" in saved and "Reads in under 2 minutes" in saved and "**Research Depth:** focused" in saved)

print("\nAll task understanding checks passed.")
