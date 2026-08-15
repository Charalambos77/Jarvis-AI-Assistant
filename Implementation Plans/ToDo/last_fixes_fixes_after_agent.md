# Audit: Missing Items Coverage + Focus Task Bug Analysis

## Part 1: Are the 8 Missing Items from v1 Addressed in v2?

| # | Missing Item | Documented in v2? | Actually Implemented in Code? |
|---|---|---|---|
| 1 | Synthesis uses naive string comparison | ✅ Yes — Multiple anti-pattern warnings + Priority 2 | ❌ **NO** — [synthesis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py) still uses `str(value)` comparison (L29) and `entries[0]["value"]` (L47) |
| 2 | No Deployment Agent | ✅ Yes — Phase 8, Priority 9 | ❌ **NO** — No `agents/deployment_agent.py` exists. Pipeline returns `"ready_for_deploy": True` (coordinator L212) |
| 3 | No Track Agent | ✅ Yes — Phase 9, Priority 7 | ❌ **NO** — No `agents/track_agent.py` exists. `/metrics/` endpoints are passive |
| 4 | No API/Provider Dashboard | ✅ Yes — Phase 1, Priority 10 | ❌ **NO** — `connectors/` still has stubs. `research_agent.py` hardcodes `tools_list = [{"google_search": {}}]` (L62) |
| 5 | No Partial Re-Execution on Gate 3 | ✅ Yes — Priority 11 with detailed algorithm | ❌ **NO** — `multi_agent_coordinator.py` L200–203 re-runs ALL execution agents |
| 6 | Quality Checker is Schema-Only | ✅ Yes — Priority 3 (two-tiered checks) | ❌ **NO** — [quality_checker.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/quality_checker.py) only checks keys + word count |
| 7 | No Max Retry / Loop Guard | ✅ Yes — Priority 12 | ❌ **NO** — Conflict loop (coordinator L130–136) has no retry limit |
| 8 | Blueprint Compression not real | ✅ Yes — Anti-pattern warnings | ❌ **NO** — [synthesis.py L47](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py#L47): `{key: entries[0]["value"]}` |

> [!IMPORTANT]
> **All 8 items are fully documented and specified in `Jarvis-multi-agent-2.md` — but NONE of them have been implemented in actual code yet.** The v2 plan is a specification document that describes the fixes in detail. The code files are unchanged from v1.

---

## Part 2: "Focus on Task 103" Bug — Root Cause Analysis

### How Focus Works (The Flow)

There are **two competing focus mechanisms** in the codebase, and Jarvis may call the wrong one:

#### Mechanism A: `focus_tasks` tool (direct)
```
User says "focus on task 103"
→ Jarvis calls focus_tasks(task_ids=[103])
→ coordinator.py: focus_tasks_impl() returns {"status": "focused", "task_ids": [103]}
→ jarvis.py: tool_listener sets FOCUS_TASK_IDS = [103]
→ UI polls /state → gets focus_task_ids: [103]
→ UI calls animateFocusTask(103) or highlights in task panel
```

#### Mechanism B: `control_interface` tool (action="focus_task")
```
User says "focus on task 103"
→ Jarvis calls control_interface(action="focus_task", payload={task_id: 103})
→ coordinator.py: returns {"status": "success", "action": "focus_task", "payload": {task_id: 103}}
→ jarvis.py: tool_listener sets UI_ACTION = {type: "control_interface", action: "focus_task", payload: {task_id: 103}}
→ UI polls /state → gets ui_action
→ UI handles focus_task action, calls animateFocusTask(103)
```

### 🔴 The Bug: Race Condition + Conflicting Tools

The system prompt (Rule 3) says:
> "When the user mentions a specific task by name or ID, call `focus_tasks` with its ID."

But the system prompt ALSO has `control_interface` with `focus_task` action described in the UI_MAP.

**Problem 1: Jarvis may call `focus_tasks` (Mechanism A) which does work correctly, BUT...**

Looking at [jarvis.py L562-563](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py#L562-L563):
```python
# Clear focused task IDs and UI action after serving so they only trigger once
FOCUS_TASK_IDS = []
```

The `/state` endpoint **clears `FOCUS_TASK_IDS` immediately after one poll**. If the UI polls `/state` slightly BEFORE Jarvis finishes its response and sets the focus IDs, the focus signal is lost. Conversely, if the UI happens to poll *during* the TTS (speaking phase) when the poll rate is 80ms, the focus IDs get picked up and immediately cleared.

**Problem 2: Timing — Jarvis speaks BEFORE the tool result reaches the UI**

The flow is:
1. User says "focus on task 103"
2. Gemini calls `focus_tasks(task_ids=[103])` → `focus_tasks_impl` returns `{"status": "focused"}`
3. Tool listener fires → sets `FOCUS_TASK_IDS = [103]`
4. Gemini sees the tool result and generates text: "Focusing on task 103, Sir."
5. Jarvis speaks: "Focusing on task 103, Sir." ← **Jarvis says it worked**
6. UI polls `/state` → gets `focus_task_ids: [103]` → clears it → **should animate**

The race: Between step 3 and step 6, if the UI polls `/state` BEFORE step 3 completes (i.e., while the tool loop is still running), it gets `focus_task_ids: []` and misses the signal. The next poll happens AFTER step 3, but **step 4-5 takes time** (Gemini API call + TTS). By the time the UI polls again, the `FOCUS_TASK_IDS` was already cleared by the previous poll.

But more critically — **there's a second poll during speech** (80ms interval). This should catch it. So the timing race is possible but unlikely with 80ms polls.

**Problem 3 (Most Likely): `liveNodeMap` doesn't have the task ID**

Looking at the UI handler [command_center.html L3765](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html#L3765):
```javascript
if (liveNodeMap.has(id)) {
    animateFocusTask(id);
} else {
    // Falls through to tasks panel highlight
    openTasksPanel();
    setTimeout(() => {
        const el = document.getElementById(`task-live-${id}`);
        // ...
    }, 350);
}
```

If task 103 exists in the DB but is NOT currently rendered as a 3D node (the `liveNodeMap` only contains tasks visible in the 3D view), it falls through to the "open tasks panel" path. Then it tries to find `document.getElementById('task-live-103')`.

**If the tasks panel wasn't open before**, the panel opens (350ms delay), but the task items might not be fully rendered yet in 350ms. The `getElementById` returns `null` and **nothing happens** — the focus silently fails.

Also if task 103 is a subtask (has `parent_id`), it may not be shown in the top-level task list at all, making `task-live-103` not exist in the DOM.

### 🔴 Summary: Three Bugs

| Bug | Severity | Description |
|---|---|---|
| **Race condition on poll** | Medium | `FOCUS_TASK_IDS` cleared on first poll. If the tool hasn't set it yet, the signal is lost. |
| **Task not in 3D view AND not in panel DOM** | **High** | Subtasks or tasks not loaded in the panel have no DOM element to highlight. Focus silently fails. |
| **Two competing tools** | Low | `focus_tasks` and `control_interface(focus_task)` do similar things via different paths. Jarvis may call one or the other inconsistently. |

### Recommended Fixes

1. **Don't clear `FOCUS_TASK_IDS` on poll** — instead, clear it after the UI sends an acknowledgment (`/ack` endpoint) or use a version counter that the UI tracks.
2. **Make the tasks panel render task 103 and ensure it's in the DOM before trying to highlight** — increase the setTimeout or use a MutationObserver.
3. **Handle subtask focus** — if task 103 has a `parent_id`, expand that parent in the panel first, then scroll to the subtask.
4. **Consolidate focus tools** — make `focus_tasks` internally trigger the `control_interface` path, or remove the duplication. Having two tools that do the same thing confuses the LLM.

---

## Part 3: Will Implementing v2 Break Jarvis?

> [!WARNING]
> **YES — if you implement v2 changes to `multi_agent_coordinator.py` and `agents/brain.py` without also updating `jarvis.py` and `coordinator.py`, the existing single-agent Jarvis will break.**

### What Will Break

1. **`brain.py` output format changes** — v2's Brain produces `{cycles: [...]}` instead of `{research_agents: [...], execution_agents: [...]}`. But `multi_agent_coordinator.py`'s `run_research_phase()` reads `agent_plan.get("research_agents", [])`. If the Brain starts outputting cycles, the old coordinator won't find any agents.

2. **`start_pipeline` in coordinator.py** — The `start_pipeline` tool in [coordinator.py L515-523](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py#L515-L523) triggers the pipeline via `jarvis.py`'s `/pipeline/start` endpoint. That endpoint calls `run_full_pipeline()`. If the pipeline's function signature or gate mechanism changes, the existing endpoint will break.

3. **Gate endpoints** — Current gates are numbered 1-3. v2 has per-cycle gates (N gates for N cycles) plus execution gates. The `/gate/approve` and `/gate/reject` endpoints in jarvis.py expect `gate_number` (1, 2, or 3). Per-cycle gates need a different addressing scheme.

### Safe Implementation Order

1. ✅ Fix the focus task bug FIRST (independent of multi-agent)
2. ✅ Implement v2 changes to `multi_agent_coordinator.py` and `brain.py` as NEW functions (don't overwrite old ones yet)
3. ✅ Update `jarvis.py` gate endpoints to support the new cycle-based gate system
4. ✅ Wire the new pipeline to the existing `start_pipeline` tool
5. ✅ Test the full pipeline end-to-end
6. ✅ Remove old pipeline code
