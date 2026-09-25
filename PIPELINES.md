# How a Jarvis pipeline works

A pipeline turns one request ("research my competitors and write me a report") into
research, a plan, and finished deliverables. It is driven by `run_full_pipeline` in
`multi_agent_coordinator.py` and stops at a gate every time a person has to decide.

Everything a pipeline writes lives in `Let Jarvis Handle It/<project>/`. Every step
also sends events to the pages (`plan.html`, `execution.html`), so a run can be
watched live.

## The stages at a glance

| # | Stage | Who does it | Stops for you? |
|---|-------|-------------|----------------|
| 0 | Intake: clarify the request | Jarvis (`jarvis.py`, `/pipeline/intake/*`) | Yes: answer questions, approve the brief |
| 1 | Plan the research cycles | Brain | No |
| 2 | Research, one cycle at a time | Research agents, then the lead's review, synthesis and the brief check | Yes: one gate per cycle |
| 3 | Master blueprint | Master synthesis | No |
| 4 | Plan the execution agents | Brain | No |
| 5 | Execution blueprint gate | You | Yes |
| 5.5 | API/MCP plugging gate | You | Yes |
| 6 | Build the deliverables | Execution agents, then the quality checker | No, unless it gives up |
| 7 | Final QA gate | You | Yes |
| 8 | Deploy | Deployment agent | No |

Every retry loop (a conflict, a rejected gate, a failed quality check) runs at most
`MAX_RETRIES` (3) times. After that the pipeline stops as `escalated_to_human`.

## Stage by stage

### 0. Intake
Before anything runs, Jarvis asks the questions it needs answered and lets you attach
files. It then describes the job back to you. You can edit that description before
approving it. Only approving it creates a pipeline. The result is saved as
`Brief/clarified_brief.md`, and every agent is later shown it word for word
(`agents/user_brief.py`). Where an agent's own one-line brief and your words
disagree, your words win.

### 1. Plan the research cycles (Brain)
`agents/brain.py` → `build_agent_plan`. The Brain splits the research into as many
**cycles** as the job needs. Each cycle is one step with one outcome, such as
"choose the niche" or "find 20 competitors per country". Cycles are put in order
(`order_cycles`), and each one names the earlier cycles it depends on. Each cycle has
**one lead specialist and at least one advisor**. The Brain also drafts a list of
execution agents, but that is only a draft; it is planned again in stage 4.

Rules that matter: agents in one cycle run **at the same time**, so no brief may
depend on another agent in the same cycle. A choice other agents need must be made in
an earlier cycle. Your counts, places and categories must be carried into the cycles
that have to meet them.

Saved as `Implementation plan/Agents/agent_plan_<id>.md`.

### 2. Research, one cycle at a time
For each cycle, in order:

1. **Refresh the briefs.** `refresh_cycle_briefs` rewrites this cycle's briefs using
   what the approved earlier cycles actually found (real company names, the chosen
   countries), instead of the Brain's guesses from before any research existed.
2. **Research agents run in parallel.** `agents/research_agent.py`. Each agent gets
   real tools: `web_search` (Gemini-grounded, returns real links), `arxiv_search`,
   `inspect_website` (a real browser: fonts, colours, CTAs and screenshots), file
   read/write, and memory search. It can also ask you a question (`ask_user`), ask
   for a tool it lacks (`request_tool`), and is paused for your OK every 6 rounds of
   tool calls (`tool_review`). It may only cite links a tool returned. Made-up links
   are removed and lower its confidence (`agents/links.py`). An agent that answers
   without using any tool is sent back once to do real research. Each result is
   saved as `research_<agent>_<id>.md`. Earlier attempts are kept as `_attemptN`
   instead of being overwritten.
3. **Lead review.** `run_lead_review`. The lead reads its own findings and the
   advisors' findings and writes the cycle's authoritative output. It weighs its own
   work like any advisor's and must list what it leaves out.
4. **Synthesis.** `agents/synthesis.py` → `run_synthesis_agent`. This compresses the
   cycle into a **cycle blueprint** and looks for conflicts, with every agent's
   original findings in view. It may not add facts or "correct" measured values. A
   disagreement that the evidence doesn't settle is marked unresolved, for you to
   decide at the gate.
   - A conflict that **needs new research** reruns **only the agents involved**, with
     briefs rewritten to settle it (`rebrief_for_conflict`). The team stays the
     same, and the other agents' findings are kept.
   - A conflict that can be settled from what is already known, or that is about
     what the review did, goes to the gate instead of back to research.
5. **Brief check.** `quality_checker.check_research_against_brief` compares the
   cycle with your brief and with the whole plan. It flags dropped scope, loosened
   criteria and failed agents. It never blocks; it tells you before you decide.
6. **Cycle gate.** `cycle_<n>_research` shows the blueprint (the "Open Cycle N
   blueprint" button), the brief check and the unresolved disagreements. The review
   page also has **Detailed plan** and **Findings so far** for every agent.
   - **Approve:** the blueprint is saved, marked approved on disk, and passed to
     every later cycle as context.
   - **Reject with a note:** the note is written into your brief as a change
     (`agents/brief_changes.py`), so every agent sees it. Then only this cycle is
     re-planned and rerun.

Saved as `cycle_blueprint_<n>_<id>.md`, plus per-agent memory under `memory/`.

### 3. Master blueprint
`run_master_synthesis` merges every approved cycle blueprint into one master
research blueprint. The tools recommended during research are attached to it. Saved
as `Implementation plan/Final Plans/master_blueprint_<id>.md`.

### 4. Plan the execution agents (Brain)
`plan_execution_agents` plans the builders from the finished research. There is one
single-purpose agent per deliverable in your brief (a report, a Google Doc, a
website...). Each gets an `output_spec` (required keys and word count) and the tools
it needs.

### 5. Execution blueprint gate
You see the master blueprint and the execution agents. If you reject it, only the
execution plan is redone. The research you approved is kept.

### 5.5 API/MCP plugging gate
This lists only the services the execution agents actually need, and whether each is
connected (`plan_tool_requirements`, `describe_connectable`). The research's own tool
suggestions are shown as reading material and never block. If you don't confirm,
the pipeline stops as `blocked`.

### 6. Build and check
`agents/execution_agent.py`. The execution agents run in parallel with real tools.
They write files into `Deliverables/`, create Google Docs, and can hand coding jobs to
the Antigravity CLI (`delegate_build`). Then `run_quality_checker` checks each output
against its spec, the master blueprint and your brief. Agents that fail are rerun, up
to 3 times, and then the pipeline escalates to you.

### 7. Final QA gate
You review the deliverables. If you reject them, only the agents your note (or the
steps you ticked) points at are rerun, with your note attached.

### 8. Deploy
`agents/deployment_agent.py` records what was produced and the real artifacts the
agents created. Publishing to outside services (YouTube, GitHub, Stripe...) is still a
stub. A final report is saved as `final_report_<id>.md`.

## Resuming and moving between PCs
A pipeline can be closed at any point and resumed. Approved cycles are skipped, and
unapproved research saved before a restart is reused. Finished stages are worked out
from what is on disk (`derive_completed_stages`). **↻ Re-execute** (`force_reexecute`)
reruns execution even if a stage says it is done. `pipeline_transfer.py` exports a
pipeline's database row into its project folder, so the folder can be carried to
another PC and imported when Jarvis starts.

## The agents, in one line each

| Agent | File | Job |
|-------|------|-----|
| Brain | `agents/brain.py` | Plans the cycles, refreshes briefs between cycles, re-plans after a rejection or conflict, and plans the execution agents |
| Research agent (lead / advisor) | `agents/research_agent.py` | Researches one focused brief with real tools and returns findings, sources and confidence |
| Lead review | `multi_agent_coordinator.py` → `run_lead_review` | Merges its cycle's findings into the authoritative output |
| Synthesis | `agents/synthesis.py` | Writes cycle blueprints, finds and sorts conflicts, and builds the master blueprint |
| Brief check | `agents/quality_checker.py` → `check_research_against_brief` | Compares each cycle with your brief before the gate |
| Execution agent | `agents/execution_agent.py` | Builds one deliverable from the master blueprint |
| Quality checker | `agents/quality_checker.py` → `run_quality_checker` | Passes or fails each deliverable against its spec and your brief |
| Deployment agent | `agents/deployment_agent.py` | Records and (eventually) publishes the results |
| Brief changes | `agents/brief_changes.py` | Turns a rejection note into a change to your brief, keeping everything it doesn't touch |

Helpers every agent shares: `tool_executor.py` (which tools an agent gets),
`website_inspector.py`, `links.py`, `tool_requests.py`, `tool_review.py`,
`agent_questions.py` and `user_brief.py`.

## Known gaps
- Agents rerun after a Final QA rejection are not checked by the quality checker
  again before the gate reopens.
- Deployment to outside services is a stub.
- The pipeline always starts with research. Tasks that need no research still go
  through at least one cycle.
