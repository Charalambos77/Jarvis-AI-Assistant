# Implementation Plan: Command-to-UI Alignments — Technical Execution

## Background

This plan implements all 15 proposed Command-to-UI Alignments from the design document. The changes span three files:

- [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py) — AI brain, system prompt, tool dispatcher
- [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) — Flask server, state management, `/state` and `/ask` routes
- [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html) — 3D scene, animation engine, pollState loop

---

## Phase 1 — Signal System (coordinator.py + jarvis.py)

The frontend needs to know *which animation to play* after each command. We will add a `ui_action` signal to the shared state so `pollState()` in the browser can dispatch the right animation.

---

### [MODIFY] [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py)

#### 1a. Update the Gemini system prompt

The current system prompt does not mention the 3D interface at all. Expand it to:

- Describe the 3D Constellation Map and what the commands do visually.
- Tell Jarvis that after any task/note mutation, it must still invoke the correct DB tool — the animation is handled separately by the frontend.
- Make Jarvis aware of the new navigation commands so it does not try to call a DB tool for "show plan" or "close panel".

#### 1b. Update the Ollama system prompt

The same changes as above must be applied to the Ollama fallback system prompt string (around line 516) so both AI providers behave consistently.

---

### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

#### 2a. Add UI_ACTION shared state variable

Alongside `ORB_STATE` and `FOCUS_TASK_IDS`, add:

```
UI_ACTION = None   # e.g. {"type": "task_created", "task_id": 5, "priority": "high"}
```

This gets written by the tool listener and cleared by `/state` after being served once.

#### 2b. Expand `jarvis_tool_listener` to set UI_ACTION

The existing `jarvis_tool_listener` already watches for `focus_tasks`. Extend it to also watch for:

| Tool Name | UI_ACTION type emitted |
|---|---|
| `add_task` (no parent_id) | `task_created` + new task_id + priority |
| `add_task` (with parent_id) | `subtask_created` + subtask_id + parent_id |
| `delete_task` | `task_deleted` + task_id |
| `complete_task` | `task_completed` + task_id |
| `add_note` | `note_created` |
| `delete_note` | `note_deleted` |
| `search_notes` | `note_search` |
| `google_search` | `google_search` |

#### 2c. Expose `ui_action` in the `/state` route

Modify the `/state` GET response to include `ui_action` in its JSON payload alongside `orb`, `messages`, and `focus_task_ids`. Clear it after serving (same pattern as `FOCUS_TASK_IDS`).

#### 2d. Add sleep/wake visual state

Add a `JARVIS_SLEEPING` boolean flag. When the voice loop sets `in_conversation = False`, set this to `True`. When the wake word triggers, set it back to `False`. Expose it in `/state` as `"sleeping": true/false` so the frontend can dim the nebula.

---

## Phase 2 — 3D Animation Engine (command_center.html)

This is the largest phase. We replace the static `tasksData` array with a live reactive node registry and build the animation system.

---

### [MODIFY] [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html)

#### 3a. Replace static `tasksData` with a live node registry

Remove the hardcoded `tasksData` array. Replace it with:

- `liveNodeMap` — a `Map<taskId, { group, line, pulses, position }>` tracking every active Three.js object by its real database task ID.
- On page load, fetch `/tasks` and call `buildInitialConstellation(tasks)` which places all open tasks as nodes on the map using a golden-angle sphere distribution algorithm (same visual style as current hardcoded positions).
- Poll `/tasks` every 5 seconds. Diff the result against `liveNodeMap` to detect new, removed, and status-changed tasks.

#### 3b. Build the animation queue system

Add a global `animationQueue = []` array. Each entry is `{ type, data, startTime, duration }`.

In the existing `animateGlobe()` render loop, after the existing particle/pulse updates, iterate `animationQueue` and call the matching animation tick function. Remove entries when `elapsed >= duration`.

---

#### 3c. Implement each animation

##### `animateTaskCreation(task)` — 6 seconds
1. Compute a random golden-angle position on the sphere for the new node.
2. **0–3s**: Grow a `THREE.Line` from `(0,0,0)` toward the target — each frame extend the drawn segment by lerping the end point from origin toward the target.
3. **3–4s**: Fade the line opacity from 1 to 0.
4. **4–6s**: Spawn 25 `THREE.Sprite` particles at the origin. Each frame move them along the beam path toward the target (staggered start times). At t=6s, remove particles and call `addNodeToScene(task)` — scales the node from 0 to 1.2 over 0.5s.

##### `animateSubtaskCreation(subtask, parentId)` — 4 seconds
1. Look up parent node position from `liveNodeMap`.
2. **0–2s**: Fire 6 fast energy pulse sprites from the core toward the parent node (each with a staggered 0.2s delay).
3. **2–3s**: Overload the parent node — scale it from 1.2 to 2.4, shift its color to white (`0xffffff`).
4. **3–4s**: Grow a short beam from parent to the subtask position. Scale parent back to normal. Scale up the subtask node from 0 to 0.7.

##### `animateTaskDeletion(taskId)` — ~3 seconds
1. Look up the node in `liveNodeMap`.
2. **Phase 1 (0–1s)**: Fire 8 charge beams from the core to the node.
3. **Phase 2 (1–1.5s)**: Scale node from 1.2 to 2.5. Shift color to white.
4. **Phase 3 (1.5–2s)**: Scale node down to 0 rapidly (collapse).
5. **Phase 3 (2–3s)**: Explode 50 sprite particles outward in random directions from the node position, fading from 1 to 0. Remove node and beam from scene. Remove from `liveNodeMap`.

##### `animateTaskCompletion(taskId)` — 1.5 seconds
1. Look up the node in `liveNodeMap`.
2. Each frame: move node toward camera (`z += 0.08 * elapsed`), scale it down from 1.2 to 0.
3. Simultaneously shrink the connecting line from node end back toward origin.
4. At end: remove from scene and `liveNodeMap`.

##### `animateFocusTask(taskId)` — 2 seconds
1. Camera LERP to close-up of node position.
2. Spawn 3 concentric `THREE.RingGeometry` meshes centered on the node. Each ring expands outward and fades to 0 over 1.5s (staggered by 0.4s each). Like a sonar ping.
3. Side panel slides in with task details.

##### `animateNoteCreated()` — 2 seconds
1. Spawn a ring of 30 golden (`0xF59E0B`) dim particles at the bottom of the nebula sphere.
2. Each frame: move them outward radially and fade from 0.6 to 0.

##### `animateNoteDeleted()` — 1 second
1. A single dim pulse ring expands from the nebula center and fades to 0.

##### `animateNoteSearch()` — 1.5 seconds
1. Create a thin horizontal `THREE.Line` plane that sweeps from `y = 1` to `y = -1` over 1.5s.
2. Briefly dim the nebula `smokeMat.opacity` from 0.8 to 0.3, then back to 0.8 once the sweep completes.

##### `animateGoogleSearch()` — 1.5 seconds
1. Spawn 3 concentric rings of bright white particles that pulse outward from the nebula center toward the screen edges.
2. Dim nebula opacity to 0.4, then restore to 0.8 when the reply arrives (detected when a new AI message arrives in `pollState`).

##### `animateWarpExit()` — 1 second (for "show plan")
1. Scale the nebula down from 1.25 to 0 over 0.6s (rapid compress).
2. Flash the entire screen white (CSS `body::after` overlay, opacity 0 → 1 over 0.3s).
3. After 1s, `window.location.href = 'plan.html'`.

##### `animateSleepMode()` — 2 seconds (for "go to sleep")
1. Animate `smokeMat.opacity` from 0.8 down to 0.15 over 2s.
2. Animate `coreMat.opacity` from 0.7 down to 0.05 over 2s.
3. Set orb indicator to slow dim pulse and label `SLEEPING`.
4. Restore on next wake: reverse the opacity animations over 1.5s.

##### `animateCoreCollapse()` — 2 seconds (for "exit completely")
1. For each entry in `liveNodeMap`, animate its connecting line retracting back to origin over 1s.
2. Scale the entire nebula from 1.25 to 0 over 1s (after beams retract).
3. After 2s, the window closes (handled by the backend).

---

#### 3d. Update `pollState()` to dispatch animations

In the `/state` poll handler, after processing messages and `focus_task_ids`, read `ui_action`:

```
if data.ui_action:
  dispatch animation based on data.ui_action.type
  pass data.ui_action.task_id / priority / parent_id into the animation function
```

Also handle the new `sleeping` field — if `data.sleeping === true` and nebula is not already dimmed, trigger `animateSleepMode()`. If `false` and nebula is dimmed, trigger the wake animation.

---

#### 3e. Update text/voice nav triggers

The existing nav trigger detection in `pollState()` already handles "show plan", "close panel", etc. Wire them to the new animation functions:

- "show plan" → `animateWarpExit()` then redirect
- "go to sleep" → `animateSleepMode()` (frontend only — backend handles window hide)
- "show brain" → camera zoom + nebula flare
- "show tasks" → camera zoom-out + nodes ping

---

## Phase 3 — Testing Order

Execute and verify in this order to build up incrementally:

1. **State signal system** — verify `/state` returns `ui_action` after a task is added.
2. **Task creation animation** — add a task via chat, verify the beam+particle sequence plays.
3. **Task deletion animation** — delete a task via chat, verify supernova.
4. **Task completion animation** — complete a task, verify screen-suck.
5. **Focus animation** — say "focus on task 2", verify sonar ping + panel open.
6. **Subtask animation** — add a subtask, verify energy charge + branch birth.
7. **Note animations** — add/delete/search notes, verify ink drop / dissolve / scan.
8. **Google search** — run a search, verify outbound pulse + nebula dim.
9. **Nav transitions** — "show plan", "go to sleep", "exit completely".

---

## File Change Summary

| File | Change |
|---|---|
| [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py) | Expand system prompt (Gemini + Ollama) |
| [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) | Add `UI_ACTION` state, expand tool listener, update `/state` route, add sleep flag |
| [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html) | Replace `tasksData` with live node registry, build animation engine, wire `pollState` to animations |
