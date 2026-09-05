# Sections — Turning a Finished Pipeline Into a Lasting Workspace

**Status:** Implemented
**Branch:** `claude/jarvis-pipeline-details-modal-hg5xk9`

---

## 1. The problem

A pipeline finishes and that is the end of it. The research on the Cyprus rental market lands
in `Let Jarvis Handle It/Cyprus Car Rental/`, the agents' findings sit in
`memory/high_value/*.json` as one opaque blob per agent per cycle
(`multi_agent_coordinator.py:848`), and nothing ever reads them again. The next pipeline —
*build the booking site for that market* — starts from an empty brief and researches the same
ground from scratch, because there is no way to say "you already know this."

The clarification gate fixed the start of a pipeline. This fixes what happens after one ends.

## 2. What we're building

A **section**: a lasting workspace grown out of one finished pipeline. The founding pipeline
stops being a one-off run and becomes standing knowledge; new pipelines started inside the
section build on top of it.

```
finished pipeline
        │
        ▼
  [ + Make section ]  on the plan card
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  SECTION WINDOW  (write + drop files, like intake)       │
│  the section BRIEF: what this is for, where it is going  │
│  [Cancel] [Create section without questions] [Continue]  │
└──────────────────────────────────────────────────────────┘
        │ continue
        ▼
┌──────────────────────────────────────────────────────────┐
│  the clarification gate, on the way IN                   │
│  gap questions, one at a time  →  the section brief      │
│  painted back for you to correct                         │
│  [Cancel] [Edit] [Create section]                        │
└──────────────────────────────────────────────────────────┘
        │ create
        ▼
  the pipeline's folder becomes the section's folder
  its memory/*.json is harvested into Knowledge/*.md
        │
        ▼
┌──────────────────────────────────────────────────────────┐
│  SECTION DASHBOARD                                       │
│  what it knows · its pipelines · its tasks, notes, chat  │
│  a pipeline → execution mode, exactly as it already is   │
│  [New pipeline here]                                     │
└──────────────────────────────────────────────────────────┘
        │ new pipeline here
        ▼
  the clarification gate, with the section's knowledge
  already folded in — never re-asking what is established
```

Reached from anywhere by **View Sections** on the Brain and Execution pages, which opens a
sidebar where every section holds one block of the height.

### Naming rule (inherited from the clarification gate)

The commit point says what it commits to: **"Create section"**, never "Continue". Nothing
exists until it is pressed — no row, no folder change, no knowledge written. "Continue" is
therefore free to mean what it means in the pipeline gate: go on to the questions.

## 3. Decisions already settled

| Decision | Ruling |
|---|---|
| What a section is | A **live workspace**, not an archive. The founding pipeline is the first of many; the point of a section is to start *more* work that uses what the first one learned. |
| Section brief | Written by the user in a window at creation, with file drops. The founding pipeline says what was researched; the brief says what it is *for*. It is the first thing folded into every later pipeline. |
| Section folder | The founding pipeline's existing `Let Jarvis Handle It/<Project>/` folder. Nothing is copied or moved — everything about a section goes in one folder. |
| Knowledge handoff | **Both.** A digest is injected into every pipeline started inside the section, *and* the full material stays on disk for agents to open when a step needs more. |
| Knowledge format | Markdown with YAML frontmatter and `[[wikilinks]]`, one note per topic, in `Knowledge/`. Additive — a later pipeline appends to an existing topic rather than replacing it. |
| Obsidian | The folder **is** an Obsidian vault if you point Obsidian at it. That is a lens, not a dependency: agents read plain markdown through the filesystem MCP server, and Obsidian never needs to be running. See §7. |
| Living summary | `Knowledge/What this section knows.md`, rewritten after each pipeline and **editable by hand**. Your edits are written back verbatim. |
| Jarvis's reach inside a section | **Focused, not sealed.** Tools scope to the section by default; the model reaches the wider brain only by explicitly passing `scope`. |
| Tasks and notes | A task or note made inside a section **belongs to it**. The brain's own lists no longer mix them in. |
| Section chat | Persisted per section and replayed into the model as history, so a section still remembers a conversation weeks later. |
| Sidebar layout | Blocks **divide the sidebar's height evenly**, however many there are. |
| Opening a section | Lands on the section's **dashboard**, not on a pipeline. Execution mode is one click in, per pipeline. |
| Deleting a section | Forgets the workspace. The folder on disk and the pipelines are **left untouched** — closing a workspace must never destroy the work done inside it. |

## 4. Architecture

### 4.1 Data model (`db.py`)

Three new tables (`db.py:50`), created by the same `IF NOT EXISTS` schema script as everything
else, plus one nullable column each on `tasks` and `notes` added by the dynamic migration:

```sql
sections           (id, name, folder, brief, founding_plan_id, created_at)
section_pipelines  (section_id, plan_id, added_at)          -- a section holds many
section_chat       (id, section_id, role, content, created_at)

ALTER TABLE tasks ADD COLUMN section_id TEXT REFERENCES sections(id)
ALTER TABLE notes ADD COLUMN section_id TEXT REFERENCES sections(id)
```

`folder` is deliberately **not** a key. `get_project_name()` derives the same name from two
similar requests, so a folder can be shared; `get_section_by_folder` exists for the lookup but
takes the earliest match rather than assuming uniqueness.

**The scoping rule lives in `db.py`, not in the callers.** `get_tasks` and `search_notes` gained
`section_id` and `include_sections`:

- no `section_id`, `include_sections=False` (the default) → `section_id IS NULL`, i.e. the brain
  only;
- `section_id="abc"` → that section only;
- `include_sections=True` → everything.

Making "the brain" the default is what stops section work leaking into the main task list
without every call site having to remember to filter.

`delete_section` (`db.py`) nulls the `section_id` on tasks and notes and drops the chat and the
pipeline links — but touches neither the `pipelines` rows nor the folder.

### 4.2 Knowledge on disk (`sections.py`)

A new module that knows nothing about Gemini. Callers that want written prose pass in a
`summarise` callable; without one the summary is still assembled from what is on disk, so a dead
model call costs polish, never knowledge.

```
Let Jarvis Handle It/<Section>/
├── Section.md                          ← front page: brief, pipelines, links
├── Knowledge/
│   ├── What this section knows.md      ← the living summary, yours to edit
│   ├── pricing findings.md             ← one note per topic, additive
│   └── vram sizing rule q4.md
├── Inputs/                             ← files dropped at creation and after
├── Brief/  Implementation plan/  Task Logs/  memory/   ← the pipeline's own, untouched
```

| Function | Purpose |
|---|---|
| `write_section_note` (`sections.py:83`) | The section's front page: brief, its pipelines, a link to the summary. |
| `harvest_pipeline_memory` (`sections.py:162`) | Reads `memory/{high_value,general}/*.json`, flattens it, and writes one markdown note per topic. The JSON key becomes the topic, the filename becomes the attribution. **Additive and idempotent** — a paragraph already present is never written twice. |
| `refresh_summary` (`sections.py:291`) | Harvests, then rewrites the living summary. Falls back to a mechanically assembled version when the model is unreachable. |
| `knowledge_digest` (`sections.py:332`) | What a new pipeline starts out knowing: the brief, the summary, and the paths of every knowledge note, capped at 6000 chars. |

`summary_body` strips both the frontmatter and the file's own `# What this section knows`
heading, because that wrapper is written by `write_summary` and would otherwise be quoted back
under a heading of its own inside the digest.

### 4.3 Section focus (`coordinator.py`)

```python
ACTIVE_SECTION = None          # coordinator.py:867 — the section Jarvis is working inside
CHAT_SESSIONS = {}             # coordinator.py:872 — one chat per section, "" for the brain
```

`get_chat_session` (`coordinator.py:953`) replaces the single global `CHAT_SESSION`. When a
section is active it builds that section's session with:

- a **system prompt** of `SYSTEM_PROMPT` plus the section's identity, the scoping rules, and its
  knowledge digest;
- a **history** replayed from `section_chat`, which is what makes a section remember.

`_scope_to_section` (`coordinator.py:977`) sits in front of `perform_tool_action` and points the
database tools at the section in play:

| Tool | Inside a section | With `scope: "brain"` | With `scope: "everything"` |
|---|---|---|---|
| `add_task`, `add_note` | filed under the section | filed globally | — |
| `get_tasks`, `search_notes` | the section's own | outside any section | both |

`scope` is the model's way out, declared on the two read tools so Gemini can emit it. It is
**not** a database argument: it is translated here and popped before the call, so it never
reaches `db.py`.

`get_snapshot_local` (`jarvis.py:533`) is section-aware for the same reason — showing the brain's
task list while the user is inside a section would have Jarvis answering about work that is not
in front of them, and acting on the wrong ids.

### 4.4 Section-aware intake (`jarvis.py`)

Drafts gained a `section_id`, and three things follow from it:

1. **`_draft_project_name`** (`jarvis.py:723`) returns the section's folder instead of calling
   `get_project_name()`. Uploads, the brief, and the pipeline all land in the same place, so a
   new pipeline cannot scatter its work into a folder of its own.
2. **`_intake_context_text`** prepends the digest under a heading telling the gate that this is
   already known and must not be asked about or re-researched. This is what stops the
   clarification questions re-litigating the founding research.
3. **`_intake_brief_markdown`** appends the full digest to the brief the agents work from.

`intake_approve_route` calls `attach_pipeline_to_section` (`jarvis.py:2776`) after
`initiate_pipeline`, and `start_pipeline_local` handles the `skip_intake` path the same way.

### 4.5 Endpoints (`jarvis.py:2821` onwards)

| Route | Method | Purpose |
|---|---|---|
| `/sections` | GET | Every section as a sidebar card: name, brief, pipeline count, whether one is running, its `plan_ids`. |
| `/sections/create` | POST | `{plan_id, name, brief}` or `{draft_id}` → creates the section, harvests the founding pipeline's memory, writes the summary and section note. **The only path that creates a section.** Refuses a pipeline that already belongs to one (409). |
| `/sections/intake/start` | POST | `{plan_id, name, brief}` → opens a section draft. Refuses an unknown or already-sectioned pipeline here too, so questions are never asked about a pipeline that could not become a section. |
| `/sections/intake/upload` | POST (multipart) | The dropped files, into the section's `Inputs/` before there is a section. |
| `/sections/intake/questions` | POST | Stores the brief as written and asks for the gaps. |
| `/sections/intake/answer` | POST | One answer; hands back the next question. |
| `/sections/intake/skip` | POST | Drop the unanswered questions and write it up anyway. |
| `/sections/intake/picture` | POST | Either the section brief, or another round of questions. |
| `/sections/intake/edit` | POST | Clean the user's edit of the brief. Never creates. |
| `/sections/intake/cancel` | POST | Forget the draft and delete the files it dropped. |
| `/sections/<id>` | GET | Everything the dashboard shows: summary, knowledge notes, pipelines, tasks, notes, chat. |
| `/sections/<id>/enter` | POST | Work inside this section — scoped tools, its own remembered conversation. |
| `/sections/exit` | POST | Back out to the brain. |
| `/sections/active` | GET | Which section is in focus, if any. |
| `/sections/<id>/chat` | POST | `{text}` → persists your message, answers through the normal `handle_request` path, persists the reply. Re-enters the section on every message, so a second window left on the brain cannot steal the focus. |
| `/sections/<id>/update` | POST | Edit the name, the brief, or the living summary. Summary text is written back verbatim. |
| `/sections/<id>/refresh` | POST | Re-read the section's pipelines and rewrite what it knows. |
| `/sections/<id>/upload` | POST (multipart) | Drop files into `Inputs/`, deduped rather than overwritten. |
| `/sections/<id>/delete` | POST | Forget the section; leave the folder and pipelines alone. |
| `/section.html`, `/sections_ui.js` | GET | The dashboard page and the shared component (`jarvis.py:1637`). |

Editing or deleting a section calls `coordinator.clear_section_chat`, because a cached chat
session was built on a system prompt that is now stale.

### 4.6 The gate on the way in (`jarvis.py`)

Making a section is a commitment, and what a section is *for* is the one thing the founding
pipeline cannot tell you — so it goes through the same gate a pipeline does. You write the
brief and drop the files, Jarvis asks only what it genuinely does not know, and then paints the
section brief back for you to correct.

A **section draft** (`SECTION_DRAFTS`, `jarvis.py`) is the pipeline gate's draft with a
different shape: `plan_id`, `folder`, the typed name and brief, the files, the running Q&A, and
`brief_text` once painted. Like `INTAKE_DRAFTS` it is memory-only and swept by the same TTL,
because cancelling must leave nothing behind.

| Function | Purpose |
|---|---|
| `_section_draft_context_text` | The founding pipeline's own findings first, under an instruction never to ask about them, then the name, the brief, the files and the answers so far. |
| `_section_draft_questions` | As many as it genuinely needs, only about what the section is for and what belongs in it — never about the app's own mechanics. |
| `_section_draft_paint` | The section brief, or another round of questions. Degrades to what the user wrote when the model is unreachable, so a dead call costs polish, never the section. |
| `_section_draft_record` | Writes the whole exchange to `Brief/section_brief.md`, next to the pipeline's own clarified brief. The database keeps the painted brief; this keeps the original wording, the questions and the answers. |

**No cap on the questions.** Jarvis asks as many as it genuinely needs; what keeps the round
short is the instruction that every question must change what gets built, plus **"Skip the
rest"** on every screen. The pipeline gate's own design says the same (`No hard cap`,
`jarvis_asks_questions.md:84`) but its code had drifted to slicing the list at five — that slice
is gone, so both gates now behave the way the design describes. Both also ask again after
reading the answers, when those opened something material: `_intake_paint_picture` and
`_section_draft_paint` may return questions instead of prose, and the window loops back.

**What makes a question worth asking** is one shared rule block, `_QUESTION_RULES`, because a
bad question is bad for the same reasons in either gate. It carries a test — *if every plausible
answer leads to the same work, drop it* — and rules out the app's own mechanics entirely: no
questions about platforms, hosting, storage, tooling, file formats, naming, or how the work will
be organised. Alongside it, the section context spells out **what a section already is** (it
lives in this app, in the founding pipeline's folder, holding its pipelines, tasks, notes and
chat). Without that, the gate asked things like *"what platform will host this workspace?"* —
which this app answered long before the user was ever shown a question.

Three things are shared with the pipeline gate rather than copied: `_ask_model_json` (one
model call: instruction, context, readable files), `_normalise_questions`, and
`_clean_edited_text`. The gates differ only in what they put in the context, which is the
whole point of splitting them there.

**What the questions are shown.** `sections.pipeline_material` reads the pipeline's
`memory/*.json` and any existing knowledge notes straight off disk. It deliberately does *not*
call `harvest_pipeline_memory` or `refresh_summary`: those write into the folder, and nothing
may be written until Create section is pressed.

**Where the dropped files go.** Straight into the section's `Inputs/`, not a staging area —
Jarvis has to read them to ask about them, and creating the section then moves nothing.
Cancelling deletes exactly the files that draft uploaded and never a directory, because the
folder is full of the founding pipeline's work.

## 5. Frontend

### 5.1 `sections_ui.js` — the shared component

One file, included by `command_center.html`, `execution.html` and `plan.html`. Those are three
standalone documents; triplicating this would guarantee they drift apart. It injects its own
styles, mounts a **View Sections** button into any `.top-right-nav` it finds, and exposes:

```js
JarvisSections.openSidebar()
JarvisSections.createFrom(planId, suggestedName)
```

**The sidebar.** 380px, right, over a scrim. The list is a flex column and every block is
`flex: 1 1 0; min-height: 0` — so however many sections there are, they divide the height evenly,
as ruled. A section with a running pipeline shows a pulsing green dot.

**The create window.** Near-full-width and tall, matching the intake modal's shape, and staged
like it: the head, body and footer are redrawn per stage rather than being one fixed form.

| Stage | What it shows | Footer |
|---|---|---|
| Brief | Section name (pre-filled from the project), the brief, the drop zone | Cancel · Create section without questions · **Continue** |
| Questions | One question at a time, with a counter, read aloud through `/jarvis/say` | Cancel · Skip the rest · **Continue** |
| Brief text | The painted brief, rendered as prose; **Edit** swaps in a textarea | Cancel · Edit · **Create section** |

Every stage's footer is rebuilt from scratch, which is what retired the old trick of replacing
the Create button with a clone of itself to shed a listener from a previous pipeline. The
whole draft — including which pipeline it belongs to — lives in one `draft` object cleared on
close, so nothing from one opening can reach the next.

Files stay client-side until **Continue**, then go up with the draft so Jarvis can read them
while asking. On the skip path there is no draft, so they are uploaded after creation exactly
as before. **Cancel** and Escape both discard the draft server-side, which deletes the files it
uploaded; a *created* draft is retired without deleting anything, since its files now belong to
the section.

While an answer is in flight the footer is disabled: the screen does not change until the next
question is drawn, and a second press would otherwise file the same question twice — which it
did, the first time this was driven through a browser.

### 5.2 `section.html` — the dashboard

Fixed header, scrolling main column, and the section's conversation pinned down the right.
Cards: what this section knows (with **Re-read pipelines** and **Edit**), pipelines, knowledge
notes, tasks, notes.

Clicking a pipeline sets `localStorage.jarvis_active_pipeline_id` and navigates to
`execution.html` — which already reads exactly that key (`execution.html:778`), so the section's
view of a pipeline *is* execution mode, with no second implementation of it.

The summary is rendered by a small in-page markdown pass (headings, bullets, `**bold**`,
`` `code` ``, `[[wikilinks]]`) that escapes its input first — it is a document you read, not
source you should have to parse. **Edit** swaps it for a textarea holding the raw text.

### 5.3 `plan.html` — the entry point

Completed plan cards carry a **+ Make section** badge. `refreshSectionPlanIds()` runs before
each `fetchPlans()` so a pipeline already inside a section is not offered again; a failed lookup
is logged and ignored rather than hiding the plans, the worst case being a button the server
then refuses with a 409.

The handler is attached in JS rather than inlined in `onclick`. It was inlined at first, and a
project name interpolated through `JSON.stringify` closed the double-quoted attribute early —
the click fell through and opened the plan detail instead of the window.

## 6. What a section hands the next pipeline

```markdown
# Section context — Cyprus Car Rental

## What this section is about
Turning the Limassol rental research into an actual booking business.

## What this section already knows
<the living summary>

## Knowledge notes available in full
- `Let Jarvis Handle It/Cyprus Car Rental/Knowledge/pricing findings.md`
- ...

## How to use this
This is standing knowledge from earlier work in this section. Treat it as already
established — do not research it again from scratch. Where you need more than the
summary carries, open the knowledge notes listed above; they are plain markdown on
disk. Build on this rather than repeating it.
```

## 7. Why Obsidian, and why it is not a dependency

Obsidian is installed on this machine with a vault at `D:\Charalambos\Desktop\AI\AI Memory`.
The question asked during clarification was whether to use it for section memory. The answer
that survives scrutiny:

- A vault is **just a folder of markdown**. Obsidian is a human UI over it — backlinks, graph,
  search, canvas. There is no server or API in the base app.
- So Obsidian gives the **agents** nothing that plain markdown does not. The retrieval win comes
  from the *format* — topic-named notes, frontmatter, wikilinks — not from the application.
- What it gives is **you**: somewhere to read and correct the living summary, which is exactly
  what the summary is for.

Hence: write the vault format, take no dependency. The `filesystem` MCP server in
`mcp_registry.json` is already pointed at `./Let Jarvis Handle It`, so agents can read section
knowledge with nothing new enabled. The two heavier options — the Local REST API plugin, or an
Obsidian MCP server — both add the failure mode that Obsidian must be running, and mostly wrap
file access that is already free.

**One caveat.** The existing `AI Memory` vault was built with `obsidian-importer` and holds a
*copy* of this repo's markdown (675 files under `Jarvis/second-brain-voice/`). If sections write
into the repo while snapshots keep being imported, there will be two divergent truths. Point a
vault **at** the live folder rather than importing copies of it.

If retrieval later needs to be semantic, that is an embedding index over the same markdown and
is unaffected by any of this.

## 8. Testing

`scripts/test_sections.py` — 80 assertions against the Flask test client, on a throwaway
database and a throwaway project folder, self-cleaning and repeatable back to back. The model,
the agents and the speech are all stubbed.

Covered: creation from a finished pipeline and its three refusals; the founding pipeline's JSON
becoming markdown with frontmatter, and re-harvesting not duplicating it; the sidebar listing;
the dashboard payload; tasks and notes staying inside a section and out of the brain; the
scoping table in §4.3 including `scope` never reaching `db.py`; the snapshot following the
focus; chat persisting in order with both sides; a new pipeline inheriting the folder, the
digest and the section's own note paths, and being filed under the section on approval; the
summary and brief edit round-trips; and deletion leaving the folder, the pipelines and the tasks
intact.

The gate on the way in has its own pass: its two refusals; that the questions round is shown the
founding pipeline's findings, the words the user wrote and the file they dropped, under the
instruction never to re-ask them; that answering ends the round and reaches the write-up; that
the painted brief is editable; that a dropped file exists before creation, is deleted by
cancelling, survives creation, and that one draft's cancellation leaves another's file alone;
that no section exists until Create section; that the brief stored is the corrected one; that
the questions and answers are recorded in `Brief/section_brief.md` with the user's own wording
kept; and that a spent draft cannot create a second section.

The model is stubbed at `_ask_model_json` — the single call both gates go through — and the
contexts it was handed are kept, which is what lets the tests assert what Jarvis was actually
shown rather than only what it replied.

The stub for `initiate_pipeline` writes a `pipelines` row, because the real one does and the
section's listing reads pipelines back out of that table — without it the section looked empty
for entirely the wrong reason.

Verified by hand in a browser against a seeded server: the sidebar's even-split blocks, the
dashboard, the rendered summary, the create window, and the **+ Make section** badge appearing
only on completed pipelines. The gate was driven end to end the same way — brief, both
questions, the painted brief, Edit, Create section — which is how the double-press bug in
§5.1 was found.

`scripts/test_intake.py` was repaired as part of this work and is green again at 56 assertions,
so the intake suite does guard the changes in §4.4. It had been written against a fake
`google.genai` exposing a `FAKE` dict, which is not in the repo and is not something the real
SDK provides, so it died on import before running a single check. The stub now lives in the test
itself: one fake client standing in for `jarvis.client`, which is the only route the intake code
takes to a model. No production code was changed to make it pass.

## 9. Not built

- **Retroactive knowledge for old sections beyond the founding pipeline.** `refresh_section_knowledge`
  reads whatever is in the folder's `memory/` at the time; pipelines that wrote nothing there
  contribute nothing.
- **Moving a pipeline between sections**, or a section that spans more than one folder.
