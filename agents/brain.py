"""
Brain Orchestrator — decides agent briefs from a user task description.
Does NOT call APIs directly. Returns spawn plans (lists of agent configs).
"""
import json
import os
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
                f"Adjust the research agent briefs to address the rejection note."
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
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Brain failed to produce valid JSON", "raw": response.text}

