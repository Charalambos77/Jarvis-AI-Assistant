"""Everything pipeline 8 showed losing or mislabelling research, checked without a real model.

A conflict rerun replaced the lead's five-country comparison with a UK-only answer.
Failed agents were kept as if finished. Real searches were flagged as unverified
because they were written differently. Attempt files were overwritten across a
restart, partial findings were never saved, a restart threw away a cycle's research
and left its old blueprint showing. The Brain planned method-only cycles and
pre-picked countries, the brief check accepted a "strategy" as the work, QA never
saw the user's brief, intake kept re-asking after "You decide", and the plan list
kept showing a gate as waiting after a restart.
"""
import asyncio, inspect, json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from google import genai

import multi_agent_coordinator as mac
from agents import brain, synthesis, quality_checker, research_agent

PROJECT = "__research_resilience_test__"
PROJECT_DIR = os.path.join(mac.BASE_DIR, "Let Jarvis Handle It", PROJECT)
AGENTS_DIR = os.path.join(PROJECT_DIR, "Implementation plan", "Agents")


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


class _Models:
    def __init__(self, reply):
        self.reply, self.contents = reply, []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.contents.append(contents)
        return type("Response", (), {"text": self.reply})()


def use_model(reply):
    models = _Models(reply)
    genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


shutil.rmtree(PROJECT_DIR, ignore_errors=True)
try:
    # ---- 1. a rerun adds to what an agent found, never erases it ------------------------------
    five_countries = {"status": "ok", "confidence": 1.0, "sources": ["web_search: AI markets"],
                      "findings": {"countries": ["US", "UK", "Canada", "Australia", "Ireland"], "uk_market": "unclear"}}
    uk_only = {"status": "ok", "confidence": 0.9, "sources": ["web_search: UK AI market 2034"],
               "findings": {"uk_market": "MarketsandMarkets and IMARC measure different scopes"}}
    merged = mac.merge_with_previous(uk_only, five_countries)
    check("a conflict rerun keeps the earlier findings and adds the resolution",
          merged["findings"]["countries"] == ["US", "UK", "Canada", "Australia", "Ireland"]
          and merged["findings"]["uk_market"].startswith("MarketsandMarkets"))
    check("sources from both attempts are kept", merged["sources"] == ["web_search: AI markets", "web_search: UK AI market 2034"])
    failed_rerun = mac.merge_with_previous({"status": "partial", "blocked_reason": "stopped"}, five_countries)
    check("if the rerun fails, the earlier result stands and says so",
          failed_rerun["findings"] == five_countries["findings"] and failed_rerun["rerun_problem"] == "stopped")
    check("with nothing good before, the rerun's result is used as it is",
          mac.merge_with_previous(uk_only, {"status": "partial", "findings": {}}) is uk_only)

    source = inspect.getsource(mac.run_full_pipeline)
    check("rerun agents have their earlier results added back",
          "merge_with_previous(r, previous_results.get(r.get(\"agent_id\")))" in source
          and "previous_results = {aid: results_by_id[aid] for aid in rerun_ids if aid in results_by_id}" in source)
    check("failed or stopped agents rerun with a conflict, instead of being kept",
          'if r.get("status") != "ok" and aid not in rerun_ids' in source)

    # ---- 2. nothing already saved gets overwritten ----------------------------------------------
    os.makedirs(AGENTS_DIR, exist_ok=True)
    mac.save_research_findings_file("88", "lead_cycle1_lead", {"status": "ok", "confidence": 0.1}, PROJECT)
    mac.save_research_findings_file("88", "lead_cycle1_lead", {"status": "ok", "confidence": 0.2}, PROJECT)
    mac.save_research_findings_file("88", "lead_cycle1_lead", {"status": "ok", "confidence": 0.3}, PROJECT)
    read = lambda name: open(os.path.join(AGENTS_DIR, name), encoding="utf-8").read()
    check("every earlier attempt is kept under the next free number, even after a restart",
          "0.1" in read("research_lead_cycle1_lead_88_attempt1.md")
          and "0.2" in read("research_lead_cycle1_lead_88_attempt2.md")
          and "0.3" in read("research_lead_cycle1_lead_88.md"))

    mac.save_research_findings_file("88", "stuck_cycle1_adv_1",
                                    {"status": "partial", "blocked_reason": "Stopped by the user", "findings": {"half": 1}}, PROJECT)
    stuck = read("research_stuck_cycle1_adv_1_88.md")
    check("partial findings are saved, with why the agent stopped",
          "**Status:** partial" in stuck and "Stopped by the user" in stuck and "half" in stuck)
    check("the cycle saves every agent's result, not only the successful ones",
          source.index("save_research_findings_file(plan_id, r.get(\"agent_id\"), r, project_name, attempt=retry_count,")
          < source.index("if \"high_value_memory\" in r"))

    # ---- 3. a restart reuses a cycle's research and hides its old blueprint ----------------------------
    plan_file = os.path.join(AGENTS_DIR, "agent_plan_88.md")
    open(plan_file, "w").write("plan")
    old = time.time() - 600
    os.utime(os.path.join(AGENTS_DIR, "research_stuck_cycle1_adv_1_88.md"), (time.time() + 5, time.time() + 5))
    mac.save_research_findings_file("88", "stale_cycle1_adv_2", {"status": "ok", "findings": {"x": 1}}, PROJECT)
    os.utime(os.path.join(AGENTS_DIR, "research_stale_cycle1_adv_2_88.md"), (old, old))
    os.utime(os.path.join(AGENTS_DIR, "research_lead_cycle1_lead_88.md"), (time.time() + 5, time.time() + 5))
    cycle = {"cycle_id": 1, "lead_specialist": {"agent_id": "lead_cycle1_lead"},
             "advisory_agents": [{"agent_id": "stuck_cycle1_adv_1"}, {"agent_id": "stale_cycle1_adv_2"},
                                 {"agent_id": "never_ran_cycle1_adv_3"}]}
    reused = mac.load_saved_cycle_research("88", cycle, PROJECT)
    check("a restart reuses finished research saved since the plan last changed",
          list(reused) == ["lead_cycle1_lead"] and reused["lead_cycle1_lead"]["confidence"] == 0.3)
    check("partial research, research older than the plan, and missing research all run again",
          not {"stuck_cycle1_adv_1", "stale_cycle1_adv_2", "never_ran_cycle1_adv_3"} & set(reused))
    check("the pipeline starts a resumed cycle from that reused research",
          "resumed_research = load_saved_cycle_research(plan_id, cycle, project_name) if existing_plan else {}" in source
          and "kept_results: dict = dict(resumed_research)" in source)

    # The brief the research was done on decides, not the plan file's age: a conflict
    # re-saves the plan while most agents keep their briefs, and their findings stand.
    mac.save_research_findings_file("88", "kept_cycle1_adv_4", {"status": "ok", "findings": {"y": 1}}, PROJECT,
                                    brief="Compare ads.")
    mac.save_research_findings_file("88", "rebriefed_cycle1_adv_5", {"status": "ok", "findings": {"z": 1}}, PROJECT,
                                    brief="Old brief.")
    for name in ("research_kept_cycle1_adv_4_88.md", "research_rebriefed_cycle1_adv_5_88.md"):
        os.utime(os.path.join(AGENTS_DIR, name), (old, old))
    by_brief = mac.load_saved_cycle_research("88", {
        "cycle_id": 1, "lead_specialist": {"agent_id": "kept_cycle1_adv_4", "brief": "Compare ads."},
        "advisory_agents": [{"agent_id": "rebriefed_cycle1_adv_5", "brief": "Old brief.\n\nSettle the conflict."}]}, PROJECT)
    check("research on the agent's current brief is reused, even when the plan was saved after it",
          list(by_brief) == ["kept_cycle1_adv_4"] and by_brief["kept_cycle1_adv_4"]["findings"] == {"y": 1})
    check("the recorded brief is not handed back as part of the result",
          mac.RESEARCHED_BRIEF_KEY not in by_brief["kept_cycle1_adv_4"])
    check("the pipeline records each agent's brief with its research", 'brief=briefs.get(r.get("agent_id"))' in source)
    check("a resumed cycle that reuses research keeps the briefs that research was done on",
          "if approved_blueprints and not resumed_research:" in source)
    check("a rejected cycle's research is moved aside so a restart can't reuse it",
          'f"research_{agent.get(\'agent_id\')}_{plan_id}.md"), "attempt")' in source)

    blueprint = os.path.join(AGENTS_DIR, "cycle_blueprint_1_88.md")
    open(blueprint, "w").write("old run")
    moved = mac.keep_previous_file(blueprint, "previous")
    check("a rerun cycle's old blueprint is moved aside, not shown as its result",
          not os.path.exists(blueprint) and moved.endswith("cycle_blueprint_1_88_previous1.md"))
    check("the pipeline moves it aside when the cycle starts", 'f"cycle_blueprint_{cycle_id}_{plan_id}.md"), "previous")' in source)
finally:
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)


# ---- 4. sources and confidence ------------------------------------------------------------------
sources, unverified, _ = research_agent.verify_sources(
    ["web_search (query: UK AI market size 2034 USD billion)", "web_search: made up search", "internal memory"],
    [{"tool": "web_search", "args": {"query": "UK AI market size 2034 USD billion"}}], [])
check("a real search written another way is not flagged as unverified",
      sources == ["web_search: UK AI market size 2034 USD billion"]
      and unverified == ["web_search: made up search", "internal memory"])

DECLARATIONS = [{"name": "web_search", "description": "Search.",
                 "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]
research_agent.get_tools_for_execution_agent = lambda *a, **k: (DECLARATIONS, {"web_search": None}, [])
research_agent.run_tool = lambda *a, **k: {"status": "ok"}
replies = [
    type("R", (), {"text": None, "function_calls": [type("C", (), {"name": "web_search", "args": {"query": "real query here"}})()]})(),
    type("R", (), {"text": json.dumps({"confidence": 1.0, "findings": {"a": 1},
                                       "sources": ["web_search: real query here", "invented one", "invented two"]}),
                   "function_calls": None})(),
]
chat = type("Chat", (), {"send_message": lambda self, m, config=None: replies.pop(0)})()
genai.Client = lambda *a, **k: type("Client", (), {"chats": type("Chats", (), {"create": staticmethod(lambda **kw: chat)})()})()
result = asyncio.run(research_agent.run_research_agent(
    {"agent_id": "a1", "role": "Analyst", "brief": "B", "tools_needed": ["web_search"]}, project_name=PROJECT))
check("confidence is capped when most cited sources came from nowhere its tools went",
      result["confidence"] == 0.6 and "Capped" in result["confidence_note"])


# ---- 5. planning, checking and asking -------------------------------------------------------------
check("forecasts from different research firms are not a conflict to research again",
      "Market-size forecasts nearly always differ this way" in synthesis._detect_conflicts_prompt("{}"))
check("research cycles must produce real findings, not a method", "RESEARCH CYCLES PRODUCE REAL FINDINGS" in brain.BRAIN_SYSTEM_PROMPT)
check("a cycle that makes a choice compares candidates instead of justifying a pre-picked list",
      "never for a list picked in advance to be justified" in brain.BRAIN_SYSTEM_PROMPT)

quality_checker.GEMINI_API_KEY = "test-key"
models = use_model('{"violations": []}')
asyncio.run(quality_checker.check_research_against_brief(
    "Find 20 competitors per country.", {"cycle_id": 2, "domain": "Strategy", "goal": "Design a method"}, {}, [],
    plan_cycles=[{"cycle_id": 2, "domain": "Strategy", "goal": "Design a method to find competitors"}]))
check("the brief check doesn't accept a strategy as the work itself", 'covered" only on paper' in models.contents[0])

models = use_model('{"adheres": true, "issues": [], "integrates": true, "implicated_agent_ids": []}')
asyncio.run(quality_checker.run_quality_checker(
    [{"agent_id": "writer_exec_1", "status": "ok", "doc_url": "x"}],
    {"execution_agents": [{"agent_id": "writer_exec_1", "brief": "Write the report.", "output_spec": {"required_keys": ["doc_url"]}}]},
    {}, user_brief="Research 100 competitors and put it in a Google Doc."))
check("QA checks every deliverable against the user's own brief",
      len(models.contents) == 2 and all("Research 100 competitors" in c for c in models.contents))
check("both QA runs in the pipeline pass the user's brief",
      inspect.getsource(mac.run_full_pipeline).count("run_quality_checker(exec_results, agent_plan, master_blueprint, user_brief=user_brief)") == 2)

import jarvis

check("intake takes 'You decide' as an answer and doesn't ask again",
      "you decide" in jarvis._QUESTION_RULES and "never ask that question again" in jarvis._QUESTION_RULES)
logger_source = inspect.getsource(jarvis.pipeline_event_logger)
running = logger_source.index('event_type == "running" and event.get("source", "").startswith("Cycle")')
check("a cycle that starts running clears a gate left showing as waiting",
      'plan["gate_status"] = "idle"' in logger_source[running:running + 600])

page = open(os.path.join(ROOT, "commands.html"), encoding="utf-8").read()
check("the Commands page says what it holds now",
      "asking whether" in page and "asking for a tool" in page)

print("\nAll research resilience checks passed.")
