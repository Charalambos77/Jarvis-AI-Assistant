# Line-Level Implementation Spec — Jarvis Multi-Agent Architecture

> Every change needed, mapped to exact files, line numbers, function signatures, and diffs against the current codebase.

---

## Resolved Decisions

> [!NOTE]
> **Flask + asyncio bridging**: ✅ **Keep the current pattern** — `threading.Thread` + `asyncio.new_event_loop()` in [jarvis.py:789-804](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py#L789-L804). Every async pipeline call gets its own event loop wrapper. No migration to Quart.

> [!NOTE]
> **Lead Specialist two-pass flow**: ✅ **Keep the full two-pass flow**. Cost analysis shows it's negligible:
>
> | Metric | With Two-Pass | Without (Single-Pass) |
> |---|---|---|
> | LLM calls (3-cycle pipeline) | 22 | 19 |
> | Total tokens | ~60,000 | ~48,000 |
> | Cost per pipeline run | **~$0.018** (1.8¢) | ~$0.013 (1.3¢) |
> | Difference | — | **+0.5¢** |
>
> 50 pipeline runs/day = under $1/day. The two-pass Lead review is the core mechanism preventing advisory agents from overwriting domain expertise — worth the half-cent.

> [!NOTE]
> **Deployment Agent scope**: ✅ **Pluggable stub** that logs what it *would* deploy. The real deployment logic is deferred until specific API/MCP connectors are built (Step 6). Additionally, a new **API/MCP Plugging Gate** is added before execution — agents research and recommend the best APIs/MCPs during their cycles, presenting options with pros/cons. The user selects which to use via a checkbox interface, then execution proceeds with the chosen tools. See Step 6 update below.

---

## Step 1: Core Multi-Agent Engine + Research Cycle Loop

### Component: Brain — Cycle-Aware Plan Output

#### [MODIFY] [brain.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/brain.py)

**Current state**: `build_agent_plan()` returns a flat dict with `research_agents` and `execution_agents` lists (L54-84). No concept of cycles.

**Change 1 — Rewrite `BRAIN_SYSTEM_PROMPT` (L15-51)**

Replace the current system prompt with a cycle-aware version. The JSON schema the Brain outputs must change from:

```diff
- {
-   "task_summary": "...",
-   "task_type": "...",
-   "research_agents": [...],
-   "execution_agents": [...]
- }
+ {
+   "task_summary": "...",
+   "task_type": "video|code|marketing|research|other",
+   "cycles": [
+     {
+       "cycle_id": 1,
+       "domain": "Brand & Identity",
+       "goal": "Understand company positioning, voice, and competitive landscape",
+       "lead_specialist": {
+         "agent_id": "cycle1_lead",
+         "role": "Brand Strategist",
+         "brief": "...",
+         "tools_needed": ["google_search", "search_memory_patterns"],
+         "memory_query": "brand positioning strategies"
+       },
+       "advisory_agents": [
+         {
+           "agent_id": "cycle1_adv_1",
+           "role": "Competitor Analyst",
+           "brief": "...",
+           "tools_needed": ["google_search"],
+           "memory_query": "competitor analysis patterns"
+         }
+       ]
+     }
+   ],
+   "recommended_tools": [
+     {
+       "service": "youtube_api",
+       "purpose": "Upload final video to YouTube channel",
+       "recommended_by": ["cycle1_lead", "cycle2_adv_1"],
+       "pros": ["Direct upload", "Metadata control", "Playlist management"],
+       "cons": ["Requires OAuth setup", "Rate limited"],
+       "alternatives": ["manual_upload"]
+     }
+   ],
+   "execution_agents": [
+     {
+       "agent_id": "agent_exec_1",
+       "role": "Script Writer",
+       "brief": "...",
+       "tools_needed": ["google_search"],
+       "output_spec": {
+         "required_keys": ["title", "hook", "body", "cta"],
+         "min_word_count": 800
+       }
+     }
+   ]
+ }
```

The system prompt must enforce:
- Minimum 3 cycles
- Each cycle has exactly 1 `lead_specialist` and 1+ `advisory_agents`
- Agent IDs follow the pattern `cycle{N}_lead`, `cycle{N}_adv_{M}`
- `execution_agents` remain a flat list (they run after ALL cycles complete)

**Change 2 — Update `build_agent_plan()` signature (L54)**

```python
def build_agent_plan(
    task: str,
    redirect_note: str | None = None,
    cycle_id: int | None = None,          # NEW: if set, re-plan only this cycle
    approved_blueprints: list[dict] | None = None,  # NEW: context from prior cycles
) -> dict:
```

When `cycle_id` is set (gate rejection re-routing), the user input includes:
```python
user_input = (
    f"ORIGINAL TASK: {task}\n\n"
    f"GATE REJECTION for Cycle {cycle_id}.\n"
    f"REJECTION NOTE: {redirect_note}\n\n"
    f"APPROVED BLUEPRINTS FROM PRIOR CYCLES:\n{json.dumps(approved_blueprints or [], indent=2)}\n\n"
    f"Re-plan ONLY Cycle {cycle_id}. Keep other cycles unchanged. "
    f"Adjust the research agent briefs to address the rejection note."
)
```

When `approved_blueprints` is provided (normal cycle progression), it's injected into the system prompt context so the Brain knows what prior cycles already established.

---

### Component: Multi-Agent Coordinator — Cycle Loop

#### [MODIFY] [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py)

**Current state**: `run_full_pipeline()` (L100-216) runs a flat flow: Brain → single research phase → synthesis → Gate 1 → Gate 2 → execution → QA → Gate 3 → return. No cycles.

**Full rewrite of `run_full_pipeline()`**. The new function signature:

```python
async def run_full_pipeline(
    task: str,
    gate_approve_fn,        # async fn(gate_id: str, data: dict) -> {approved, redirect_note}
    event_logger=None,      # callable(event_dict) for observability — Step 11
) -> dict:
```

> [!WARNING]
> **Gate numbering changes**: The current code uses `gate_number=1,2,3` (integers). The new system needs per-cycle gates. Gate IDs become strings: `"cycle_1_research"`, `"cycle_2_research"`, ..., `"execution_blueprint"`, `"final_qa"`. This is a **breaking change** for `jarvis.py` gate state management and the UI.

**New flow pseudocode**:

```python
async def run_full_pipeline(task, gate_approve_fn, event_logger=None):
    conn = db.get_connection(DB_PATH)
    MAX_RETRIES = 3  # Step 8: loop guard

    try:
        # Phase 1: Brain builds cycle plan
        agent_plan = build_agent_plan(task)
        cycles = agent_plan.get("cycles", [])
        if len(cycles) < 3:
            return {"error": "Brain must plan at least 3 research cycles"}

        approved_blueprints = []

        # Phase 2: Ordered research cycle loop
        for cycle in cycles:
            cycle_id = cycle["cycle_id"]
            retry_count = 0

            while retry_count < MAX_RETRIES:
                # 2a: Spawn Lead + Advisory agents in parallel (Pass 1)
                all_agents = [cycle["lead_specialist"]] + cycle.get("advisory_agents", [])
                research_output = await run_research_phase_for_cycle(
                    all_agents, conn, agent_plan.get("task_type"),
                    approved_blueprints=approved_blueprints
                )

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
                    agent_plan_update = build_agent_plan(
                        task, redirect_note=f"Conflicts in cycle {cycle_id}: {conflict_note}",
                        cycle_id=cycle_id, approved_blueprints=approved_blueprints
                    )
                    cycle.update(agent_plan_update.get("cycles", [{}])[0])
                    retry_count += 1
                    continue

                # 2c: Per-cycle approval gate
                gate_result = await gate_approve_fn(
                    gate_id=f"cycle_{cycle_id}_research",
                    data={
                        "cycle": cycle,
                        "synthesis": synthesis_result,
                        "approved_so_far": approved_blueprints,
                    }
                )

                if gate_result.get("approved"):
                    approved_blueprints.append(synthesis_result.get("blueprint", {}))
                    break  # advance to next cycle
                else:
                    # Gate rejected — re-brief specific agents
                    redirect_note = gate_result.get("redirect_note", "")
                    # Re-plan only this cycle
                    agent_plan_update = build_agent_plan(
                        task, redirect_note=redirect_note,
                        cycle_id=cycle_id, approved_blueprints=approved_blueprints
                    )
                    # Update only this cycle's agents
                    updated_cycles = agent_plan_update.get("cycles", [])
                    if updated_cycles:
                        cycle.update(updated_cycles[0])
                    retry_count += 1

            if retry_count >= MAX_RETRIES:
                return {"status": "escalated_to_human",
                        "message": f"Cycle {cycle_id} failed after {MAX_RETRIES} retries"}

        # Phase 3: Master Blueprint Compilation
        master_blueprint = await run_master_synthesis(approved_blueprints)

        # Phase 4: Brain builds execution plan
        # (execution_agents already in agent_plan, but Brain may refine based on master_blueprint)

        # Phase 5: Gate — Review Execution Blueprint
        exec_retry = 0
        while exec_retry < MAX_RETRIES:
            gate2 = await gate_approve_fn(
                gate_id="execution_blueprint",
                data={"master_blueprint": master_blueprint, "execution_agents": agent_plan["execution_agents"]}
            )
            if gate2.get("approved"):
                break
            redirect_note = gate2.get("redirect_note", "")
            agent_plan = build_agent_plan(task, redirect_note=redirect_note, approved_blueprints=approved_blueprints)
            exec_retry += 1

        # Phase 6: Execution + Quality Check
        exec_output = await run_execution_phase(agent_plan, master_blueprint)
        exec_results = exec_output.get("agent_results", [])

        qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint)

        qa_retry = 0
        while not qa_result["all_passed"] and qa_retry < MAX_RETRIES:
            failed_ids = qa_result["failed_agents"]
            exec_output = await run_execution_phase(
                agent_plan, master_blueprint, agent_ids_to_run=failed_ids  # Step 7: partial re-exec
            )
            # Merge results
            retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
            exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
            qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint)
            qa_retry += 1

        # Phase 7: Gate — Final QA
        final_retry = 0
        while final_retry < MAX_RETRIES:
            gate3 = await gate_approve_fn(
                gate_id="final_qa",
                data={"exec_results": exec_results}
            )
            if gate3.get("approved"):
                break
            # Step 7: Partial re-execution for rejected components
            redirect_note = gate3.get("redirect_note", "")
            rejected_ids = await identify_rejected_agents(redirect_note, agent_plan)
            exec_output = await run_execution_phase(
                agent_plan, master_blueprint,
                agent_ids_to_run=rejected_ids, gate_redirect_note=redirect_note
            )
            retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
            exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
            final_retry += 1

        # Phase 8: Deployment
        deploy_result = await run_deployment_agent(exec_results, master_blueprint)

        return {
            "status": "complete",
            "task": task,
            "master_blueprint": master_blueprint,
            "exec_results": exec_results,
            "deploy_result": deploy_result,
        }

    finally:
        conn.close()
```

**New functions to add to `multi_agent_coordinator.py`**:

| Function | Signature | Purpose |
|---|---|---|
| `run_research_phase_for_cycle` | `async (agents: list[dict], conn, task_type: str, approved_blueprints: list[dict]) -> dict` | Like current `run_research_phase` but injects `approved_blueprints` into each agent's context |
| `run_lead_review` | `async (lead_config: dict, lead_result: dict, advisory_results: list[dict], approved_blueprints: list[dict]) -> dict` | Pass 2 — Lead Specialist LLM call reviewing advisory findings |
| `run_master_synthesis` | `async (approved_blueprints: list[dict]) -> dict` | Merges N cycle blueprints into one master blueprint via LLM |
| `identify_rejected_agents` | `async (redirect_note: str, agent_plan: dict) -> list[str]` | LLM call to parse redirect note and return agent IDs to re-run |

**Existing functions to modify**:

| Function | Change |
|---|---|
| `run_research_phase` (L22-63) | **Rename** to `run_research_phase_for_cycle`. Add `approved_blueprints` param. Inject blueprints into each agent's context via a new `prior_context` arg to `run_research_agent()`. |
| `run_execution_phase` (L66-97) | Add `agent_ids_to_run: list[str] | None = None` param. When set, filter `execution_agents` to only those matching IDs. |

**Imports to add** (L6-16):

```diff
+ from agents.synthesis import run_synthesis_agent, run_master_synthesis
- from agents.synthesis import run_synthesis_agent
```

---

### Component: Research Agent — Context Injection

#### [MODIFY] [research_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/research_agent.py)

**Change 1 — Add `prior_context` parameter (L15-18)**:

```diff
  async def run_research_agent(
      agent_config: dict,
      memory_context: str | None = None,
+     prior_context: str | None = None,   # approved blueprints from prior cycles
  ) -> dict:
```

**Change 2 — Inject prior_context into system prompt (L30-57)**:

Add after the memory context section (around L36):

```python
{"APPROVED RESEARCH FROM PRIOR CYCLES (use as established context):\n" + prior_context if prior_context else ""}
```

**Change 3 — Remove hardcoded tools_list (L62)**:

```diff
- tools_list = [{"google_search": {}}]
+ # Build tools list dynamically from agent config
+ tools_needed = agent_config.get("tools_needed", ["google_search"])
+ tools_list = []
+ for tool_name in tools_needed:
+     if tool_name == "google_search":
+         tools_list.append({"google_search": {}})
+     # Other tool types will be added by the API/Provider Registry (Step 6)
+ if not tools_list:
+     tools_list = [{"google_search": {}}]  # fallback
```

---

## Step 2: Synthesis Agent Refactor

#### [MODIFY] [synthesis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py)

**Current state**: Pure dict-merging with string comparison conflict detection (L1-53). No LLM calls. This is the exact anti-pattern the design doc warns about.

**Full rewrite**. The file becomes:

```python
"""
Synthesis Agent — LLM-powered conflict detection and blueprint compression.
Replaces the naive dict-merge approach with structured Gemini calls.
"""
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_synthesis_agent(authoritative_output: dict) -> dict:
    """
    Takes the Lead Specialist's authoritative cycle output.
    1. Runs LLM conflict detection for internal contradictions.
    2. If no conflicts, compresses into a hyper-dense cycle blueprint.
    
    Returns: {"status": "ok"|"conflict", "blueprint": {...}, "has_conflicts": bool, "conflicts": [...]}
    """
    ...  # LLM call with response_mime_type="application/json"


async def run_master_synthesis(approved_blueprints: list[dict]) -> dict:
    """
    Takes N approved cycle blueprints and produces one unified Master Research Blueprint.
    This is the document that feeds into the Execution Plan.
    """
    ...  # LLM call that reads all blueprints and produces a unified master


def _detect_conflicts_prompt(findings_json: str) -> str:
    """System prompt for conflict detection."""
    return f"""Analyze the following research output for internal contradictions.
    
    FINDINGS:
    {findings_json}
    
    Return a JSON object:
    {{
        "has_conflicts": true/false,
        "conflicts": [
            {{
                "description": "what contradicts what",
                "agents_involved": ["agent_id_1", "agent_id_2"],
                "options": [
                    {{"name": "Option A", "pros": "...", "cons": "..."}},
                    {{"name": "Option B", "pros": "...", "cons": "..."}}
                ]
            }}
        ]
    }}
    
    If no contradictions, return {{"has_conflicts": false, "conflicts": []}}"""


def _compress_blueprint_prompt(findings_json: str) -> str:
    """System prompt for blueprint compression."""
    return f"""You are the Synthesis Agent. Compress the following research findings 
    into a single hyper-dense blueprint JSON. Resolve overlapping concepts.
    Keep unique, complementary, high-value strategy details from every source.
    Do NOT arbitrarily discard any agent's unique contributions.
    
    FINDINGS:
    {findings_json}
    
    Return a single flat JSON blueprint."""
```

**Key contract**: Both `run_synthesis_agent` and `run_master_synthesis` must be `async` functions that use `asyncio.get_running_loop().run_in_executor()` for the Gemini call (same pattern as [research_agent.py:72-80](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/research_agent.py#L72-L80)).

---

## Step 3: Two-Tiered Quality Checker Agent

#### [MODIFY] [quality_checker.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/quality_checker.py)

**Current state**: Pure schema checks (required keys, word count, error status). No LLM calls. L1-53.

**Add LLM-powered verification**. New signature:

```python
async def run_quality_checker(
    execution_results: list[dict],
    agent_plan: dict,           # NEW: for per-agent specs
    master_blueprint: dict,     # NEW: for integration check
) -> dict:
```

**Tier 1 — Individual Verification** (keep existing schema checks + add LLM):

```python
# For each agent result:
# 1. Schema checks (existing L20-36 logic)
# 2. NEW: LLM call — "Does this output adhere to the agent's brief, spec, and tone?"
#    Input: agent's brief (from agent_plan) + agent's output
#    Output: {"adheres": bool, "issues": ["..."]}
```

**Tier 2 — Global Integration Verification** (entirely new):

```python
# After all individual checks pass:
# LLM call — "Do all these outputs align and integrate seamlessly?"
# Input: all execution results + master_blueprint
# Output: {"integrates": bool, "issues": ["..."]}
```

**Return format** (same as current but richer):

```python
{
    "all_passed": bool,
    "results": [{"agent_id": str, "passed": bool, "issues": [str]}],
    "failed_agents": [str],
    "integration_check": {"passed": bool, "issues": [str]}
}
```

**Import changes**: Add `asyncio`, `json`, `os`, `google.genai`, `google.genai.types`, `dotenv`.

---

## Step 4: Memory Pattern Table Integration

#### [NO CHANGE] [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py)

**The `memory_patterns` table already exists** (L39-47) with the exact schema the design doc specifies. The `save_memory_pattern()` (L351-359) and `search_memory_patterns()` (L361-374) functions already exist and work correctly.

**No changes needed for Step 4.** The table, migration, and query functions are already implemented.

---

## Step 5: HITL Gate API Endpoints & UI Integration

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

**Current state**: Gate endpoints exist at L672-701 (`/gate/status`, `/gate/approve`, `/gate/reject`). The `PIPELINE_STATE` dict (L116-121) tracks a single integer `current_gate`.

**Change 1 — Update `PIPELINE_STATE` (L116-121)**:

```diff
  PIPELINE_STATE = {
-     "current_gate": None,        # None | 1 | 2 | 3
+     "current_gate": None,        # None | "cycle_1_research" | "execution_blueprint" | "final_qa"
      "gate_status": "idle",       # "idle" | "waiting" | "approved" | "rejected"
      "redirect_note": None,       # human's rejection reason
      "phase": "idle",             # e.g. "research" | "synthesis" | "execution" | "deployed"
+     "cycle_data": None,          # dict with cycle info when gate is cycle-level
+     "gate_data": None,           # the full data payload passed to the gate
  }
```

**Change 2 — Update `gate_fn` in `start_pipeline_local()` (L405-421) and `/pipeline/start` (L767-787)**:

Both `gate_fn` closures need to accept `gate_id: str` instead of `gate_number: int`:

```diff
- async def gate_fn(gate_number: int, data: dict) -> dict:
+ async def gate_fn(gate_id: str, data: dict) -> dict:
      with PIPELINE_LOCK:
-         PIPELINE_STATE["current_gate"] = gate_number
+         PIPELINE_STATE["current_gate"] = gate_id
          PIPELINE_STATE["gate_status"] = "waiting"
          PIPELINE_STATE["redirect_note"] = None
+         PIPELINE_STATE["gate_data"] = data
```

**Change 3 — Update `/gate/approve` endpoint (L678-687)** to include `gate_data` in response:

```python
@app.route("/gate/approve", methods=["POST"])
def gate_approve():
    data = request.get_json(force=True) or {}
    with PIPELINE_LOCK:
        gate = PIPELINE_STATE.get("current_gate")
        if gate is None:
            return jsonify({"error": "No gate is currently active"}), 400
        PIPELINE_STATE["gate_status"] = "approved"
        PIPELINE_STATE["redirect_note"] = None
        # NEW: accept per-step approvals
        approved_steps = data.get("approved_steps")  # optional list of step IDs
    push_message("system", f"Gate '{gate}' approved. Advancing pipeline.")
    return jsonify({"status": "approved", "gate": gate})
```

**Change 4 — Update `/gate/reject` endpoint (L690-701)** similarly.

**Change 5 — Add `/gate/data` endpoint** (NEW):

```python
@app.route("/gate/data", methods=["GET"])
def gate_data():
    """Returns the full data payload for the currently active gate."""
    with PIPELINE_LOCK:
        return jsonify({
            "gate": PIPELINE_STATE.get("current_gate"),
            "status": PIPELINE_STATE.get("gate_status"),
            "data": PIPELINE_STATE.get("gate_data"),
        })
```

#### [MODIFY] [plan.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/plan.html)

**Current state**: Static 6-stage layout with hardcoded `approvePlan()` / `rejectPlan()` JS functions (L723-760) that only toggle CSS classes. No backend wiring.

**Changes needed**:

1. **Wire `approvePlan()` to `POST /gate/approve`** (L723-740):

```diff
  function approvePlan() {
+     fetch('/gate/approve', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' })
+         .then(r => r.json())
+         .then(data => {
              const hitlCard = document.getElementById('hitl-card');
              // ... existing CSS changes ...
+         })
+         .catch(err => console.error('Approve failed:', err));
  }
```

2. **Wire `rejectPlan()` to `POST /gate/reject`** (L742-760):

```diff
  function rejectPlan() {
+     const note = prompt("Rejection reason:");
+     if (note === null) return;
+     fetch('/gate/reject', {
+         method: 'POST',
+         headers: {'Content-Type': 'application/json'},
+         body: JSON.stringify({ redirect_note: note })
+     })
+     .then(r => r.json())
+     .then(data => {
          const hitlCard = document.getElementById('hitl-card');
          // ... existing CSS changes ...
+     });
  }
```

3. **Add polling for gate status** (new JS at bottom of `<script>` section):

```javascript
setInterval(async () => {
    const res = await fetch('/gate/status');
    const state = await res.json();
    // Update UI based on state.current_gate, state.gate_status, state.phase
    updatePipelineUI(state);
}, 3000);
```

---

## Step 6: API Provider Config Registry + API/MCP Plugging Gate

> [!IMPORTANT]
> **Key design**: Agents research and recommend the best APIs/MCPs/tools during their research cycles. By the time the API/MCP Plugging Gate fires, the pipeline already has agent-sourced recommendations with pros/cons. The user sees these recommendations in a checkbox interface and selects which to use — Jarvis doesn't dump a blank list, it presents curated options.

#### [MODIFY] [api_connector.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/api_connector.py)

**Current state**: A no-op stub (L1-29) that returns `"not_configured"`.

**Add configuration registry**:

```python
"""
API Provider Configuration Registry.
Maps service names to their API keys, endpoints, status, and fallback chains.
Users plug in APIs/MCPs via the API/MCP Plugging Gate before execution.
"""
import os
import json
from dotenv import load_dotenv

load_dotenv()

# Registry: service_name -> config
API_REGISTRY: dict[str, dict] = {}
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_registry.json")


def load_registry() -> dict:
    """Load registry from disk or return defaults."""
    global API_REGISTRY
    if os.path.exists(REGISTRY_PATH):
        with open(REGISTRY_PATH, "r") as f:
            API_REGISTRY = json.load(f)
    return API_REGISTRY


def save_registry():
    """Persist registry to disk."""
    with open(REGISTRY_PATH, "w") as f:
        json.dump(API_REGISTRY, f, indent=2)


def get_service_config(service_name: str) -> dict | None:
    """Get config for a specific service."""
    return API_REGISTRY.get(service_name)


def get_service_status(service_name: str) -> str:
    """Returns 'up', 'down', or 'rate_limited'."""
    config = API_REGISTRY.get(service_name, {})
    return config.get("status", "unknown")


def mark_service_status(service_name: str, status: str):
    """Update a service's status (called when API errors occur)."""
    if service_name in API_REGISTRY:
        API_REGISTRY[service_name]["status"] = status
        save_registry()


def get_fallback_for(service_name: str) -> str | None:
    """Return the fallback service name, if configured."""
    config = API_REGISTRY.get(service_name, {})
    return config.get("fallback")


def register_service(service_name: str, config: dict):
    """Register or update a service in the registry (called during plugging gate)."""
    API_REGISTRY[service_name] = config
    save_registry()


def get_required_services(master_blueprint: dict) -> list[dict]:
    """
    Analyzes the master blueprint to determine which APIs/MCPs are needed.
    Returns a list of {"service": str, "purpose": str, "status": str}.
    """
    required = []
    # Extract tools_needed from execution agents in the blueprint
    for agent in master_blueprint.get("execution_agents", []):
        for tool in agent.get("tools_needed", []):
            status = get_service_status(tool)
            required.append({
                "service": tool,
                "purpose": f"Used by {agent.get('role', 'agent')}",
                "status": status,
                "configured": status != "unknown",
            })
    return required


def get_tools_for_agent(tools_needed: list[str]) -> list[dict]:
    """
    Dynamically construct a Gemini tools list based on the agent's tools_needed config.
    This replaces hardcoded tools_list in research_agent.py.
    """
    tools = []
    for tool_name in tools_needed:
        if tool_name == "google_search":
            tools.append({"google_search": {}})
        # Future: map tool_name to API connector calls
    return tools or [{"google_search": {}}]
```

**New file**: `api_registry.json` in project root:

```json
{
    "google_search": {"status": "up", "fallback": null},
    "youtube_api": {"status": "up", "api_key_env": "YOUTUBE_API_KEY", "fallback": "supadata"},
    "supadata": {"status": "up", "api_key_env": "SUPADATA_API_KEY", "fallback": null}
}
```

#### API/MCP Plugging Gate (New Pipeline Step)

This is a **new gate** inserted between the Execution Blueprint approval (Gate 2) and execution (Phase 6) in `run_full_pipeline()`.

**How recommendations flow through the pipeline**:

```
Research Agents (cycles 1-3)
  └─ Each agent can include "recommended_tools" in their output
       └─ Synthesis Agent preserves tool recommendations in cycle blueprints
            └─ Master Synthesis aggregates all recommendations, deduplicates,
               and merges pros/cons from multiple agents
                 └─ Master Blueprint has a top-level "tool_recommendations" list
                      └─ API/MCP Plugging Gate presents these to the user
```

**Research Agent output contract** — agents can optionally include tool recommendations in their research output:

```python
# In research_agent.py system prompt, add instruction:
# "If your research reveals specific APIs, services, or tools that would be
#  valuable for executing this task, include them in your output under
#  'recommended_tools' with pros, cons, and why you recommend them."

# Agent output may include:
{
    "findings": "...",
    "recommended_tools": [
        {
            "service": "supadata_api",
            "purpose": "Scrape YouTube competitor video metadata at scale",
            "pros": ["High rate limit", "Returns transcript data", "Affordable"],
            "cons": ["No direct upload capability", "Third-party dependency"],
            "why": "Found during competitor analysis — most reliable scraping API"
        }
    ]
}
```

**Master Synthesis aggregation** — `run_master_synthesis()` in `synthesis.py` collects and deduplicates all tool recommendations across blueprints:

```python
# In the master synthesis prompt, add:
# "Aggregate all 'recommended_tools' from every cycle blueprint.
#  Merge recommendations from multiple agents for the same service.
#  For each tool, list all agents that recommended it and consolidate pros/cons.
#  If agents disagreed on a tool, present both perspectives."

# Master Blueprint output includes:
{
    "unified_research": "...",
    "tool_recommendations": [
        {
            "service": "youtube_api",
            "purpose": "Upload final video",
            "recommended_by": ["cycle1_lead", "cycle2_adv_1"],
            "pros": ["Direct upload", "Metadata control"],
            "cons": ["Requires OAuth", "Rate limited"],
            "alternatives": ["manual_upload"],
            "agent_consensus": "strong"  # strong|mixed|weak
        },
        {
            "service": "supadata_api",
            "purpose": "Scrape competitor data",
            "recommended_by": ["cycle2_lead"],
            "pros": ["Fast", "Reliable"],
            "cons": ["Paid"],
            "alternatives": ["manual_research"],
            "agent_consensus": "weak"
        }
    ]
}
```

**In `multi_agent_coordinator.py`**, after the execution blueprint is approved:

```python
# Phase 5.5: API/MCP Plugging Gate
# Agents have already researched and recommended the best tools.
# Now present their recommendations to the user for selection.
from connectors.api_connector import load_registry, get_service_status
load_registry()

# Enrich recommendations with current registry status
tool_recs = master_blueprint.get("tool_recommendations", [])
for rec in tool_recs:
    rec["configured"] = get_service_status(rec["service"]) != "unknown"
    rec["current_status"] = get_service_status(rec["service"])

# Present to user with agent reasoning
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
    return {"status": "blocked", "message": "User did not confirm API/MCP configuration."}

# Store user's selections for execution agents to use
selected_tools = plugging_gate.get("selected_tools", [t["service"] for t in tool_recs])
```

**In the UI (`plan.html`)**, when `gate_id == "api_mcp_plugging"`, the gate card shows:
- Agent-recommended services with **who recommended them** and **why**
- Pros/cons for each option (as researched by agents)
- Consensus strength (strong/mixed/weak)
- Current config status (✅ configured / ❌ needs API key)
- **Checkboxes** to select which services to use
- Links to configure missing services (opens APIs/MCPs page)
- Approve button confirms the selected set

**New API endpoints in `jarvis.py`**:

```python
@app.route("/registry", methods=["GET"])
def get_registry():
    """Returns the full API/MCP registry."""
    from connectors.api_connector import load_registry
    return jsonify({"registry": load_registry()})


@app.route("/registry/update", methods=["POST"])
def update_registry():
    """Register or update a service. Used during API/MCP plugging."""
    data = request.get_json(force=True) or {}
    service_name = data.get("service")
    config = data.get("config", {})
    if not service_name:
        return jsonify({"error": "service name required"}), 400
    from connectors.api_connector import register_service
    register_service(service_name, config)
    return jsonify({"status": "updated", "service": service_name})
```

#### [MODIFY] [mcp_connector.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/mcp_connector.py)

Similar structure — add registry support for MCP services. Keep the existing `call_mcp()` stub but add `get_mcp_config()` and `list_available_mcps()`.

#### [MODIFY] [__init__.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/__init__.py)

```diff
  from .api_connector import call_external_api
+ from .api_connector import load_registry, get_service_config, get_tools_for_agent, register_service, get_required_services
  from .mcp_connector import call_mcp

- __all__ = ["call_external_api", "call_mcp"]
+ __all__ = ["call_external_api", "call_mcp", "load_registry", "get_service_config", "get_tools_for_agent", "register_service", "get_required_services"]
```

---

## Step 7: Partial Re-Execution Logic

#### [MODIFY] [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py)

**Change 1 — Add `agent_ids_to_run` to `run_execution_phase()` (L66-97)**:

```diff
  async def run_execution_phase(
      agent_plan: dict,
      blueprint: dict,
-     gate_redirect_note: str | None = None
+     gate_redirect_note: str | None = None,
+     agent_ids_to_run: list[str] | None = None,
  ) -> dict:
      execution_agents = agent_plan.get("execution_agents", [])
+     if agent_ids_to_run:
+         execution_agents = [cfg for cfg in execution_agents if cfg["agent_id"] in agent_ids_to_run]
      if not execution_agents:
          return {"status": "error", "message": "No execution agents defined."}
```

**Change 2 — Add `identify_rejected_agents()` function** (new):

```python
async def identify_rejected_agents(redirect_note: str, agent_plan: dict) -> list[str]:
    """
    Uses an LLM call to parse the human's redirect note and identify
    which specific agent IDs produced the rejected component.
    """
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
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
    response = await loop.run_in_executor(
        None,
        lambda: client.models.generate_content(model="gemini-2.5-flash", contents=prompt, config=config)
    )
    return json.loads(response.text)
```

---

## Step 8: Max Retry & Loop Guards

Already integrated into Step 1's `run_full_pipeline()` pseudocode above. The specific additions:

#### [MODIFY] [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py)

**Add at top of file** (after L18):

```python
MAX_RETRIES = 3  # Configurable loop guard for all retry loops
```

**Apply to**:
- Cycle research gate rejections (per-cycle `while retry_count < MAX_RETRIES`)
- Conflict resolution loops
- QA re-runs
- Gate 3 rejections

**Escalation format** when limit hit:

```python
return {
    "status": "escalated_to_human",
    "message": f"Failed after {MAX_RETRIES} retries at {context}.",
    "retry_history": [...],  # list of what was tried
}
```

---

## Step 9: Deployment Agent (Pluggable Stub)

> [!NOTE]
> This is intentionally a **stub**. Real deployment logic will be added when specific API/MCP connectors are built. The stub logs what *would* be deployed and returns a deployment receipt. The actual APIs/MCPs needed are confirmed by the user at the **API/MCP Plugging Gate** (Step 6) before execution begins.

#### [NEW] [deployment_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/deployment_agent.py)

```python
"""
Deployment Agent — takes final execution results and performs actual deployment.

Currently a pluggable stub. Real deployment logic is added per-connector:
- YouTube: upload video via YouTube Data API
- GitHub: push code via GitHub API
- Stripe: create products/prices via Stripe API
- etc.

The user confirms which APIs/MCPs are plugged in at the API/MCP Plugging Gate
(Step 6) BEFORE execution begins. This agent reads the registry to know
which connectors are available.
"""
import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()


async def run_deployment_agent(
    exec_results: list[dict],
    master_blueprint: dict,
    target_platform: str | None = None,
) -> dict:
    """
    Executes deployment based on the execution results and blueprint.
    
    Checks the API registry to see which connectors are available.
    For each execution result, routes to the appropriate connector.
    If no connector is available, logs what WOULD be deployed.
    """
    from connectors.api_connector import load_registry, get_service_status
    registry = load_registry()
    
    deployment_log = []
    
    for result in exec_results:
        agent_id = result.get("agent_id", "unknown")
        status = result.get("status", "unknown")
        
        # Check if a target connector exists in registry
        target = target_platform or "local"
        connector_status = get_service_status(target) if target != "local" else "local"
        
        if connector_status in ("up", "local"):
            # Future: call the actual connector here
            deployment_log.append({
                "agent_id": agent_id,
                "status": "deployed" if status == "ok" else "skipped",
                "platform": target,
                "connector_status": connector_status,
                "output_keys": list(result.keys()),
                "note": "Stub — would deploy here when connector is implemented",
            })
        else:
            deployment_log.append({
                "agent_id": agent_id,
                "status": "blocked",
                "platform": target,
                "connector_status": connector_status,
                "note": f"Connector '{target}' is {connector_status}. Cannot deploy.",
            })
    
    return {
        "status": "deployed",
        "platform": target_platform or "local",
        "deployment_log": deployment_log,
        "artifacts_count": len(exec_results),
        "registry_snapshot": {k: v.get("status", "unknown") for k, v in registry.items()},
    }
```

**Import in coordinator** (L10):

```diff
+ from agents.deployment_agent import run_deployment_agent
```

---

## Step 10: Track Agent & Closed-Loop Feedback

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

**Current state**: `track_agent_loop()` (L724-754) is a basic background thread that polls `TRACKED_METRICS` every 5 minutes and calls `coordinator.handle_request()` with a text prompt.

**Changes needed**:

**Change 1 — Add LLM-powered pattern extraction (L737-754)**:

When performance is ABOVE threshold (a win), the current code does nothing. Add:

```python
if threshold > 0 and value >= threshold:
    # Extract the WHY via LLM
    pattern_prompt = (
        f"A deployment just performed well on metric '{metric_name}' "
        f"(value: {value:.3f}, threshold: {threshold:.3f}). "
        f"Analyze WHY this succeeded based on the execution blueprint and "
        f"produce an actionable insight pattern. Return JSON: "
        f'{{"pattern": "...", "metric_name": "{metric_name}", "metric_value": {value}}}'
    )
    try:
        pattern_response = coordinator.handle_request(pattern_prompt)
        # Parse and save to memory
        conn = db.get_connection(DB_PATH)
        db.save_memory_pattern(conn, pattern=pattern_response,
                               task_type=None, metric_name=metric_name,
                               metric_value=value, outcome='win')
        conn.close()
    except Exception as e:
        print(f"[Track Agent] Error extracting pattern: {e}")
```

**Change 2 — When below threshold (L737-754)**, the corrective loop should spawn a pipeline sub-task at Phase 6 (execution), not just send a text prompt to the coordinator:

```python
if threshold > 0 and value < threshold:
    # ... existing alert code ...
    # Instead of just handle_request(), spawn a corrective sub-task
    corrective_task = f"Corrective sub-task: metric '{metric_name}' underperforming ({value:.3f} < {threshold:.3f})"
    # This enters the pipeline at Phase 6, bypassing research
    start_pipeline_local({"task": corrective_task})
```

---

## Step 11: Real-Time Observability Dashboard

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

**Add after L126 (after TRACKED_METRICS):**

```python
# --- Agent Observability State ---
AGENT_EVENT_LOG: list[dict] = []       # append-only log of all agent lifecycle events
AGENT_REGISTRY: dict[str, dict] = {}   # maps agent_id -> full config + status + output
AGENT_OBS_LOCK = threading.Lock()
```

**Add new API endpoints** (after L720):

```python
@app.route("/agents", methods=["GET"])
def get_agents():
    """Returns the full agent registry."""
    with AGENT_OBS_LOCK:
        return jsonify({"agents": dict(AGENT_REGISTRY)})


@app.route("/agents/<agent_id>", methods=["GET"])
def get_agent_detail(agent_id):
    """Returns a single agent's full detail."""
    with AGENT_OBS_LOCK:
        agent = AGENT_REGISTRY.get(agent_id)
        if not agent:
            return jsonify({"error": f"Agent '{agent_id}' not found"}), 404
        return jsonify(agent)


@app.route("/agents/events", methods=["GET"])
def get_agent_events():
    """Returns the event log. Supports ?since=<timestamp> for polling."""
    since = request.args.get("since", type=float, default=0)
    with AGENT_OBS_LOCK:
        if since > 0:
            events = [e for e in AGENT_EVENT_LOG if e.get("timestamp", 0) > since]
        else:
            events = list(AGENT_EVENT_LOG)
        return jsonify({"events": events})


@app.route("/agents/interactions", methods=["GET"])
def get_agent_interactions():
    """Returns the interaction log (all prompts sent and results received)."""
    with AGENT_OBS_LOCK:
        interactions = [
            e for e in AGENT_EVENT_LOG
            if e.get("event_type") in ("prompt_sent", "result_received", "conflict", "gate_waiting")
        ]
        return jsonify({"interactions": interactions})
```

**Event type enum** (for reference, not enforced in code):

```
spawned | running | completed | error | memory_query | conflict |
gate_waiting | gate_resolved | re_spawned | prompt_sent | result_received
```

**The multi_agent_coordinator must call an `event_logger`** callback at each lifecycle point. This is passed via `run_full_pipeline(task, gate_fn, event_logger=...)` from `jarvis.py`.

The event_logger callback in `jarvis.py`:

```python
def pipeline_event_logger(event: dict):
    """Called by multi_agent_coordinator at each agent lifecycle point."""
    import time
    event["timestamp"] = time.time()
    with AGENT_OBS_LOCK:
        AGENT_EVENT_LOG.append(event)
        # Also update registry if this is an agent event
        agent_id = event.get("source") or event.get("agent_id")
        if agent_id and agent_id not in ("Brain", "System"):
            if agent_id not in AGENT_REGISTRY:
                AGENT_REGISTRY[agent_id] = {}
            AGENT_REGISTRY[agent_id].update({
                "status": event.get("event_type"),
                "last_update": event["timestamp"],
            })
            if event.get("event_type") == "completed":
                AGENT_REGISTRY[agent_id]["output"] = event.get("data")
            if event.get("event_type") == "spawned":
                AGENT_REGISTRY[agent_id]["config"] = event.get("data")
```

---

## Verification Plan

### Automated Tests

```bash
# Unit test: Brain outputs cycle-structured JSON
python -c "from agents.brain import build_agent_plan; import json; p = build_agent_plan('Create a YouTube video about AI tools'); print(json.dumps(p, indent=2)); assert 'cycles' in p; assert len(p['cycles']) >= 3"

# Unit test: Synthesis agent detects conflicts via LLM
python -c "from agents.synthesis import run_synthesis_agent; import asyncio; r = asyncio.run(run_synthesis_agent({'findings': {'a': 'use React', 'b': 'React is unsuitable'}})); print(r)"

# Integration test: Full pipeline with mock gate
python -c "
import asyncio
from multi_agent_coordinator import run_full_pipeline

async def auto_gate(gate_id, data):
    print(f'Auto-approving gate: {gate_id}')
    return {'approved': True}

result = asyncio.run(run_full_pipeline('Create a marketing plan for a SaaS product', auto_gate))
print(result['status'])
assert result['status'] == 'complete'
"
```

### Manual Verification

1. Start the app with `python jarvis.py`
2. Navigate to Plan page
3. Start a pipeline via voice: "Start a pipeline for creating a YouTube video about AI tools"
4. Verify the plan.html UI shows cycle progression
5. Test approve/reject buttons hit the real endpoints
6. Check `/agents/events` endpoint returns live data
7. Check `/gate/data` returns the current gate's payload

---

## Files Summary

| File | Action | Step |
|---|---|---|
| [brain.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/brain.py) | MODIFY — cycle-aware prompts + signature | 1 |
| [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py) | MAJOR REWRITE — cycle loop, partial re-exec, retry guards | 1, 7, 8 |
| [research_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/research_agent.py) | MODIFY — add prior_context, dynamic tools | 1, 6 |
| [synthesis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py) | FULL REWRITE — LLM-powered | 2 |
| [quality_checker.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/quality_checker.py) | MAJOR REWRITE — two-tiered LLM checks | 3 |
| [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py) | NO CHANGE | 4 |
| [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) | MODIFY — gate strings, observability endpoints, event logger | 5, 10, 11 |
| [plan.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/plan.html) | MODIFY — wire approve/reject to backend, add polling | 5 |
| [api_connector.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/api_connector.py) | MAJOR REWRITE — config registry | 6 |
| [mcp_connector.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/mcp_connector.py) | MODIFY — registry support | 6 |
| [connectors/__init__.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/connectors/__init__.py) | MODIFY — new exports | 6 |
| [deployment_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/deployment_agent.py) | NEW FILE | 9 |
| [api_registry.json](file:///d:/Charalambos/Desktop/AI/second-brain-voice/api_registry.json) | NEW FILE | 6 |
