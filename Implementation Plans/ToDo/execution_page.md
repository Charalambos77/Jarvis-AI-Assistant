# Implementation Plan: Execution Mode Switch & Inter-Agent Console/Chat Interface

This implementation plan details how Jarvis transitions into **Execution Mode** upon voice trigger (`"Jarvis switch to execution mode"`), using the layout and aesthetics from [agent_map_demo.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/Previews/agent_map_demo.html) without deleting or overwriting the preview file.

## Key Changes & Requirements

1. **Voice Triggers & Flash Transitions**:
   - Voice trigger `"Jarvis switch to execution mode"` or `"switch to execution mode"` triggers a full-screen cyan/white flash overlay and switches the UI view into Execution Mode.
   - Voice trigger `"go back to the brain"` or `"switch to brain mode"` triggers the same flash overlay and returns the UI to the main Brain view.
2. **Dynamic SPA (Single-Page Application) Rendering**:
   - Transitions take place without reloading `plan.html` or refreshing the browser page, preventing Three.js/WebGL re-initialization flashes.
3. **Navigation & Tab State**:
   - The top navigation bar remains active.
   - In Execution Mode, the primary button label dynamically morphs from `BRAIN` to `PROJECT`.
   - The `PLAN` and `APIS/MCPS` tabs remain active and functional.
   - Two new navigation buttons appear: `CONSOLE` (task output logs) and `CHAT` (inter-agent dialogue).
4. **Dynamic Running Agents Constellation**:
   - The 6 placeholder categories (`marketing`, `operations`, `intelligence`, `customer`, `back office`, `sales`, `deals`) are replaced by active sub-agents currently executing in the project.
5. **Real-Time Backend IPC & Streaming**:
   - `voice_assistant.py` and `coordinator.py` broadcast mode updates via Flask Server-Sent Events (`/api/stream_events`).
   - `/api/console_logs` and `/api/agent_chat` endpoints provide real-time streams for task execution outputs and inter-agent dialogues.

---

## User Review Required

> [!IMPORTANT]
> The preview file [agent_map_demo.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/Previews/agent_map_demo.html) will be preserved in `Previews/` untouched as requested. All new rendering logic will be incorporated directly into [agent_map_final.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agent_map_final.html) and [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py).

---

## Proposed Changes

### Core Backend & Voice Handler

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)
- Add `/api/stream_events` Server-Sent Events (SSE) route for real-time IPC between Python processes and frontend.
- Add `/api/console_logs` and `/api/agent_chat` queue endpoints.
- Update `/api/voice_command` handler to detect `"switch to execution mode"` and `"go back to the brain"`, emitting `{ "action": "SWITCH_MODE", "mode": "EXECUTION" }` or `{ "action": "SWITCH_MODE", "mode": "BRAIN" }`.

#### [MODIFY] [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py)
- Add natural language recognition for execution mode navigation commands.
- Broadcast inter-agent chat logs and active running agents list to `jarvis.py` state buffer.

---

### User Interface & Visual Mode System

#### [MODIFY] [agent_map_final.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/agent_map_final.html)
- Add CSS Flash Overlay element (`#flash-overlay`) with keyframe transition for smooth mode shifts.
- Update top navigation header:
  - Add dynamic tab switching between `BRAIN` and `PROJECT`.
  - Add `CONSOLE` and `CHAT` nav tab buttons.
- Integrate active running agent Cytoscape trigonometric layout engine from `agent_map_demo.html`.
- Add `#console-modal` panel for CLI/task execution logs.
- Add `#agent-chat-modal` panel for Jarvis <-> Agent and Agent <-> Agent communication log.
- Connect EventSource listener to `/api/stream_events` for zero-latency voice triggering.

---

## Verification Plan

### Automated & Integration Tests
1. **Server SSE Endpoint Verification**:
   - Test `/api/stream_events` using `curl` or Python script to verify event broadcasting.
2. **Voice Intent Recognition**:
   - Send simulated voice transcript `"Jarvis switch to execution mode"` through `/api/voice_command` and assert event payload.

### Manual Verification
1. Speak `"Jarvis switch to execution mode"`: Confirm full-screen white/cyan flash transition activates and view morphs to project execution layout without full page reload.
2. Check top navigation tabs: Confirm `PROJECT`, `PLAN`, `APIS/MCPS`, `CONSOLE`, and `CHAT` buttons are visible and functional.
3. Open `CONSOLE` and `CHAT` panels to confirm real-time streaming of task outputs and agent messages.
4. Speak `"go back to the brain"`: Confirm flash transition activates and interface cleanly returns to the central Brain mode.
