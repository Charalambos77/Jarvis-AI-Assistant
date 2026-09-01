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
1. Generate at least 3 research cycles.
2. Each cycle must target a distinct domain or layer of the task (e.g. Domain 1: Target Audience/Identity, Domain 2: Content Strategy/Structuring, Domain 3: Virality/SEO).
3. Each cycle must have exactly 1 `lead_specialist` and at least 1 `advisory_agents`.
4. ROLE-FIRST AGENT IDENTIFIERS: Always put the descriptive role name FIRST, followed by the cycle/execution tag (e.g. `brand_strategist_cycle1_lead`, `competitor_analyst_cycle1_adv_1`, `script_writer_exec_1`). The `role` field must be the exact human-readable name of the specialist (e.g. "Brand Strategist", "Competitor Analyst").
5. STRICT SINGLE-PURPOSE AGENT ROLES: Every agent (both research specialists and execution agents) MUST have exactly ONE focused role and function. NEVER bundle multiple tasks or responsibilities into compound agent roles (e.g. DO NOT create "SEO & Virality Specialist" or "Metadata & Description Writer"). Split them into separate, dedicated individual agents (e.g. "SEO Specialist" and "Virality Researcher", or "Metadata Tag Specialist" and "Description Writer").
6. `execution_agents` remains a flat list of distinct, single-purpose agents that will execute based on the final synthesized blueprint.
7. Provide specific recommendations in the `recommended_tools` section based on tools that research agents might need.
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

    return _normalize_cycle_agent_ids(parsed)


def finalize_execution_plan(
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
        return revised
    except Exception as e:
        print(f"[Brain] finalize_execution_plan failed, keeping original tools_needed: {e}")
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

