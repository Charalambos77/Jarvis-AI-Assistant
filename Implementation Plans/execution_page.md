# Implementation Plan: Standalone Execution Page & Real-Time Logging Engine

This document details the complete design and code specifications for the **Execution Page (`execution.html`)** and its supporting backend infrastructure.

---

## Complete Feature Matrix

### 1. Backend Real-Time Logging Engine (`jarvis.py`)
- **Console Log Storage**: In-memory `CONSOLE_LOGS` buffer with lock (500 max entries) storing timestamped level, source, and text.
- **Inter-Agent Chat Storage**: In-memory `AGENT_CHAT_LOGS` buffer with lock (300 max entries) storing sender, receiver, and message.
- **REST Log Endpoints**:
  - `GET /api/console_logs`: returns active task execution logs, CLI outputs, and pipeline gate statuses.
  - `GET /api/agent_chat`: returns live inter-agent dialogue messages.
- **Live Event Broadcaster**: `append_console_log()` and `append_agent_chat()` push real-time events to the UI stream.

### 2. Standalone Execution Page (`execution.html`)
- **Independent Page Structure**: Dedicated HTML page served at `execution.html` (matching `plan.html`).
- **Cyberpunk Stardust Aesthetic**: Full Three.js stardust nebula canvas background consistent with the core Jarvis theme.
- **Real-Time Task Console Panel**: Displays CLI logs, build output, script execution, and gate statuses.
- **Inter-Agent Dialogue Feed Panel**: Displays live messaging between sub-agents (e.g. ResearchAgent $\rightarrow$ SynthesisAgent $\rightarrow$ DeploymentAgent) and Jarvis.
- **Task & Blueprint Status**: Active step metrics and execution pipeline status.
- **Voice / Chat Terminal Widget**: Embedded bottom bar listening to user commands and speech.

### 3. Voice-Only Navigation System
- **No On-Screen Navigation Buttons**: Header top bar is completely headless. No buttons on `agent_map_final.html`, `plan.html`, or `execution.html` for switching to/from execution page.
- **Voice Command Entry**: Saying *"Jarvis, open execution page"*, *"go to execution mode"*, or starting a pipeline task triggers navigation to `execution.html`.
- **Voice Command Exit**: Saying *"Jarvis, go back"*, *"open brain"*, or *"open plan"* triggers navigation back to `agent_map_final.html` or `plan.html`.

---

## Detailed Code Specs

### Component 1: Flask Log Endpoints & Log Collectors

```python
# In jarvis.py
CONSOLE_LOGS = []
CONSOLE_LOGS_LOCK = threading.Lock()

AGENT_CHAT_LOGS = []
AGENT_CHAT_LOGS_LOCK = threading.Lock()

def append_console_log(level: str, text: str, source: str = "System"):
    entry = {"timestamp": time.time(), "level": level, "text": text, "source": source}
    with CONSOLE_LOGS_LOCK:
        CONSOLE_LOGS.append(entry)
        if len(CONSOLE_LOGS) > 500:
            CONSOLE_LOGS.pop(0)

def append_agent_chat(sender: str, receiver: str, message: str):
    entry = {"timestamp": time.time(), "sender": sender, "receiver": receiver, "message": message}
    with AGENT_CHAT_LOGS_LOCK:
        AGENT_CHAT_LOGS.append(entry)
        if len(AGENT_CHAT_LOGS) > 300:
            AGENT_CHAT_LOGS.pop(0)

@app.route("/api/console_logs", methods=["GET"])
def get_console_logs():
    with CONSOLE_LOGS_LOCK:
        return jsonify({"logs": list(CONSOLE_LOGS)})

@app.route("/api/agent_chat", methods=["GET"])
def get_agent_chat_logs():
    with AGENT_CHAT_LOGS_LOCK:
        return jsonify({"chat": list(AGENT_CHAT_LOGS)})
```

### Component 2: Standalone `execution.html`
- Full Three.js stardust canvas background.
- Dual-panel interface: Task Console Stream + Inter-Agent Dialogue Stream.
- Auto-updating log containers polling `/api/console_logs` and `/api/agent_chat`.
- Embedded voice terminal at bottom.
- Headless layout (no header navigation buttons).

### Component 3: Voice Navigation Routing in `jarvis.py`
- Parse intent in `handle_request()`:
  - Navigation commands trigger `UI_ACTION = {"type": "navigate", "url": "execution.html"}` or `"agent_map_final.html"`.

---

## Verification Plan

1. **Backend Verification**: Call `/api/console_logs` and `/api/agent_chat` and verify log JSON output.
2. **Voice Navigation Entry**: Speak *"Jarvis, go to execution page"* $\rightarrow$ Browser navigates to `execution.html`.
3. **Stream Verification**: Verify console logs and inter-agent messages stream live into the `execution.html` panels.
4. **Voice Navigation Exit**: Speak *"Jarvis, go back"* or *"Jarvis, open brain"* $\rightarrow$ Browser navigates back to `agent_map_final.html`.
