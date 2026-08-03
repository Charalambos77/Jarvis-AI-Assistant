"""
Research Agent — runs a single focused research task asynchronously.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_research_agent(
    agent_config: dict,
    memory_context: str | None = None,
) -> dict:
    """
    Runs a single research agent asynchronously.

    agent_config keys: agent_id, role, brief, tools_needed, memory_query
    memory_context: pre-fetched memory patterns to inject into the prompt
    Returns a dict with agent_id + structured findings.
    """
    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "Researcher")
    brief = agent_config.get("brief", "")

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

{"RELEVANT PAST PATTERNS FROM MEMORY:\n" + memory_context if memory_context else ""}

CRITICAL RULES:
1. Focus ONLY on your brief. Do not go beyond it.
2. Output your findings as a JSON object.
3. Every claim must be backed by evidence (search results, data, or memory patterns).
4. Include an "agent_id" field set to "{agent_id}" in your output.
5. Include a "confidence" field from 0.0 to 1.0 rating how certain you are.

Output format:
{{
  "agent_id": "{agent_id}",
  "role": "{role}",
  "confidence": 0.0-1.0,
  "findings": {{
    "key": "value",
    ...
  }},
  "sources": ["source1", "source2"],
  "recommendation": "one sentence action recommendation"
}}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build tools list based on what the agent needs
    tools_list = [{"google_search": {}}]

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=tools_list,
        response_mime_type="application/json",
    )

    try:
        # [FIX #3] Use get_running_loop(), not get_event_loop() — required in Python 3.10+
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Execute your research brief now. Task context: {brief}",
                config=config
            )
        )
        result = json.loads(response.text)
        result["agent_id"] = agent_id  # ensure it's always set
        result["status"] = "ok"
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e),
            "findings": {}
        }
