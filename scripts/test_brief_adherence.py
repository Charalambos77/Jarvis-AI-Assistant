"""The user's brief reaches every agent, and each research cycle is checked against it.

Pipeline 6 asked for competitors of a one-person custom-software studio in five
native-English countries and got Salesforce, SAP and India. The agents only ever
saw the planner's one-line paraphrase of their slice, and nothing compared their
results with what the user asked for. This walks each link of that chain without
calling a real model.
"""
import asyncio, inspect, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

BRIEF = "Competitors are custom software development agencies in native-English countries only."


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


class _FakeModels:
    """Records every prompt; replies with `reply`, or raises it if it is an exception."""
    def __init__(self, reply):
        self.reply = reply
        self.contents = []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        # The intake gate sends a list (instruction, context, files); keep it as plain text.
        self.contents.append(contents if isinstance(contents, str) else "\n".join(str(c) for c in contents))
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("Response", (), {"text": self.reply})()


def use_model(reply) -> _FakeModels:
    models = _FakeModels(reply)
    genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


# ---- 1. the prompt block -----------------------------------------------------
from agents.user_brief import user_brief_block, clip_brief, MAX_BRIEF_CHARS

check("no brief means no block", user_brief_block(None) == "" and user_brief_block("   ") == "")
block = user_brief_block(BRIEF)
check("the block carries the brief verbatim", BRIEF in block)
check("the block says the user's brief wins", "the user's brief wins" in block)
check("an oversized brief is clipped, not dropped",
      MAX_BRIEF_CHARS < len(clip_brief("x" * (MAX_BRIEF_CHARS + 5000))) < MAX_BRIEF_CHARS + 100)


# ---- 2. research and execution agents are shown it ---------------------------
from agents import research_agent, execution_agent


class _NoModel(Exception):
    pass


def _refuse(*a, **k):
    raise _NoModel("no real model in tests")


def system_prompt_of(run, *args, **kwargs):
    """The system prompt an agent announces before it first reaches the model."""
    events = []
    try:
        asyncio.run(run(*args, event_logger=events.append, **kwargs))
    except _NoModel:
        pass
    for e in events:
        data = e.get("data")
        if (e.get("event_type") == "thinking" and isinstance(data, dict)
                and data.get("thinking_type") == "system_prompt"):
            return data.get("content", "")
    return ""


genai.Client = _refuse
research_agent.get_tools_for_execution_agent = lambda *a, **k: ([], {}, [])
execution_agent.get_tools_for_execution_agent = lambda *a, **k: ([], {}, [])

research_cfg = {"agent_id": "competitor_analyst_cycle1_adv_1", "role": "Competitor Analyst",
                "brief": "Research direct competitors.", "tools_needed": []}
prompt = system_prompt_of(research_agent.run_research_agent, research_cfg,
                          project_name="__brief_test__", user_brief=BRIEF)
check("a research agent is shown the user's brief", BRIEF in prompt)
prompt = system_prompt_of(research_agent.run_research_agent, research_cfg, project_name="__brief_test__")
check("a research agent without a brief gets no brief section", prompt and "THE USER'S BRIEF" not in prompt)

exec_cfg = {"agent_id": "report_writer_exec_1", "role": "Report Writer",
            "brief": "Write the report.", "tools_needed": [], "output_spec": {}}
prompt = system_prompt_of(execution_agent.run_execution_agent, exec_cfg, {},
                          project_name="__brief_test__", user_brief=BRIEF)
check("an execution agent is shown the user's brief", BRIEF in prompt)


# ---- 3. the coordinator passes it along --------------------------------------
import multi_agent_coordinator as mac

seen = {}


async def fake_research_agent(agent_config, *args, **kwargs):
    seen["research"] = kwargs.get("user_brief")
    return {"agent_id": agent_config["agent_id"], "status": "ok", "findings": {}}


async def fake_execution_agent(cfg, blueprint, note=None, **kwargs):
    seen["execution"] = kwargs.get("user_brief")
    return {"agent_id": cfg["agent_id"], "status": "ok"}


mac.run_research_agent = fake_research_agent
mac.run_execution_agent = fake_execution_agent

asyncio.run(mac.run_research_phase_for_cycle(
    [{"agent_id": "a1"}], None, "research", [],
    project_name="__no_such_project__", user_brief=BRIEF))
check("the research phase hands the brief to its agents", seen.get("research") == BRIEF)

asyncio.run(mac.run_execution_phase(
    {"execution_agents": [{"agent_id": "e1"}]}, {},
    project_name="__no_such_project__", user_brief=BRIEF))
check("the execution phase hands the brief to its agents", seen.get("execution") == BRIEF)

models = use_model('{"findings": {"merged": true}}')
asyncio.run(mac.run_lead_review(
    {"agent_id": "lead1", "role": "Lead", "brief": "Lead the cycle."},
    {"agent_id": "lead1", "findings": {}}, [], [], user_brief=BRIEF))
check("the lead review is shown the user's brief", any(BRIEF in c for c in models.contents))

source = inspect.getsource(mac.run_full_pipeline)
check("every research gate runs the brief check before it opens",
      "check_research_against_brief(" in source
      and source.index("check_research_against_brief(") < source.index('gate_id=f"cycle_{cycle_id}_research"'))
check("the gate carries the brief check to the user", source.count('"brief_check": brief_check') == 2)


# ---- 4. the brief check itself -----------------------------------------------
from agents import quality_checker as qc

qc.GEMINI_API_KEY = "test-key"
cycle = {"cycle_id": 1, "domain": "Competitor Identification", "goal": "Find 20 competitors per country"}
blueprint = {"countries": ["United States", "India"], "competitors": ["Salesforce", "SAP"]}
agent_results = [
    {"agent_id": "fine_agent", "status": "ok"},
    {"agent_id": "stuck_agent", "status": "partial",
     "blocked_reason": "Exceeded max tool-call turns without a final answer."},
    {"agent_id": "crashed_agent", "status": "error", "error": "boom"},
]
violation = {"constraint": "Native-English countries only", "problem": "India is included"}

models = use_model(json.dumps({"violations": [violation, {"constraint": "x", "problem": ""}]}))
report = asyncio.run(qc.check_research_against_brief(BRIEF, cycle, blueprint, agent_results))
check("a completed check says so", report["checked"] is True)
check("violations reach the report, empty ones dropped", report["violations"] == [violation])
check("failed and stopped-short agents are listed with their reason",
      [f["agent_id"] for f in report["failed_agents"]] == ["stuck_agent", "crashed_agent"]
      and "max tool-call turns" in report["failed_agents"][0]["reason"])
check("the check reads the brief, the cycle's job and its results",
      BRIEF in models.contents[0] and "Competitor Identification" in models.contents[0]
      and "Salesforce" in models.contents[0])

use_model(RuntimeError("model unavailable"))
report = asyncio.run(qc.check_research_against_brief(BRIEF, cycle, blueprint, agent_results))
check("a check that cannot run reports unchecked, never clean",
      report["checked"] is False and report["violations"] == [] and len(report["failed_agents"]) == 2)

use_model('{"violations": "none"}')
report = asyncio.run(qc.check_research_against_brief(BRIEF, cycle, blueprint, agent_results))
check("a malformed answer reports unchecked", report["checked"] is False)

models = use_model('{"violations": []}')
report = asyncio.run(qc.check_research_against_brief(None, cycle, blueprint, agent_results))
check("with no brief there is nothing to check against, and no model call",
      report["checked"] is False and not models.contents and len(report["failed_agents"]) == 2)


# ---- 5. the intake gate settles what counts ----------------------------------
import jarvis

intake_models = _FakeModels('{"plan_text": "The plan."}')
jarvis.client = type("Client", (), {"models": intake_models})()
draft = jarvis.create_intake_draft("Research my top competitors")
try:
    jarvis._intake_next_questions(draft)
    jarvis._intake_paint_picture(draft)
finally:
    with jarvis.INTAKE_DRAFTS_LOCK:
        jarvis.INTAKE_DRAFTS.pop(draft["draft_id"], None)

check("questions make sure Jarvis knows what kind of thing qualifies",
      jarvis._WHAT_COUNTS_QUESTION_RULE in intake_models.contents[0])
check("the written plan must spell out what counts",
      jarvis._WHAT_COUNTS_PLAN_RULE in intake_models.contents[1] and "What counts" in intake_models.contents[1])
check("a second round of questions keeps the same rule",
      jarvis._WHAT_COUNTS_QUESTION_RULE in intake_models.contents[1])

# ---- 6. what pipeline 7's first cycle got wrong -----------------------------------
# Three agents picked three companies, the blueprint rewrote measured fonts and
# added details nobody found, and dropping "20 from each of 5 countries" passed.
from agents import brain, synthesis

check("the Brain knows agents in one cycle can't use each other's results",
      "RUN AT THE SAME TIME" in brain.BRAIN_SYSTEM_PROMPT)
check("the Brain must keep the user's counts and places",
      "KEEP THE USER'S SCOPE" in brain.BRAIN_SYSTEM_PROMPT)

models = use_model('{"findings": {}}')
asyncio.run(mac.run_lead_review(
    {"agent_id": "lead1", "role": "Lead", "brief": "Lead the cycle."},
    {"agent_id": "lead1", "findings": {}}, [], [], user_brief=BRIEF))
check("the lead review may not add or change facts",
      synthesis.MERGE_FACT_RULES in models.contents[-1])
check("the lead review keeps an advisor's different subject apart",
      "studied a different subject" in models.contents[-1])
check("the cycle blueprint may not add or change facts",
      synthesis.MERGE_FACT_RULES in synthesis._compress_blueprint_prompt("{}"))
models = use_model('{"tool_recommendations": []}')
asyncio.run(synthesis.run_master_synthesis([{"x": 1}]))
check("the master blueprint may not add or change facts",
      synthesis.MERGE_FACT_RULES in models.contents[-1])

genai.Client = _refuse
prompt = system_prompt_of(research_agent.run_research_agent, research_cfg,
                          project_name="__brief_test__", user_brief=BRIEF)
check("a research agent's own evidence outranks earlier cycles",
      "contradicts_prior_research" in prompt)

plan = [{"cycle_id": 1, "domain": "Pick one company", "goal": "Choose the single best company"},
        {"cycle_id": 2, "domain": "Marketing", "goal": "Study its marketing"}]
models = use_model('{"violations": []}')
asyncio.run(qc.check_research_against_brief(BRIEF, cycle, blueprint, agent_results, plan_cycles=plan))
check("the brief check sees the whole plan",
      "Choose the single best company" in models.contents[0] and "Study its marketing" in models.contents[0])
check("the brief check flags requirements no cycle covers", "no cycle covers this" in models.contents[0])
check("every research gate hands the whole plan to the brief check",
      "plan_cycles=cycles" in inspect.getsource(mac.run_full_pipeline))

check("intake asks when later answers contradict the request",
      "conflicts with or narrows" in jarvis._QUESTION_RULES
      and jarvis._QUESTION_RULES in intake_models.contents[0])

# ---- 7. execution is planned from the research, not before it ---------------------------
check("the Brain's first execution roster is marked as a draft", "only a DRAFT" in brain.BRAIN_SYSTEM_PROMPT)

draft = [{"agent_id": "writer_exec_1", "role": "Writer", "brief": "Write something.", "tools_needed": [],
          "output_spec": {"required_keys": ["text"]}}]
master = {"competitor_profiles": ["LeewayHertz"], "tool_recommendations": [{"service": "google_docs_api"}]}
planned = [{"agent_id": "report_writer_exec_1", "role": "Report Writer",
            "brief": "Write the LeewayHertz profile from competitor_profiles into the Google Doc.",
            "tools_needed": ["google_docs_api"], "output_spec": {"required_keys": ["doc_url"], "min_word_count": 1500}}]

models = use_model(json.dumps({"execution_agents": planned}))
roster = brain.plan_execution_agents("THE TASK", draft, master, user_brief=BRIEF)
check("execution planning reads the finished research and the user's brief",
      "competitor_profiles" in models.contents[0] and BRIEF in models.contents[0])
check("the planned roster replaces the draft, briefs and output specs included",
      [a["agent_id"] for a in roster] == ["report_writer_exec_1"]
      and roster[0]["output_spec"]["min_word_count"] == 1500 and roster[0]["brief"].startswith("Write the LeewayHertz"))

use_model(RuntimeError("model unavailable"))
check("if the model fails, the draft roster is kept", brain.plan_execution_agents("THE TASK", draft, master) == draft)
use_model('{"execution_agents": [{"role": "no id or brief"}]}')
check("an unusable answer keeps the draft roster", brain.plan_execution_agents("THE TASK", draft, master) == draft)

models = use_model(json.dumps({"execution_agents": planned}))
brain.plan_execution_agents("THE TASK", draft, master, redirect_note="Use one section per company",
                            rejected_steps=["writer_exec_1"])
check("a rejection re-plans with the user's note and the rejected agents",
      "Use one section per company" in models.contents[0] and "writer_exec_1" in models.contents[0])

source = inspect.getsource(mac.run_full_pipeline)
check("Phase 4 and the execution gate both plan execution from research",
      source.count("plan_execution_agents(") == 2 and "finalize_execution_plan" not in source)
check("rejecting the execution plan no longer rebuilds the research cycles",
      source.count("agent_plan = build_agent_plan(") == 1)

check("the draft label shows only before execution is re-planned",
      jarvis.execution_roster_is_draft({"phase": "research", "agent_plan": {}}) is True
      and jarvis.execution_roster_is_draft({"phase": "research", "agent_plan": {"execution_plan_final": True}}) is False
      and jarvis.execution_roster_is_draft({"phase": "complete", "agent_plan": {}}) is False)

print("\nAll brief adherence checks passed.")
