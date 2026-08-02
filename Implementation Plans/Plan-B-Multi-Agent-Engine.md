# Implementation Plan B: Multi-Agent Async Engine

> [!IMPORTANT]
> **Do Plan A first.** This plan assumes Priority 1 and 2 from Plan A are done
> (memory table exists, memory tools are registered). Everything else in Plan A
> can be done before or after this plan independently.

> [!WARNING]
> This plan **rewrites the core of `coordinator.py`**. The existing synchronous
> single-agent loop will be replaced with an async orchestration engine. Back up
> `coordinator.py` before starting.

---

## What This Builds

Right now `coordinator.py` is this:
```
User Input → Gemini/Ollama (one call) → Tool calls → Reply
```

After this plan it becomes:
```
User Input → Brain (Orchestrator) → Spawns N Agents in parallel (asyncio)
           → Each Agent: own system prompt + own tools + memory query
           → All results collected → Synthesis Agent → Blueprint
           → Gate waits for human → Execution Agents (parallel)
           → Quality Checker → Gate → Deploy
```

---

## Architecture Decision: How Agents Are Implemented

Each "agent" is a **separate Gemini/Ollama API call** with:
- Its own focused `system_instruction` (e.g. "You are a Hook Researcher. Your only job is...")
- A scoped subset of tools relevant to its role
- An optional memory query result pre-injected into its prompt
- A structured JSON output format so results can be merged by the Synthesis Agent

**We use `asyncio` + `asyncio.gather()`** to run all N agents in parallel.
Gemini's Python SDK supports async via `client.aio.models.generate_content()`.
For Ollama (OpenAI-compatible), we use `httpx.AsyncClient`.

---

## File Plan

| File | Action |
|---|---|
| `coordinator.py` | **Untouched** — voice assistant stays as-is |
| `agents/__init__.py` | [NEW] Package init |
| `agents/brain.py` | [NEW] Brain Orchestrator — decides which agents to spawn |
| `agents/research_agent.py` | [NEW] Generic research agent runner |
| `agents/execution_agent.py` | [NEW] Generic execution agent runner |
| `agents/synthesis.py` | [NEW] Synthesis + conflict detection (standalone, no coordinator import) |
| `agents/quality_checker.py` | [NEW] Quality Checker (standalone, no coordinator import) |
| `multi_agent_coordinator.py` | [NEW] Top-level pipeline orchestrator |

> [!IMPORTANT]
> **`coordinator.py` is NOT touched.** The voice assistant keeps working normally.
> Plan A's `run_synthesis_agent` and `run_quality_checker` should be placed directly
> in `agents/synthesis.py` and `agents/quality_checker.py`, NOT in `coordinator.py`.
> This avoids the import side-effect bug where importing `coordinator` starts
> the Ollama thread and Gemini client prematurely.

---

## Step 1 — Create the `agents/` Package

### [`agents/__init__.py`](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/__init__.py)
```python
# Jarvis Multi-Agent Engine
```

---

## Step 2 — Brain Orchestrator [`agents/brain.py`]

The Brain's job is to take the user's task and decide:
1. What research agents to spawn and what each one's brief is
2. What execution agents to spawn after Gate 1 is approved
3. How to handle Gate rejection notes

```python
"""
Brain Orchestrator — decides agent briefs from a user task description.
Does NOT call APIs directly. Returns spawn plans (lists of agent configs).
"""
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BRAIN_SYSTEM_PROMPT = """
You are the Central Brain Orchestrator of the Jarvis multi-agent system.

Your ONLY job is to produce structured JSON agent spawn plans.
You do NOT execute tasks yourself. You decide who to hire.

When given a task, output a JSON object with this exact structure:
{
  "task_summary": "one sentence description",
  "task_type": "video|code|marketing|research|other",
  "research_agents": [
    {
      "agent_id": "agent_research_1",
      "role": "Hook Researcher",
      "brief": "Research the best hook formats for YouTube videos about AI tools. Focus on retention data.",
      "tools_needed": ["google_search", "search_memory_patterns"],
      "memory_query": "youtube hook formats high retention"
    }
  ],
  "execution_agents": [
    {
      "agent_id": "agent_exec_1",
      "role": "Script Writer",
      "brief": "Write a full YouTube script based on the approved research blueprint.",
      "tools_needed": ["google_search"],
      "output_spec": {
        "required_keys": ["title", "hook", "body", "cta"],
        "min_word_count": 800
      }
    }
  ]
}

Spawn between 2 and 8 research agents depending on task complexity.
Spawn between 1 and 6 execution agents depending on what needs to be built.
Always include at least one memory query per research agent.
"""


def build_agent_plan(task: str, redirect_note: str | None = None) -> dict:
    """
    Ask the Brain to produce an agent spawn plan for the given task.
    If redirect_note is provided, it means Gate 1 was rejected and we are re-planning.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    user_input = task
    if redirect_note:
        user_input = (
            f"ORIGINAL TASK: {task}\n\n"
            f"GATE REJECTION NOTE: {redirect_note}\n\n"
            f"Adjust the research agent briefs to address the rejection note. "
            f"Do not restart from scratch — only modify what the note targets."
        )

    config = types.GenerateContentConfig(
        system_instruction=BRAIN_SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_input,
        config=config
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Brain failed to produce valid JSON", "raw": response.text}
```

---

## Step 3 — Research Agent Runner [`agents/research_agent.py`]

Each research agent is a focused Gemini call with its own system prompt built
from the Brain's brief.

```python
"""
Research Agent — runs a single focused research task asynchronously.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_research_agent(
    agent_config: dict,
    memory_context: str | None = None,
) -> dict:
    """
    Runs a single research agent asynchronously.

    agent_config keys: agent_id, role, brief, tools_needed, memory_query
    memory_context: pre-fetched memory patterns to inject into the prompt
    Returns a dict with agent_id + structured findings.
    """
    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "Researcher")
    brief = agent_config.get("brief", "")

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

{"RELEVANT PAST PATTERNS FROM MEMORY:\n" + memory_context if memory_context else ""}

CRITICAL RULES:
1. Focus ONLY on your brief. Do not go beyond it.
2. Output your findings as a JSON object.
3. Every claim must be backed by evidence (search results, data, or memory patterns).
4. Include an "agent_id" field set to "{agent_id}" in your output.
5. Include a "confidence" field from 0.0 to 1.0 rating how certain you are.

Output format:
{{
  "agent_id": "{agent_id}",
  "role": "{role}",
  "confidence": 0.0-1.0,
  "findings": {{
    "key": "value",
    ...
  }},
  "sources": ["source1", "source2"],
  "recommendation": "one sentence action recommendation"
}}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build tools list based on what the agent needs
    tools_list = [{"google_search": {}}]

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=tools_list,
        response_mime_type="application/json",
    )

    try:
        # [FIX #3] Use get_running_loop(), not get_event_loop() — required in Python 3.10+
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Execute your research brief now. Task context: {brief}",
                config=config
            )
        )
        result = json.loads(response.text)
        result["agent_id"] = agent_id  # ensure it's always set
        result["status"] = "ok"
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e),
            "findings": {}
        }
```

---

## Step 4 — Execution Agent Runner [`agents/execution_agent.py`]

Execution agents are structured the same as research agents but receive the
approved blueprint as context.

```python
"""
Execution Agent — builds a specific deliverable based on the approved blueprint.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_execution_agent(
    agent_config: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None,
) -> dict:
    """
    Runs a single execution agent asynchronously.
    Blueprint is the compressed research output approved at Gate 1.
    gate_redirect_note is set if Gate 2 or Gate 3 was rejected.
    """
    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "Builder")
    brief = agent_config.get("brief", "")
    output_spec = agent_config.get("output_spec", {})

    blueprint_str = json.dumps(blueprint, indent=2)

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

APPROVED RESEARCH BLUEPRINT (use this as your source of truth):
{blueprint_str}

{"GATE REJECTION NOTE (address this specifically in your output):\n" + gate_redirect_note if gate_redirect_note else ""}

REQUIRED OUTPUT KEYS: {json.dumps(output_spec.get("required_keys", []))}
MINIMUM WORD COUNT: {output_spec.get("min_word_count", 0)}

RULES:
1. Stay strictly within your brief.
2. Your output must include ALL required keys.
3. Include "agent_id": "{agent_id}" and "status": "ok" in your response.
4. If you cannot complete the task, set "status": "error" and explain why.

Output valid JSON only.
"""

    client = genai.Client(api_key=GEMINI_API_KEY)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        # [FIX #3] Use get_running_loop(), not get_event_loop() — required in Python 3.10+
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents="Execute your deliverable now according to your brief and blueprint.",
                config=config
            )
        )
        result = json.loads(response.text)
        result["agent_id"] = agent_id
        result.setdefault("status", "ok")
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e)
        }
```

---

## Step 5 — Multi-Agent Coordinator [`multi_agent_coordinator.py`]

This is the new top-level orchestrator. It wires all the pieces together.
`coordinator.py` remains unchanged for the voice assistant — this is a **separate
file** that `jarvis.py` calls for complex multi-agent tasks.

```python
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
```

---

## Step 6 — Flask Integration [`jarvis.py`]

Add a new endpoint that kicks off the full multi-agent pipeline:

```python
@app.route("/pipeline/start", methods=["POST"])
def start_pipeline():
    """Starts the multi-agent pipeline for a complex task."""
    data = request.get_json(force=True) or {}
    task = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "task is required"}), 400

    push_message("system", f"Pipeline started: {task[:80]}...")

    async def gate_fn(gate_number: int, data: dict) -> dict:
        """Polls PIPELINE_STATE until the gate is resolved."""
        with PIPELINE_LOCK:
            PIPELINE_STATE["current_gate"] = gate_number
            PIPELINE_STATE["gate_status"] = "waiting"
            PIPELINE_STATE["redirect_note"] = None

        push_message("system", f"Gate {gate_number} is open. Waiting for your approval.")

        # Poll every 2 seconds until approved or rejected
        import asyncio
        while True:
            await asyncio.sleep(2)
            with PIPELINE_LOCK:
                status = PIPELINE_STATE["gate_status"]
                note = PIPELINE_STATE["redirect_note"]
            if status in ("approved", "rejected"):
                with PIPELINE_LOCK:
                    PIPELINE_STATE["current_gate"] = None
                    PIPELINE_STATE["gate_status"] = "idle"
                return {"approved": status == "approved", "redirect_note": note}

    def run_pipeline():
        import asyncio
        from multi_agent_coordinator import run_full_pipeline
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # [FIX #5] Removed the dead lambda: None third argument
            result = loop.run_until_complete(run_full_pipeline(task, gate_fn))
            push_message("ai", f"Pipeline complete. {result.get('status', 'done')}.")
        except Exception as e:
            push_message("system", f"Pipeline error: {e}")
        finally:
            loop.close()

    # Run pipeline in background thread so Flask responds immediately
    threading.Thread(target=run_pipeline, daemon=True).start()
    return jsonify({"status": "pipeline_started", "task": task})
```

---

## Step 7 — Trigger from UI [`agent_map_final.html`]

Wire the **"Launch Agent Console"** button in the side panel to start the pipeline:

```javascript
document.getElementById('action-btn').addEventListener('click', () => {
    const task = prompt("Enter the task for Jarvis to run:");
    if (!task) return;

    fetch('http://127.0.0.1:5000/pipeline/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task })
    })
    .then(r => r.json())
    .then(data => {
        appendChatMessage(`Pipeline started: "${task}"`, 'user');
        appendChatMessage("Spawning research agents... Check the pipeline view.", 'jarvis');
        // Redirect to pipeline view after 1.5s
        setTimeout(() => window.location.href = 'jarvis_plan_preview.html', 1500);
    });
});
```

---

## Installation Requirements

Add to `requirements.txt`:
```
httpx>=0.27.0
```

Gemini's async support is built into the existing `google-genai` SDK — no new package needed.

---

## Bug Fix Summary

| # | Bug | Location | Fix |
|---|---|---|---|
| 1 | Importing `coordinator` triggers Ollama+Gemini startup | `multi_agent_coordinator.py` line 377 | Import from `agents/synthesis.py` and `agents/quality_checker.py` instead |
| 2 | `blueprint = agent_plan` assigns wrong type after Gate 2 rejection | `multi_agent_coordinator.py` line 501 | Re-run research phase and re-synthesise to produce a real blueprint |
| 3 | `asyncio.get_event_loop()` deprecated in Python 3.10+, error in 3.12 | `research_agent.py` L243, `execution_agent.py` L335 | Changed to `asyncio.get_running_loop()` |
| 4 | `return_exceptions=False` crashes whole pipeline if one agent fails | `run_research_phase`, `run_execution_phase` | Changed to `return_exceptions=True` with per-result error checking |
| 5 | `gate_get_redirect_fn` parameter never used inside `run_full_pipeline` | Function signature + Flask call | Removed from signature and call site |

---

## Verification Plan

### Step-by-Step Test
1. Start Jarvis: `python jarvis.py`
2. Open `agent_map_final.html` in browser
3. Click **Brain** node → click **Launch Agent Console**
4. Enter a task: `"Create a YouTube script about AI tools"`
5. Check the terminal — you should see:
   ```
   [Pipeline] Phase 1: Brain building agent plan...
   [Pipeline] Phase 2: Spawning 4 research agents in parallel...
   [Pipeline] Phase 3: Synthesis agent...
   [Pipeline] GATE 1: Waiting for human approval...
   ```
6. Open `jarvis_plan_preview.html` — click **Approve**
7. Terminal should advance to execution phase
8. Final output JSON should appear in terminal and be sent to the AI message stream

### What "Working" Looks Like
- Multiple `[Multi-Agent] Spawning N agents...` logs appearing simultaneously
- Gate polling visible in terminal (every 2s check)
- Clicking Approve/Reject in the HTML UI actually unblocks the pipeline
- Final `exec_results` contains structured JSON from each execution agent

---

## What This Does NOT Cover

- Real deployment integrations (YouTube upload, GitHub push, Stripe) — those are
  per-platform connectors added to `connectors/`
- A streaming UI that shows each agent's live progress — that requires WebSockets
- Fine-tuned agent role templates per task type (video vs. code vs. marketing)
