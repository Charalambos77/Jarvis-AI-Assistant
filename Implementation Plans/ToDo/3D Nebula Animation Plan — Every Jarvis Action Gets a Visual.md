# 3D Nebula Animation Plan — Every Jarvis Action Gets a Visual

Detailed animation design for each Jarvis tool action that interacts with the 3D Nebula UI. Every action Jarvis performs should have a visual representation in the Three.js scene so the user can **see** what's happening.

---

## Existing Animation Inventory

Before adding new ones, here's what already exists in [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html):

| Function | Line | What It Does | Duration |
|---|---|---|---|
| `triggerCreatedAnimation` | 1543 | Laser beam from center → position, then particle trail | 6.0s |
| `triggerSubtaskCreatedAnimation` | 1620 | Charge pulses → parent flash → branch beam grows | 4.0s |
| `triggerNoteCreatedAnimationOnTask` | 1721 | Charge pulses → parent flash → branch beam (amber) | 4.0s |
| `triggerNoteDeletedAnimation` | 1823 | Node hidden → 20 amber particles explode outward | 1.5s |
| `triggerNoteCompletedAnimation` | 1882 | Shrink toward center, scale to 0 | 1.5s |
| `triggerDeletedAnimation` | 1916 | 8 charge beams → supernova flare → collapse → 50-particle explosion | 3.0s |
| `triggerCompletedAnimation` | 2023 | Move toward camera, scale to 0, line shrinks | 1.5s |
| `animateNoteCreated` | 2056 | 30 amber particles in a ring, expand outward | 2.0s |
| `animateNoteDeleted` | 2097 | Expanding amber ring | 1.0s |
| `animateNoteSearch` | 2121 | Purple sweep line scans top-to-bottom, nebula dims | 1.5s |
| `animateGoogleSearch` | 2148 | 3 white expanding rings, nebula dims | 1.5s |
| `animateWarpExit` | 2188 | Nebula/core scale to 0, white overlay → page nav | 1.0s |
| `animateSleepMode` | 2224 | Fade nebula/core opacity down (sleep) or up (wake) | 2.0s |
| `triggerSpeakingBomb` | 2251 | Shockwave bomb displaces nearby particles | ~1.0s |
| `animateCoreCollapse` | 2284 | All node lines collapse to center, nebula shrinks | 2.0s |
| `animateFocusTask` | 2308 | Camera moves to task, 3 sonar rings pulse from node | 2.0s |

### Animation Architecture Pattern

Every animation follows this pattern (lines 1516-1528):
```javascript
animationQueue.push({
    type: 'ANIMATION_NAME',          // unique string identifier
    duration: 2.0,                   // seconds
    startTime: Date.now(),           // when it started
    tick: (elapsed) => { ... },      // called every frame, elapsed = seconds since start
    onComplete: () => { ... }        // cleanup when duration expires
});
```

Key shared resources:
- `texture` — The shared glow sprite texture (white circle with radial gradient)
- `nodesGroup` — The `THREE.Group` that holds all task/note node geometry
- `scene` — The root Three.js scene
- `liveNodeMap` — `Map<id, {group, hitMesh, line, pulses, pulseObj, position, data}>` registry of all visible nodes
- `smokeMat` / `coreMat` — The nebula and core cloud materials (for opacity changes)
- `nebula` / `core` — The nebula and core cloud meshes (for scale changes)

---

## New Animations — Detailed Design

---

### 1. `animateTaskUpdated(taskId, changedFields)` — Update Task

**What the user sees:** The task node **pulses and morphs** — a shockwave ripple emanates from the node, its color transitions if priority changed, and a brief data-stream particle effect wraps around it.

**Visual breakdown:**
- **Phase A (0–0.5s) — Shockwave Pulse:** The node group rapidly scales up to 1.8× then snaps back to 1.0× with an elastic ease. A single expanding ring (like sonar) pulses outward from the node position, colored by the **new** priority color.
- **Phase B (0.5–1.5s) — Color Morph:** If the priority changed, the center sprite and glow sprite smoothly `lerpColors` from the old priority color to the new one. 12 tiny particles orbit the node in a tight helix (radius 0.06), colored with the new priority, then fade out.
- **Phase C (1.5–2.0s) — Settle:** The node group gently bounces to final scale 1.0. Orbiting particles fade to 0 opacity. The node's line color also transitions to match the new color.

**Duration:** 2.0 seconds

**Code location:** New function after `triggerCompletedAnimation` (line 2054). Called from the `pollState` dispatcher.

**Dispatcher addition** (line ~2919 area):
```javascript
} else if (act.type === 'task_updated') {
    animateTaskUpdated(act.task_id, act.changed_fields || {});
    fetchAndRenderLiveTasks();
}
```

**`onComplete` cleanup:** Remove the sonar ring mesh and orbiting particle sprites from `nodesGroup`. Update the `liveNodeMap` registry entry's `data.color` if priority changed. Rebuild the node's display data to reflect new values.

---

### 2. `animateBatchSubtasksCreated(parentId, count)` — Add Subtasks (Batch)

**What the user sees:** The parent node **overcharges** — it absorbs energy, flashes white-hot, then **fractures** outward, spawning multiple subtask nodes simultaneously in a starburst pattern.

**Visual breakdown:**
- **Phase A (0–2.0s) — Energy Absorption:** `count × 4` charge particles stream from the nebula center toward the parent node (same as existing `triggerSubtaskCreatedAnimation` Phase A, but with more particles converging simultaneously). Parent node's glow sprite scales up gradually to 1.5×.
- **Phase B (2.0–3.0s) — Overcharge Flash:** Parent node flashes white (center sprite color → `0xffffff`), scale pulses to 2.0× with a `Math.sin` oscillation. The parent's glow opacity increases to 1.0.
- **Phase C (3.0–5.0s) — Staggered Fracture:** For each subtask `i` (0 to `count-1`), with a stagger delay of `i * 0.3s`:
  - A branch beam grows from parent position to the calculated subtask position (using `getSubtaskOffset(i, count)`).
  - The existing `triggerSubtaskCreatedAnimation` is called for each with a time offset, but **only Phase C** (the branch beam growth), since the parent overcharge already happened.

**Duration:** 5.0 + (count × 0.3) seconds

**Code location:** New function after the existing `triggerSubtaskCreatedAnimation` (line 1719).

**Dispatcher addition:**
```javascript
} else if (act.type === 'subtasks_batch_created') {
    animateBatchSubtasksCreated(act.parent_id, act.count);
    fetchAndRenderLiveTasks();
    showFloatingNotification(`${act.count} subtasks spawned from Task #${act.parent_id}`);
}
```

**`onComplete` cleanup:** Remove all charge particles. Reset parent node scale and color. Call `addNodeToScene` for each subtask.

---

### 3. `animateBatchTasksCreated(taskIds, count)` — Create Multiple Tasks

**What the user sees:** Multiple laser beams fire **simultaneously** from the nebula center to different spherical positions — like a starburst of creation rays. Each beam follows the exact same pattern as `triggerCreatedAnimation` but with **staggered start times** (0.4s apart) so they fan out in a cascading wave.

**Visual breakdown:**
- For each task `i` (0 to `count-1`):
  - **Delay:** `i * 0.4s` before this task's animation starts
  - **Phase 1 (0–3s):** Laser line grows from center `(0,0,0)` to `getSphericalPosition(liveNodeMap.size + i, liveNodeMap.size + count)`
  - **Phase 2 (3–4s):** Laser fades
  - **Phase 3 (4–6s):** 25 particles trail along the beam path, fade at destination
  - **onComplete:** `addNodeToScene(task, position)` for each

This is literally `triggerCreatedAnimation` called `count` times with staggered `startTime` values of `Date.now() + (i * 400)`.

**Duration:** 6.0 + (count × 0.4) seconds total for the entire batch

**Code location:** No new animation function needed — the dispatcher calls `triggerCreatedAnimation` in a loop with `setTimeout` stagger.

**Dispatcher addition:**
```javascript
} else if (act.type === 'tasks_batch_created') {
    const ids = act.task_ids || [];
    ids.forEach((id, i) => {
        setTimeout(() => {
            const tempTask = { id: id, content: 'New Task', priority: act.priorities?.[i] || 'medium', status: 'open' };
            const targetPos = getSphericalPosition(liveNodeMap.size, liveNodeMap.size + 1);
            triggerCreatedAnimation(tempTask, targetPos);
        }, i * 400);
    });
    showFloatingNotification(`${ids.length} tasks created`);
}
```

---

### 4. `animateBatchTasksDeleted(taskIds)` — Delete Multiple Tasks

**What the user sees:** Multiple nodes get destroyed **simultaneously** with staggered supernova explosions — like a chain reaction of collapsing stars.

**Visual breakdown:**
- Same as existing `triggerDeletedAnimation` called for each task ID, staggered by `i * 0.5s`.
- Each node goes through the full charge-beams → supernova flare → collapse → 50-particle explosion sequence.
- The stagger creates a dramatic "domino destruction" visual across the constellation.

**Duration:** 3.0 + (count × 0.5) seconds total

**Code location:** No new animation function needed — the dispatcher calls `triggerDeletedAnimation` in a loop with `setTimeout` stagger.

**Dispatcher addition:**
```javascript
} else if (act.type === 'tasks_batch_deleted') {
    const ids = act.task_ids || [];
    ids.forEach((id, i) => {
        setTimeout(() => {
            triggerDeletedAnimation(id);
        }, i * 500);
    });
    showFloatingNotification(`${ids.length} tasks deleted`);
}
```

---

### 5. Add Note — `animateNoteCreated()` / `triggerNoteCreatedAnimationOnTask()`

**Status:** ✅ Already exists (lines 2056 and 1721). No changes needed.

- If the note is linked to a task: `triggerNoteCreatedAnimationOnTask` plays (charge pulses → parent flash → amber branch beam grows → amber node appears).
- If standalone: `animateNoteCreated` plays (30 amber particles ring around the nebula base).

---

### 6. Search Notes — `animateNoteSearch()`

**Status:** ✅ Already exists (line 2121). No changes needed.

- Purple sweep line scans top-to-bottom. Nebula dims during scan. Restores on complete.

---

### 7. Delete Note — `animateNoteDeleted()` / `triggerNoteDeletedAnimation()`

**Status:** ✅ Already exists (lines 2097 and 1823). No changes needed.

- If the note has a 3D node: `triggerNoteDeletedAnimation` plays (node hidden → 20 amber particles explode outward).
- If generic: `animateNoteDeleted` plays (expanding amber ring).

---

### 8. Complete Note — `triggerNoteCompletedAnimation()`

**Status:** ✅ Already exists (line 1882). No changes needed.

- Note node flies toward the center, shrinks to 0, line retracts. Removed from scene on complete.

---

### 9. `animateBatchNotesCreated(count, taskIds)` — Create Multiple Notes

**What the user sees:** Same as individual note creation but **staggered** — multiple amber energy beams fire from their respective parent task nodes (if linked) or amber particle rings appear sequentially.

**Visual breakdown:**
- For each note `i` (0 to `count-1`):
  - **Delay:** `i * 0.5s`
  - If linked to a task: `triggerNoteCreatedAnimationOnTask` fires from the parent node
  - If standalone: `animateNoteCreated` fires

**Duration:** 4.0 + (count × 0.5) seconds total

**Code location:** Dispatcher calls existing animation functions with stagger.

**Dispatcher addition:**
```javascript
} else if (act.type === 'notes_batch_created') {
    const noteInfos = act.notes || [];
    noteInfos.forEach((info, i) => {
        setTimeout(() => {
            if (info.task_id && liveNodeMap.has(info.task_id)) {
                const tempNote = { id: info.note_id, content: 'New Note', status: 'open' };
                const parentReg = liveNodeMap.get(info.task_id);
                const offset = getNoteOffset(i, noteInfos.length);
                const targetPos = parentReg.position.clone().add(offset);
                triggerNoteCreatedAnimationOnTask(tempNote, info.task_id, targetPos);
            } else {
                animateNoteCreated();
            }
        }, i * 500);
    });
    showFloatingNotification(`${noteInfos.length} notes created`);
}
```

---

### 10. `animateBatchNotesDeleted(noteIds)` — Delete Multiple Notes

**What the user sees:** Multiple amber explosion bursts staggered in rapid succession — each note node pops like a firecracker chain.

**Visual breakdown:**
- Same as individual `triggerNoteDeletedAnimation` called for each note, staggered by `i * 0.3s` (faster stagger than tasks since note explosions are smaller/shorter — 1.5s each).

**Duration:** 1.5 + (count × 0.3) seconds total

**Dispatcher addition:**
```javascript
} else if (act.type === 'notes_batch_deleted') {
    const ids = act.note_ids || [];
    ids.forEach((id, i) => {
        setTimeout(() => {
            const noteKey = 'note-' + id;
            if (liveNodeMap.has(noteKey)) {
                triggerNoteDeletedAnimation(noteKey);
            } else {
                animateNoteDeleted();
            }
        }, i * 300);
    });
    showFloatingNotification(`${ids.length} notes deleted`);
}
```

---

### 11. `animatePipelineStarted()` — Start Multi-Agent Pipeline

**What the user sees:** The nebula **inhales** — all particles contract inward briefly, then release outward in a powerful shockwave. A bright cyan ring expands from the core like a stellar ignition. The nebula briefly surges brighter, as if the brain is powering up a massive operation.

**Visual breakdown:**
- **Phase A (0–1.0s) — Inhale Contraction:** The nebula mesh scales down to 0.85× and the core scales down to 0.7×. Particles appear to be sucked inward. `smokeMat.opacity` increases to 1.0 (denser).
- **Phase B (1.0–1.5s) — Ignition Flash:** The core flashes bright white (`coreMat.opacity` → 1.0, then a white glow sprite appears at center, scale `0 → 0.4`). A bright cyan ring (`THREE.RingGeometry`) appears at center.
- **Phase C (1.5–3.0s) — Shockwave Release:** The nebula and core snap back to 1.25× then settle to 1.0× (overshoot bounce). The cyan ring expands rapidly outward (scale `1 → 15`), fading in opacity. 6 radial beam lines shoot outward from center to the edges (representing the pipeline's parallel agents being dispatched), each fading over 1s.
- **Phase D (3.0–4.0s) — Settle:** Everything returns to normal. The `smokeMat.opacity` gently returns to `0.8`.

**Duration:** 4.0 seconds

**Code location:** New function after `animateCoreCollapse` (line 2306).

**Dispatcher addition:**
```javascript
} else if (act.type === 'pipeline_started') {
    animatePipelineStarted();
    showFloatingNotification('Multi-agent pipeline ignited');
}
```

**`onComplete` cleanup:** Remove the cyan ring mesh and all 6 radial beam lines from `scene`. Reset nebula/core scale and material opacities.

---

### 12. `animateGateStatusCheck()` — Check Gate Status

**What the user sees:** A brief **scanning pulse** radiates from the nebula core — like a radar ping checking on the pipeline's progress. More subtle than the pipeline start.

**Visual breakdown:**
- **Phase A (0–0.5s):** A single thin cyan ring (`THREE.RingGeometry(0.05, 0.06, 32)`) appears at center, starts expanding outward.
- **Phase B (0.5–1.2s):** The ring continues expanding (scale `1 → 10`), opacity fades from 0.6 to 0. Simultaneously, the core sprite briefly brightens (`coreMat.opacity` pulses from 0.7 → 1.0 → 0.7 in a smooth sine wave).
- **Phase C (0–1.2s):** A subtle orange glow sprite at center (pipeline status color — orange for "waiting") pulses once.

**Duration:** 1.2 seconds

**Code location:** New function after `animatePipelineStarted`.

**Dispatcher addition:**
```javascript
} else if (act.type === 'gate_status_checked') {
    animateGateStatusCheck();
}
```

**`onComplete` cleanup:** Remove the ring mesh and orange glow sprite.

---

### 13. `animateMetricUpdated(metricName)` — Update a Tracked Metric

**What the user sees:** A small **data pulse** — a green particle shoots upward from the nebula core along the Y-axis (like a metric value being "pushed up"), leaving a brief trail.

**Visual breakdown:**
- **Phase A (0–0.8s):** A single green sprite (`#22C55E`) starts at `(0, 0, 0)` and moves upward to `(0, 0.8, 0)`. 8 tiny trailing particles follow in a line, each delayed by `0.05s`, all fading as they travel.
- **Phase B (0.8–1.2s):** At `(0, 0.8, 0)`, the lead particle bursts into a small flash (scale `0.04 → 0.15 → 0`). The trailing particles converge on the same point and dissolve.

**Duration:** 1.2 seconds

**Code location:** New function after `animateGateStatusCheck`.

**Dispatcher addition:**
```javascript
} else if (act.type === 'metric_updated') {
    animateMetricUpdated(act.metric_name);
    showFloatingNotification(`Metric "${act.metric_name}" updated`);
}
```

**`onComplete` cleanup:** Remove the lead sprite and all 8 trailing sprites from `nodesGroup`.

---

### 14. `animateMetricsRead()` — Read Tracked Metrics

**What the user sees:** Multiple green data pulses fire **downward** from above the nebula toward the core — like metrics being "pulled down" for reading. The reverse direction of `animateMetricUpdated`.

**Visual breakdown:**
- **Phase A (0–1.0s):** 5 green sprites start at different positions above the nebula `(x, 0.9, z)` in a loose ring, and travel inward/downward toward `(0, 0, 0)`. Each one leaves a 4-particle trail.
- **Phase B (1.0–1.5s):** As each sprite reaches the center, the core pulses slightly brighter (a gentle `coreMat.opacity` bump of `+0.15`).

**Duration:** 1.5 seconds

**Code location:** New function after `animateMetricUpdated`.

**Dispatcher addition:**
```javascript
} else if (act.type === 'metrics_read') {
    animateMetricsRead();
}
```

**`onComplete` cleanup:** Remove all green sprites and trails from `nodesGroup`. Reset `coreMat.opacity` to `0.7`.

---

### 15. "Break Task Down" Button — Uses `animateBatchSubtasksCreated`

**What happens:** Clicking the button sends a chat message to Jarvis. Jarvis calls `add_subtasks`, which triggers the `subtasks_batch_created` UI action, which calls `animateBatchSubtasksCreated` (animation #2 above).

**Code change in HTML** ([command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html) line 730):
```html
<!-- Replace alert() with: -->
onclick="sendToJarvis('Break task #' + currentDetailTaskId + ' down into subtasks')"
```

**Additional code:** Track `currentDetailTaskId` in the `openDetailPanel` function when `data.taskObj` is present:
```javascript
let currentDetailTaskId = null;
// Inside openDetailPanel, when data.taskObj is set:
currentDetailTaskId = t.id;
```

**Animation flow:** Button click → `sendToJarvis` → Jarvis calls `add_subtasks` → backend sets `UI_ACTION = {type: 'subtasks_batch_created', ...}` → `pollState` picks it up → `animateBatchSubtasksCreated(parentId, count)` fires.

---

### 16. "Create Implementation Plan" Button — Uses `animateNoteCreated` (task-linked variant)

**What happens:** Clicking the button sends a chat message to Jarvis. Jarvis generates an implementation plan, saves it as a note linked to the task via `add_note(content=plan, task_id=X)`. This triggers the existing `note_created` UI action with a `task_id`, which fires `triggerNoteCreatedAnimationOnTask`.

**Code change in HTML** (line 733):
```html
<!-- Replace alert() with: -->
onclick="sendToJarvis('Create an implementation plan for task #' + currentDetailTaskId + ' and save it as a note')"
```

**Animation flow:** Button click → `sendToJarvis` → Jarvis creates plan text → calls `add_note` with `task_id` → backend sets `UI_ACTION = {type: 'note_created', task_id: X, note_id: Y}` → `pollState` picks it up → `triggerNoteCreatedAnimationOnTask` fires (amber charge pulses → parent flash → branch beam → note node appears).

---

### 17. "Let Jarvis Handle It" Button — Uses `animatePipelineStarted`

**What happens:** Clicking the button sends a chat message to Jarvis. Jarvis calls `start_pipeline(task=...)`, which triggers the `pipeline_started` UI action, which fires `animatePipelineStarted` (animation #11 above).

**Code change in HTML** (line 736):
```html
<!-- Replace alert() with: -->
onclick="sendToJarvis('Start a pipeline to handle task #' + currentDetailTaskId + ' autonomously')"
```

**Animation flow:** Button click → `sendToJarvis` → Jarvis calls `start_pipeline` → backend sets `UI_ACTION = {type: 'pipeline_started'}` → `pollState` picks it up → `animatePipelineStarted()` fires (inhale → ignition flash → shockwave → 6 radial beams).

---

## File Change Summary

| File | What Changes |
|---|---|
| [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html) | Add 5 new animation functions (`animateTaskUpdated`, `animateBatchSubtasksCreated`, `animatePipelineStarted`, `animateGateStatusCheck`, `animateMetricUpdated`, `animateMetricsRead`). Update `pollState` dispatcher with 9 new `act.type` handlers. Rewire 3 task detail buttons. Add `currentDetailTaskId` tracking variable. |
| [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py) | Update `jarvis_tool_listener` to emit new `UI_ACTION` types: `task_updated`, `subtasks_batch_created`, `tasks_batch_created`, `tasks_batch_deleted`, `notes_batch_created`, `notes_batch_deleted`, `pipeline_started`, `gate_status_checked`, `metric_updated`, `metrics_read`. Include `changed_fields`, `task_ids`, `note_ids`, `metric_name` in payloads. |
| [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) | Update the `UI_ACTION` assignment in the tool listener to include the new action type payloads from `coordinator.py`. |

---

## New vs. Reused Animation Summary

| Action | Animation Strategy | New Code? |
|---|---|---|
| Update task | **New** `animateTaskUpdated` — pulse + color morph + orbit particles | ✅ New function |
| Add subtasks (batch) | **New** `animateBatchSubtasksCreated` — overcharge parent + staggered fracture | ✅ New function |
| Batch create tasks | **Reuse** `triggerCreatedAnimation` × N with `setTimeout` stagger | ❌ Dispatcher only |
| Batch delete tasks | **Reuse** `triggerDeletedAnimation` × N with `setTimeout` stagger | ❌ Dispatcher only |
| Add note | **Reuse** existing `animateNoteCreated` / `triggerNoteCreatedAnimationOnTask` | ❌ Already exists |
| Search notes | **Reuse** existing `animateNoteSearch` | ❌ Already exists |
| Delete note | **Reuse** existing `animateNoteDeleted` / `triggerNoteDeletedAnimation` | ❌ Already exists |
| Complete note | **Reuse** existing `triggerNoteCompletedAnimation` | ❌ Already exists |
| Batch create notes | **Reuse** note creation × N with `setTimeout` stagger | ❌ Dispatcher only |
| Batch delete notes | **Reuse** note deletion × N with `setTimeout` stagger | ❌ Dispatcher only |
| Start pipeline | **New** `animatePipelineStarted` — inhale + ignition + shockwave + radial beams | ✅ New function |
| Check gate status | **New** `animateGateStatusCheck` — radar ping + core pulse | ✅ New function |
| Update metric | **New** `animateMetricUpdated` — green pulse shoots upward with trail | ✅ New function |
| Read metrics | **New** `animateMetricsRead` — green pulses descend into core | ✅ New function |
| "Break task down" | Rewire button → triggers `add_subtasks` → reuses batch subtask animation | ❌ Button rewire |
| "Create impl. plan" | Rewire button → triggers `add_note` → reuses note creation animation | ❌ Button rewire |
| "Let Jarvis handle it" | Rewire button → triggers `start_pipeline` → reuses pipeline animation | ❌ Button rewire |

**Total: 6 new animation functions, 9 new dispatcher cases, 3 button rewires.**
