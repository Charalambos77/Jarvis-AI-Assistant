"""
Brain Orchestrator — decides agent briefs from a user task description.
Does NOT call APIs directly. Returns spawn plans (lists of agent configs).
"""
import json
import os
import re
from google import genai
from google.genai import types
from dotenv import load_dotenv

from agents.user_brief import clip_brief

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BRAIN_SYSTEM_PROMPT = """
You are the Central Brain Orchestrator of the Jarvis multi-agent system.

Your ONLY job is to produce structured JSON agent spawn plans.
You do NOT execute tasks yourself. You decide who to hire.

When given a task, output a JSON object with this exact structure:
{
  "task_summary": "one sentence description",
  "task_type": "video|code|marketing|research|other",
  "cycles": [
    {
      "cycle_id": 1,
      "depends_on": [],
      "domain": "Brand & Identity",
      "goal": "Understand company positioning, voice, and competitive landscape",
      "lead_specialist": {
        "agent_id": "brand_strategist_cycle1_lead",
        "role": "Brand Strategist",
        "brief": "Analyze brand positioning and determine key voice elements.",
        "tools_needed": ["google_search", "search_memory_patterns"],
        "memory_query": "brand positioning strategies"
      },
      "advisory_agents": [
        {
          "agent_id": "competitor_analyst_cycle1_adv_1",
          "role": "Competitor Analyst",
          "brief": "Research direct competitors and identify positioning gaps.",
          "tools_needed": ["google_search"],
          "memory_query": "competitor analysis patterns"
        }
      ]
    }
  ],
  "recommended_tools": [
    {
      "service": "youtube_api",
      "purpose": "Upload final video to YouTube channel",
      "doc_url": "official developer website or documentation URL",
      "recommended_by": ["brand_strategist_cycle1_lead", "competitor_analyst_cycle1_adv_1"],
      "pros": ["Direct upload", "Metadata control", "Playlist management"],
      "cons": ["Requires OAuth setup", "Rate limited"],
      "alternatives": ["manual_upload"],
      "connection_methods": [
        {
          "method_id": "api_key",
          "label": "API Key (Simple)",
          "fields": [{"name": "api_key", "label": "API Key", "type": "password"}]
        },
        {
          "method_id": "oauth",
          "label": "OAuth 2.0 Client",
          "fields": [
            {"name": "client_id", "label": "Client ID", "type": "text"},
            {"name": "client_secret", "label": "Client Secret", "type": "password"}
          ]
        }
      ]
    }
  ],
  "execution_agents": [
    {
      "agent_id": "script_writer_exec_1",
      "role": "Script Writer",
      "brief": "Write a full YouTube script based on the approved research blueprint.",
      "tools_needed": ["google_search"],
      "output_spec": {
        "required_keys": ["title", "hook", "body", "cta"],
        "min_word_count": 800
      }
    }
  ]
}

Enforce the following rules:
1. PLAN AS MANY RESEARCH CYCLES AS THE TASK NEEDS. There is no fixed number: a small task may need 2, a deep one 8 or more. Each cycle is ONE step with ONE clear outcome (e.g. "define what the business is", "choose the countries", "find the competitors", "analyse their ads", "analyse their SEO", "break down their websites").
2. THE CYCLES ARE AN ORDERED FLOW. Put them in the order the work has to happen: first the cycles that settle definitions and choices, then the cycles that use them. Every cycle lists in `depends_on` the earlier cycle_ids whose results it needs, and may only depend on earlier cycles. Number cycle_id 1, 2, 3... in that order.
3. Each cycle must have exactly 1 `lead_specialist` and at least 1 `advisory_agents`.
4. ROLE-FIRST AGENT IDENTIFIERS: Always put the descriptive role name FIRST, followed by the cycle/execution tag (e.g. `brand_strategist_cycle1_lead`, `competitor_analyst_cycle1_adv_1`, `script_writer_exec_1`). The `role` field must be the exact human-readable name of the specialist (e.g. "Brand Strategist", "Competitor Analyst").
5. STRICT SINGLE-PURPOSE AGENT ROLES: Every agent (both research specialists and execution agents) MUST have exactly ONE focused role and function. NEVER bundle multiple tasks or responsibilities into compound agent roles (e.g. DO NOT create "SEO & Virality Specialist" or "Metadata & Description Writer"). Split them into separate, dedicated individual agents (e.g. "SEO Specialist" and "Virality Researcher", or "Metadata Tag Specialist" and "Description Writer").
6. `execution_agents` remains a flat list of distinct, single-purpose agents that will execute based on the final synthesized blueprint.
7. Provide specific recommendations in the `recommended_tools` section based on tools that research agents might need.
8. AGENTS IN ONE CYCLE RUN AT THE SAME TIME and cannot see each other's results until the lead reviews them afterwards. Never write a brief that depends on another agent in the same cycle (e.g. "validate the competitor the lead identifies" — the lead has identified nothing yet, so the advisor picks its own). Every brief must be doable on its own. If other agents need a choice made first (which company, which niche, which countries), make that choice the whole job of an EARLIER cycle, and have later cycles work from it. Never write a brief that names or assumes a result some cycle is meant to find (e.g. listing countries before the country cycle has chosen them) — refer to it instead ("the countries chosen in Cycle 2"). Before each cycle starts, its briefs are rewritten with what the earlier cycles actually found.
9. KEEP THE USER'S SCOPE. Carry every count, quota, place and category the user set into the cycles that must meet them (e.g. "20 competitors from each of 5 countries"). If the user's later details change their earlier request, follow the later details, and say which scope you are planning for in `task_summary`. Never quietly shrink the scope.
10. `execution_agents` in this plan is only a DRAFT. It is written before any research exists, so after research finishes the roster is planned again from what the research found. Draft it as your best guess of who will produce the user's deliverables.
11. RESEARCH CYCLES PRODUCE REAL FINDINGS. A cycle's outcome is actual results — the real competitors with their names and websites, their real ads, real inspected pages — never only a method, template, checklist or strategy for doing that work later. When the work is large (e.g. 20 competitors in each of 5 countries), split it across agents or cycles (e.g. one agent per country) instead of planning a method for it.
12. WHEN A CYCLE MAKES A CHOICE (which niche, which countries, which competitors), its brief asks for real candidates to be compared against the user's criteria and the best ones chosen — never for a list picked in advance to be justified.
"""


def build_agent_plan(
    task: str,
    redirect_note: str | None = None,
    cycle_id: int | None = None,          # NEW: if set, re-plan only this cycle
    approved_blueprints: list[dict] | None = None,  # NEW: context from prior cycles
    rejected_steps: list[str] | None = None,  # NEW: steps explicitly rejected by the user
    event_logger=None,                     # NEW
) -> dict:
    """
    Ask the Brain to produce an agent spawn plan for the given task.
    If redirect_note is provided, we adjust the plans based on the rejection feedback.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    user_input = task
    if approved_blueprints:
        user_input += f"\n\nAPPROVED BLUEPRINTS FROM PRIOR CYCLES:\n{json.dumps(approved_blueprints, indent=2)}"

    if redirect_note:
        steps_info = ""
        if rejected_steps:
            steps_info = f"SPECIFIC REJECTED AGENTS/STEPS: {', '.join(rejected_steps)}\n"

        if cycle_id is not None:
            user_input = (
                f"ORIGINAL TASK: {task}\n\n"
                f"GATE REJECTION for Cycle {cycle_id}.\n"
                f"REJECTION NOTE: {redirect_note}\n\n"
                f"{steps_info}"
                f"APPROVED BLUEPRINTS FROM PRIOR CYCLES:\n{json.dumps(approved_blueprints or [], indent=2)}\n\n"
                f"Re-plan ONLY Cycle {cycle_id}. Keep other cycles unchanged. "
                f"Adjust the research agent briefs to address the rejection note.\n\n"
                f"CRITICAL — AGENT ID NUMBERING: This is Cycle {cycle_id}, not Cycle 1. Every "
                f"agent_id you generate for this re-plan MUST use the suffix `_cycle{cycle_id}_lead` "
                f"or `_cycle{cycle_id}_adv_N` (e.g. `some_role_cycle{cycle_id}_lead`). Do NOT copy the "
                f"`_cycle1_...` pattern from the example format unless {cycle_id} == 1 — reusing "
                f"another cycle's real agent_id string here silently corrupts that cycle's data."
            )
        else:
            user_input = (
                f"ORIGINAL TASK: {task}\n\n"
                f"GATE REJECTION NOTE: {redirect_note}\n\n"
                f"{steps_info}"
                f"APPROVED BLUEPRINTS FROM PRIOR CYCLES:\n{json.dumps(approved_blueprints or [], indent=2)}\n\n"
                f"Adjust the agent briefs to address the rejection note. "
                f"Do not restart from scratch — only modify what the note targets."
            )

    # NEW: Emit thinking event (what Brain is considering) and Narrative
    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": "Brain",
            "data": {
                "thinking_type": "system_prompt",
                "role": "Brain Orchestrator",
                "content": BRAIN_SYSTEM_PROMPT
            }
        })
        event_logger({
            "event_type": "thinking",
            "source": "Brain",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Brain Orchestrator",
                "content": user_input
            }
        })
        event_logger({
            "event_type": "narrative",
            "source": "Brain",
            "data": {
                "phase": "planning",
                "message": "Brain is analyzing the task and deciding which specialists to hire...",
                "icon": "🧠"
            }
        })

    config = types.GenerateContentConfig(
        system_instruction=BRAIN_SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    # NEW: Emit prompt_sent
    if event_logger:
        event_logger({
            "event_type": "prompt_sent",
            "source": "Brain",
            "data": {
                "role": "Brain Orchestrator",
                "content": user_input
            }
        })

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_input,
        config=config
    )

    # NEW: Emit response_received
    if event_logger:
        event_logger({
            "event_type": "response_received",
            "source": "Brain",
            "data": {
                "role": "Brain Orchestrator",
                "content": response.text
            }
        })

    try:
        parsed = json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Brain failed to produce valid JSON", "raw": response.text}

    plan = _normalize_cycle_agent_ids(parsed)
    # A single-cycle re-plan keeps the flow it already sits in.
    return order_cycles(plan) if cycle_id is None else plan


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def order_cycles(agent_plan: dict) -> dict:
    """Put the cycles in flow order, each depending only on cycles before it.

    `depends_on` names the earlier cycles whose results a cycle needs. A cycle that
    doesn't say builds on the one just before it; anything pointing at itself, a
    later cycle or a cycle that doesn't exist is dropped, so the flow can't loop.
    """
    cycles = [c for c in (agent_plan.get("cycles") or []) if isinstance(c, dict)]
    cycles.sort(key=lambda c: _as_int(c.get("cycle_id")) or 0)
    earlier: list[int] = []
    for cycle in cycles:
        cycle_id = _as_int(cycle.get("cycle_id"))
        wanted = cycle.get("depends_on")
        if not isinstance(wanted, list):
            wanted = earlier[-1:]
        cycle["depends_on"] = sorted({
            d for d in (_as_int(x) for x in wanted)
            if d is not None and d in earlier and (cycle_id is None or d < cycle_id)
        })
        if cycle_id is not None:
            earlier.append(cycle_id)
    agent_plan["cycles"] = cycles
    return agent_plan


def refresh_cycle_briefs(task: str, cycle: dict, approved_blueprints: list[dict], event_logger=None) -> dict:
    """Rewrite a cycle's briefs from what the approved cycles before it actually found.

    Every brief is written at planning time, before any research exists, so a later
    cycle's brief can only guess at what earlier cycles will settle. In pipeline 8
    the Brain listed five countries in an advisor's brief while the lead of the same
    cycle was still choosing them. Just before a cycle starts, its agents keep their
    ids, roles and tools, and their briefs are rewritten to name the real results.
    The cycle is updated in place and returned; on any failure it is left as it was.
    """
    if not approved_blueprints:
        return cycle
    agents = [cycle.get("lead_specialist")] + list(cycle.get("advisory_agents") or [])
    agents = [a for a in agents if isinstance(a, dict) and a.get("agent_id")]
    if not agents:
        return cycle

    roster = [{"agent_id": a["agent_id"], "role": a.get("role", ""), "brief": a.get("brief", "")} for a in agents]
    builds_on = ", ".join(f"Cycle {d}" for d in (cycle.get("depends_on") or [])) or "the cycles before it"
    user_input = f"""ORIGINAL TASK: {task}

The research runs as an ordered flow of cycles, and the next one is about to start. Its agent briefs
were written before any research happened, so they may guess at things earlier cycles have since found —
which countries, which competitors, what the business is. Rewrite each brief so it works from what the
approved cycles actually found.

NEXT CYCLE: Cycle {cycle.get("cycle_id")} — {cycle.get("domain", "")}
GOAL: {cycle.get("goal", "")}
BUILDS ON: {builds_on}

ITS AGENTS (keep every agent_id exactly; rewrite only each brief):
{json.dumps(roster, indent=2)}

APPROVED RESULTS OF THE EARLIER CYCLES, IN ORDER:
{json.dumps(approved_blueprints, indent=2)}

RULES:
1. Replace anything a brief guessed with the concrete results above: name the actual countries, companies, niche or other choices the earlier cycles settled.
2. If the earlier cycles have not settled something a brief needs, say so in the brief instead of inventing it.
3. Keep each agent's job the same. Do not do the research yourself, and do not add or remove agents.
4. Every brief must be doable on its own: agents in one cycle run at the same time and cannot see each other's results.

Return JSON: {{"agents": [{{"agent_id": "...", "brief": "..."}}]}}"""

    if event_logger:
        event_logger({"event_type": "narrative", "source": "Brain", "data": {
            "phase": "planning", "icon": "🧭",
            "message": f"Updating Cycle {cycle.get('cycle_id')}'s briefs with what the earlier cycles found..."}})

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=user_input,
            config=types.GenerateContentConfig(
                system_instruction="You are the Central Brain Orchestrator. Output valid JSON only.",
                response_mime_type="application/json",
            ),
        )
        rewritten = {
            a.get("agent_id"): a.get("brief")
            for a in (json.loads(response.text).get("agents") or [])
            if isinstance(a, dict) and isinstance(a.get("brief"), str) and a.get("brief").strip()
        }
    except Exception as e:
        print(f"[Brain] Could not update Cycle {cycle.get('cycle_id')}'s briefs; keeping them as planned: {e}")
        return cycle

    for agent in agents:
        if agent["agent_id"] in rewritten:
            agent["brief"] = rewritten[agent["agent_id"]].strip()
    return cycle


def plan_execution_agents(
    task: str,
    draft_agents: list[dict],
    master_blueprint: dict,
    user_brief: str | None = None,
    redirect_note: str | None = None,
    rejected_steps: list[str] | None = None,
    event_logger=None,
) -> list[dict]:
    """Plan the execution agents from the finished research.

    The Brain's first plan drafts `execution_agents` before any research has
    happened. Only their tools used to be revised afterwards, so who does the work
    and what each agent must produce stayed a pre-research guess. This plans the
    whole roster — roles, briefs, tools and output specs — from the master
    blueprint and the user's brief. On a rejection at the execution blueprint gate
    it re-plans the roster with the user's note, leaving the research cycles alone.

    Falls back to the draft with only its tools revised if the model fails or
    returns no usable agents; it never blocks the pipeline.
    """
    draft_agents = draft_agents or []
    brief = clip_brief(user_brief) if user_brief else ""
    brief_block = ""
    if brief and brief not in task:
        brief_block = "THE USER'S BRIEF (authoritative for what must be delivered):\n" + brief + "\n\n"

    rejection = ""
    if redirect_note or rejected_steps:
        rejection = (
            "THE USER REJECTED THE LAST EXECUTION PLAN (shown above as the draft).\n"
            f"Their note: {redirect_note or '(none)'}\n"
            f"Agents they rejected: {', '.join(rejected_steps or []) or '(none named)'}\n"
            "Change what the note and the rejected agents target. Keep the agents the user did not "
            "reject unless the note says otherwise.\n\n"
        )

    user_input = f"""ORIGINAL TASK: {task}

{brief_block}Research is now COMPLETE. Plan the execution agents that will produce what the user asked for.

DRAFT ROSTER (written before any research; a starting point only — add, drop, merge or rewrite agents):
{json.dumps(draft_agents, indent=2)}

COMPLETED RESEARCH — MASTER BLUEPRINT (the source of truth):
{json.dumps(master_blueprint, indent=2)}

{rejection}RULES:
1. Every deliverable the user's brief asks for (a Google Doc, a website, a script, a report) must have an agent whose job is to produce it.
2. Each brief names the master blueprint sections the agent works from and states the concrete content it must produce.
3. `output_spec` reflects what the research actually found: for example, require one section per competitor only if the blueprint covers those competitors. `required_keys` are keys of the agent's final JSON; set `min_word_count` only where length matters.
4. One focused purpose per agent. Role-first ids ending in `_exec_N` (e.g. `report_writer_exec_1`), and `role` is the human-readable name.
5. `tools_needed` uses the exact service names from the blueprint's tool_recommendations. Don't just keyword-match the brief: read each tool's "purpose" and "cons" for dependencies — if uploading a file and editing a document's content are separate tools, an agent that must do both needs both. Leave it empty if the agent needs no external tool.
6. Do not plan research. Research is finished.

Return JSON: {{"execution_agents": [{{"agent_id": "...", "role": "...", "brief": "...", "tools_needed": [], "output_spec": {{"required_keys": [], "min_word_count": 0}}}}]}}"""

    if event_logger:
        event_logger({"event_type": "thinking", "source": "Brain", "data": {
            "thinking_type": "user_prompt", "role": "Brain Orchestrator (execution planning)", "content": user_input}})
        event_logger({"event_type": "narrative", "source": "Brain", "data": {
            "phase": "planning", "icon": "🧠",
            "message": "Planning the execution agents from the finished research..."}})

    config = types.GenerateContentConfig(
        system_instruction=(
            "You are the Central Brain Orchestrator planning execution agents from completed research. "
            "Output valid JSON only."
        ),
        response_mime_type="application/json",
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(model="gemini-2.5-flash", contents=user_input, config=config)
        if event_logger:
            event_logger({"event_type": "response_received", "source": "Brain", "data": {
                "role": "Brain Orchestrator (execution planning)", "content": response.text}})
        roster = _clean_execution_roster(json.loads(response.text).get("execution_agents"))
        if roster:
            return roster
        print("[Brain] Execution planning returned no usable agents; keeping the draft roster.")
    except Exception as e:
        print(f"[Brain] Execution planning failed, keeping the draft roster: {e}")
    return _revise_draft_tools(task, draft_agents, master_blueprint, event_logger=event_logger)


def _clean_execution_roster(agents) -> list[dict]:
    """Usable agents only: each has an id and a brief, and ids are unique."""
    roster, seen = [], set()
    for a in agents if isinstance(agents, list) else []:
        if not isinstance(a, dict) or not a.get("agent_id") or not a.get("brief"):
            continue
        agent_id = str(a["agent_id"])
        if agent_id in seen:
            n = 2
            while f"{agent_id}_{n}" in seen:
                n += 1
            agent_id = f"{agent_id}_{n}"
        seen.add(agent_id)
        spec = a.get("output_spec") if isinstance(a.get("output_spec"), dict) else {}
        tools = a.get("tools_needed") if isinstance(a.get("tools_needed"), list) else []
        roster.append({**a, "agent_id": agent_id, "role": a.get("role") or agent_id,
                       "tools_needed": tools, "output_spec": spec})
    return roster


def _revise_draft_tools(
    task: str,
    execution_agents: list[dict],
    master_blueprint: dict,
    event_logger=None,
) -> list[dict]:
    """
    Revises execution agents' tools_needed AFTER research is complete, using
    the actual master blueprint findings — instead of trusting whatever Brain
    guessed at Phase 1, before any research had happened.

    Observed failure mode this fixes: Brain's very first planning pass (before
    research) drafts execution_agents.tools_needed as a rough guess (e.g. just
    ["google_drive_api"]). Research cycles run afterward and can discover a
    more accurate, complete answer (e.g. Cycle 3 finding that BOTH
    "Google Drive API" and "Google Docs API" are separately required) — but
    that better answer only ever lands in master_blueprint.tool_recommendations
    for display at the gate. Nothing fed it back into the execution agent's
    already-fixed tools_needed, so the agent stayed bound to the stale,
    incomplete pre-research guess. This closes that loop: conclusions about
    what tools are actually needed get made AFTER research, using what
    research actually found — not before it.

    Only tools_needed is touched; agent_id/role/brief/output_spec are left
    exactly as originally planned. Falls back to the original list unchanged
    on any failure (never blocks the pipeline on this refinement step).
    """
    if not execution_agents:
        return execution_agents

    client = genai.Client(api_key=GEMINI_API_KEY)

    user_input = f"""ORIGINAL TASK: {task}

Research is now COMPLETE. Below is the execution agent roster that was drafted BEFORE any research
had happened — its "tools_needed" fields are only a rough first guess, not ground truth.

DRAFT EXECUTION AGENTS (do not change agent_id, role, brief, or output_spec — only tools_needed):
{json.dumps(execution_agents, indent=2)}

COMPLETED RESEARCH — MASTER BLUEPRINT (the actual, informed source of truth):
{json.dumps(master_blueprint, indent=2)}

Revise each execution agent's "tools_needed" list to accurately reflect what the completed research
determined is actually required. Use the exact service names found in the master blueprint's
tool_recommendations.

IMPORTANT: don't just keyword-match tool names mentioned literally in the brief's wording — the brief
was also written before research and may itself only name one tool where the completed research (see
each tool's "cons"/reasoning fields) reveals the underlying goal actually needs more than one. E.g. if
the brief only says "upload the report as a Google Doc" but the master blueprint's reasoning explains
that the upload tool converts a file but a SEPARATE tool is required to edit/insert the document's
actual content afterward, that second tool is genuinely required to fulfill the brief's real goal even
though the brief itself never names it — include it. Read each tool's "cons"/"purpose" text for exactly
this kind of dependency before deciding an agent only needs one tool.

If an agent genuinely needs no external tool, leave its tools_needed empty.

Return JSON: {{"execution_agents": [ ...same agents, each with a corrected tools_needed... ]}}"""

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": "Brain",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Brain Orchestrator (post-research finalization)",
                "content": user_input,
            },
        })
        event_logger({
            "event_type": "narrative",
            "source": "Brain",
            "data": {
                "phase": "planning",
                "message": "Finalizing execution agents' tools against completed research findings...",
                "icon": "🧠",
            },
        })

    config = types.GenerateContentConfig(
        system_instruction=(
            "You are the Central Brain Orchestrator finalizing an execution plan after research. "
            "Output valid JSON only."
        ),
        response_mime_type="application/json",
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_input,
            config=config,
        )
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": "Brain",
                "data": {"role": "Brain Orchestrator (post-research finalization)", "content": response.text},
            })
        parsed = json.loads(response.text)
        revised = parsed.get("execution_agents")
        if not isinstance(revised, list) or not revised:
            return execution_agents
        # Only tools_needed may change, so take just that from each matching agent.
        # Returning the model's list as-is let a malformed answer replace the roster.
        tools_by_id = {
            a.get("agent_id"): a.get("tools_needed")
            for a in revised
            if isinstance(a, dict) and isinstance(a.get("tools_needed"), list)
        }
        return [
            {**a, "tools_needed": tools_by_id[a.get("agent_id")]} if a.get("agent_id") in tools_by_id else a
            for a in execution_agents
        ]
    except Exception as e:
        print(f"[Brain] Revising the draft execution tools failed, keeping them as they were: {e}")
        return execution_agents


_CYCLE_SUFFIX_RE = re.compile(r"_cycle\d+_")


def _normalize_cycle_agent_ids(agent_plan: dict) -> dict:
    """
    Defense-in-depth against Brain's observed habit of copying the `_cycle1_...`
    example from its own system prompt verbatim, regardless of which cycle it's
    actually planning — most visible on conflict/rejection re-plans of a single
    cycle (cycle_id is not None), where it would otherwise reuse the literal
    agent_id of an unrelated, already-approved cycle and silently corrupt that
    agent's entry in jarvis.py's global AGENT_REGISTRY / conversation logs.

    Rewrites every agent_id's `_cycleN_` segment to match its own cycle's real
    `cycle_id` field (which Brain does set correctly — it's only the agent_id
    string itself that drifts). No-op if Brain already got it right.
    """
    for cycle in agent_plan.get("cycles", []):
        real_cid = cycle.get("cycle_id")
        if real_cid is None:
            continue
        agents = [cycle.get("lead_specialist")] if cycle.get("lead_specialist") else []
        agents += cycle.get("advisory_agents", [])
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            aid = agent.get("agent_id")
            if isinstance(aid, str) and _CYCLE_SUFFIX_RE.search(aid):
                agent["agent_id"] = _CYCLE_SUFFIX_RE.sub(f"_cycle{real_cid}_", aid)
    return agent_plan

