# Dynamic 3D Constellation Animations for Task Life Cycle

This plan details the implementation of custom 3D webgl/Three.js animations on the Constellation Map inside [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html).

---

## User Review Required

> [important]
> To support dynamic animations, we will transition the 3D Constellation Map from using **hardcoded** mock tasks (`tasksData`) to visualizing **live** tasks from the SQLite database.
> The initial layout will load from `/tasks`, and any subsequent changes (creating, deleting, or completing tasks) will trigger the corresponding 3D WebGL animations in real-time.

---

## Animation Designs & Timings

### 1. Create New Task (6-second sequence)
When a new main task is added to the database:
- **0.0s - 3.0s (Beam Growth)**: A glowing laser line (colored by task priority) grows from the neural center `(0,0,0)` to its computed 3D destination.
- **3.0s - 4.0s (Beam Fade)**: The laser beam fades out.
- **4.0s - 6.0s (Particle Assembly)**: 25+ glowing particles shoot rapidly along the line from the core to the target position, merging together to form the task node (which scales up from `0` to `1.2`).

### 2. Create Subtask (4-second sequence)
When a subtask is added to an existing parent task:
- **0.0s - 2.0s (Energy Charge)**: Multiple fast energy pulses shoot from the core towards the parent task node.
- **2.0s - 3.0s (Parent Overload)**: On impact, the parent task node flashes with intense brightness (scale increases by `2x`, color shifts to white-hot).
- **3.0s - 4.0s (Subtask Birth)**: The connecting beam between parent and subtask grows, and the subtask node scales up at its target position.

### 3. Remove Task (Explosion sequence)
When a task is deleted:
- **Phase 1 (Charge)**: A barrage of beams from the core target the node.
- **Phase 2 (Supernova)**: The node flares up to twice its size and glows with high intensity.
- **Phase 3 (Collapse & Explode)**: The node collapses into a tiny point, then explodes outward into a dispersing cloud of 50+ fading particles. The connecting line fades to 0.

### 4. Mark Task Completed (Sucked into Screen sequence)
When a task is marked done:
- The node scales down to `0` while moving rapidly along the Z-axis toward the camera (`z = 2.5`), giving the visual impression of being sucked through the viewport.
- The connecting line shrinks and fades away.

---

## Proposed Changes

### [command_center.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/command_center.html)
- Replace static `tasksData` array with a dynamic reactive system that tracks active 3D nodes.
- Maintain a map of active Three.js objects (lines, nodes, particle pools) indexed by task/subtask IDs.
- Update `/tasks` polling handler to detect differences:
  - **New task ID**: trigger `animateTaskCreation(taskData)`
  - **New subtask ID**: trigger `animateSubtaskCreation(subtaskData, parentPosition)`
  - **Task ID removed**: trigger `animateTaskRemoval(taskId)`
  - **Task ID marked completed (`status === 'done'`)**: trigger `animateTaskCompletion(taskId)`
- Add custom tick functions in the main Three.js `animateGlobe()` loop to update ongoing animations using linear interpolation (LERP) and easing functions.

---

## Verification Plan

### Manual Verification
1. Open the Command Center in your browser.
2. Say *"Hey Jarvis, add a task to clean the workspace"* or type it in. Verify:
   - A beam grows from the center, fades, and then particles form the new node on the map.
3. Say *"add a subtask to task [ID] named write unit tests"* -> Verify:
   - Energy pulses hit the parent node, it flashes, and the subtask node branches out.
4. Mark a task as completed -> Verify the node zooms into the screen.
5. Delete a task -> Verify it explodes.
