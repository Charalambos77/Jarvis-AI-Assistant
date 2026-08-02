# Implementation Plan A: Quick Wins & Agent Support Layer

This plan covers everything **except** the multi-agent async engine (that is Plan B).
All items here are independently deployable — each one works and adds value even if
the next item isn't done yet. Execute top-to-bottom.

---

## Priority 1 — Memory Pattern Table [`db.py`]

**What:** Add a `memory_patterns` table to the SQLite database so the system can
store and retrieve winning patterns from past runs.

**Why now:** Every other feature depends on this existing. Build it first so the
schema is in place before anything tries to write to it.

### Changes to [`db.py`](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py)

Add to the `SCHEMA` string:

```sql
CREATE TABLE IF NOT EXISTS memory_patterns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL,       -- the insight (e.g. "hook under 5 words performs 2x better")
    task_type   TEXT,                -- e.g. "video", "code", "marketing"
    metric_name TEXT,                -- e.g. "retention_rate", "ctr", "error_rate"
    metric_value REAL,               -- e.g. 0.82 (82%)
    outcome     TEXT NOT NULL DEFAULT 'win',  -- 'win' | 'loss'
    created_at  TEXT NOT NULL
);
```

Add two new functions:

```python
def save_memory_pattern(conn, pattern, task_type=None,
                        metric_name=None, metric_value=None, outcome='win'):
    conn.execute(
        """INSERT INTO memory_patterns
           (pattern, task_type, metric_name, metric_value, outcome, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (pattern, task_type, metric_name, metric_value, outcome, _now())
    )
    conn.commit()

def search_memory_patterns(conn, query, task_type=None, outcome=None):
    like = f"%{query}%"
    sql = "SELECT * FROM memory_patterns WHERE pattern LIKE ?"
    params = [like]
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    if outcome:
        sql += " AND outcome = ?"
        params.append(outcome)
    sql += " ORDER BY COALESCE(metric_value, -1) DESC, created_at DESC LIMIT 20"
    # [FIX #2] COALESCE handles NULL metric_value — patterns without a metric
    # (qualitative insights) would otherwise sink to the bottom in DESC sort.
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
```

Add migration block to `get_connection()`:

```python
# Memory patterns table migration
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_patterns'")
if not cursor.fetchone():
    conn.execute("""CREATE TABLE memory_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT NOT NULL,
        task_type TEXT,
        metric_name TEXT,
        metric_value REAL,
        outcome TEXT NOT NULL DEFAULT 'win',
        created_at TEXT NOT NULL
    )""")
    conn.commit()
```

---

## Priority 2 — Memory Tool in Coordinator [`coordinator.py`]

**What:** Expose `search_memory_patterns` and `save_memory_pattern` as callable
tools so the Brain and agents can use them through the existing tool-calling system.

### Changes to [`coordinator.py`](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py)

Add two entries to `TOOLS` list:

```python
{
    "name": "search_memory_patterns",
    "description": (
        "Query Long-Term Memory for past winning or losing patterns relevant to a task. "
        "Use this BEFORE starting research or execution to avoid repeating past mistakes "
        "and to leverage proven strategies."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What pattern to search for"},
            "task_type": {"type": "string", "description": "Filter by task type: video, code, marketing, etc."},
            "outcome": {"type": "string", "enum": ["win", "loss"], "description": "Filter by outcome"}
        },
        "required": ["query"]
    }
},
{
    "name": "save_memory_pattern",
    "description": (
        "Save a discovered pattern or insight into Long-Term Memory after a task completes. "
        "Use this when the Track Agent identifies a high-performing or low-performing pattern."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "task_type": {"type": "string"},
            "metric_name": {"type": "string"},
            "metric_value": {"type": "number"},
            "outcome": {"type": "string", "enum": ["win", "loss"]}
        },
        "required": ["pattern"]
    }
},
```

Add to `TOOL_IMPL` dict:

```python
"search_memory_patterns": lambda conn, **kw: db.search_memory_patterns(conn, **kw),
"save_memory_pattern":    lambda conn, **kw: db.save_memory_pattern(conn, **kw),
```

---

## Priority 3 — HITL Gate API Endpoints [`jarvis.py`]

**What:** Three Flask endpoints that the pipeline UI uses to advance or reject a
gate, and to poll the current pipeline state.

**Why:** Your Approve/Reject buttons in `jarvis_plan_preview.html` currently call
`alert()`. These endpoints are what they need to call instead.

### Changes to [`jarvis.py`](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

Add this state block near the top (after `FOCUS_TASK_IDS`):

```python
# --- Pipeline Gate State ---
PIPELINE_STATE = {
    "current_gate": None,        # None | 1 | 2 | 3
    "gate_status": "idle",       # "idle" | "waiting" | "approved" | "rejected"
    "redirect_note": None,       # human's rejection reason
    "phase": "idle",             # e.g. "research" | "synthesis" | "execution" | "deployed"
}
PIPELINE_LOCK = threading.Lock()
```

Add three routes:

```python
@app.route("/gate/status", methods=["GET"])
def gate_status():
    with PIPELINE_LOCK:
        return jsonify(PIPELINE_STATE.copy())


@app.route("/gate/approve", methods=["POST"])
def gate_approve():
    with PIPELINE_LOCK:
        gate = PIPELINE_STATE.get("current_gate")
        if gate is None:
            return jsonify({"error": "No gate is currently active"}), 400
        PIPELINE_STATE["gate_status"] = "approved"
        PIPELINE_STATE["redirect_note"] = None
    push_message("system", f"Gate {gate} approved. Advancing pipeline.")
    return jsonify({"status": "approved", "gate": gate})


@app.route("/gate/reject", methods=["POST"])
def gate_reject():
    data = request.get_json(force=True) or {}
    note = data.get("redirect_note", "").strip()
    with PIPELINE_LOCK:
        gate = PIPELINE_STATE.get("current_gate")
        if gate is None:
            return jsonify({"error": "No gate is currently active"}), 400
        PIPELINE_STATE["gate_status"] = "rejected"
        PIPELINE_STATE["redirect_note"] = note or None
    push_message("system", f"Gate {gate} rejected. Note: {note or 'none provided'}")
    return jsonify({"status": "rejected", "gate": gate, "redirect_note": note})
```

---

## Priority 4 — UI Wiring [`jarvis_plan_preview.html`]

**What:** Wire the existing Approve / Reject buttons to the Flask endpoints.
Add a redirect-note textarea that appears on rejection.

### Changes to [`jarvis_plan_preview.html`](file:///d:/Charalambos/Desktop/AI/second-brain-voice/Previews/jarvis_plan_preview.html)

Replace the existing `approvePlan()` and `rejectPlan()` JS functions with:

```javascript
const FLASK_BASE = 'http://127.0.0.1:5000';

async function approvePlan() {
    const res = await fetch(`${FLASK_BASE}/gate/approve`, { method: 'POST' });
    const data = await res.json();

    const hitlCard = document.getElementById('hitl-card');
    const execCard = document.getElementById('execute-card');
    const statusText = document.getElementById('status-text');
    const execStatus = document.getElementById('exec-status');

    hitlCard.className = "stage-card green-glow";
    statusText.innerHTML = '<span class="status-dot green"></span>APPROVED';
    statusText.style.color = '#4ade80';
    execCard.className = "stage-card cyan-glow";
    execStatus.className = "status-dot green";
    document.querySelector('.jarvis-toast').innerText =
        "Plan approved. Spawning execution agents...";
}

async function rejectPlan() {
    const note = prompt("Rejection reason / redirect note (optional):") || "";
    const res = await fetch(`${FLASK_BASE}/gate/reject`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ redirect_note: note })
    });

    const hitlCard = document.getElementById('hitl-card');
    const statusText = document.getElementById('status-text');

    hitlCard.className = "stage-card";
    hitlCard.style.borderColor = 'rgba(239, 68, 68, 0.5)';
    hitlCard.style.boxShadow = '0 0 30px rgba(239, 68, 68, 0.2)';
    statusText.innerHTML = '<span class="status-dot orange" style="background:#ef4444;box-shadow:0 0 8px #ef4444;"></span>REJECTED';
    statusText.style.color = '#f87171';
    document.querySelector('.jarvis-toast').innerText =
        note ? `Rejected. Note sent to Brain: "${note}"` : "Rejected. Routing back to Brain.";
}

// Poll gate status every 3 seconds and update UI accordingly
async function pollGateStatus() {
    try {
        const res = await fetch(`${FLASK_BASE}/gate/status`);
        const data = await res.json();
        // Update stage indicators based on data.phase / data.current_gate
        const toast = document.querySelector('.jarvis-toast');
        if (data.phase && data.phase !== 'idle') {
            toast.innerText = `Pipeline phase: ${data.phase.toUpperCase()} | Gate ${data.current_gate ?? '—'}: ${data.gate_status}`;
        }
    } catch (e) { /* server not running, ignore */ }
}
setInterval(pollGateStatus, 3000);
```

---

## Priority 5 — Synthesis Agent Function

> [!IMPORTANT]
> **File location:** Create this in `agents/synthesis.py`, NOT in `coordinator.py`.
> Plan B imports it from `agents/synthesis` directly. Adding it to `coordinator.py`
> would cause Plan B's startup to trigger the Ollama thread prematurely (Bug #1 from Plan B audit).
> Create the `agents/` directory first (just an empty folder + `__init__.py`).

**What:** A new `run_synthesis_agent()` function that takes a list of research
agent result dicts, detects conflicts, and returns a compressed blueprint.

```python
def run_synthesis_agent(agent_results: list[dict]) -> dict:
    """
    Takes N research agent result dicts.
    1. Detects conflicts between any two agents on the same key.
    2. If conflicts found, returns {"status": "conflict", "conflicts": [...]}
       for the Brain to adjudicate.
    3. If no conflicts, returns {"status": "ok", "blueprint": {...}}
    """
    if not agent_results:
        return {"status": "error", "message": "No agent results to synthesise."}

    # Step 1: Merge all results into one flat dict, tracking sources
    merged = {}
    for result in agent_results:
        for key, value in result.items():
            if key not in merged:
                merged[key] = [{"agent": result.get("agent_id", "unknown"), "value": value}]
            else:
                merged[key].append({"agent": result.get("agent_id", "unknown"), "value": value})

    # Step 2: Conflict detection — multiple agents disagreeing on the same key.
    # [FIX #1] Skip meta-keys that are EXPECTED to differ per agent.
    # Without this, agent_id, confidence, and status would ALWAYS be flagged as conflicts.
    SKIP_KEYS = {"agent_id", "status", "confidence", "sources", "recommendation", "role"}
    conflicts = []
    for key, entries in merged.items():
        if key in SKIP_KEYS:
            continue
        unique_values = set(str(e["value"]) for e in entries)
        if len(unique_values) > 1:
            conflicts.append({
                "key": key,
                "disagreements": entries
            })

    if conflicts:
        return {
            "status": "conflict",
            "conflicts": conflicts,
            "message": (
                f"{len(conflicts)} conflict(s) detected. "
                "Brain must adjudicate before proceeding to Gate 1."
            )
        }

    # Step 3: No conflicts — compress into blueprint
    blueprint = {key: entries[0]["value"] for key, entries in merged.items()}
    return {
        "status": "ok",
        "blueprint": blueprint,
        "agent_count": len(agent_results),
        "key_count": len(blueprint)
    }
```

---

## Priority 6 — Quality Checker Agent Function

> [!IMPORTANT]
> **File location:** Create this in `agents/quality_checker.py`, NOT in `coordinator.py`.
> Same reason as Priority 5 — Plan B imports from `agents/` directly.

**What:** A `run_quality_checker()` function that validates each execution agent's
output against a spec from the blueprint.

```python
def run_quality_checker(execution_results: list[dict], spec: dict) -> dict:
    """
    Validates each execution agent's output against the blueprint spec.
    Returns per-agent pass/fail with reasons.

    spec example:
    {
        "min_word_count": 500,
        "required_keys": ["title", "body", "cta"],
        "task_type": "content"
    }
    """
    results = []
    all_passed = True

    for agent_result in execution_results:
        agent_id = agent_result.get("agent_id", "unknown")
        issues = []

        # Check required keys
        for key in spec.get("required_keys", []):
            if key not in agent_result:
                issues.append(f"Missing required key: '{key}'")

        # Check min word count (for content tasks)
        if spec.get("min_word_count"):
            body = str(agent_result.get("body", ""))
            wc = len(body.split())
            if wc < spec["min_word_count"]:
                issues.append(
                    f"Word count {wc} is below minimum {spec['min_word_count']}"
                )

        # Check for error signals from the agent itself
        if agent_result.get("status") == "error":
            issues.append(f"Agent self-reported error: {agent_result.get('error', 'unknown')}")

        passed = len(issues) == 0
        if not passed:
            all_passed = False

        results.append({
            "agent_id": agent_id,
            "passed": passed,
            "issues": issues
        })

    return {
        "all_passed": all_passed,
        "results": results,
        "failed_agents": [r["agent_id"] for r in results if not r["passed"]]
    }
```

---

## Priority 7 — Basic Track Agent Thread [`jarvis.py`]

**What:** A background thread that polls a simple in-memory metric store
and triggers the corrective loop if a metric drops below its threshold.
(Real API integrations — YouTube, GA — are wired in separately per platform.)

Add near the top of `jarvis.py` (alongside `PIPELINE_STATE`):

```python
# --- Track Agent Metric Store ---
TRACKED_METRICS = {}   # e.g. {"youtube_ctr": {"value": 0.025, "threshold": 0.03}}
TRACKED_METRICS_LOCK = threading.Lock()
```

Add this route to allow the system (or manual testing) to push a metric:

```python
@app.route("/metrics/update", methods=["POST"])
def update_metric():
    data = request.get_json(force=True) or {}
    name = data.get("name")
    value = data.get("value")
    threshold = data.get("threshold")
    if not name or value is None:
        return jsonify({"error": "name and value required"}), 400
    with TRACKED_METRICS_LOCK:
        TRACKED_METRICS[name] = {"value": float(value), "threshold": float(threshold or 0)}
    return jsonify({"status": "updated", "metric": name, "value": value})
```

Add the Track Agent loop function:

```python
def track_agent_loop():
    """Background thread. Checks metrics every 5 minutes.
    If any metric is below its threshold, signals the Brain to spawn
    a corrective sub-task."""
    print("[Track Agent] Started monitoring.")
    while True:
        time.sleep(300)  # 5 minutes
        with TRACKED_METRICS_LOCK:
            metrics_snapshot = dict(TRACKED_METRICS)

        for metric_name, data in metrics_snapshot.items():
            value = data.get("value", 0)
            threshold = data.get("threshold", 0)
            if threshold > 0 and value < threshold:
                msg = (
                    f"[Track Agent] ALERT: '{metric_name}' is {value:.3f}, "
                    f"below threshold {threshold:.3f}. Signalling Brain."
                )
                print(msg)
                push_message("system", msg)
                # Signal Brain via the coordinator
                corrective_prompt = (
                    f"Track Agent alert: metric '{metric_name}' is underperforming "
                    f"(current: {value:.3f}, threshold: {threshold:.3f}). "
                    f"Spawn a corrective sub-task to address this."
                )
                try:
                    reply = coordinator.handle_request(corrective_prompt)
                    push_message("ai", reply)
                except Exception as e:
                    print(f"[Track Agent] Error signalling Brain: {e}")
```

Start it in `__main__`:

```python
track_thread = threading.Thread(target=track_agent_loop, daemon=True)
track_thread.start()
```

---

## Verification Checklist

After each priority, verify before moving to the next:

- [ ] **P1** — Run Jarvis, open `second_brain.db` in DB Browser, confirm `memory_patterns` table exists
- [ ] **P2** — Ask Jarvis "search memory for video hooks" — should return empty list (not crash)
- [ ] **P3** — `curl http://127.0.0.1:5000/gate/status` returns `{"current_gate": null, ...}`
- [ ] **P4** — Click Approve in the pipeline UI, confirm the green animation triggers AND the Flask server logs the request
- [ ] **P5** — Call `run_synthesis_agent([{"agent_id":"a1","hook":"short"},{"agent_id":"a2","hook":"long"}])` — should return a conflict
- [ ] **P6** — Call `run_quality_checker([{"agent_id":"a1"}], {"required_keys":["title"]})` — should return failed with missing key
- [ ] **P7** — `POST /metrics/update` with `{"name":"ctr","value":0.01,"threshold":0.03}`, wait 5 min (or lower sleep for testing), confirm Brain receives the corrective prompt

---

## What This Plan Does NOT Cover

The **multi-agent async engine** — parallel spawning of N real AI agents
with `asyncio`, independent system prompts, tool grants, and result collection.
That is **Plan B**.
