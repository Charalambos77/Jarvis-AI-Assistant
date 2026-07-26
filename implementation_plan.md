# Jarvis Multiagent Upgrade Implementation Plan

## Goal
Create the cheapest possible multiagent-style version of Jarvis while keeping everything local and avoiding new paid infrastructure. The result should feel modular, easy to extend, and still run on a single PC with the current local browser UI.

---

## Overview
This plan splits Jarvis into logical agent roles without requiring remote servers, cloud services, or message brokers. The cheapest option is to use the current Python codebase and local process boundaries.

### Agent roles
- **Speech agent**: listens for wake word, records audio, transcribes speech
- **Coordinator agent**: receives user text, chooses the right specialist, manages conversation context
- **Tool agents**: handles tasks and notes via local database operations
- **UI agent**: serves the browser command center and shows current state
- **Voice/TTS agent**: speaks responses back to the user

---

## Phase 1 — Design the cheap multiagent architecture

1. Review existing components
   - `voice_assistant.py`: speech capture and TTS
   - `jarvis.py`: coordinator and browser server
   - `db.py`: data access and tools
2. Define clear responsibilities for each module
   - keep `db.py` as the local tool layer
   - keep the browser as the UI layer
   - use a small orchestrator interface in `jarvis.py`
   - keep audio/TTS logic in `voice_assistant.py`
3. Decide on communication style
   - cheapest: direct Python function calls inside one process
   - optionally: local Flask endpoints for more explicit agent boundaries
   - do not add message queues, remote databases, or paid APIs yet

---

## Phase 2 — Implement the minimal local multiagent version

1. Refactor into modules
   - `speech_agent.py` or keep the speech logic in `voice_assistant.py`
   - `coordinator.py` inside or alongside `jarvis.py`
   - `task_agent.py` and `notes_agent.py` as wrappers around `db.py`
   - `ui_agent.py` or keep the current Flask UI code in `jarvis.py`
   - `tts_agent.py` or keep current `speak()` logic in `voice_assistant.py`
2. Create a coordinator interface
   - accept plain text requests
   - decide which tool agent should handle each request
   - manage simple session and context state
3. Keep the database local
   - continue using `second_brain.db`
   - use `db.py` methods for all CRUD operations
   - no new storage services needed
4. Keep the UI local and secure
   - keep the website bound to `127.0.0.1`
   - optionally allow later LAN access behind a secure boundary

---

## Phase 3 — Build incrementally and test

1. Start with the coordinator
   - route user requests to the correct tool function
   - return the response text for UI and TTS
2. Integrate speech input
   - speech agent sends transcribed text to the coordinator
   - coordinator returns a response for speaking and UI update
3. Validate tool behavior
   - ensure task additions, searches, updates work correctly
   - keep the current `add_task`, `get_tasks`, `search_notes`, etc.
4. Keep the browser UI alive
   - show the conversation and task/note state
   - use it as the visual “agent monitor”

---

## Phase 4 — Keep costs at zero

- No cloud servers
- No new paid services required
- No additional hardware required
- No external message brokers
- Use the existing Python `venv` and installed packages

> Optional: ElevenLabs or another TTS service can be added later if you want better voice quality, but keep the cheapest version using `pyttsx3`.

---

## Optional future extension

After the local multiagent design works, you can later add these without major rewrites:

- separate processes for UI and coordinator
- local LAN access to the browser interface
- a remote mobile/web client
- specialist agents for calendar, reminders, email, or search
- a secure local API gateway

## API / MCP readiness

This architecture can be made API- and MCP-ready without changing the basic local design:

- add an **API connector agent** that knows how to call third-party services when needed
- add an **MCP connector agent** for Claude Desktop / MCP integration later
- keep the core coordinator and tool agents unchanged, and route external operations through connectors
- use local Flask endpoints or internal function hooks as the integration points
- keep the base version cheap and local, then enable external connectors when you want them

---

## Recommended next steps

1. Map current code to the five agent roles above
2. Refactor one layer at a time, starting with the coordinator
3. Keep communication simple and local
4. Test with existing voice + UI flow first
5. Only separate processes or networking after the local version is stable
