"""
Multi-Agent Coordinator — orchestrates the full pipeline:
Brain → Research Agents (parallel) → Synthesis → Gate 1
→ Execution Agents (parallel) → Quality Check → Gate 3 → Deploy
"""
import asyncio
import json
import os
import db
from agents.brain import build_agent_plan
from agents.research_agent import run_research_agent
from agents.execution_agent import run_execution_agent
# [FIX #1] Import from agents/, NOT from coordinator.
# Importing coordinator.py triggers Ollama thread + Gemini client at module load time.
from agents.synthesis import run_synthesis_agent
from agents.quality_checker import run_quality_checker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")


async def run_research_phase(agent_plan: dict, db_conn) -> dict:
    """Spawns all research agents in parallel and returns their results."""
    research_agents = agent_plan.get("research_agents", [])
    if not research_agents:
        return {"status": "error", "message": "No research agents defined in plan."}

    # For each agent, fetch memory context first
    tasks = []
    for agent_config in research_agents:
        memory_query = agent_config.get("memory_query", "")
        memory_context = None
        if memory_query:
            patterns = db.search_memory_patterns(db_conn, memory_query,
                                                  task_type=agent_plan.get("task_type"))
            if patterns:
                memory_context = "\n".join(
                    f"- [{p['outcome'].upper()}] {p['pattern']} "
                    f"(metric: {p.get('metric_name','?')} = {p.get('metric_value','?')})"
                    for p in patterns
                )

        tasks.append(run_research_agent(agent_config, memory_context))

    # Run all research agents in parallel
    # [FIX #4] return_exceptions=True so one failing agent doesn't crash the whole gather.
    # We filter out exceptions below and treat them as error results.
    print(f"[Multi-Agent] Spawning {len(tasks)} research agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            agent_id = research_agents[i].get("agent_id", f"agent_{i}")
            results.append({"agent_id": agent_id, "status": "error", "error": str(r), "findings": {}})
        else:
            results.append(r)

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


async def run_execution_phase(
    agent_plan: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None
) -> dict:
    """Spawns all execution agents in parallel."""
    execution_agents = agent_plan.get("execution_agents", [])
    if not execution_agents:
        return {"status": "error", "message": "No execution agents defined."}

    tasks = [
        run_execution_agent(cfg, blueprint, gate_redirect_note)
        for cfg in execution_agents
    ]

    # [FIX #4] return_exceptions=True — handle per-agent failures gracefully
    print(f"[Multi-Agent] Spawning {len(tasks)} execution agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        if isinstance(r, Exception):
            agent_id = execution_agents[i].get("agent_id", f"exec_{i}")
            results.append({"agent_id": agent_id, "status": "error", "error": str(r)})
        else:
            results.append(r)

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


async def run_full_pipeline(
    task: str,
    gate_approve_fn,           # async fn(gate_number, data) -> {approved, redirect_note}
) -> dict:
    # [FIX #5] Removed unused gate_get_redirect_fn parameter — it was dead code.
    """
    Runs the complete multi-agent pipeline.

    gate_approve_fn: An async function that blocks until the gate is resolved
                     and returns {"approved": True/False, "redirect_note": "..."}
    """
    conn = db.get_connection(DB_PATH)

    try:
        # === PHASE 1: Brain builds agent plan ===
        print("[Pipeline] Phase 1: Brain building agent plan...")
        agent_plan = build_agent_plan(task)
        if "error" in agent_plan:
            return agent_plan

        # === PHASE 2: Research agents run in parallel ===
        print("[Pipeline] Phase 2: Parallel research agents...")
        research_output = await run_research_phase(agent_plan, conn)
        agent_results = research_output.get("agent_results", [])

        # === PHASE 3: Synthesis + conflict check ===
        print("[Pipeline] Phase 3: Synthesis agent...")
        synthesis_result = run_synthesis_agent(agent_results)

        # If conflicts, re-brief with Brain (simplified: return conflict for human review)
        if synthesis_result.get("status") == "conflict":
            print("[Pipeline] Conflicts detected. Escalating to Brain...")
            # Re-plan with conflict context
            conflict_note = synthesis_result.get("message", "Conflicts detected in research.")
            agent_plan = build_agent_plan(task, redirect_note=conflict_note)
            research_output = await run_research_phase(agent_plan, conn)
            synthesis_result = run_synthesis_agent(research_output.get("agent_results", []))

        blueprint = synthesis_result.get("blueprint", {})

        # === GATE 1: Human review of research ===
        print("[Pipeline] GATE 1: Waiting for human approval of research...")
        gate1 = await gate_approve_fn(gate_number=1, data=synthesis_result)
        if not gate1.get("approved"):
            redirect_note = gate1.get("redirect_note")
            print(f"[Pipeline] Gate 1 rejected. Note: {redirect_note}. Re-planning...")
            agent_plan = build_agent_plan(task, redirect_note=redirect_note)
            research_output = await run_research_phase(agent_plan, conn)
            synthesis_result = run_synthesis_agent(research_output.get("agent_results", []))
            blueprint = synthesis_result.get("blueprint", {})

        # === GATE 2: Human review of execution blueprint ===
        print("[Pipeline] GATE 2: Waiting for human approval of blueprint...")
        gate2 = await gate_approve_fn(gate_number=2, data=blueprint)
        if not gate2.get("approved"):
            redirect_note = gate2.get("redirect_note")
            print(f"[Pipeline] Gate 2 rejected. Note: {redirect_note}. Re-building blueprint...")
            # [FIX #2] Re-plan with redirect note, then re-run research to produce a NEW blueprint.
            # The old code did `blueprint = agent_plan` which assigned a spawn config (wrong type).
            agent_plan = build_agent_plan(task, redirect_note=redirect_note)
            research_output = await run_research_phase(agent_plan, conn)
            synthesis_result = run_synthesis_agent(research_output.get("agent_results", []))
            blueprint = synthesis_result.get("blueprint", blueprint)  # fallback to old if synthesis fails

        # === PHASE 5: Execution agents run in parallel ===
        print("[Pipeline] Phase 5: Parallel execution agents...")
        exec_output = await run_execution_phase(agent_plan, blueprint)
        exec_results = exec_output.get("agent_results", [])

        # === QUALITY CHECK ===
        print("[Pipeline] Quality checking execution outputs...")
        exec_specs = [
            cfg.get("output_spec", {})
            for cfg in agent_plan.get("execution_agents", [])
        ]
        # Use first spec for simplicity; in practice, check per-agent
        combined_spec = exec_specs[0] if exec_specs else {}
        qa_result = run_quality_checker(exec_results, combined_spec)

        # Auto re-spawn failed agents (one retry)
        if not qa_result["all_passed"]:
            failed_ids = qa_result["failed_agents"]
            print(f"[Pipeline] QA failed for agents: {failed_ids}. Re-spawning...")
            failed_configs = [
                cfg for cfg in agent_plan.get("execution_agents", [])
                if cfg["agent_id"] in failed_ids
            ]
            retry_tasks = [run_execution_agent(cfg, blueprint) for cfg in failed_configs]
            retry_results = await asyncio.gather(*retry_tasks)
            # Replace failed results with retried results
            retry_map = {r["agent_id"]: r for r in retry_results}
            exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]

        # === GATE 3: Human final review ===
        print("[Pipeline] GATE 3: Waiting for human final approval...")
        gate3 = await gate_approve_fn(gate_number=3, data={"exec_results": exec_results})
        if not gate3.get("approved"):
            redirect_note = gate3.get("redirect_note")
            # Re-run only the parts that were rejected
            print(f"[Pipeline] Gate 3 rejected. Note: {redirect_note}. Partial re-execution...")
            exec_output = await run_execution_phase(
                agent_plan, blueprint, gate_redirect_note=redirect_note
            )
            exec_results = exec_output.get("agent_results", [])

        # === DEPLOY ===
        print("[Pipeline] Pipeline complete. Ready for deployment.")
        return {
            "status": "complete",
            "task": task,
            "blueprint": blueprint,
            "exec_results": exec_results,
            "ready_for_deploy": True
        }

    finally:
        conn.close()
