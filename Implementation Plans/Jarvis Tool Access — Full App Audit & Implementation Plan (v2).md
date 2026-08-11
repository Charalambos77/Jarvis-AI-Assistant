# Jarvis Tool Access — Full App Audit & Implementation Plan (v2)

A complete inventory of every function the UI exposes, mapped against what Jarvis can actually invoke through his tools. Then: an implementation plan to close every gap.

---

## Design Decisions (Resolved)

| Decision | Resolution |
|---|---|
| `update_task` re-parenting | **No.** `parent_id` is not changeable via `update_task`. Re-parenting only happens if explicitly requested by the user — handled separately. |
| Pipeline gate autonomy | **Human-only.** Jarvis can start pipelines and check gate status, but gate approval/rejection is exclusively for human review. Jarvis gets no `approve_gate` / `reject_gate` tools. |
| Settings change safety | **Confirmation required.** Jarvis must ask "Are you sure?" before applying settings changes. Implemented via a two-step flow in the system prompt rules. |
| Metrics tools | **Give Jarvis full access.** Both `update_metric` and `read_metrics` tools so Jarvis can programmatically track and report KPIs. |
| 3D node issues | **Out of scope.** Known issues with 3D note nodes will be addressed in a separate implementation plan. |

---

## Complete App Function Inventory

### 1. Navigation (Page-Level)

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Go to Brain Core | `control_interface → go_to_brain` | ✅ Has |
| Go to Plan Page | `control_interface → go_to_plan` | ✅ Has |
| Go to APIs/MCPs Page | `control_interface → go_to_apis` | ✅ Has |

### 2. Side Panel Operations

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Open Tasks panel | `control_interface → open_side_panel` | ✅ Has |
| Open Notes panel | `control_interface → open_notes_panel` | ✅ Has |
| Open Settings panel | `control_interface → open_settings_panel` | ✅ Has |
| Open Task Detail | `control_interface → open_task_detail` + `payload.task_id` | ✅ Has |
| Close side panel | `control_interface → close_side_panel` | ✅ Has |

### 3. Task Database (CRUD)

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Add task | `add_task` | ✅ Has |
| Get tasks (filtered) | `get_tasks` | ✅ Has |
| Complete task | `complete_task` | ✅ Has |
| Delete task | `delete_task` | ✅ Has |
| **Update task** (content, priority, due date, effort, status) | **No tool** | ❌ Missing |
| **Add subtasks** (batch decomposition) | **No coordinator tool** (only in MCP server) | ❌ Missing |
| **Create multiple tasks at once** | **No tool** | ❌ Missing |
| **Delete multiple tasks at once** | **No tool** | ❌ Missing |
| Focus/highlight task in 3D | `focus_tasks` | ✅ Has |

### 4. Note Database (CRUD)

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Add note | `add_note` | ✅ Has |
| Search notes | `search_notes` | ✅ Has |
| Delete note | `delete_note` | ✅ Has |
| Update note | `update_note` | ✅ Has |
| Complete note | `complete_note` | ✅ Has |
| **Create multiple notes at once** | **No tool** | ❌ Missing |
| **Delete multiple notes at once** | **No tool** | ❌ Missing |

### 5. 3D / Visual Controls

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Reset 3D camera | `control_interface → reset_camera` | ✅ Has |
| Sleep / Wake / Exit / Flash notification | `control_interface → go_to_sleep / wake_up / exit_completely / flash_notification` | ✅ Has |

### 6. Settings

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Open settings panel | `control_interface → open_settings_panel` | ✅ Has |
| **Read current settings** | **No tool** | ❌ Missing |
| **Change settings** (with confirmation) | **No tool** | ❌ Missing |

### 7. Pipeline / Multi-Agent Coordinator

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| **Start multi-agent pipeline** | **No tool** | ❌ Missing |
| **Check gate status** | **No tool** | ❌ Missing |
| Approve gate | Human-only (by design) | 🚫 Intentionally excluded |
| Reject gate | Human-only (by design) | 🚫 Intentionally excluded |

### 8. Metrics / Track Agent

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| **Update a tracked metric** | **No tool or coordinator access** | ❌ Missing |
| **Read tracked metrics** | **No endpoint or tool** | ❌ Missing |

### 9. Task Detail Panel Buttons

| UI Button | Jarvis Tool Access | Status |
|---|---|---|
| **"Break task down"** | Shows `alert()` — not wired | ❌ Missing |
| **"Create implementation plan"** | Shows `alert()` — not wired | ❌ Missing |
| **"Let Jarvis handle it"** | Shows `alert()` — not wired | ❌ Missing |

### 10. Memory, Connectors, App State

| UI Function | Jarvis Tool Access | Status |
|---|---|---|
| Search / save memory patterns | `search_memory_patterns` / `save_memory_pattern` | ✅ Has |
| External API / MCP / Google Search | `call_external_api` / `call_mcp` / `google_search` | ✅ Has |
| Read UI snapshot | `read_app_snapshot` | ✅ Has |

---

## Summary: 13 Missing Capabilities

| # | Missing Capability | Category |
|---|---|---|
| 1 | `update_task` — edit priority, content, due date, effort, status | Task CRUD |
| 2 | `add_subtasks` — batch decompose a task into children | Task CRUD |
| 3 | `batch_create_tasks` — create multiple tasks in one call | Task CRUD (Batch) |
| 4 | `batch_delete_tasks` — delete multiple tasks in one call | Task CRUD (Batch) |
| 5 | `batch_create_notes` — create multiple notes in one call | Note CRUD (Batch) |
| 6 | `batch_delete_notes` — delete multiple notes in one call | Note CRUD (Batch) |
| 7 | `read_settings` — read current provider, voice speed, etc. | Settings |
| 8 | `change_settings` — modify settings (with confirmation) | Settings |
| 9 | `start_pipeline` — launch the multi-agent pipeline | Pipeline |
| 10 | `get_gate_status` — check current pipeline gate state | Pipeline |
| 11 | `update_metric` — set/update a tracked KPI metric | Metrics |
| 12 | `read_metrics` — read all tracked metrics | Metrics |
| 13 | Wire 3 dead Task Detail buttons to Jarvis chat | Frontend |

---

## Proposed Changes

### Component 1 — Database Layer

#### [MODIFY] [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py)

Add 5 new functions:

**`update_task`** — Modify any field of an existing task (except `parent_id`):
```python
def update_task(
    conn, task_id, content=None, priority=None,
    effort_estimate=None, scheduled_at=None, due_date=None, status=None
) -> bool:
    # Builds a dynamic UPDATE query for only the provided fields
```

**`batch_create_tasks`** — Create multiple tasks in one transaction:
```python
def batch_create_tasks(conn, tasks: list[dict]) -> list[int]:
    # Each dict can have: content, priority, effort_estimate, scheduled_at, due_date, parent_id
    # Returns list of created task IDs
```

**`batch_delete_tasks`** — Delete multiple tasks by ID:
```python
def batch_delete_tasks(conn, task_ids: list[int]) -> int:
    # Cleans up dependencies, deletes all in one transaction
    # Returns count of deleted tasks
```

**`batch_create_notes`** — Create multiple notes in one transaction:
```python
def batch_create_notes(conn, notes: list[dict]) -> list[int]:
    # Each dict can have: content, tags, task_id
    # Returns list of created note IDs
```

**`batch_delete_notes`** — Delete multiple notes by ID:
```python
def batch_delete_notes(conn, note_ids: list[int]) -> int:
    # Returns count of deleted notes
```

> [!NOTE]
> `add_subtasks` already exists in [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py#L162-L166) — it just needs a coordinator tool.

---

### Component 2 — AI Coordinator

#### [MODIFY] [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py)

**Add 12 new tool definitions to `TOOLS` list:**

```python
# --- Task Editing ---
{
    "name": "update_task",
    "description": "Update an existing task. Change its content, priority, effort estimate, scheduled time, due date, or status. Cannot change parent_id.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "content": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            "effort_estimate": {"type": "string", "enum": ["small", "medium", "large"]},
            "scheduled_at": {"type": "string"},
            "due_date": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "in_progress", "done"]}
        },
        "required": ["task_id"]
    }
},

# --- Batch Subtask Decomposition ---
{
    "name": "add_subtasks",
    "description": "Break a task into multiple subtasks at once. Each step becomes a child task.",
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "integer"},
            "steps": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["parent_id", "steps"]
    }
},

# --- Batch Create Tasks ---
{
    "name": "batch_create_tasks",
    "description": "Create multiple tasks in one call. Each item needs at least 'content'.",
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                        "effort_estimate": {"type": "string", "enum": ["small", "medium", "large"]},
                        "scheduled_at": {"type": "string"},
                        "due_date": {"type": "string"},
                        "parent_id": {"type": "integer"}
                    },
                    "required": ["content"]
                }
            }
        },
        "required": ["tasks"]
    }
},

# --- Batch Delete Tasks ---
{
    "name": "batch_delete_tasks",
    "description": "Delete multiple tasks at once by their IDs.",
    "parameters": {
        "type": "object",
        "properties": {
            "task_ids": {"type": "array", "items": {"type": "integer"}}
        },
        "required": ["task_ids"]
    }
},

# --- Batch Create Notes ---
{
    "name": "batch_create_notes",
    "description": "Create multiple notes in one call. Each item needs at least 'content'.",
    "parameters": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "tags": {"type": "string"},
                        "task_id": {"type": "integer"}
                    },
                    "required": ["content"]
                }
            }
        },
        "required": ["notes"]
    }
},

# --- Batch Delete Notes ---
{
    "name": "batch_delete_notes",
    "description": "Delete multiple notes at once by their IDs.",
    "parameters": {
        "type": "object",
        "properties": {
            "note_ids": {"type": "array", "items": {"type": "integer"}}
        },
        "required": ["note_ids"]
    }
},

# --- Settings ---
{
    "name": "read_settings",
    "description": "Read the current app settings: AI provider, voice speed, wake word threshold, and theme.",
    "parameters": {"type": "object", "properties": {}}
},
{
    "name": "change_settings",
    "description": "Change app settings. IMPORTANT: Always confirm with the user before applying. Say what you're about to change and ask 'Shall I proceed?'",
    "parameters": {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["gemini", "ollama"]},
            "voice_speed": {"type": "integer", "description": "WPM (100-300)"},
            "wake_word_threshold": {"type": "number", "description": "0.1-0.9"},
            "theme": {"type": "string", "enum": ["cyberpunk", "dark", "light"]}
        }
    }
},

# --- Pipeline ---
{
    "name": "start_pipeline",
    "description": "Launch the multi-agent pipeline for a complex task. The pipeline runs through research, synthesis, human gate review, execution, and deploy phases.",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Task description for the pipeline"}
        },
        "required": ["task"]
    }
},
{
    "name": "get_gate_status",
    "description": "Check the current pipeline gate status: which gate is active, waiting/approved/rejected, and the pipeline phase.",
    "parameters": {"type": "object", "properties": {}}
},

# --- Metrics ---
{
    "name": "update_metric",
    "description": "Set or update a tracked KPI metric with a name, current value, and threshold.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Metric name (e.g. youtube_ctr)"},
            "value": {"type": "number", "description": "Current value"},
            "threshold": {"type": "number", "description": "Alert threshold"}
        },
        "required": ["name", "value"]
    }
},
{
    "name": "read_metrics",
    "description": "Read all currently tracked metrics and their values/thresholds.",
    "parameters": {"type": "object", "properties": {}}
}
```

**Add corresponding implementations to `TOOL_IMPL`:**

```python
"update_task":       lambda conn, **kw: db.update_task(conn, **kw),
"add_subtasks":      lambda conn, **kw: db.add_subtasks(conn, **kw),
"batch_create_tasks": lambda conn, **kw: db.batch_create_tasks(conn, **kw),
"batch_delete_tasks": lambda conn, **kw: db.batch_delete_tasks(conn, **kw),
"batch_create_notes": lambda conn, **kw: db.batch_create_notes(conn, **kw),
"batch_delete_notes": lambda conn, **kw: db.batch_delete_notes(conn, **kw),
"read_settings":     lambda conn, **kw: <fetch from /settings>,
"change_settings":   lambda conn, **kw: <post to /settings>,
"start_pipeline":    lambda conn, **kw: <post to /pipeline/start>,
"get_gate_status":   lambda conn, **kw: <fetch from /gate/status>,
"update_metric":     lambda conn, **kw: <post to /metrics/update>,
"read_metrics":      lambda conn, **kw: <fetch from /metrics/get>,
```

**Update `SYSTEM_PROMPT` rules — add safety confirmation rule:**
```
"8. Before changing any settings (change_settings), ALWAYS tell the user what you're about to change "
"and ask 'Shall I proceed, Sir?' — only apply after explicit confirmation.\n"
"9. You CANNOT approve or reject pipeline gates. Those are human-only review steps.\n"
```

**Update `UI_MAP`** with new voice examples:
```
"  'update task 3 priority to high' → update_task\n"
"  'change voice speed to 200' → change_settings (with confirmation)\n"
"  'break task 5 down into steps' → add_subtasks\n"
"  'add 3 tasks: buy milk, clean house, call mom' → batch_create_tasks\n"
"  'delete tasks 4, 5, and 6' → batch_delete_tasks\n"
"  'save these 3 notes...' → batch_create_notes\n"
"  'delete notes 1, 2, and 3' → batch_delete_notes\n"
"  'start a pipeline for X' → start_pipeline\n"
"  'check pipeline status' → get_gate_status\n"
"  'what are my current settings?' → read_settings\n"
"  'track youtube CTR at 0.03' → update_metric\n"
"  'show all metrics' → read_metrics\n"
```

**Update `jarvis_tool_listener`** in [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) to emit UI actions for new tools:
```python
elif name == "update_task":
    UI_ACTION = {"type": "task_updated", "task_id": args.get("task_id")}
elif name == "add_subtasks":
    UI_ACTION = {"type": "subtasks_batch_created", "parent_id": args.get("parent_id"),
                 "count": len(result) if isinstance(result, list) else 0}
elif name == "batch_create_tasks":
    UI_ACTION = {"type": "tasks_batch_created",
                 "count": len(result) if isinstance(result, list) else 0}
elif name == "batch_delete_tasks":
    UI_ACTION = {"type": "tasks_batch_deleted",
                 "count": result if isinstance(result, int) else 0}
elif name == "batch_create_notes":
    UI_ACTION = {"type": "notes_batch_created",
                 "count": len(result) if isinstance(result, list) else 0}
elif name == "batch_delete_notes":
    UI_ACTION = {"type": "notes_batch_deleted",
                 "count": result if isinstance(result, int) else 0}
```

---

### Component 3 — Backend Server

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

Add a `/metrics/get` endpoint so the `read_metrics` tool can fetch current tracked metrics:

```python
@app.route("/metrics/get", methods=["GET"])
def get_metrics():
    with TRACKED_METRICS_LOCK:
        return jsonify({"metrics": dict(TRACKED_METRICS)})
```

---

### Component 4 — Frontend

#### [MODIFY] [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html)

**1. Wire the 3 Task Detail buttons** (lines 728-736):

Replace the `alert()` calls with actual Jarvis chat commands. The `openDetailPanel` function already has the task data — we need to track the currently viewed task ID and use it in the buttons.

```javascript
// Track current task ID when opening detail panel
let currentDetailTaskId = null;

// In openDetailPanel, when data.taskObj is present:
currentDetailTaskId = t.id;

// Button rewiring:
// "Break task down":
onclick="sendToJarvis('Break task #' + currentDetailTaskId + ' down into subtasks')"

// "Create implementation plan":
onclick="sendToJarvis('Create an implementation plan for task #' + currentDetailTaskId + ' and save it as a note attached to the task')"

// "Let Jarvis handle it":
onclick="sendToJarvis('Start a pipeline to handle task #' + currentDetailTaskId + ' autonomously')"
```

**2. Handle new UI actions in `pollState`** (around line 2919):

```javascript
} else if (act.type === 'task_updated') {
    fetchAndRenderLiveTasks();
} else if (act.type === 'subtasks_batch_created') {
    fetchAndRenderLiveTasks();
    showFloatingNotification(`${act.count} subtasks created for Task #${act.parent_id}`);
} else if (act.type === 'tasks_batch_created') {
    fetchAndRenderLiveTasks();
    showFloatingNotification(`${act.count} tasks created`);
} else if (act.type === 'tasks_batch_deleted') {
    fetchAndRenderLiveTasks();
    showFloatingNotification(`${act.count} tasks deleted`);
} else if (act.type === 'notes_batch_created') {
    showFloatingNotification(`${act.count} notes created`);
} else if (act.type === 'notes_batch_deleted') {
    showFloatingNotification(`${act.count} notes deleted`);
}
```

---

## File Change Summary

| File | Action | What Changes |
|---|---|---|
| [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py) | MODIFY | Add `update_task`, `batch_create_tasks`, `batch_delete_tasks`, `batch_create_notes`, `batch_delete_notes` |
| [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py) | MODIFY | Add 12 new tools to `TOOLS` + `TOOL_IMPL`, update `UI_MAP` and `SYSTEM_PROMPT` |
| [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) | MODIFY | Add `/metrics/get` endpoint, update `jarvis_tool_listener` for 6 new UI actions |
| [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html) | MODIFY | Wire 3 dead buttons, handle 6 new UI action types in `pollState` |

---

## Verification Plan

### Automated Tests
```bash
# Single item operations
curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "update task 1 priority to high"}'

curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "break task 1 down into: research, design, build, test"}'

# Batch operations
curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "add 3 tasks: buy groceries, clean the house, call the bank"}'

curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "delete tasks 4, 5, and 6"}'

curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "save these notes: remember to check server logs, follow up with client on Monday"}'

curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "delete notes 1, 2, and 3"}'

# Settings (safety confirmation)
curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "what are my current settings?"}'

curl -X POST http://localhost:5000/ask -H "Content-Type: application/json" \
  -d '{"text": "change voice speed to 200"}'

# Pipeline & Metrics
curl http://localhost:5000/gate/status
curl http://localhost:5000/metrics/get
```

### Manual Verification
1. **Batch create**: Say "add 3 tasks: X, Y, Z" → All 3 appear in task panel and as 3D nodes with creation animations
2. **Batch delete**: Say "delete tasks 4, 5, and 6" → All 3 nodes explode, notification toast appears
3. **Update task**: Say "update task 3 priority to high" → Task badge changes color, 3D node color updates on next poll
4. **Settings safety**: Say "switch to Gemini" → Jarvis asks "Shall I switch from Ollama to Gemini, Sir?" → Only applies after "yes"
5. **Break task down button**: Click it in task detail → chat sends decomposition request, subtask nodes spawn
6. **Let Jarvis handle it button**: Click it → chat sends pipeline start request
7. **Metrics**: Say "track youtube CTR at 2.5% with threshold 3%" → metric stored, Say "show all metrics" → Jarvis reads them back
