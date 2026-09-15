"""The review stage's "Detailed plan" and "Findings so far" buttons, end to end on the API.

Everything is scoped to one pipeline: agent ids repeat across pipelines, so a
lookup keyed by agent id alone would show one run's findings inside another.
"""
import json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import jarvis
from agents import tool_executor

app = jarvis.app.test_client()
PROJECT = "__agent_details_test__"
PLAN, OTHER_PLAN, RESTARTED_PLAN = "__details_1", "__details_2", "__details_3"


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


TOOL_CALLS = []


def fake_tools(tools_needed, project_name, context=None):
    TOOL_CALLS.append(list(tools_needed or []))
    return ([{"name": "web_search"}, {"name": "write_file"}], {},
            ["web_scraper (no real connector implemented yet — do not claim to have used it)"])


tool_executor.get_tools_for_execution_agent = fake_tools

AGENT_PLAN = {
    "cycles": [{
        "cycle_id": 1, "domain": "Competitor Identification", "goal": "Find 20 competitors per country",
        "lead_specialist": {"agent_id": "lead_cycle1_lead", "role": "Market Research Lead",
                            "brief": "Lead the research.", "tools_needed": ["google_search"],
                            "memory_query": "competitor patterns"},
        "advisory_agents": [
            {"agent_id": "analyst_cycle1_adv_1", "role": "Competitor Analyst",
             "brief": "List competitors.", "tools_needed": ["google_search", "web_scraper"]},
            {"agent_id": "odd..id", "role": "Odd", "brief": "x", "tools_needed": []},
        ],
    }],
    "execution_agents": [{"agent_id": "writer_exec_1", "role": "Report Writer", "brief": "Write it.",
                          "tools_needed": [], "output_spec": {"required_sections": ["intro"]}}],
}
APPROVED = [{"countries": ["United States", "United Kingdom"]}]


def plan_entry(pid):
    return {"id": pid, "task": "t", "project_name": PROJECT, "status": "running",
            "agent_plan": json.loads(json.dumps(AGENT_PLAN)), "approved_blueprints": APPROVED}


agents_dir = os.path.join(jarvis.BASE_DIR, "Let Jarvis Handle It", PROJECT, "Implementation plan", "Agents")
events = []
now = time.time()
try:
    with jarvis.PLAN_STORE_LOCK:
        for pid in (PLAN, OTHER_PLAN, RESTARTED_PLAN):
            jarvis.PLAN_STORE.append(plan_entry(pid))

    def ev(pid, event_type, data, **ids):
        e = {"plan_id": pid, "event_type": event_type, "data": data, "timestamp": now + len(events), **ids}
        events.append(e)
        return e

    # Another pipeline's agent with the very same id, finished with other findings.
    ev(OTHER_PLAN, "completed", {"status": "ok", "findings": {"wrong": "other pipeline"}}, agent_id="analyst_cycle1_adv_1")
    # This pipeline: the analyst searched, one call failed, and it stopped short.
    ev(PLAN, "spawned", {"role": "Competitor Analyst"}, agent_id="analyst_cycle1_adv_1")
    ev(PLAN, "tool_result", {"tool": "web_search", "args": {"query": "top agencies UK"},
                             "result": {"status": "ok", "summary": "Agency A, Agency B"}}, source="analyst_cycle1_adv_1")
    ev(PLAN, "tool_result", {"tool": "web_search", "args": {"query": "x" * 2000},
                             "result": {"status": "error", "error": "rate limited"}}, source="analyst_cycle1_adv_1")
    ev(PLAN, "error", "Exceeded max tool-call turns without a final answer.", source="analyst_cycle1_adv_1")
    ev(PLAN, "completed", {"status": "partial", "confidence": 0.4, "findings": {"competitors": ["Agency A", "Agency B"]},
                           "blocked_reason": "Exceeded max tool-call turns without a final answer.",
                           "sources": ["https://example.com"]}, agent_id="analyst_cycle1_adv_1")
    # The lead is still working.
    ev(PLAN, "running", None, agent_id="lead_cycle1_lead")
    with jarvis.AGENT_OBS_LOCK:
        jarvis.AGENT_EVENT_LOG.extend(events)

    # A pipeline whose events are gone (Jarvis restarted) but whose file was saved.
    os.makedirs(agents_dir, exist_ok=True)
    with open(os.path.join(agents_dir, f"research_analyst_cycle1_adv_1_{RESTARTED_PLAN}.md"), "w", encoding="utf-8") as f:
        f.write("# Research Findings\n### competitors\nAgency C\n")

    def get(pid, agent, query=""):
        r = app.get(f"/api/plans/{pid}/agents/{agent}{query}")
        return r.status_code, r.get_json()

    # ---- Detailed plan ----------------------------------------------------------
    code, d = get(PLAN, "analyst_cycle1_adv_1")
    check("an agent of the plan is found", code == 200 and d["kind"] == "research" and d["role"] == "Competitor Analyst")
    check("the plan carries the brief and its cycle",
          d["plan"]["brief"] == "List competitors." and d["plan"]["cycle"]["domain"] == "Competitor Identification"
          and d["plan"]["cycle"]["goal"] == "Find 20 competitors per country" and d["plan"]["is_lead"] is False)
    check("tools show what really works and what the agent won't get",
          d["plan"]["tools"]["available"] == ["web_search", "write_file"]
          and "web_scraper" in d["plan"]["tools"]["unavailable"][0]
          and TOOL_CALLS[-1] == ["google_search", "web_scraper"])

    code, d = get(PLAN, "lead_cycle1_lead")
    check("the lead is marked as lead, with its memory query", d["plan"]["is_lead"] is True and d["plan"]["memory_query"] == "competitor patterns")

    calls_before = len(TOOL_CALLS)
    code, d = get(PLAN, "analyst_cycle1_adv_1", "?tools=0")
    check("?tools=0 skips resolving tools", code == 200 and d["plan"]["tools"] is None and len(TOOL_CALLS) == calls_before)

    # ---- Findings so far --------------------------------------------------------
    f = d["findings"]
    check("findings come from this pipeline only, never another run's same-named agent",
          f["output"]["findings"] == {"competitors": ["Agency A", "Agency B"]} and "other pipeline" not in json.dumps(f))
    check("an agent that stopped short shows as partial with what blocked it",
          f["status"] == "partial" and "max tool-call turns" in f["output"]["blocked_reason"])
    check("tool calls so far are listed, failures marked",
          [c["ok"] for c in f["tool_calls"]] == [True, False] and "Agency A" in f["tool_calls"][0]["result"])
    check("long tool arguments are clipped", len(f["tool_calls"][1]["args"]) < 600)
    check("errors are listed", f["errors"] and "max tool-call turns" in f["errors"][0])

    code, d = get(PLAN, "lead_cycle1_lead")
    check("a running agent with nothing yet shows as running",
          d["findings"]["status"] == "running" and d["findings"]["output"] is None and d["findings"]["saved_file"] is None)

    code, d = get(RESTARTED_PLAN, "analyst_cycle1_adv_1")
    check("after a restart the saved findings file is shown",
          d["findings"]["status"] == "completed" and "Agency C" in d["findings"]["saved_file"])

    code, d = get(PLAN, "writer_exec_1")
    check("an execution agent that hasn't run shows the approved research it will use",
          d["kind"] == "execution" and d["findings"]["status"] == "not started"
          and d["findings"]["approved_research"] == APPROVED
          and d["plan"]["output_spec"] == {"required_sections": ["intro"]})

    # ---- Refusals -----------------------------------------------------------------
    check("an unknown plan is a 404", get("__no_such_plan__", "analyst_cycle1_adv_1")[0] == 404)
    check("an agent from outside the plan is a 404", get(PLAN, "someone_else")[0] == 404)
    check("an id that is unsafe in a file name is refused", get(PLAN, "odd..id")[0] == 400)
finally:
    with jarvis.PLAN_STORE_LOCK:
        jarvis.PLAN_STORE[:] = [p for p in jarvis.PLAN_STORE if p["id"] not in (PLAN, OTHER_PLAN, RESTARTED_PLAN)]
    with jarvis.AGENT_OBS_LOCK:
        jarvis.AGENT_EVENT_LOG[:] = [e for e in jarvis.AGENT_EVENT_LOG if e not in events]
    shutil.rmtree(os.path.join(jarvis.BASE_DIR, "Let Jarvis Handle It", PROJECT), ignore_errors=True)

print("\nAll agent details checks passed.")
