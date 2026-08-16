"""
Multi-Agent Coordinator — orchestrates the full pipeline:
Brain → Multi-Cycle Research Loops → Lead Reviews → Syntheses → Gates
→ Master Compilation → Execution Blueprint Gate → Execution → Quality Checks → Final Gate → Deploy
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
import db
from agents.brain import build_agent_plan
from agents.research_agent import run_research_agent
from agents.execution_agent import run_execution_agent
from agents.synthesis import run_synthesis_agent, run_master_synthesis
from agents.quality_checker import run_quality_checker
from agents.deployment_agent import run_deployment_agent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")


async def run_research_phase_for_cycle(
    agents: list[dict],
    db_conn,
    task_type: str,
    approved_blueprints: list[dict],
    event_logger=None,
) -> dict:
    """Spawns all research agents for a cycle in parallel and returns their results."""
    if not agents:
        return {"status": "error", "message": "No agents defined for this research cycle."}

    prior_context = json.dumps(approved_blueprints, indent=2) if approved_blueprints else None

    # For each agent, fetch memory context first
    tasks = []
    for agent_config in agents:
        agent_id = agent_config.get("agent_id")
        if event_logger:
            event_logger({"event_type": "spawned", "agent_id": agent_id, "data": agent_config})
            event_logger({"event_type": "running", "agent_id": agent_id})
        
        memory_query = agent_config.get("memory_query", "")
        memory_context = None
        if memory_query:
            if event_logger:
                event_logger({"event_type": "memory_query", "agent_id": agent_id, "data": memory_query})
            patterns = db.search_memory_patterns(db_conn, memory_query, task_type=task_type)
            if patterns:
                memory_context = "\n".join(
                    f"- [{p['outcome'].upper()}] {p['pattern']} "
                    f"(metric: {p.get('metric_name','?')} = {p.get('metric_value','?')})"
                    for p in patterns
                )

        tasks.append(run_research_agent(agent_config, memory_context, prior_context))

    print(f"[Multi-Agent] Spawning {len(tasks)} research agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        agent_id = agents[i].get("agent_id", f"agent_{i}")
        if isinstance(r, Exception):
            results.append({"agent_id": agent_id, "status": "error", "error": str(r), "findings": {}})
            if event_logger:
                event_logger({"event_type": "error", "agent_id": agent_id, "data": str(r)})
        else:
            results.append(r)
            if event_logger:
                event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


async def run_lead_review(
    lead_config: dict,
    lead_result: dict,
    advisory_results: list[dict],
    approved_blueprints: list[dict],
) -> list[dict]:
    """
    Pass 2 — Lead Specialist LLM call reviewing advisory findings and merging them.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    lead_id = lead_config.get("agent_id", "lead")
    lead_role = lead_config.get("role", "Lead Specialist")
    lead_brief = lead_config.get("brief", "")

    prompt = f"""You are the Lead Specialist ({lead_role}, ID: {lead_id}) for this research cycle.
Your task is to review the research findings from your advisory agents and consolidate them into your final authoritative findings.

YOUR BRIEF:
{lead_brief}

YOUR INITIAL FINDINGS:
{json.dumps(lead_result.get("findings", lead_result), indent=2)}

ADVISORY FINDINGS TO REVIEW:
{json.dumps(advisory_results, indent=2)}

APPROVED BLUEPRINTS FROM PRIOR CYCLES (for context):
{json.dumps(approved_blueprints, indent=2)}

Produce your final authoritative findings. Output a single JSON object. Do not overwrite your core domain focus, but enhance it with the advisory insights.
The output format must be JSON matching your original format:
{{
  "agent_id": "{lead_id}",
  "role": "{lead_role}",
  "confidence": 0.0-1.0,
  "findings": {{
    "key": "value",
    ...
  }},
  "sources": ["source1", "source2"],
  "recommendation": "one sentence action recommendation"
}}
"""
    config = types.GenerateContentConfig(
        system_instruction=f"You are the {lead_role}. Output valid JSON only.",
        response_mime_type="application/json",
    )

    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config
            )
        )
        final_lead_result = json.loads(response.text)
        final_lead_result["agent_id"] = lead_id
        return [final_lead_result]
    except Exception as e:
        print(f"[Lead Review] Error parsing Lead review output: {e}")
        # Fallback: just return lead_result + advisory_results as a list
        return [lead_result] + advisory_results


async def identify_rejected_agents(redirect_note: str, agent_plan: dict) -> list[str]:
    """
    Uses an LLM call to parse the human's redirect note and identify
    which specific agent IDs produced the rejected component.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    agent_list = json.dumps([
        {"agent_id": a["agent_id"], "role": a["role"], "brief": a["brief"]}
        for a in agent_plan.get("execution_agents", [])
    ], indent=2)

    prompt = f"""Given this human rejection note and list of execution agents,
    identify which agent IDs need to be re-run.

    REJECTION NOTE: {redirect_note}

    AGENTS:
    {agent_list}

    Return a JSON array of agent_id strings that need re-running.
    Example: ["agent_exec_1", "agent_exec_3"]"""

    config = types.GenerateContentConfig(response_mime_type="application/json")
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash", 
                contents=prompt, 
                config=config
            )
        )
        result = json.loads(response.text)
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and "rejected_agent_ids" in result:
            return result["rejected_agent_ids"]
        return [a.get("agent_id") for a in agent_plan.get("execution_agents", [])]
    except Exception as e:
        print(f"[Identify Rejected Agents] Error identifying agents: {e}")
        # Default fallback: re-run all agents
        return [a.get("agent_id") for a in agent_plan.get("execution_agents", [])]


async def run_execution_phase(
    agent_plan: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None,
    agent_ids_to_run: list[str] | None = None,
    event_logger=None,
) -> dict:
    """Spawns all execution agents in parallel."""
    execution_agents = agent_plan.get("execution_agents", [])
    if not execution_agents:
        return {"status": "error", "message": "No execution agents defined."}

    # Filter execution agents if a subset is requested
    if agent_ids_to_run is not None:
        execution_agents = [cfg for cfg in execution_agents if cfg.get("agent_id") in agent_ids_to_run]
        if not execution_agents:
            return {"status": "ok", "agent_results": [], "agent_count": 0}

    # Trigger spawned and running events for active execution agents
    for cfg in execution_agents:
        agent_id = cfg.get("agent_id")
        if event_logger:
            event_logger({"event_type": "spawned", "agent_id": agent_id, "data": cfg})
            event_logger({"event_type": "running", "agent_id": agent_id})

    tasks = [
        run_execution_agent(cfg, blueprint, gate_redirect_note)
        for cfg in execution_agents
    ]

    # return_exceptions=True — handle per-agent failures gracefully
    print(f"[Multi-Agent] Spawning {len(tasks)} execution agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        agent_id = execution_agents[i].get("agent_id", f"exec_{i}")
        if isinstance(r, Exception):
            results.append({"agent_id": agent_id, "status": "error", "error": str(r)})
            if event_logger:
                event_logger({"event_type": "error", "agent_id": agent_id, "data": str(r)})
        else:
            results.append(r)
            if event_logger:
                event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


def save_agent_plan_file(plan_id: str, agent_plan: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"agent_plan_{plan_id}.md")
    
    content = f"# Agent Spawn Plan - Plan ID: {plan_id}\n"
    content += f"**Task Summary:** {agent_plan.get('task_summary', 'N/A')}\n"
    content += f"**Task Type:** {agent_plan.get('task_type', 'N/A')}\n\n"
    content += "## Research Cycles\n"
    for cycle in agent_plan.get('cycles', []):
        content += f"### Cycle {cycle.get('cycle_id')}: {cycle.get('domain', 'N/A')}\n"
        content += f"- **Goal:** {cycle.get('goal', 'N/A')}\n"
        lead = cycle.get('lead_specialist', {})
        content += f"- **Lead Specialist:** {lead.get('role')} (ID: {lead.get('agent_id')})\n"
        content += f"  - Brief: {lead.get('brief')}\n"
        content += f"- **Advisory Agents:**\n"
        for adv in cycle.get('advisory_agents', []):
            content += f"  - {adv.get('role')} (ID: {adv.get('agent_id')}): {adv.get('brief')}\n"
        content += "\n"

    content += "## Execution Agents\n"
    for exec_a in agent_plan.get('execution_agents', []):
        content += f"### {exec_a.get('role')} (ID: {exec_a.get('agent_id')})\n"
        content += f"- **Brief:** {exec_a.get('brief')}\n"
        content += f"- **Required Keys:** {', '.join(exec_a.get('output_spec', {}).get('required_keys', []))}\n"
        content += f"- **Min Word Count:** {exec_a.get('output_spec', {}).get('min_word_count', 0)}\n\n"

    content += "## Full JSON Payload\n```json\n" + json.dumps(agent_plan, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_research_findings_file(plan_id: str, agent_id: str, findings: dict, project_name: str = "Default Project"):
    if not plan_id or not agent_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"research_{agent_id}_{plan_id}.md")
    
    content = f"# Research Findings - Agent ID: {agent_id} (Plan ID: {plan_id})\n"
    content += f"**Role:** {findings.get('role', 'Researcher')}\n"
    content += f"**Confidence:** {findings.get('confidence', 'N/A')}\n\n"
    content += "## Findings Details\n"
    
    fds = findings.get("findings", {})
    if isinstance(fds, dict):
        for k, v in fds.items():
            content += f"### {k}\n{v}\n\n"
    else:
        content += f"{fds}\n\n"
        
    content += "## Recommendation\n"
    content += f"{findings.get('recommendation', 'N/A')}\n\n"
    
    content += "## Sources\n"
    for src in findings.get('sources', []):
        content += f"- {src}\n"
    content += "\n"
    
    content += "## Full JSON Payload\n```json\n" + json.dumps(findings, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_cycle_blueprint_file(plan_id: str, cycle_id: int, blueprint: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"cycle_blueprint_{cycle_id}_{plan_id}.md")
    
    content = f"# Cycle {cycle_id} Blueprint (Plan ID: {plan_id})\n\n"
    content += "## Synthesized Cycle Details\n"
    content += "```json\n" + json.dumps(blueprint, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_master_blueprint_file(plan_id: str, master_blueprint: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"master_blueprint_{plan_id}.md")
    
    content = f"# Master Research Blueprint (Plan ID: {plan_id})\n\n"
    
    tool_recs = master_blueprint.get("tool_recommendations", [])
    if tool_recs:
        content += "## Tool Recommendations\n"
        for tool in tool_recs:
            content += f"### Tool: {tool.get('service', 'N/A')}\n"
            content += f"- **Purpose:** {tool.get('purpose', 'N/A')}\n"
            content += f"- **Consensus Strength:** {tool.get('agent_consensus', tool.get('consensus_strength', 'N/A'))}\n"
            content += f"- **Recommended By:** {', '.join(tool.get('recommended_by', []))}\n"
            content += f"- **Pros:** {', '.join(tool.get('pros', []))}\n"
            content += f"- **Cons:** {', '.join(tool.get('cons', []))}\n"
            content += f"- **Alternatives:** {', '.join(tool.get('alternatives', []))}\n\n"
            
    content += "## Compiled Blueprint Details\n"
    content += "```json\n" + json.dumps(master_blueprint, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_execution_output_file(plan_id: str, agent_id: str, output: dict, project_name: str = "Default Project"):
    if not plan_id or not agent_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"execution_{agent_id}_{plan_id}.md")
    
    content = f"# Execution Deliverable - Agent ID: {agent_id} (Plan ID: {plan_id})\n\n"
    
    for k, v in output.items():
        if k not in ("agent_id", "status"):
            content += f"## {k}\n{v}\n\n"
            
    content += "## Full JSON Payload\n```json\n" + json.dumps(output, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_final_report_file(plan_id: str, report: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"final_report_{plan_id}.md")
    
    content = f"# Final Pipeline Execution Report (Plan ID: {plan_id})\n\n"
    content += f"**Task:** {report.get('task', 'N/A')}\n\n"
    
    deploy = report.get("deploy_result", {})
    content += "## Deployment Status\n"
    content += f"- **Status:** {deploy.get('status', 'N/A')}\n"
    content += f"- **Message:** {deploy.get('message', 'N/A')}\n"
    if "url" in deploy:
        content += f"- **URL:** {deploy.get('url')}\n"
    content += "\n"
    
    content += "## Full JSON Payload\n```json\n" + json.dumps(report, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

MAX_RETRIES = 3  # Configurable loop guard for all retry loops


async def run_full_pipeline(
    task: str,
    gate_approve_fn,        # async fn(gate_id: str, data: dict) -> {approved, redirect_note}
    event_logger=None,      # callable(event_dict) for observability — Step 11
    plan_id: str | None = None,
    project_name: str = "Default Project"
) -> dict:
    """
    Runs the complete multi-agent pipeline with ordered cycles.
    """
    conn = db.get_connection(DB_PATH)
    retry_history = []

    # Wrap the event_logger to inject plan_id automatically
    original_event_logger = event_logger
    def local_logger(event: dict):
        if original_event_logger:
            event["plan_id"] = plan_id
            original_event_logger(event)

    event_logger = local_logger

    try:
        # Check if we are resuming an existing plan
        existing_plan = None
        if plan_id:
            try:
                pipelines = db.get_pipelines(conn)
                for p in pipelines:
                    if p["id"] == plan_id:
                        existing_plan = p
                        break
            except Exception as e:
                print(f"[Pipeline] Error checking for existing plan: {e}")

        agent_plan = None
        approved_blueprints = []

        if existing_plan:
            # Check if agent plan exists on disk and load it to support user edits
            plan_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
            plan_file = os.path.join(plan_dir, f"agent_plan_{plan_id}.md")
            if os.path.exists(plan_file):
                try:
                    with open(plan_file, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    if "```json" in file_content:
                        parts = file_content.split("```json")
                        json_part = parts[-1].split("```")[0].strip()
                        agent_plan = json.loads(json_part)
                        print(f"[Pipeline] Successfully read and updated agent plan from disk: {plan_file}")
                except Exception as e:
                    print(f"[Pipeline] Error reading agent plan from disk: {e}")
                    agent_plan = existing_plan.get("agent_plan")
            else:
                agent_plan = existing_plan.get("agent_plan")
            
            approved_blueprints = existing_plan.get("approved_blueprints", [])
            
            # Fallback: if approved_blueprints is empty in DB, reconstruct it from cycle blueprint files on disk
            if not approved_blueprints:
                for i in range(1, 10):
                    cb_file = os.path.join(plan_dir, f"cycle_blueprint_{i}_{plan_id}.md")
                    if os.path.exists(cb_file):
                        try:
                            with open(cb_file, "r", encoding="utf-8") as f:
                                cb_content = f.read()
                            if "```json" in cb_content:
                                cb_json_part = cb_content.split("```json")[-1].split("```")[0].strip()
                                cb_data = json.loads(cb_json_part)
                                approved_blueprints.append(cb_data)
                                print(f"[Pipeline] Reconstructed approved blueprint for Cycle {i} from disk.")
                        except Exception as e:
                            print(f"[Pipeline] Error reading cycle blueprint {i} from disk: {e}")

            print(f"[Pipeline] Resuming existing plan '{plan_id}'. Approved blueprints so far: {len(approved_blueprints)}")

        if not agent_plan:
            # Phase 1: Brain builds cycle plan
            print("[Pipeline] Phase 1: Central Brain generating multi-cycle agent plan...")
            agent_plan = build_agent_plan(task)
            if "error" in agent_plan:
                return agent_plan
            save_agent_plan_file(plan_id, agent_plan, project_name)
            if event_logger:
                event_logger({"event_type": "agent_plan_compiled", "source": "Brain", "data": agent_plan})

        cycles = agent_plan.get("cycles", [])
        if len(cycles) < 3:
            return {"error": "Brain must plan at least 3 research cycles"}

        # Phase 2: Ordered research cycle loop
        for cycle in cycles:
            cycle_id = cycle["cycle_id"]
            domain = cycle.get("domain", f"Cycle {cycle_id}")

            cycle_index = cycle_id - 1
            if cycle_index < len(approved_blueprints):
                print(f"[Pipeline] Cycle {cycle_id} ({domain}) is already approved. Skipping research.")
                continue

            print(f"[Pipeline] Starting Cycle {cycle_id}: {domain}")
            retry_count = 0

            while retry_count < MAX_RETRIES:
                if event_logger:
                    event_logger({"event_type": "running", "source": f"Cycle {cycle_id}"})
                
                # 2a: Spawn Lead + Advisory agents in parallel (Pass 1)
                all_agents = [cycle["lead_specialist"]] + cycle.get("advisory_agents", [])
                research_output = await run_research_phase_for_cycle(
                    all_agents, conn, agent_plan.get("task_type", "research"),
                    approved_blueprints=approved_blueprints, event_logger=event_logger
                )
                
                # Save each research agent's findings
                for r in research_output.get("agent_results", []):
                    if r.get("status") == "ok":
                        save_research_findings_file(plan_id, r.get("agent_id"), r, project_name)

                # 2a: Lead Specialist review (Pass 2)
                lead_config = cycle["lead_specialist"]
                advisory_results = [r for r in research_output["agent_results"]
                                    if r["agent_id"] != lead_config["agent_id"]]
                lead_result = next(r for r in research_output["agent_results"]
                                   if r["agent_id"] == lead_config["agent_id"])

                authoritative_output = await run_lead_review(
                    lead_config, lead_result, advisory_results, approved_blueprints
                )

                # 2b: Synthesis (per-cycle)
                synthesis_result = await run_synthesis_agent(authoritative_output)

                if synthesis_result.get("has_conflicts"):
                    # Route conflicts to Brain for adjudication
                    conflict_note = json.dumps(synthesis_result["conflicts"])
                    print(f"[Pipeline] Conflict in Cycle {cycle_id}. Re-briefing Brain...")
                    retry_history.append(f"Cycle {cycle_id} conflict retry {retry_count + 1} due to findings contradictions.")
                    if event_logger:
                        event_logger({"event_type": "conflict", "source": "synthesis", "data": synthesis_result})
                    agent_plan_update = build_agent_plan(
                        task, redirect_note=f"Conflicts in cycle {cycle_id}: {conflict_note}",
                        cycle_id=cycle_id, approved_blueprints=approved_blueprints
                    )
                    updated_cycles = agent_plan_update.get("cycles", [])
                    if updated_cycles:
                        cycle.update(updated_cycles[0])
                    retry_count += 1
                    continue

                # Save cycle blueprint
                save_cycle_blueprint_file(plan_id, cycle_id, synthesis_result.get("blueprint", {}), project_name)

                # 2c: Per-cycle approval gate
                print(f"[Pipeline] Waiting for human approval of Cycle {cycle_id} research...")
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": f"cycle_{cycle_id}_research", "data": cycle})
                
                gate_result = await gate_approve_fn(
                    gate_id=f"cycle_{cycle_id}_research",
                    data={
                        "cycle": cycle,
                        "synthesis": synthesis_result,
                        "approved_so_far": approved_blueprints,
                    }
                )

                if gate_result.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": f"cycle_{cycle_id}_research", "data": gate_result})
                        event_logger({
                            "event_type": "cycle_approved",
                            "source": f"cycle_{cycle_id}_research",
                            "data": {
                                "cycle_id": cycle_id,
                                "blueprint": synthesis_result.get("blueprint", {})
                            }
                        })
                    approved_blueprints.append(synthesis_result.get("blueprint", {}))
                    break  # advance to next cycle
                else:
                    # Gate rejected — re-brief specific agents
                    redirect_note = gate_result.get("redirect_note", "")
                    rejected_steps = gate_result.get("rejected_steps")
                    print(f"[Pipeline] Cycle {cycle_id} gate rejected. Re-planning. Note: {redirect_note}")
                    retry_history.append(f"Cycle {cycle_id} rejection retry {retry_count + 1}. Feedback: {redirect_note}")
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": f"cycle_{cycle_id}_research", "data": gate_result})
                    # Re-plan only this cycle
                    agent_plan_update = build_agent_plan(
                        task, redirect_note=redirect_note,
                        cycle_id=cycle_id, approved_blueprints=approved_blueprints,
                        rejected_steps=rejected_steps
                    )
                    # Update only this cycle's agents
                    updated_cycles = agent_plan_update.get("cycles", [])
                    if updated_cycles:
                        cycle.update(updated_cycles[0])
                    retry_count += 1

            if retry_count >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Cycle {cycle_id} research loop.",
                    "retry_history": retry_history,
                }

        # Phase 3: Master Blueprint Compilation
        master_blueprint = None
        if existing_plan:
            blueprint_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
            blueprint_file = os.path.join(blueprint_dir, f"master_blueprint_{plan_id}.md")
            if os.path.exists(blueprint_file):
                try:
                    with open(blueprint_file, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    if "```json" in file_content:
                        parts = file_content.split("```json")
                        json_part = parts[-1].split("```")[0].strip()
                        disk_bp = json.loads(json_part)
                        
                        if "tool_recommendations" not in disk_bp or not disk_bp["tool_recommendations"]:
                            db_bp = existing_plan.get("master_blueprint", {})
                            disk_bp["tool_recommendations"] = db_bp.get("tool_recommendations", [])
                        
                        master_blueprint = disk_bp
                        print(f"[Pipeline] Successfully read and updated master blueprint from disk: {blueprint_file}")
                except Exception as e:
                    print(f"[Pipeline] Error reading master blueprint from disk: {e}")
                    master_blueprint = existing_plan.get("master_blueprint")
            else:
                master_blueprint = existing_plan.get("master_blueprint")

        if not master_blueprint or not master_blueprint.get("tool_recommendations"):
            print("[Pipeline] Phase 3: Compiling Master Blueprint from all cycles...")
            master_blueprint = await run_master_synthesis(approved_blueprints)
            save_master_blueprint_file(plan_id, master_blueprint, project_name)
            if event_logger:
                event_logger({"event_type": "blueprint_compiled", "source": "synthesis", "data": master_blueprint})
        else:
            print("[Pipeline] Resuming: Found existing Master Blueprint. Skipping compilation.")

        # Check if the execution blueprint gate was already approved
        skip_exec_gate = False
        if existing_plan and existing_plan.get("phase") in ('execution', 'qa', 'deploy', 'complete'):
            skip_exec_gate = True

        if not skip_exec_gate:
            # Phase 5: Gate — Review Execution Blueprint
            print("[Pipeline] Phase 5: Waiting for human approval of execution blueprint...")
            exec_retry = 0
            while exec_retry < MAX_RETRIES:
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": "execution_blueprint", "data": {"master_blueprint": master_blueprint, "execution_agents": agent_plan["execution_agents"]}})
                
                gate2 = await gate_approve_fn(
                    gate_id="execution_blueprint",
                    data={"master_blueprint": master_blueprint, "execution_agents": agent_plan["execution_agents"]}
                )
                if gate2.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": "execution_blueprint", "data": gate2})
                    break
                redirect_note = gate2.get("redirect_note", "")
                rejected_steps = gate2.get("rejected_steps")
                print(f"[Pipeline] Execution Blueprint gate rejected. Re-planning. Note: {redirect_note}")
                retry_history.append(f"Execution Blueprint rejection retry {exec_retry + 1}. Feedback: {redirect_note}")
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "execution_blueprint", "data": gate2})
                agent_plan = build_agent_plan(
                    task, redirect_note=redirect_note, approved_blueprints=approved_blueprints,
                    rejected_steps=rejected_steps
                )
                exec_retry += 1

            if exec_retry >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Execution Blueprint gate.",
                    "retry_history": retry_history,
                }

        # Check if plugging gate was already approved
        skip_plugging_gate = False
        if existing_plan and existing_plan.get("phase") in ('execution', 'qa', 'deploy', 'complete'):
            # Only skip if all recommended APIs/MCPs are configured
            from connectors.api_connector import load_registry, get_service_status
            load_registry()
            tool_recs = master_blueprint.get("tool_recommendations", [])
            
            def is_api_or_mcp(service_name):
                if not service_name:
                    return False
                s = str(service_name).lower()
                libraries = [
                    "react", "react.js", "next.js", "nextjs", "tailwind", "tailwind css", "tailwindcss", "typescript", "styled-components", 
                    "styled_components", "css", "html", "javascript", "webpack", "babel", 
                    "vite", "eslint", "prettier", "jest", "cypress", "playwright",
                    "npm", "yarn", "pip", "python", "node.js", "nodejs", "express", "django",
                    "laravel", "spring", "flask", "fastapi", "redux", "redux toolkit", "redux-toolkit", "git"
                ]
                for lib in libraries:
                    if s == lib or s.startswith(lib + " ") or s.endswith(" " + lib):
                        return False
                return True
                
            unconfigured_apis = [
                rec for rec in tool_recs 
                if is_api_or_mcp(rec["service"]) and get_service_status(rec["service"]) == "unknown"
            ]
            if not unconfigured_apis:
                skip_plugging_gate = True

        if not skip_plugging_gate:
            # Phase 5.5: API/MCP Plugging Gate
            from connectors.api_connector import load_registry, get_service_status
            load_registry()

            tool_recs = master_blueprint.get("tool_recommendations", [])
            for rec in tool_recs:
                rec["configured"] = get_service_status(rec["service"]) != "unknown"
                rec["current_status"] = get_service_status(rec["service"])

            print("[Pipeline] Phase 5.5: Waiting for human confirmation of API/MCP tools...")
            if event_logger:
                event_logger({"event_type": "gate_waiting", "source": "api_mcp_plugging", "data": {"tool_recommendations": tool_recs}})
            
            plugging_gate = await gate_approve_fn(
                gate_id="api_mcp_plugging",
                data={
                    "tool_recommendations": tool_recs,
                    "message": (
                        "Your agents researched and recommend the following APIs/MCPs. "
                        "Select the ones you want to use. Unconfigured services will need API keys."
                    ),
                    "registry": load_registry(),
                }
            )
            if not plugging_gate.get("approved"):
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "api_mcp_plugging", "data": plugging_gate})
                return {"status": "blocked", "message": "User did not confirm API/MCP configuration."}

            if event_logger:
                event_logger({"event_type": "gate_resolved", "source": "api_mcp_plugging", "data": plugging_gate})

        # Check if execution phase completed
        skip_execution = False
        exec_results = []
        if existing_plan and existing_plan.get("phase") in ('qa', 'deploy', 'complete'):
            skip_execution = True
            exec_results = existing_plan.get("exec_results", [])
            print("[Pipeline] Resuming: Execution deliverables already completed. Skipping execution agents.")

        if not skip_execution:
            # Phase 6: Execution + Quality Check
            print("[Pipeline] Phase 6: Parallel execution agents...")
            exec_output = await run_execution_phase(agent_plan, master_blueprint, event_logger=event_logger)
            exec_results = exec_output.get("agent_results", [])
            for r in exec_results:
                if r.get("status") == "ok":
                    save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)

            qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint)

            qa_retry = 0
            while not qa_result["all_passed"] and qa_retry < MAX_RETRIES:
                failed_ids = qa_result["failed_agents"]
                print(f"[Pipeline] Quality check failed for: {failed_ids}. Re-running those agents...")
                for res in qa_result["results"]:
                    if not res["passed"]:
                        print(f"  - Agent {res['agent_id']} issues: {res['issues']}")
                
                retry_history.append(f"QA verification failure retry {qa_retry + 1} for agents {failed_ids}.")
                exec_output = await run_execution_phase(
                    agent_plan, master_blueprint, agent_ids_to_run=failed_ids, event_logger=event_logger
                )
                retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
                exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
                for r in exec_results:
                    if r.get("status") == "ok":
                        save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)
                qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint)
                qa_retry += 1

            if qa_retry >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Quality Checker verification.",
                    "retry_history": retry_history,
                }

            if event_logger:
                event_logger({"event_type": "execution_completed", "source": "execution", "data": exec_results})

        # Check if Final QA was approved
        skip_final_qa = False
        if existing_plan and (existing_plan.get("status") == "complete" or existing_plan.get("phase") in ('deploy', 'complete')):
            skip_final_qa = True

        if not skip_final_qa:
            # Phase 7: Gate — Final QA
            print("[Pipeline] Phase 7: Waiting for final human QA gate...")
            final_retry = 0
            while final_retry < MAX_RETRIES:
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": "final_qa", "data": exec_results})
                
                gate3 = await gate_approve_fn(
                    gate_id="final_qa",
                    data={"exec_results": exec_results}
                )
                if gate3.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": "final_qa", "data": gate3})
                    break
                redirect_note = gate3.get("redirect_note", "")
                rejected_steps = gate3.get("rejected_steps")
                print(f"[Pipeline] Final QA gate rejected. Re-running target agents. Note: {redirect_note}")
                retry_history.append(f"Final QA rejection retry {final_retry + 1}. Feedback: {redirect_note}")
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "final_qa", "data": gate3})
                if rejected_steps:
                    rejected_ids = rejected_steps
                else:
                    rejected_ids = await identify_rejected_agents(redirect_note, agent_plan)
                exec_output = await run_execution_phase(
                    agent_plan, master_blueprint,
                    agent_ids_to_run=rejected_ids, gate_redirect_note=redirect_note, event_logger=event_logger
                )
                retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
                exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
                for r in exec_results:
                    if r.get("status") == "ok":
                        save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)
                final_retry += 1

            if final_retry >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Final human QA gate.",
                    "retry_history": retry_history,
                }

        # Check if deployment completed
        skip_deploy = False
        if existing_plan and existing_plan.get("status") == "complete":
            skip_deploy = True

        if not skip_deploy:
            # Phase 8: Deployment
            print("[Pipeline] Phase 8: Calling deployment agent...")
            if event_logger:
                event_logger({"event_type": "running", "source": "DeploymentAgent"})
            deploy_result = await run_deployment_agent(exec_results, master_blueprint)
            if event_logger:
                event_logger({"event_type": "completed", "source": "DeploymentAgent", "data": deploy_result})

            final_rep = {
                "task": task,
                "master_blueprint": master_blueprint,
                "exec_results": exec_results,
                "deploy_result": deploy_result,
            }
            save_final_report_file(plan_id, final_rep, project_name)

            return {
                "status": "complete",
                "task": task,
                "master_blueprint": master_blueprint,
                "exec_results": exec_results,
                "deploy_result": deploy_result,
            }

        return {
            "status": "complete",
            "task": task,
            "master_blueprint": master_blueprint,
            "exec_results": exec_results,
            "deploy_result": existing_plan.get("deploy_result", {}),
        }

    finally:
        conn.close()
