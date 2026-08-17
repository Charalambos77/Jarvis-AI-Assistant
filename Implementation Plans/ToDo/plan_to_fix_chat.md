# Fix Chat, Thoughts & Add Narrative Panels

## Problem

The current `agent_talk.task_log.html` page has two panels: **Task Console Stream** (left) and **Inter-Agent Dialogue Stream** (right, with CHAT/THOUGHTS tabs). Both show essentially the same lifecycle event data (`[SPAWNED]`, `[RUNNING]`, `[COMPLETED]`) — just formatted differently. Neither shows actual agent conversation content or reasoning.

### What's broken specifically:

1. **CHAT tab** — Shows the same lifecycle events as the console (`append_agent_chat()` in [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py#L185-L190) just reformats the same `event_logger` data). It does NOT show the actual prompts sent to agents or the responses they return.

2. **THOUGHTS tab** — Shows `streamed_thoughts` (partial text chunks from research agents streaming) or falls back to raw JSON findings. It does NOT show what the Brain is "considering" before calling Gemini (the system prompt + user prompt being constructed).

3. **No Narrative** — There is no human-readable progress story panel.

## Proposed Changes

The fix requires backend changes (to capture and expose actual prompts/responses and thinking data) plus frontend changes (to render 3 distinct panels).

---

### Backend: New Event Types & API Endpoints

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

**New data stores** (alongside existing `CONSOLE_LOGS`, `AGENT_CHAT_LOGS`):

```python
# --- Actual Agent Conversation Logs (prompts sent + responses received) ---
AGENT_CONVERSATION_LOGS = []
AGENT_CONVERSATION_LOGS_LOCK = threading.Lock()

# --- Brain/Agent Thinking Logs (system prompts + input construction) ---
AGENT_THINKING_LOGS = []
AGENT_THINKING_LOGS_LOCK = threading.Lock()

# --- Narrative Progress Log (human-readable pipeline story) ---
NARRATIVE_LOGS = []
NARRATIVE_LOGS_LOCK = threading.Lock()
```

**New append helpers:**

```python
def append_agent_conversation(agent_id: str, direction: str, role: str, content: str):
    """direction: 'prompt_sent' or 'response_received'"""
    entry = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "role": role,
        "direction": direction,
        "content": content
    }
    with AGENT_CONVERSATION_LOGS_LOCK:
        AGENT_CONVERSATION_LOGS.append(entry)
        if len(AGENT_CONVERSATION_LOGS) > 500:
            AGENT_CONVERSATION_LOGS.pop(0)

def append_agent_thinking(agent_id: str, role: str, thinking_type: str, content: str):
    """thinking_type: 'system_prompt' | 'user_prompt' | 'config_construction' | 'decision'"""
    entry = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "role": role,
        "thinking_type": thinking_type,
        "content": content
    }
    with AGENT_THINKING_LOGS_LOCK:
        AGENT_THINKING_LOGS.append(entry)
        if len(AGENT_THINKING_LOGS) > 500:
            AGENT_THINKING_LOGS.pop(0)

def append_narrative(phase: str, message: str, icon: str = "➡️"):
    entry = {
        "timestamp": time.time(),
        "phase": phase,
        "message": message,
        "icon": icon
    }
    with NARRATIVE_LOGS_LOCK:
        NARRATIVE_LOGS.append(entry)
        if len(NARRATIVE_LOGS) > 200:
            NARRATIVE_LOGS.pop(0)
```

**New API endpoints:**

```python
@app.route("/api/agent_conversations", methods=["GET"])
def get_agent_conversations():
    with AGENT_CONVERSATION_LOGS_LOCK:
        return jsonify({"conversations": list(AGENT_CONVERSATION_LOGS)})

@app.route("/api/agent_thinking", methods=["GET"])
def get_agent_thinking():
    with AGENT_THINKING_LOGS_LOCK:
        return jsonify({"thinking": list(AGENT_THINKING_LOGS)})

@app.route("/api/narrative", methods=["GET"])
def get_narrative():
    with NARRATIVE_LOGS_LOCK:
        return jsonify({"narrative": list(NARRATIVE_LOGS)})
```

**Update `pipeline_event_logger()`** to route new event types into the correct stores:

- `event_type: "prompt_sent"` → `append_agent_conversation()`
- `event_type: "response_received"` → `append_agent_conversation()`
- `event_type: "thinking"` → `append_agent_thinking()`
- `event_type: "narrative"` → `append_narrative()`

The existing lifecycle events (`spawned`, `running`, `completed`, etc.) continue going to `CONSOLE_LOGS` as before.

---

### Backend: Emit New Event Types from Agent Modules

#### [MODIFY] [brain.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/brain.py)

Add an optional `event_logger` parameter to `build_agent_plan()` so we can emit:

1. **Thinking event** — The full `BRAIN_SYSTEM_PROMPT` and the constructed `user_input` string (what the Brain is "considering") before calling Gemini
2. **Prompt sent event** — The actual prompt sent to the Gemini API
3. **Response received event** — The raw JSON response from Gemini
4. **Narrative event** — "Brain is analyzing the task and deciding which specialists to hire..."

```python
def build_agent_plan(
    task: str,
    redirect_note: str | None = None,
    cycle_id: int | None = None,
    approved_blueprints: list[dict] | None = None,
    rejected_steps: list[str] | None = None,
    event_logger=None,  # NEW
) -> dict:
    # ... existing user_input construction ...

    # NEW: Emit thinking event (what Brain is considering)
    if event_logger:
        event_logger({
            "event_type": "thinking",
            "agent_id": "Brain",
            "data": {
                "thinking_type": "system_prompt",
                "role": "Brain Orchestrator",
                "content": BRAIN_SYSTEM_PROMPT
            }
        })
        event_logger({
            "event_type": "thinking",
            "agent_id": "Brain",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Brain Orchestrator",
                "content": user_input
            }
        })
        event_logger({
            "event_type": "narrative",
            "data": {
                "phase": "planning",
                "message": "Brain is analyzing the task and deciding which specialists to hire...",
                "icon": "🧠"
            }
        })

    # ... existing Gemini call ...

    # NEW: Emit prompt_sent / response_received
    if event_logger:
        event_logger({
            "event_type": "prompt_sent",
            "agent_id": "Brain",
            "data": {
                "role": "Brain Orchestrator",
                "content": user_input
            }
        })
    
    response = client.models.generate_content(...)
    
    if event_logger:
        event_logger({
            "event_type": "response_received",
            "agent_id": "Brain",
            "data": {
                "role": "Brain Orchestrator",
                "content": response.text
            }
        })
```

---

#### [MODIFY] [research_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/research_agent.py)

Add `event_logger` parameter to `run_research_agent()`. Emit:

1. **Thinking** — The constructed `system_prompt` (what the agent was told to do) + the user content string
2. **Prompt sent** — The brief/content sent to Gemini
3. **Response received** — The full parsed response text
4. **Narrative** — "Research Agent [role] is now investigating [brief]..."

```python
async def run_research_agent(
    agent_config: dict,
    memory_context: str | None = None,
    prior_context: str | None = None,
    on_chunk_callback=None,
    event_logger=None,  # NEW
) -> dict:
    # ... existing system_prompt construction ...

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "agent_id": agent_id,
            "data": {
                "thinking_type": "system_prompt",
                "role": role,
                "content": system_prompt
            }
        })
        event_logger({
            "event_type": "narrative",
            "data": {
                "phase": "research",
                "message": f"{role} ({agent_id}) is now investigating: {brief[:100]}...",
                "icon": "🔍"
            }
        })

    # ... after response ...
    if event_logger:
        event_logger({
            "event_type": "prompt_sent",
            "agent_id": agent_id,
            "data": {"role": role, "content": f"Execute your research brief now. Task context: {brief}"}
        })
        event_logger({
            "event_type": "response_received",
            "agent_id": agent_id,
            "data": {"role": role, "content": response_text}
        })
```

---

#### [MODIFY] [execution_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/execution_agent.py)

Same pattern — add `event_logger`, emit thinking/prompt/response/narrative events.

---

#### [MODIFY] [synthesis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py)

Same pattern for both `run_synthesis_agent()` and `run_master_synthesis()`.

---

#### [MODIFY] [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py)

**Thread the `event_logger` through** to all agent calls that now accept it:

- `build_agent_plan(task, ..., event_logger=event_logger)` (line ~545)
- `run_research_agent(agent_config, ..., event_logger=event_logger)` (inside `run_research_phase_for_cycle`)
- `run_execution_agent(cfg, ..., event_logger=event_logger)` (inside `run_execution_phase`)
- `run_synthesis_agent(authoritative_output, event_logger=event_logger)`
- `run_master_synthesis(approved_blueprints, event_logger=event_logger)`
- `run_lead_review(lead_config, ..., event_logger=event_logger)`

**Add narrative events** at key pipeline milestones:

```python
# At cycle start:
event_logger({"event_type": "narrative", "data": {"phase": "research", "message": f"Starting Cycle {cycle_id}: {domain}...", "icon": "🔄"}})

# At synthesis:
event_logger({"event_type": "narrative", "data": {"phase": "synthesis", "message": f"Synthesizing Cycle {cycle_id} research into blueprint...", "icon": "🔬"}})

# At gate:
event_logger({"event_type": "narrative", "data": {"phase": "gate", "message": f"Waiting for your approval of Cycle {cycle_id} research...", "icon": "🚧"}})

# At execution:
event_logger({"event_type": "narrative", "data": {"phase": "execution", "message": "Execution agents producing deliverables...", "icon": "⚡"}})

# At QA:
event_logger({"event_type": "narrative", "data": {"phase": "qa", "message": "Quality checker validating agent outputs...", "icon": "✅"}})
```

---

### Frontend: Redesign the 3-Panel Layout

#### [MODIFY] [agent_talk.task_log.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agent_talk.task_log.html)

Replace the current 2-panel layout with a **3-tab layout** on the right panel:

**Current tabs:** `CHAT` | `THOUGHTS`

**New tabs:** `CHAT` | `THOUGHTS` | `NARRATIVE`

Each tab polls a different API endpoint:

| Tab | API Endpoint | Content |
|-----|-------------|---------|
| **CHAT** | `/api/agent_conversations` | Full prompts sent to each agent and full responses back. Shows `direction: prompt_sent` as outgoing bubbles and `direction: response_received` as incoming bubbles. Agent role and ID shown in header. |
| **THOUGHTS** | `/api/agent_thinking` | System prompts being constructed, user prompts being assembled, config decisions. Styled like a "Thinking..." panel with grey background, monospace font, and a subtle pulsing animation. |
| **NARRATIVE** | `/api/narrative` | A clean, human-readable timeline. Each entry shows icon + timestamp + message. Progressive story: "🧠 Brain analyzing task → 🔍 SEO Specialist researching → 🔬 Synthesizing findings → 🚧 Waiting for approval → ⚡ Executing..." |

**Visual design changes for each tab:**

1. **CHAT tab** — Redesign bubbles to clearly show direction:
   - **→ Prompt sent** (right-aligned, cyan border): `Brain → Research Agent: "Execute your research brief..."`
   - **← Response received** (left-aligned, purple border): `Research Agent → Brain: "{ findings: {...} }"`
   - Long JSON responses are collapsed by default with "View Full Response" toggle
   - Agent role shown as a colored badge

2. **THOUGHTS tab** — Styled like the screenshot you showed me:
   - Grey/dark background card with the label "Thinking..."
   - Shows the system prompt being constructed in monospace font
   - Shows user_input being assembled step by step
   - Collapsible sections: "System Prompt", "User Input", "Config"
   - Blinking cursor `█` when agent is actively thinking

3. **NARRATIVE tab** — Clean timeline:
   - Vertical timeline with phase-colored dots
   - Each entry: `[icon] [time] [message]`
   - Phase-based color coding (research=blue, synthesis=purple, gate=yellow, execution=green, qa=cyan)
   - Auto-scrolls to latest entry

**The left panel (Task Console Stream)** stays as-is — it continues showing lifecycle events from `/api/console_logs`.

---

## Resolved Design Decisions

> [!NOTE]
> **Response truncation in CHAT tab**: ✅ **Option A chosen** — Show collapsed by default with expandable "View Full Response" toggle.
> Already described in the plan at **line 315**: *"Long JSON responses are collapsed by default with 'View Full Response' toggle"*

> [!NOTE]
> **Thinking panel scope**: ✅ **Option A chosen** — Show the Brain's static `BRAIN_SYSTEM_PROMPT` once at the top as a collapsible section; only show dynamic `user_input` construction for subsequent calls.
> Already partially described at **lines 320-322**: *"Shows the system prompt being constructed in monospace font... Collapsible sections: 'System Prompt', 'User Input', 'Config'"*
> The frontend must deduplicate: if a `thinking_type: "system_prompt"` entry has the same `content` as a previously rendered one, skip it and only render new `user_prompt` entries.

---

## Verification Plan

### Manual Verification
1. Start Jarvis, navigate to the Task Logs/Chat page
2. Start a pipeline via voice or text input
3. Verify **CHAT tab** shows actual Brain→Agent prompts and Agent→Brain responses (not lifecycle events)
4. Verify **THOUGHTS tab** shows the system prompt and user input being constructed (like the "Thinking..." panel in the screenshot)
5. Verify **NARRATIVE tab** shows a clean progressive story of what's happening
6. Verify the **Task Console Stream** (left panel) still shows lifecycle events as before
7. Verify that the existing `execution.html` chat box and bottom-right conversation still work unchanged

### Files Changed Summary

| File | Change Type | Purpose |
|------|------------|---------|
| [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py) | MODIFY | Add 3 new data stores, 3 new API endpoints, update `pipeline_event_logger()` |
| [brain.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/brain.py) | MODIFY | Add `event_logger` param, emit thinking/prompt/response/narrative events |
| [research_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/research_agent.py) | MODIFY | Add `event_logger` param, emit thinking/prompt/response/narrative events |
| [execution_agent.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/execution_agent.py) | MODIFY | Add `event_logger` param, emit thinking/prompt/response/narrative events |
| [synthesis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agents/synthesis.py) | MODIFY | Add `event_logger` param, emit thinking/prompt/response/narrative events |
| [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py) | MODIFY | Thread `event_logger` to all agent calls, add narrative events at milestones |
| [agent_talk.task_log.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agent_talk.task_log.html) | MODIFY | Add NARRATIVE tab, redesign CHAT to show conversations, redesign THOUGHTS to show reasoning |
