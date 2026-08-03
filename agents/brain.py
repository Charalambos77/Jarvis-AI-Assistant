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
  "research_agents": [
    {
      "agent_id": "agent_research_1",
      "role": "Hook Researcher",
      "brief": "Research the best hook formats for YouTube videos about AI tools. Focus on retention data.",
      "tools_needed": ["google_search", "search_memory_patterns"],
      "memory_query": "youtube hook formats high retention"
    }
  ],
  "execution_agents": [
    {
      "agent_id": "agent_exec_1",
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

Spawn between 2 and 8 research agents depending on task complexity.
Spawn between 1 and 6 execution agents depending on what needs to be built.
Always include at least one memory query per research agent.
"""


def build_agent_plan(task: str, redirect_note: str | None = None) -> dict:
    """
    Ask the Brain to produce an agent spawn plan for the given task.
    If redirect_note is provided, it means Gate 1 was rejected and we are re-planning.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    user_input = task
    if redirect_note:
        user_input = (
            f"ORIGINAL TASK: {task}\n\n"
            f"GATE REJECTION NOTE: {redirect_note}\n\n"
            f"Adjust the research agent briefs to address the rejection note. "
            f"Do not restart from scratch — only modify what the note targets."
        )

    config = types.GenerateContentConfig(
        system_instruction=BRAIN_SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_input,
        config=config
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Brain failed to produce valid JSON", "raw": response.text}
