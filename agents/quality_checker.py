"""
Quality Checker Agent — validates execution outputs against the blueprint specs and briefly.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_quality_checker(
    execution_results: list[dict],
    agent_plan: dict,
    master_blueprint: dict,
) -> dict:
    """
    Validates each execution agent's output against the blueprint spec and brief.
    Returns per-agent pass/fail with reasons.
    """
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

AGENT BRIEF:
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

MASTER BLUEPRINT:
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
