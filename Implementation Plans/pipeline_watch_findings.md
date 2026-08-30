# Pipeline Watch Findings — To Fix Later

### [NOT FIXED — precise root cause found, earlier fix was incomplete] Conflict-triggered cycle retries grab the wrong cycle entirely
- **What happened:** Plan 13's Cycle 3 retry-1 (triggered by a synthesis
  conflict) spawned agents named `breakthrough_definition_strategist_cycle1_lead`
  / `arxiv_search_specialist_cycle1_adv_1` — the mislabeling symptom I
  thought I'd fixed with `_normalize_cycle_agent_ids()` in `agents/brain.py`.
- **Real root cause (more precise than the earlier entry above):**
  `multi_agent_coordinator.py:749`, the **conflict**-triggered retry branch,
  does `cycle.update(updated_cycles[0])` — grabs whichever cycle is FIRST in
  Brain's returned array, with **no `cycle_id` matching at all**. If Brain's
  re-plan response still includes multiple cycles (very plausible — the
  "re-plan ONLY Cycle N" instruction is just prose in `user_input`, easily
  not followed strictly), index `[0]` is often actually cycle 1's real,
  correctly-labeled data, which then overwrites cycle 3's slot wholesale —
  not just the label, the entire cycle content. The **gate-rejection** retry
  branch a few lines below (`multi_agent_coordinator.py:804-808`) already
  does this correctly: `matching_cycle = next((c for c in updated_cycles if
  c.get("cycle_id") == cycle_id), None)` first, falling back to `[0]` only
  if no match. Every instance of this bug observed all session (plan 11
  Cycle 1/3 retries, plan 12 Cycle 2 retry, plan 13 Cycle 3 retry) was a
  **conflict** retry, never a gate-rejection retry — consistent with this
  being the one broken branch.
- **Why my earlier brain.py fix didn't catch this:** `_normalize_cycle_agent_ids()`
  only fixes each cycle's own internal suffix-vs-cycle_id consistency; it
  can't detect "the wrong cycle object was selected from the array
  entirely" — from `build_agent_plan`'s perspective, the cycle at index 0
  legitimately says `cycle_id: 1`, so there's nothing to normalize.
- **Proposed fix:** mirror the gate-rejection branch's matching logic at
  line 749 — `matching_cycle = next((c for c in updated_cycles if
  c.get("cycle_id") == cycle_id), None); cycle.update(matching_cycle or
  updated_cycles[0])`.
- **Status:** Not fixed yet — found live, mid-session, then user redirected
  focus to a new pipeline (14) before applying it.


### [FIXED] Gate approval could resolve the wrong pipeline's gate when multiple plans run concurrently
- **What happened:** With plan 12 and plan 13 both waiting on separate gates
  at the same time, clicking Approve (intending it for one plan) approved
  the other instead.
- **Root cause:** Two bugs stacked. (1) `jarvis.py` kept a single global
  `PIPELINE_STATE` dict — every pipeline thread's `gate_fn` polled the same
  shared slot, and `/gate/approve`/`/gate/reject` took no `plan_id` at all,
  so they always resolved whichever gate happened to occupy that one slot.
  (2) `plan.html`'s `toggleMainView()` picked `plansCache.find(p => p.status
  === 'running')` — literally "whichever plan is first in the list" —
  regardless of which one the user meant.
- **Fix:** `PIPELINE_STATE` → `PIPELINE_STATES` keyed by `plan_id`, with each
  pipeline thread's `gate_fn` polling only its own entry. `/gate/approve` and
  `/gate/reject` now require `plan_id` (400 error without it). `plan.html`'s
  `submitApproval()` sends `plan_id: selectedPlanId`; `toggleMainView()` now
  prefers whichever plan is genuinely gate-waiting, keeping the current
  selection if it's among them, instead of "first running."
- **Verified live:** ran plan 12 and plan 13 concurrently, both waiting on
  separate gates simultaneously; approved plan 13's gate and confirmed plan
  12's gate stayed untouched (`api_mcp_plugging`/`waiting`, unchanged).


Live-observed issues found while watching real pipeline runs, not yet fixed.
Each entry: what happened, root cause, proposed fix, where.

---

## Session: 2026-08-30, watching plans 11 and 12

### [FIXED live during this session] Tool `tools_needed` name mismatches
- **What happened:** Execution/research agents whose brief listed a tool as
  `"arxiv_search_api"` (or other Brain-invented phrasings) got zero real
  tools bound, fell back to a blind LLM call, and self-reported fabricated
  "API access failure" narratives that then poisoned the whole blueprint.
- **Root cause:** `agents/tool_executor.py`'s `TOOL_ALIASES` was an
  exact-match table; Brain is an LLM and phrases the same tool differently
  almost every run (`arxiv_api`, `arxiv_search`, `arxiv_search_api`,
  `arxiv_api_client` have all been observed for the same capability).
- **Fix applied:** Added `_resolve_tool_key()` — exact alias lookup first,
  then a keyword-based fuzzy fallback (`"arxiv" in key` → `arxiv_api`,
  `"search"/"web"/"internet"/"browse" in key` → `google_search`). Verified
  live against `arxiv_search_api` and a made-up `"ARXIV Data Retriever"`.
  Confirmed `google_docs_api`/`google_drive_api` still correctly report
  unavailable (no false positive from the fuzzy match).
- **Status:** Done. Not re-verified end-to-end on a pipeline that actually
  re-runs execution fresh (both resumes tested afterward hit the
  stale-resume issue below instead).

### [FIXED] Resuming a plan whose `phase` is already `qa`/`deploy`/`complete` replays stale exec_results instead of re-running
- **What happened:** Resumed plan 9 and plan 11 both skipped execution
  entirely and replayed old, pre-fix `exec_results` straight to the
  `final_qa` gate, because `multi_agent_coordinator.py`'s `run_full_pipeline`
  checks `existing_plan.get("phase") in ('qa','deploy','complete')` and
  treats execution as already-done. There's no way from the UI to say "no,
  actually re-run execution" for a plan already past that checkpoint.
- **Where:** `multi_agent_coordinator.py`, the `skip_execution` block (~line
  977) inside `run_full_pipeline`.
- **Proposed fix:** Add a `force_reexecute` flag threaded through
  `resume_pipeline` (UI button + `/pipeline/resume` route) that, when set,
  ignores the phase check and re-runs `run_execution_phase` fresh instead of
  reusing `existing_plan.get("exec_results")`.
- **Status:** Fixed. Added `force_reexecute` param threaded through
  `run_full_pipeline` (multi_agent_coordinator.py) → `resume_pipeline_local`
  + `POST /pipeline/resume` (jarvis.py) → a "↻ Re-execute" badge on any
  plan card already past execution (plan.html), which confirms before
  discarding the plan's current exec_results. Verified live: force-resumed
  plan 9 (stale exec_results from before any fixes existed) and confirmed
  it genuinely re-spawned execution agents fresh instead of skipping to
  final_qa with old data.

### [FIXED] Brain reuses literal agent_ids across cycles on conflict retry, causing an actual collision
- **What happened:** On every conflict-triggered retry observed (plan 11
  Cycle 1 retry, plan 11 Cycle 3 retry, plan 12 Cycle 2 retry), Brain's
  re-plan for the retried cycle emits agent_ids suffixed `_cycle1_lead` /
  `_cycle1_adv_N` regardless of which cycle is actually being retried.
  Initially assessed this as cosmetic-only. **Verified it is not**: in plan
  12, checked `Task Logs/pipeline_12.md` directly — `fusion_energy_researcher_cycle1_lead`
  and `arxiv_search_strategist_cycle1_adv_1` are the literal agent_ids of
  **Cycle 1's real agents** (spawned 02:22:40). Brain's retry of **Cycle 2**
  then reused those *exact same* agent_ids for its retry agents (respawned
  02:26:03) — a genuine string collision within the same pipeline run, not
  just a confusing label.
- **Actual impact:** Pipeline *logic* still completes correctly — each
  cycle's `agent_results` list lives in its own local scope in
  `run_research_phase_for_cycle`/`run_full_pipeline`, never merged globally
  by agent_id. But `jarvis.py`'s `AGENT_REGISTRY`, `AGENT_CONVERSATION_LOGS`,
  and `AGENT_THINKING_LOGS` (used by execution.html's constellation panels
  and agent_talk.task_log.html's Chat/Thoughts tabs) are all keyed globally
  by `agent_id`. Cycle 2's retry silently overwrote Cycle 1's registry entry
  — opening that agent's detail panel or chat history now shows merged/wrong
  data from two different agent instances treated as one.
- **Root cause:** `agents/brain.py`'s `BRAIN_SYSTEM_PROMPT` only ever shows
  `cycle1` in its illustrative examples (`brand_strategist_cycle1_lead`,
  `competitor_analyst_cycle1_adv_1`). Confirmed via grep — no code anywhere
  parses a cycle number back out of an agent_id string, so this is purely an
  LLM-generation artifact: when Brain re-plans a single cycle via
  `redirect_note`/`cycle_id` (`build_agent_plan`'s `cycle_id is not None`
  branch), it pattern-matches the prompt's example literally instead of
  substituting the actual cycle number.
- **Proposed fix:** In `agents/brain.py`, when `cycle_id is not None`,
  explicitly instruct: "agent_id suffixes MUST use `cycle{cycle_id}`, i.e.
  `_cycle{cycle_id}_lead` / `_cycle{cycle_id}_adv_N` — never default to
  cycle1." Belt-and-suspenders: post-process the returned JSON in
  `build_agent_plan` to rewrite/guarantee-unique the suffix server-side
  (e.g. append cycle_id + a short random/incrementing tag) rather than
  trusting the LLM to get it right, since collisions corrupt observability
  state silently with no error raised anywhere.
- **Status:** Fixed, belt-and-suspenders as proposed. Added an explicit
  "this is Cycle {cycle_id}, not Cycle 1" instruction to the re-plan prompt
  in `build_agent_plan` (defense in depth, not trusted alone). Primary fix:
  `_normalize_cycle_agent_ids()` in `agents/brain.py` deterministically
  rewrites every agent_id's `_cycleN_` segment to match its own cycle's real
  `cycle_id` field after every `build_agent_plan` call (fresh plans and
  re-plans alike) — no-op if Brain already got it right. Unit-tested against
  the exact collision recorded above (plan 12's `fusion_energy_researcher_cycle1_lead`
  reused for Cycle 2): confirmed it now rewrites to `_cycle2_lead` correctly
  while leaving Cycle 1's real entry untouched.
- **Cross-checked against plan 11:** same root cause fired (Cycle 3 retry
  labeled `research_strategist_cycle1_lead` etc.), but plan 11's actual
  Cycle 1 agent_ids (`arxiv_research_specialist_cycle1_lead`,
  `keyword_strategist_cycle1_adv_1`, `data_filter_analyst_cycle1_adv_2`)
  didn't happen to match, so no real collision occurred there — cosmetic
  only in that run. Confirms severity is non-deterministic: same bug,
  sometimes harmless (plan 11), sometimes a real collision (plan 12, where
  Brain also reused a matching role name). Same fix covers both cases.

### [FIXED] `agents/execution_agent.py` can crash on an empty Gemini response mid tool-calling loop
- **What happened:** Plan 7's `agent_exec_1` ("Technical Report Writer")
  died with `status: "error"`, `error: "Expecting value: line 1 column 1
  (char 0)"` — a `json.loads("")` crash.
- **Root cause:** In the tool-calling loop, when a turn returns no
  `function_calls`, the code does `final_text = response.text or ""`. If
  Gemini's `.text` is genuinely empty on that turn (observed in practice —
  not just a hypothetical), `final_text` becomes `""`, which is **not**
  `None`, so the existing `if final_text is None: final_text = "{}"` guard
  never triggers, and `json.loads(cleaned)` blows up on the empty string.
- **Already fixed in the sibling file:** `agents/research_agent.py` (which I
  rewrote using the same loop) has `if not cleaned: cleaned = "{}"` right
  before the `json.loads(cleaned)` call — that guard covers both `None` and
  `""`. `agents/execution_agent.py` was written first, before I added that
  extra guard, and never got the same line added.
- **Proposed fix:** In `agents/execution_agent.py`, add `if not cleaned:
  cleaned = "{}"` immediately before `result = json.loads(cleaned)` (~line
  187), matching `research_agent.py`'s handling exactly.
- **Status:** Not fixed. One-line fix, low effort, real crash risk (kills
  that execution agent's attempt entirely instead of degrading to a
  self-reported "partial").
- **Reproduced live:** happened again minutes later in plan 12 —
  `report_compiler_exec_1`'s `response_received` content was a literal empty
  string, confirmed via `/api/agent_conversations`. Not a rare edge case;
  recurs readily within the tool-calling loop. Raises priority — worth
  fixing alongside the alias work rather than leaving for later.
- **Confirmed as the sole blocker for plan 12's completion:** `report_compiler_exec_1`
  hit this exact crash on all 3 QA retries and exhausted `MAX_RETRIES`,
  escalating to human. Everything else in this run worked correctly — Cycle
  1-3 research all resolved clean (with real tool calls throughout),
  `arxiv_search`/`arxiv_search_api` naming both resolved fine (fuzzy-alias
  fix holding), and `google_doc_uploader_exec_1` degraded gracefully,
  writing a real fallback file itself (`Deliverables/fusion_energy_breakthroughs_report.txt`,
  confirmed on disk) once it saw the compiler had nothing usable. This one
  crash is the single thing standing between this pipeline and a genuinely
  complete run — worth fixing first.
- **Fixed:** added `if not cleaned: cleaned = "{}"` in `agents/execution_agent.py`
  immediately before `json.loads(cleaned)` (~line 187), matching
  `research_agent.py` exactly. Empty responses now parse to `{}` → get
  `agent_id`/`status: "ok"` defaulted on, then fail the Quality Checker's
  normal `required_keys` validation and retry cleanly through the existing
  QA loop, instead of throwing an uncaught JSON-decode exception. Syntax
  verified, restarted live. Not yet re-observed end-to-end on a run that
  happens to trigger the empty-response case again (can't force Gemini to
  reproduce it on demand) — but the failure path is now provably the same
  shape as `research_agent.py`'s, which has not exhibited this crash.
