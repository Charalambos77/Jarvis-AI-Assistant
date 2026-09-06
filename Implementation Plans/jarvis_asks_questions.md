# Jarvis Asks Questions — Pipeline Intake & Clarification Loop

**Status:** Implemented
**Branch:** `claude/jarvis-pipeline-details-modal-hg5xk9`

---

## 1. The problem

Today a pipeline is born from a single sentence. Pressing **Let Jarvis handle it**
(`command_center.html:757`) sends the chat message *"Start a pipeline to handle task #N
autonomously"*, the LLM calls the `start_pipeline` tool (`coordinator.py:534`), which routes
through `start_pipeline_local` → `initiate_pipeline(task)` (`jarvis.py:666`). That function
immediately:

1. allocates a plan id,
2. derives a project name via `get_project_name()`,
3. writes a `pipelines` row through `db.save_pipeline`,
4. spawns a daemon thread running `run_full_pipeline(task, ...)`.

There is no opportunity to say *"this website is for a dentist in Limassol, here are their
photos and their old brochure."* The agents research and build on one line of text and their
own assumptions.

## 2. What we're building

A **clarification gate that sits in front of the pipeline**. Nothing about the multi-agent
engine changes; we insert a conversation before it and hand it a far richer brief.

The shape of the conversation:

```
start_pipeline requested
        │
        ▼
  "Want to give me more details?"  ──No──►  today's behavior: pipeline starts on the one-liner
        │ Yes
        ▼
┌──────────────────────────────────────────────────────────┐
│  INTAKE MODAL  (near-full-width, tall)                   │
│  big free-text details + drop any files                  │
│  [Cancel] [Continue with this info] [Continue with       │
│                                      clarification       │
│                                      questions]          │
└──────────────────────────────────────────────────────────┘
        │ questions                          │ this info
        ▼                                    │
┌──────────────────────────────────────────┐ │
│  QUESTION MODAL — one question at a time │ │
│  full text on screen, spoken aloud       │ │
│  [Cancel] [Continue]                     │ │
│  [Skip the rest — build with what you    │ │
│   have]                                  │ │
│  last question → [Paint a picture or ask │ │
│                   more questions]        │ │
└──────────────────────────────────────────┘ │
        │ Jarvis decides: more gaps? ──loop──┘
        ▼ no gaps
┌──────────────────────────────────────────────────────────┐
│  PICTURE MODAL — the written plan                        │
│  [Cancel] [Edit] [Start building]                        │
│  Edit → you rewrite → Jarvis cleans it → shown back here │
│  Jarvis may return more questions instead of a plan      │
└──────────────────────────────────────────────────────────┘
        │ Start building
        ▼
  plan row created + project folder + run_full_pipeline(full brief)
```

### Naming rule (applies everywhere)

**"Continue" alone always means "advance to the next thing" — never "commit and build."**
Every button that actually commits says what it commits to: **"Continue with this info"**
on intake, **"Start building"** on the picture.

## 3. Decisions already settled

| Decision | Ruling |
|---|---|
| Entry points | All three — the button, a typed "start a pipeline for X", and the voice command. Every path that would reach `start_pipeline` gets the ask first. |
| When the pipeline row is created | **Only on final approval.** No `pipelines` row, no project folder, no agent thread while clarifying. Cancel = it never existed. |
| Files | Saved to `Let Jarvis Handle It/<project>/Inputs/`. Images and PDFs are additionally sent to Gemini so it can *see* them when writing questions and the plan. Other formats stored and referenced by name/path so agents can open them. |
| Voice | Each question is spoken — word-for-word if short, a one-line gist if long. The **full text is always visible on screen**. Answers are typed. Respects the mute button. |
| Loop safety | No hard cap. Every question screen carries **"Skip the rest — build with what you have"**, and Jarvis is instructed to only ask about gaps that would genuinely change the build. |
| Editing the plan | Edit → Jarvis reformats → **the cleaned version is shown back for approval** before anything starts. Editable as many times as you like. |
| Answering "No" to the details ask | Pipeline starts immediately on the one-line task — exactly today's behavior. |
| Cancel | Aborts everything at any screen. Draft discarded, uploaded files for that draft deleted. |
| How agents treat the brief | Authoritative context, **not a cage**. Agents still research and search the web for anything the brief doesn't cover. They stay inside it only where it explicitly constrains them — if it names a specific source, tool, or approach, they use that instead of choosing their own. |

## 4. Architecture

### 4.1 Server-side draft store (`jarvis.py`)

A new in-memory store, mirroring the existing `PLAN_STORE` / `PLAN_STORE_LOCK` pattern
(`jarvis.py:112-121`), holds intake drafts *before* they become pipelines:

```python
INTAKE_DRAFTS = {}            # draft_id -> draft dict
INTAKE_DRAFTS_LOCK = threading.Lock()
```

A draft:

```python
{
  "draft_id": "d3f9a1",
  "task": "Build a website for my dentist client",   # original one-liner
  "project_name": "Dentist Website",                 # derived once, used for the Inputs folder
  "details": "...",                                  # free text from intake
  "files": [ {"name": ..., "path": ..., "mime": ..., "size": ...} ],
  "qa": [ {"question": ..., "answer": ...} ],        # every answered round, in order
  "pending_questions": [ "...", "..." ],             # current unanswered batch
  "rounds": 2,                                       # how many question rounds so far
  "plan_text": "...",                                # the current picture, once painted
  "stage": "intake" | "questions" | "picture" | "done",
  "created": 1735900000.0
}
```

Drafts are intentionally **not** persisted to SQLite — an abandoned draft should leave no
trace, matching the "Cancel = nothing ever happened" ruling. A janitor drops drafts older
than a few hours (and their `Inputs/` folder) on each new intake.

### 4.2 New endpoints (`jarvis.py`)

| Route | Method | Purpose |
|---|---|---|
| `/pipeline/intake/start` | POST | `{task}` → creates a draft, returns `draft_id` + derived `project_name`. Called by the frontend when you answer **Yes**. |
| `/pipeline/intake/upload` | POST (multipart) | `draft_id` + one or more files → saves under `Let Jarvis Handle It/<project>/Inputs/`, returns the stored file list. |
| `/pipeline/intake/remove_file` | POST | `{draft_id, name}` → deletes a file you changed your mind about. |
| `/pipeline/intake/questions` | POST | `{draft_id, details}` → stores details, asks Gemini for the next batch of gap questions, returns them. Also used for each subsequent round. |
| `/pipeline/intake/answer` | POST | `{draft_id, question, answer}` → appends to `qa`, returns the next pending question or `{done: true}`. |
| `/pipeline/intake/picture` | POST | `{draft_id}` → Jarvis decides: more questions (returns `{questions: [...]}`) or paints the plan (returns `{plan_text: ...}`). |
| `/pipeline/intake/edit` | POST | `{draft_id, edited_text}` → Gemini reformats the edit cleanly, stores and returns it for re-approval. |
| `/pipeline/intake/approve` | POST | `{draft_id}` → writes `clarified_brief.md`, calls `initiate_pipeline(brief, project_name=..., brief_path=...)`, returns `plan_id`. **The only path that creates a pipeline.** |
| `/pipeline/intake/skip` | POST | `{draft_id}` → "Skip the rest": drops the unanswered questions. |
| `/pipeline/intake/cancel` | POST | `{draft_id}` → deletes the draft and its `Inputs/` folder. |
| `/intake-file/<draft_id>/<name>` | GET | Serves an uploaded file back for the thumbnail chips. Only files that draft actually recorded — never an arbitrary path. |
| `/jarvis/say` | POST | Speaks one line through the existing `speak()`, so the orb and mute behave normally. |

All of these are plain Flask routes alongside the existing `/pipeline/start` family, and all
mutate `INTAKE_DRAFTS` under `INTAKE_DRAFTS_LOCK`.

### 4.3 Rerouting the trigger (`coordinator.py`, `jarvis.py`)

`start_pipeline_local` (`jarvis.py:781`) no longer calls `initiate_pipeline` directly. Instead:

1. It creates a draft and stores the `draft_id`.
2. It sets `UI_ACTION = {"type": "pipeline_intake_ask", "draft_id": ..., "task": ...}`, which
   the frontend picks up through the existing `/state` polling channel
   (`jarvis.py:1218`, consumed at `command_center.html:3622`).
3. It pushes a spoken/chat message: *"Do you want to give me more details before I start?"*
4. It returns `{"status": "awaiting_details", "draft_id": ...}` so the LLM knows the pipeline
   has **not** started and does not claim otherwise.

The `start_pipeline` tool description in `coordinator.py:534` and the `UI_MAP` line
(`coordinator.py:767`) are updated to say the tool opens the intake flow rather than launching
the pipeline, so the model narrates it correctly.

A "no" answer is handled by the frontend calling `/pipeline/start` (existing route) — nothing
new needed on the server for that path.

### 4.4 `initiate_pipeline` signature change (`jarvis.py:666`)

```python
def initiate_pipeline(task: str, project_name: str | None = None,
                      brief_path: str | None = None) -> str:
```

- `project_name` is passed through when the draft already derived one, so the `Inputs/` folder
  the files were uploaded into is the *same* folder the pipeline uses (no second
  `get_project_name()` call producing a different name).
- `brief_path` is stored on the plan entry and forwarded to `run_full_pipeline` so agents can
  re-read the full brief.
- Everything else is unchanged. Called with no new arguments, it behaves exactly as today —
  the "No, just build it" path is untouched.

### 4.5 The brief on disk

```
Let Jarvis Handle It/<Project Name>/
├── Inputs/                     ← uploaded files, original names (deduped)
└── Brief/
    └── clarified_brief.md      ← the complete record
```

`clarified_brief.md` layout:

```markdown
# Clarified Brief — <Project Name>

## Original request
<the one-line task>

## Details from the user
<free text from the intake modal>

## Clarifications
**Q:** ...
**A:** ...
(every round, in order)

## Attached files
- Inputs/logo.png — image/png
- Inputs/old-brochure.pdf — application/pdf

## Approved plan
<the final, approved picture text>
```

### 4.6 Handing the brief to the agents

- `initiate_pipeline` passes the **full approved brief text** as the `task` argument to
  `run_full_pipeline` (`multi_agent_coordinator.py:532`), plus a new optional
  `brief_path` parameter.
- `run_full_pipeline` stores `brief_path` and includes a short standing instruction in the
  agent prompts:

  > The brief below is authoritative for what the user wants. It is not a limit on how you
  > work: research and search the web freely for anything the brief does not cover. Follow the
  > brief exactly where it constrains you — if it names a specific source, tool, or approach,
  > use that one instead of choosing your own. Attached files live at the paths listed in the
  > brief; open them when relevant.

- Because the task string is now long, the UI needs a short label. `initiate_pipeline` keeps
  the original one-liner on the plan entry as `"task_summary"`, and the constellation /
  projects list renders `task_summary` where it currently renders `task`.

### 4.7 Prompting (all Gemini, `gemini-2.5-flash`, same client as `jarvis.py:49`)

Three prompt jobs, each returning strict JSON:

**Gap questions** — given the task, details, file list, images/PDFs inline, and all prior Q&A:
> Find only the gaps that would genuinely change what gets built. Do not ask what you can
> reasonably infer, and never ask something already answered. If nothing material is missing,
> return an empty list. Prefer short, plain questions; give each a one-line spoken gist.

Returns `[{"question": "...", "gist": "..."}]`. An empty list means "ready to paint."

**Paint the picture** — given everything:
> Write the plan as a picture of what the user asked for, with every clarification folded in.
> Plain language, concrete, no invented requirements. If something material is still unclear,
> return questions instead of a plan.

Returns `{"plan_text": "..."} ` or `{"questions": [...]}` — this is what makes the loop work.

**Clean an edit** — given the user's rewritten text:
> Return the user's edited plan in a clean, consistent format. Preserve every one of their
> decisions and change no meaning. Do not add requirements they did not write.

Returns `{"plan_text": "..."}`.

Each call is wrapped so an API failure degrades gracefully: questions failing → skip to the
picture; picture failing → show the raw assembled brief for editing, with a visible note.

## 5. Frontend (`command_center.html`)

### 5.1 The modal

One overlay element, three states, reusing the page's existing dark/cyan visual language
(`--text-muted`, the `#22D3EE` accent, the `.action-btn` styling used at `:748-758`).

- **Dimensions:** `width: 92vw; max-width: 1600px; height: 86vh` — near-full-width and
  genuinely tall, as specified. Backdrop blur, click-outside does *not* close (too easy to
  lose a long brief); Escape maps to Cancel with a confirm.
- **Scroll:** the body of each state scrolls internally; the button row is pinned to the
  bottom so the actions are always reachable.
- **State machine:** a single `intakeState` object (`draft_id`, `stage`, `questions`,
  `currentQuestionIndex`, `planText`, `isEditing`) drives which panel renders.

**Intake state**
- Large `<textarea>` for details, autofocused.
- Drop zone + file picker, `multiple`, no `accept` filter (any type). Dropped files upload
  immediately to `/pipeline/intake/upload` and appear as removable chips with name/size, image
  files showing a thumbnail.
- Buttons: `Cancel` · `Continue with this info` · `Continue with clarification questions`.

**Questions state**
- One question, large type, full text always visible. A subtle `Question 2 of 4` counter.
- Answer `<textarea>` below it.
- Buttons: `Cancel` · `Continue`, plus a quieter `Skip the rest — build with what you have`.
- On Continue: POST the answer, fade the current question out, fade the next in. When the batch
  is exhausted, the primary button relabels to **`Paint a picture or ask more questions`**;
  pressing it hits `/pipeline/intake/picture` and either loads a fresh question batch (loop) or
  transitions to the picture state.

**Picture state**
- The plan rendered as readable markdown-ish HTML, scrollable.
- Buttons: `Cancel` · `Edit` · `Start building`.
- `Edit` swaps the rendered plan for a full-height `<textarea>` holding the raw text; the
  buttons become `Cancel` · `Cancel edit` · `Done editing`. `Done editing` POSTs to
  `/pipeline/intake/edit`, and the **cleaned result is rendered back into the picture state**
  for approval — never straight to building.
- `Start building` POSTs `/pipeline/intake/approve`, closes the modal, and lets the existing
  pipeline-started animations and polling take over with the returned `plan_id`.

### 5.2 Wiring

- New branch in the `ui_action` dispatcher (`command_center.html:3622`):
  `act.type === 'pipeline_intake_ask'` → shows the yes/no prompt (a compact confirm bar, not
  the big modal). **Yes** opens the intake modal against `act.draft_id`; **No** POSTs to
  `/pipeline/start` with the original task.
- The **Let Jarvis handle it** button (`:757`) keeps sending its chat message — the flow it
  triggers now routes through the intake ask, so the button needs no change. (Worth confirming
  during implementation that the LLM reliably reaches `start_pipeline` from that phrasing; if
  not, point the button directly at `/pipeline/intake/start`.)
- Voice path needs no special handling: the voice command already lands in the same
  `start_pipeline` tool, so the modal opens on screen and the spoken prompt comes from
  `push_message`/`speak`.

### 5.3 Speaking the questions

When a question is displayed, the frontend POSTs its `gist` (or the full text, if short) to a
small `/jarvis/say` helper that calls the existing `speak()` (`jarvis.py:277`) — the same path
the rest of the app uses, so mute (`MIC_MUTED` / the mute button) and the orb animation behave
consistently. Length threshold: read verbatim under ~140 characters, otherwise read the gist.

## 6. Files touched

| File | Change |
|---|---|
| `jarvis.py` | `INTAKE_DRAFTS` store + lock; nine `/pipeline/intake/*` routes; `/jarvis/say`; three Gemini prompt helpers; `start_pipeline_local` rerouted to open the intake ask; `initiate_pipeline` gains `project_name` / `brief_path`; `task_summary` on the plan entry; brief writer; draft janitor. |
| `command_center.html` | The intake/questions/picture modal and its CSS; file upload UI; `intakeState` machine; `pipeline_intake_ask` branch in the `ui_action` dispatcher; `task_summary` used for plan labels. |
| `coordinator.py` | `start_pipeline` tool description + `UI_MAP` line updated to say it opens the details flow, not that it launches the pipeline. |
| `multi_agent_coordinator.py` | `run_full_pipeline` accepts `brief_path`; the standing brief instruction added to agent prompts. |
| `db.py` | `save_pipeline` also persists `task_summary` and `brief_path` inside the existing `data` JSON blob — no schema migration needed, and `get_pipelines` already merges that blob back. |
| `requirements.txt` | No new dependencies. Flask handles multipart; Gemini reads images/PDFs natively via `google-genai`. |

## 7. Build order

1. **Server draft store + intake routes** — `start`, `upload`, `remove_file`, `cancel`. Verify
   with curl that files land in `Inputs/` and Cancel wipes them.
2. **Reroute `start_pipeline_local`** to emit `pipeline_intake_ask` and return
   `awaiting_details`. Confirm no pipeline row is created.
3. **Modal shell + intake state** in `command_center.html`, wired to steps 1–2, with the
   yes/no prompt. At this point "Continue with this info" should build correctly.
4. **Question routes + prompt**, then the questions state, one at a time, with the relabel to
   "Paint a picture or ask more questions" and the skip escape.
5. **Picture route + prompt**, the picture state, and the loop-back when Jarvis returns
   questions instead of a plan.
6. **Edit route + clean-and-show-back**, verifying you always approve what you can see.
7. **Approve route** — brief written, `initiate_pipeline` called with the full brief, project
   name and brief path.
8. **Agent-side brief handling** in `multi_agent_coordinator.py`, plus `task_summary` labels
   in the UI.
9. **Speaking**, mute behavior, and the failure fallbacks.

## 8. What to verify before calling it done

- "No" to the details ask starts a pipeline exactly as today.
- Cancel at every screen leaves no `pipelines` row, no project folder, no files.
- Uploading a photo and a PDF, then asking a question that only their *contents* could answer,
  proves Gemini is genuinely seeing them.
- The question loop can run at least twice, and the skip escape works from any question.
- Editing the plan twice in a row keeps both edits and shows the cleaned text each time.
- After Start building, `clarified_brief.md` exists, contains every Q&A and every file path,
  and the agents' first phase demonstrably references the details.
- Constellation and project-list labels stay short (`task_summary`), not the whole brief.
- Mute silences the spoken questions; the full text stays on screen regardless.


---

## 9. Implementation notes — decisions made while building

Three places where the build had to settle something the plan left open:

**The "Let Jarvis handle it" button now calls the gate directly.** The plan kept the button
sending its chat message and relying on the model to pick the `start_pipeline` tool. That is a
coin flip on a button press, so the button now calls `/pipeline/intake/start` and opens the ask
itself. Typed chat and voice still travel the tool path, which routes to the same gate.

**Mute.** The app has only a microphone mute (`MIC_MUTED`), no separate speech mute. `/jarvis/say`
treats it as "Jarvis, be quiet" and stays silent while it is on. Nothing is lost by that: the
question text is on screen regardless. If a dedicated speech mute is ever added, this is the one
line to repoint.

**Long tasks in the model's context.** Because `task` now holds the whole brief,
`get_pipelines_local` hands the assistant `task_summary` in the `task` field instead — otherwise
every "list my projects" call would drag several full briefs into the prompt.

`start_pipeline_local` also accepts `skip_intake: true` for the "No, just build it" path, so the
old behavior is reachable without duplicating `initiate_pipeline`'s logic.

## 10. Test coverage

`scripts/test_intake.py` drives the whole gate through Flask's test client with the model
stubbed, covering 40 assertions: draft creation without a pipeline, multi-type upload, path
traversal being stripped, duplicate filenames, thumbnail scoping, file removal from draft *and*
disk, the one-at-a-time question walk, the picture looping back to more questions, the skip
escape, the edit round-trip (including keeping the user's exact words when the model fails),
approval writing the brief and starting exactly one pipeline, cancel leaving nothing behind, and
a dead model degrading to an editable brief instead of trapping the user.

Two verification gaps to close by hand against the real app:
- Gemini genuinely *seeing* an uploaded photo/PDF (the test stubs the model out).
- The modal's look and feel at real viewport sizes.

Note: `agents/research_agent.py` needs Python 3.12+ (it has a backslash inside an f-string), so
`multi_agent_coordinator` could not be imported for testing on 3.11 — its changes were verified
by parsing the source instead. That constraint is pre-existing and unrelated to this work.


## 11. Fixes from the post-implementation review

Three defects found by re-reading the diff adversarially, all fixed and covered by tests:

**Cancel could delete another pipeline's files.** `get_project_name` derives the project folder
from the task text, so two similar requests share one `Inputs/` folder. Cancel used to `rmtree`
that whole folder — which would take an already-approved, possibly still-running pipeline's
uploads with it. Cancel now deletes only the files that draft itself recorded, and removes the
folders only while they are empty.

**A pipeline started by voice on another page vanished.** `execution.html`, `plan.html` and
`provider_comparison.html` all poll `/state` and consume `ui_action`, and `/state` clears the
action after serving it — so whichever page polled first swallowed the intake ask, and the
pipeline never started. Those three pages now hand the draft to the command centre through
`?intake=<draft_id>`, and a new `GET /pipeline/intake/draft` lets it pick the draft back up with
the typed details and uploaded files intact.

**A second pipeline overwrote the first's brief.** Same project-name collision: the second
approval wrote over `clarified_brief.md` while the first pipeline's `brief_path` still pointed at
it, so a resumed run would read the wrong brief. Briefs are now written to the next free
`clarified_brief (n).md`.

The suite in `scripts/test_intake.py` is now 54 assertions, self-cleaning, and repeatable back to
back.
