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
    prior_context: str | None = None,   # approved blueprints from prior cycles
    on_chunk_callback=None
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

{"APPROVED RESEARCH FROM PRIOR CYCLES (use as established context):\n" + prior_context if prior_context else ""}

CRITICAL RULES:
1. Focus ONLY on your brief. Do not go beyond it.
2. Output your findings as a JSON object.
3. Every claim must be backed by evidence (search results, data, or memory patterns).
4. Include an "agent_id" field set to "{agent_id}" in your output.
5. Include a "confidence" field from 0.0 to 1.0 rating how certain you are.
6. If your research reveals specific APIs, services, or tools that would be valuable for executing this task, include them in your output under "recommended_tools".
7. Save critical discoveries, formulas, constants, or key technical specifications under "high_value_memory" as a key-value dictionary.
8. Save general notes, minor facts, background logs, or broad summaries under "general_memory" as a key-value dictionary.

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
  "recommendation": "one sentence action recommendation",
  "recommended_tools": [
    {{
      "service": "youtube_api",
      "purpose": "why it is needed",
      "doc_url": "official developer website or documentation URL",
      "pros": ["pro1", "pro2"],
      "cons": ["con1", "con2"],
      "why": "specific reason",
      "alternatives": ["alt1"],
      "connection_methods": [
        {{
          "method_id": "api_key",
          "label": "API Key (Simple)",
          "fields": [{{"name": "api_key", "label": "API Key", "type": "password"}}]
        }},
        {{
          "method_id": "oauth",
          "label": "OAuth 2.0 Client",
          "fields": [
            {{"name": "client_id", "label": "Client ID", "type": "text"}},
            {{"name": "client_secret", "label": "Client Secret", "type": "password"}}
          ]
        }}
      ]
    }}
  ],
  "high_value_memory": {{
    "critical_constant_or_key_formula": "value"
  }},
  "general_memory": {{
    "background_notes_or_context": "value"
  }}
}}
"""

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Build tools list dynamically from agent config
    tools_needed = agent_config.get("tools_needed", ["google_search"])
    tools_list = []
    for tool_name in tools_needed:
        if tool_name == "google_search":
            tools_list.append({"google_search": {}})
        # Other tool types will be added by the API/Provider Registry (Step 6)
    # [FIX] Gemini API does not allow response_mime_type="application/json" when tools are present
    config_args = {
        "system_instruction": system_prompt,
    }
    if tools_list:
        config_args["tools"] = tools_list
    else:
        config_args["response_mime_type"] = "application/json"

    config = types.GenerateContentConfig(**config_args)

    try:
        # [FIX #3] Use get_running_loop(), not get_event_loop() — required in Python 3.10+
        loop = asyncio.get_running_loop()

        def run_stream():
            full_text = ""
            for chunk in client.models.generate_content_stream(
                model="gemini-2.5-flash",
                contents=f"Execute your research brief now. Task context: {brief}",
                config=config
            ):
                if chunk.text:
                    full_text += chunk.text
                    if on_chunk_callback:
                        loop.call_soon_threadsafe(on_chunk_callback, agent_id, full_text)
            return full_text

        response_text = await loop.run_in_executor(None, run_stream)
        result = json.loads(response_text)
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
