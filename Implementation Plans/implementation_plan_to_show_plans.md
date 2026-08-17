# Implementation Plan: HITL Gate Approval Modal & Plan Listing UI

This document details the step-level approval and rejection system for Jarvis multi-agent execution plans. The core features are:
1. An implementation plan popup card (modal) that blocks interaction until approved or rejected.
2. Step-by-step checkboxes to select specific steps to reject.
3. A reason text area specifying what the agents should change.
4. Backend wiring to re-plan/re-run only the target steps.
5. A main plan list showing all plans (main plan vs. agent sub-plans), sharing the same approval/rejection modal.

---

## User Review Required

> [!IMPORTANT]
> The approval card will appear as a fullscreen overlay modal on `plan.html` when a pipeline gate is active. Closing the popup will return to the main plans view.

---

## Proposed Changes

### 1. Backend Routes & State Updates

#### [MODIFY] [jarvis.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/jarvis.py)
- Expand `PIPELINE_STATE` to hold current gate plan details (`gate_data` and `rejected_steps`).
- Update the `/gate/reject` route to accept `rejected_steps` (indices/IDs of steps to change) and the `redirect_note`.
- Add a `GET /gate/data` endpoint so the UI can fetch the plan structure.
- Add an in-memory `PLAN_STORE` list and a `GET /plans` route to list all active/past plans.
- Feed synthesis and execution plan details into the pipeline coordinator so they are visible in `gate_data`.

### 2. Multi-Agent Engine Integration

#### [MODIFY] [multi_agent_coordinator.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/multi_agent_coordinator.py)
- Modify the gate functions to pass the selected `rejected_steps` and `redirect_note` back to the central brain.
- Incorporate step-level feedback in `build_agent_plan` so agents only modify target steps and leave others intact.

### 3. UI Redesign

#### [MODIFY] [plan.html](file:///d:/Charalambos/Desktop/AI/second-brain-voice/plan.html)
- Replace the current static 6-column pipeline view with:
  1. A **Main Plans Listing View** showing cards for the main user-initiated plans and agent sub-plans, with status indicators (e.g., Gate 1 Waiting, Approved, Rejected).
  2. A **Glassmorphic Approval Modal** popup that appears on gate activation.
- The Modal includes:
  - Scrollable list of numbered plan steps.
  - A checkbox next to each step.
  - A "Reason for adjustment" textarea (shown when checkboxes are selected or when Reject is clicked).
  - "Approve" (green) and "Reject" (red) actions.
  - Option to close the popup to browse other plans.
- Wire AJAX requests (`fetch`) to `/gate/approve` and `/gate/reject` sending the checked step IDs and reason.

---

## Verification Plan

### Automated/Manual Tests
- Run `python jarvis.py` and access the Plan page.
- Trigger a mock pipeline and verify the popup modal appears.
- Select specific checkboxes, write a rejection comment, press Reject, and verify the backend receives step list + comment.
- Close the modal and confirm all past and current plans are selectable in the listing.
