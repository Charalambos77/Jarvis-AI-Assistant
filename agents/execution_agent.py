"""
Execution Agent — builds a specific deliverable based on the approved blueprint.
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_execution_agent(
    agent_config: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None,
    event_logger=None,                  # NEW
) -> dict:
    """
    Runs a single execution agent asynchronously.
    Blueprint is the compressed research output approved at Gate 1.
    gate_redirect_note is set if Gate 2 or Gate 3 was rejected.
    """
    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "Builder")
    brief = agent_config.get("brief", "")
    output_spec = agent_config.get("output_spec", {})

    blueprint_str = json.dumps(blueprint, indent=2)

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

APPROVED RESEARCH BLUEPRINT (use this as your source of truth):
{blueprint_str}

{"GATE REJECTION NOTE (address this specifically in your output):\n" + gate_redirect_note if gate_redirect_note else ""}

REQUIRED OUTPUT KEYS: {json.dumps(output_spec.get("required_keys", []))}
MINIMUM WORD COUNT: {output_spec.get("min_word_count", 0)}

RULES:
1. Stay strictly within your brief.
2. Your output must include ALL required keys.
3. Include "agent_id": "{agent_id}" and "status": "ok" in your response.
4. If you cannot complete the task, set "status": "error" and explain why.

Output valid JSON only.
"""

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": agent_id,
            "data": {
                "thinking_type": "system_prompt",
                "role": role,
                "content": system_prompt
            }
        })
        event_logger({
            "event_type": "narrative",
            "source": agent_id,
            "data": {
                "phase": "execution",
                "message": f"Execution Agent {role} ({agent_id}) is producing deliverable...",
                "icon": "⚡"
            }
        })

    client = genai.Client(api_key=GEMINI_API_KEY)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
    )

    try:
        # [FIX #3] Use get_running_loop(), not get_event_loop() — required in Python 3.10+
        loop = asyncio.get_running_loop()

        # Emit prompt_sent
        if event_logger:
            event_logger({
                "event_type": "prompt_sent",
                "source": agent_id,
                "data": {
                    "role": role,
                    "content": "Execute your deliverable now according to your brief and blueprint."
                }
            })

        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents="Execute your deliverable now according to your brief and blueprint.",
                config=config
            )
        )

        # Emit response_received
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": agent_id,
                "data": {
                    "role": role,
                    "content": response.text
                }
            })

        result = json.loads(response.text)
        result["agent_id"] = agent_id
        result.setdefault("status", "ok")
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e)
        }
