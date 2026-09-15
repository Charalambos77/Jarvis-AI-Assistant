"""
Quality Checker Agent — validates execution outputs against the blueprint specs and briefly.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

from agents.user_brief import clip_brief

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_quality_checker(
    execution_results: list[dict],
    agent_plan: dict,
    master_blueprint: dict,
    user_brief: str | None = None,
) -> dict:
    """
    Validates each execution agent's output against the blueprint spec and brief.
    Returns per-agent pass/fail with reasons.

    `user_brief` is what the user actually asked for. The checks used to read only
    each agent's own brief, so a deliverable could pass while missing what the
    user wanted.
    """
    brief_section = ""
    if user_brief:
        brief_section = (
            "THE USER'S BRIEF (what the user actually asked for — the output has to serve it):\n"
            f"{clip_brief(user_brief)}\n\n"
        )
    client = genai.Client(api_key=GEMINI_API_KEY)
    loop = asyncio.get_running_loop()

    execution_agents = agent_plan.get("execution_agents", [])
    agent_map = {a.get("agent_id"): a for a in execution_agents}

    results = []
    all_passed = True

    for agent_result in execution_results:
        agent_id = agent_result.get("agent_id", "unknown")
        issues = []

        cfg = agent_map.get(agent_id, {})
        spec = cfg.get("output_spec", {})

        # 1. Schema checks
        for key in spec.get("required_keys", []):
            if key not in agent_result:
                issues.append(f"Missing required key: '{key}'")

        if spec.get("min_word_count"):
            body = ""
            for key in ["body", "content", "script", "text"]:
                if key in agent_result:
                    body = str(agent_result[key])
                    break
            if not body and "findings" in agent_result:
                body = str(agent_result["findings"])
            if not body:
                body = " ".join(str(v) for k, v in agent_result.items() if k not in ["agent_id", "status"])

            wc = len(body.split())
            if wc < spec["min_word_count"]:
                issues.append(
                    f"Word count {wc} is below minimum {spec['min_word_count']}"
                )

        if agent_result.get("status") == "error":
            issues.append(f"Agent self-reported error: {agent_result.get('error', 'unknown')}")

        # 2. LLM validation (Tier 1)
        if cfg and len(issues) == 0:
            prompt = f"""You are the Quality Checker Agent. Verify if the execution output below adheres to the agent's brief and spec.

{brief_section}AGENT BRIEF:
{cfg.get("brief")}

AGENT OUTPUT:
{json.dumps(agent_result, indent=2)}

Check if this output is complete, matches the requested brief/tone, and is of high quality.
Return a JSON object:
{{
  "adheres": true/false,
  "issues": ["list of issues if any"]
}}
"""
            config = types.GenerateContentConfig(
                system_instruction="You are a Quality Checker. Output valid JSON only.",
                response_mime_type="application/json",
            )
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=config
                    )
                )
                res = json.loads(response.text)
                if not res.get("adheres", True):
                    issues.extend(res.get("issues", ["Output does not adhere to the brief."]))
            except Exception as e:
                print(f"[Quality Checker] LLM validation error for agent {agent_id}: {e}")

        passed = len(issues) == 0
        if not passed:
            all_passed = False

        results.append({
            "agent_id": agent_id,
            "passed": passed,
            "issues": issues
        })

    # Tier 2: Global Integration Verification
    integration_passed = True
    integration_issues = []
    integration_implicated_agents = []
    if all_passed and len(execution_results) > 0:
        prompt = f"""You are the Quality Checker Agent. Check if all individual execution outputs integrate and align seamlessly based on the master blueprint.

{brief_section}MASTER BLUEPRINT:
{json.dumps(master_blueprint, indent=2)}

EXECUTION OUTPUTS:
{json.dumps(execution_results, indent=2)}

Do these outputs fit together coherently as a single product? Are there any structural contradictions, API mismatches, design conflicts, or missing components between the designs, code, database schemas, and other deliverables?

NOTE: The QA/Testing deliverables are expected to log simulated bugs or defects (e.g. BUG-001, BUG-002, etc.) and state that release criteria are not met. Do NOT treat these logged bugs/defects as integration failures or contradictions. That is normal QA reporting. Only mark "integrates" as false if there are actual structural, architectural, or API alignment conflicts between the different deliverables.

If "integrates" is false, identify EXACTLY which agent_id(s) (from the "agent_id" field of the execution outputs above) are actually responsible for each conflict — e.g. if two deliverables disagree, name whichever one is wrong or out of date, not both by default, unless both genuinely need to change. Only include an agent_id in "implicated_agent_ids" if fixing that specific agent's output is the correct way to resolve the conflict. Leave it empty only if you truly cannot attribute the conflict to specific agents.

Return a JSON object:
{{
  "integrates": true/false,
  "issues": ["list of issues if any"],
  "implicated_agent_ids": ["agent_id of each deliverable that needs to be redone to resolve the conflict"]
}}
"""
        config = types.GenerateContentConfig(
            system_instruction="You are a Quality Checker. Output valid JSON only.",
            response_mime_type="application/json",
        )
        try:
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=config
                )
            )
            res = json.loads(response.text)
            if not res.get("integrates", True):
                integration_passed = False
                integration_issues = res.get("issues", ["Integration issues detected."])
                integration_implicated_agents = res.get("implicated_agent_ids", []) or []
                all_passed = False
        except Exception as e:
            print(f"[Quality Checker] Global integration check failed: {e}")

    failed_agents = [r["agent_id"] for r in results if not r["passed"]]
    if not integration_passed and not failed_agents:
        implicated = [
            aid for aid in integration_implicated_agents
            if aid in {r.get("agent_id") for r in execution_results}
        ]
        # Only fall back to rerunning every agent if the integration check
        # couldn't attribute the conflict to specific deliverables at all.
        failed_agents = implicated or [r.get("agent_id") for r in execution_results]

    return {
        "all_passed": all_passed,
        "results": results,
        "failed_agents": failed_agents,
        "integration_check": {"passed": integration_passed, "issues": integration_issues}
    }


# How much of a cycle's results the brief check reads. A cycle blueprint is the
# synthesised, compressed output, so this is generous; it only guards against a
# runaway blueprint blowing the model's input.
MAX_FINDINGS_CHARS = 60000


async def check_research_against_brief(
    user_brief: str | None,
    cycle: dict,
    cycle_blueprint: dict,
    agent_results: list[dict],
    plan_cycles: list[dict] | None = None,
) -> dict:
    """Compare one research cycle's results with what the user actually asked for.

    `plan_cycles` is every cycle in the plan. Without it the check could only judge
    this cycle's own job, so a requirement no cycle was ever given — pipeline 7
    dropped "20 competitors from each of 5 countries" at planning — passed as clean.

    Runs just before the cycle's approval gate. Agents drift from a brief quietly —
    the wrong kind of company, places the brief ruled out, too few results — and
    nothing else in the pipeline compares their work with the user's own words.

    Agents that failed or stopped short are listed straight from their results, no
    model needed. Everything else is judged by the model. If that judgement cannot
    be made, the report says unchecked rather than clean: "no problems found" must
    only ever mean the check actually ran.

    Returns {"checked": bool, "violations": [{"constraint", "problem"}],
             "failed_agents": [{"agent_id", "status", "reason"}]}.
    """
    failed_agents = []
    for r in agent_results or []:
        if not isinstance(r, dict):
            continue
        status = r.get("status", "ok")
        if status != "ok":
            failed_agents.append({
                "agent_id": r.get("agent_id", "unknown"),
                "status": status,
                "reason": r.get("blocked_reason") or r.get("error") or "No reason given.",
            })

    report = {"checked": False, "violations": [], "failed_agents": failed_agents}
    brief = clip_brief(user_brief)
    if not brief or not GEMINI_API_KEY:
        return report

    cycle = cycle or {}
    findings = json.dumps(cycle_blueprint or {}, indent=2, ensure_ascii=False)
    if len(findings) > MAX_FINDINGS_CHARS:
        findings = findings[:MAX_FINDINGS_CHARS] + "\n[... results truncated ...]"

    plan_lines = "\n".join(
        f"- Cycle {c.get('cycle_id', '?')}: {c.get('domain', '')} — {c.get('goal', '')}"
        for c in (plan_cycles or []) if isinstance(c, dict)
    ) or "(not given)"

    prompt = f"""You are checking one research cycle against the user's brief, before the user approves it.

THE USER'S BRIEF:
{brief}

THE WHOLE RESEARCH PLAN (every cycle, in order):
{plan_lines}

THIS CYCLE'S JOB:
Domain: {cycle.get("domain", "")}
Goal: {cycle.get("goal", "")}

WHAT THIS CYCLE PRODUCED:
{findings}

List every place where these results break a concrete requirement of the brief. Look especially for:
- the wrong kind of thing chosen: the brief describes one type of company, customer, product or source and the results use a different type
- places, languages, markets or categories the brief rules in or out
- counts, quotas or rankings the brief sets, where this cycle was responsible for meeting them
- scope quietly narrowed: fewer items, places or categories than the brief asks for
- work this cycle was meant to do that is missing, thin or only partly done
- a requirement of the brief (a count, quota, place, category or deliverable) that NO cycle in the whole plan is set up to meet. Report it even though it is not this cycle's job, because nothing later will catch it, and say "no cycle covers this".
- a requirement "covered" only on paper: a cycle whose goal is to design a method, strategy, template or checklist for some work does not deliver that work. If no cycle produces the actual items (e.g. the real list of competitors), that requirement is covered by no cycle.

Rules:
- Only report real, specific breaks you can point to in the results or the plan. Name the offending items.
- Where the user's later details plainly change an earlier requirement, judge against the later details.
- Judge this cycle's own results only against this cycle's job. Work a later cycle is set up to do is not a violation.
- Do not report style, wording, or ideas for improvement.
- If nothing breaks the brief, return an empty list.

Return a JSON object:
{{
  "violations": [
    {{"constraint": "the requirement from the brief, in a few words", "problem": "what in the results breaks it, specifically"}}
  ]
}}
"""
    config = types.GenerateContentConfig(
        system_instruction="You are a Brief Compliance Checker. Output valid JSON only.",
        response_mime_type="application/json",
    )
    loop = asyncio.get_running_loop()
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config
            )
        )
        violations = json.loads(response.text).get("violations")
        if not isinstance(violations, list):
            raise ValueError("'violations' is not a list")
    except Exception as e:
        print(f"[Brief Check] Could not check cycle {cycle.get('cycle_id')} against the brief: {e}")
        return report

    report["violations"] = [
        {"constraint": str(v.get("constraint", "")).strip(), "problem": str(v.get("problem", "")).strip()}
        for v in violations
        if isinstance(v, dict) and str(v.get("problem", "")).strip()
    ]
    report["checked"] = True
    return report
