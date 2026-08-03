# Implementation Plan: Commands Aligned to the 3D Constellation Interface

---

## Goal

Every voice and text command Jarvis understands should feel native to the 3D Constellation Map. Commands that used to open a grid modal or reload a list must now drive the 3D scene — camera moves, particle animations, panel slides, and node state changes. This plan maps all current commands to the new UI and proposes visual animations in the same style as the animation plan (beam growth, particle assembly, supernova collapse, screen-suck).

---

## Current Command Inventory

| Voice / Text Command | Jarvis Tool | Current UI Behavior (OLD) |
| :--- | :--- | :--- |
| "add task [text]" | add_task | Task added silently, list reloads |
| "complete task [id]" | complete_task | Task marked done, list reloads |
| "delete task [id]" | delete_task | Task removed, list reloads |
| "focus on task [id]" | focus_tasks | Highlights item in left panel list |
| "add note [text]" | add_note | Note added, right panel reloads |
| "delete note [id]" | delete_note | Note removed, right panel reloads |
| "search notes [query]" | search_notes | Filters notes in right panel |
| "update note [id]" | update_note | Note updated, right panel reloads |
| "add subtask to task [id]" | add_task (with parent_id) | Subtask added, list reloads |
| "google search [query]" | google_search | Text reply in conversation |
| "show plan" / "open plan" | UI nav trigger | Redirects to plan.html |
| "show tasks" / "open task database" | UI nav trigger | Opens side panel task list |
| "show brain" / "focus on brain" | UI nav trigger | Zooms camera to core, opens panel |
| "close panel" | UI nav trigger | Slides side panel closed |
| "end conversation" / "go to sleep" | Voice loop | Hides window, Jarvis sleeps |
| "exit completely" | Voice loop | Kills the app process |

---

## Proposed Command-to-UI Alignments

### 1. add task [text] — Beam Birth Animation

Animation Style: Matches Create New Task from the animation plan.

- Jarvis confirms the task verbally.
- The 3D scene triggers the 6-second beam animation:
  - A priority-colored laser grows from the nebula core to the new node position.
  - Beam fades, then 25+ particles swarm from the core to assemble the task node.
- The new live node is added to the scene and starts pulsing normally.
- The side panel silently refreshes its task list in the background.

---

### 2. delete task [id] — Supernova Collapse

Animation Style: Matches Remove Task from the animation plan.

- Jarvis confirms deletion verbally.
- The targeted node receives a barrage of charge beams from the core.
- Node flares up to twice its size (white-hot glow).
- Collapses inward and explodes outward in a 50+ particle burst.
- The connection beam from core to node fades to 0 and is removed from the scene.

---

### 3. complete task [id] — Screen-Suck Warp

Animation Style: Matches Mark Task Completed from the animation plan.

- Jarvis confirms completion verbally.
- The node rapidly scales down while accelerating along the Z-axis toward the camera — sucked through the screen.
- The connection beam shrinks from the node end back to the core and disappears.
- The side panel task list quietly updates to reflect the new done status.

---

### 4. focus on task [id] / "open task [id]" — Camera Warp + Panel Slide

Animation Style: New — gravitational pull focus.

- The 3D camera smoothly LERP-transitions to a close-up position targeting the node.
- The targeted node pulses with a bright ring (3 concentric expanding rings, like a sonar ping) that fade over 2 seconds.
- The side panel slides in from the left with the task's details populated.
- If the task is a live DB task (not a constellation node), the Tasks list opens and the item glows amber with a pulsing border.

---

### 5. add subtask to task [id] — Energy Charge + Branch Birth

Animation Style: Matches Create Subtask from the animation plan.

- Multiple fast energy pulses shoot from the core toward the parent task node.
- Parent node overloads — flashes white-hot (scale 2x) for 1 second.
- A new shorter beam grows from the parent to the subtask position.
- The subtask node scales up at its branch position and starts pulsing.

---

### 6. add note [text] — Ink Drop

Animation Style: New — softer, distinct from task creation.

- Unlike the hard laser birth of tasks, notes use a softer approach: a slow expanding ring of dim golden particles emerges from the bottom of the nebula and disperses outward.
- This reflects that notes are ambient information, not task nodes.
- A small amber NOTE SAVED flash appears on the chat indicator badge.
- Jarvis response text in chat confirms it.

---

### 7. delete note [id] — Fade Dissolve

Animation Style: New — quiet, matches the subtlety of notes.

- Since notes are not 3D nodes, no 3D explosion is triggered.
- Instead, a ripple of dim particles pulses once from the nebula center outward.
- The chat confirms deletion.

---

### 8. search notes [query] — Scan Sweep

Animation Style: New — scanning beam.

- A faint horizontal scanning line sweeps across the nebula from top to bottom (like a radar sweep).
- The nebula dims slightly during the scan, then brightens once results return.
- Results are shown in the chat transcript or the side panel notes list.

---

### 9. google search [query] — Signal Pulse Out

Animation Style: New — outbound signal.

- A ring of bright particles pulses outward from the center to the edges of the screen, symbolizing an outbound query to the internet.
- Nebula dims slightly (Jarvis is thinking outward).
- Once results come back, the nebula brightens and a reply fades into the chat.

---

### 10. "show brain" / "focus on brain" — Zoom to Core

- Camera LERP-transitions to close-up center (0, 0, 1.8).
- The nebula core briefly flares with increased particle opacity.
- Side panel opens with Brain context details.

---

### 11. "show tasks" / "open task database" — Constellation Overview Zoom-Out

- Camera zooms out to the default orbit position (0, 0, 2.5) to show all nodes.
- Side panel opens with the live database task list.
- Existing constellation nodes briefly ping with a small ring to indicate they are real tasks.

---

### 12. "show plan" / "open plan" — Warp Exit

- The nebula rapidly compresses to a bright point at the center.
- A white flash covers the screen.
- Browser redirects to plan.html.

---

### 13. "close panel" / "hide panel" — Panel Slide + Camera Reset

- Side panel slides out to the left.
- Camera gently LERP-transitions back to the default overview position.
- Targeted node (if any) stops pulsing its selection ring.

---

### 14. "end conversation" / "go to sleep" — Dimming Fade

- Jarvis says goodbye.
- The nebula particles slowly dim to a much lower opacity (sleep mode visual).
- Orb indicator transitions to a slow, dim heartbeat pulse labeled SLEEPING.
- App window hides. On next wake-word trigger, the nebula brightens back up.

---

### 15. "exit completely" — Core Collapse

- Jarvis says goodbye.
- All constellation node beams rapidly retract back to the center.
- The nebula core rapidly compresses inward to a point.
- The window closes and the process exits.

---

## Implementation Sequence

### coordinator.py
- Expand the system prompt to describe all new commands in context of the 3D interface.
- Add ui_action output field so Jarvis can signal the frontend what animation to trigger (e.g. {"ui_action": "task_created", "task_id": 5}).

### jarvis.py
- The /ask and /state routes expose ui_action from the AI response to the frontend.
- Update the handle_request response to include ui_action metadata.

### command_center.html
- Connect the 3D Constellation Map to live /tasks polling (replace hardcoded tasksData).
- Build the animation system: animateTaskCreation, animateTaskDeletion, animateTaskCompletion, animateSubtaskCreation, etc.
- Hook pollState() to check ui_action field and dispatch the correct animation.
- Add ambient animations for notes, search sweeps, and nav transitions.

---

## Verification Plan

Test each command class and verify the matching 3D response:

1. "add a task to review the quarterly report" -> beam birth animation on the constellation map.
2. "complete task 3" -> node screen-suck animation, node disappears.
3. "delete task 2" -> supernova collapse, beam fades.
4. "focus on task 1" -> camera warps to node, sonar ping, side panel opens.
5. "add a subtask to task 1 called update the footer" -> energy charge, parent flash, branch birth.
6. "search notes productivity" -> scan sweep animation, results in chat.
7. "show plan" -> warp exit and page redirect.
8. "go to sleep" -> nebula dims, orb sleeps.
