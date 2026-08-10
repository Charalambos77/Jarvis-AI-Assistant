# Jarvis as a First-Class App User

Jarvis currently reacts to voice/text input but has no structured identity within the app — he can't reliably "see" the current UI state before deciding what to do. This plan gives Jarvis a dedicated identity layer and a **live app-state feed** so every command he issues is grounded in what the UI actually looks like at that moment.

---

## The Core Problem Today

| Gap | Effect |
|---|---|
| Jarvis has no "eyes" on the UI | He can issue `control_interface` actions but doesn't know what page/panel is currently open |
| No Jarvis session identity | UI commands and user commands share the same `/ask` endpoint and conversation history indistinguishably |
| `control_interface` has no confirmation loop | Jarvis fires a command and hopes the UI acted on it — there's no acknowledgement back |
| Jarvis can't self-initiate | He only acts in response to voice/text — he can't proactively observe state and decide to act |

---

## Proposed Architecture

### Approach: **Jarvis Session + App Snapshot Feed**

The cleanest way is to give Jarvis:

1. **A live snapshot of app state** — a single `/jarvis/snapshot` endpoint he can call before every decision to know exactly: which page is open, which panel is open, which tasks are visible, what the orb state is, and the last N messages.
2. **A dedicated `/jarvis/command` endpoint** — separate from `/ask`, authenticated with a Jarvis session token, so his commands are distinguishable from human input in the conversation log.
3. **A command confirmation bus** — after Jarvis fires a `control_interface` action, the UI sends a `POST /jarvis/ack` confirming the action landed, so Jarvis can retry or pivot if it didn't.
4. **A Jarvis autonomy loop** (optional phase 2) — a background thread that wakes on a schedule or on events, takes a snapshot, and autonomously decides if any proactive action is needed (e.g. reminders, metric alerts, task suggestions).

---

## Proposed Changes

### Component 1 — Backend: `jarvis.py`

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)

- Add a `JARVIS_SESSION_TOKEN` environment variable (auto-generated on first run, saved to `.env`).
- Add a `JARVIS_UI_SNAPSHOT` global dict updated by the existing `/state` polling logic — it captures `current_page`, `panel_open`, `orb`, `sleeping`, `messages[-10:]`.
- Add `GET /jarvis/snapshot` — returns the full current app snapshot including tasks. Requires the session token header `X-Jarvis-Token`.
- Add `POST /jarvis/command` — receives `{"text": "...", "source": "jarvis"}`, routes through `handle_request` exactly like `/ask`, but tags messages with `role: "jarvis"` in `CONVO` so they appear distinctly in the chat UI.
- Add `POST /jarvis/ack` — receives `{"action": "...", "status": "ok"|"failed"}` from the UI after a `control_interface` fires, stores it in a `JARVIS_ACK_QUEUE` deque so Jarvis can read acknowledgements back.

---

### Component 2 — Backend: `coordinator.py`

#### [MODIFY] [coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/coordinator.py)

- Add a `read_app_snapshot` tool to `TOOLS` — when Jarvis's model calls this, it fetches `/jarvis/snapshot` from localhost, giving Jarvis real-time awareness of the UI state **inside** the LLM tool-call loop.
- Add the tool implementation to `TOOL_IMPL`.
- Update `UI_MAP` in `SYSTEM_PROMPT` to include: *"Before any UI command, call `read_app_snapshot` to know the current page and open panels. Never navigate somewhere already open."*
- Update the system prompt to describe Jarvis's own identity: he is a session user named `jarvis`, separate from the human user.

**New tool definition:**
```json
{
  "name": "read_app_snapshot",
  "description": "Read the live state of the app UI: which page is open, which panel is open, the orb state, sleeping state, and the last 10 conversation messages. Call this BEFORE any control_interface action.",
  "parameters": { "type": "object", "properties": {} }
}
```

---

### Component 3 — Frontend: `command_center.html`

#### [MODIFY] [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html)

- **Track UI state for snapshot**: When the page, panel, or orb state changes, `POST /jarvis/state-update` with `{current_page, panel_open, orb}`. This keeps the server snapshot accurate.
- **Distinguish Jarvis messages**: Messages with `role: "jarvis"` render with a distinct teal glow and a small ⚡ icon prefix, visually separating Jarvis's autonomous actions from user conversation.
- **ACK after `control_interface`**: When the UI processes a `control_interface` UI action from the `/state` poll, immediately fire `POST /jarvis/ack` with `{action, status: "ok"}` so Jarvis knows it landed.
- **Show Jarvis-identity badge**: A subtle "JARVIS SESSION ACTIVE" badge near the orb indicator when Jarvis is in autonomous mode.

---

### Component 4 — Configuration: `.env` + `settings.json`

#### [MODIFY] `.env`

Add: `JARVIS_SESSION_TOKEN=` (auto-generated UUID on first launch if absent).

---

## UI/UX Changes Summary

| Element | Change |
|---|---|
| Chat messages | `role: "jarvis"` → teal `⚡ JARVIS:` prefix, distinct from blue AI replies |
| Orb indicator | New state: `"autonomous"` with a distinct green pulse |
| Side panel | Shows "Jarvis is looking at this page" context tag when snapshot is taken |
| ACK flow | After any Jarvis-triggered navigation, a ghost toast: *"Jarvis confirmed: opened Plan page"* |

---

## Open Questions

> [!IMPORTANT]
> **Q1 — Autonomy scope**: Should the Jarvis autonomy loop (phase 2, proactive self-initiated actions) be included in this implementation, or just the snapshot + identity layer first?

> [!IMPORTANT]
> **Q2 — Security**: The `JARVIS_SESSION_TOKEN` only protects the `/jarvis/*` endpoints from external calls. Since the server already only binds to `127.0.0.1` this is mostly symbolic — is that fine, or do you want a stricter check?

> [!IMPORTANT]
> **Q3 — Jarvis message style**: Should Jarvis's autonomous messages appear in the same chat stream as user conversation, or in a separate **"Jarvis Log"** section in the side panel?

> [!IMPORTANT]
> **Q4 — Snapshot trigger**: Should Jarvis call `read_app_snapshot` automatically before **every** request, or only when he decides a UI action is likely?

---

## Verification Plan

### Automated
- `curl -H "X-Jarvis-Token: <token>" http://localhost:5000/jarvis/snapshot` → returns valid JSON with `current_page`, `panel_open`, `orb`, `tasks`.
- Send `POST /jarvis/command` with `{"text": "open the plan page"}` → `/state` poll returns `control_interface: go_to_plan` action → `POST /jarvis/ack` fires back.

### Manual
1. Say "Hey Jarvis, open the plan page" — Jarvis calls `read_app_snapshot`, sees Brain Core is open, calls `go_to_plan`, UI navigates, ACK fires, Jarvis confirms aloud.
2. Say "Hey Jarvis, open tasks" — if Tasks panel is already open, Jarvis's snapshot tells him that and he says "Tasks are already open, Sir" instead of re-opening.
3. Check chat: Jarvis messages show `⚡ JARVIS:` prefix in teal, distinct from your messages.
