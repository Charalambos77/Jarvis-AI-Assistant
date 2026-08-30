"""
Research Agent — runs a single focused research task asynchronously.

Like execution_agent.py, this agent gets real function-calling tools bound
automatically from its `tools_needed` brief (via agents/tool_executor.py)
and must actually call them to gather real information — a "arXiv Search
Specialist" with tools_needed=["arxiv_api"] gets a real arxiv_search
function now, instead of silently getting zero tools and having to
rationalize why it "can't reach arXiv".
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

from agents.tool_executor import get_tools_for_execution_agent, run_tool

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MAX_TOOL_TURNS = 6


async def run_research_agent(
    agent_config: dict,
    memory_context: str | None = None,
    prior_context: str | None = None,   # approved blueprints from prior cycles
    on_chunk_callback=None,
    event_logger=None,                   # NEW
    project_name: str = "Default Project",
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
    tools_needed = agent_config.get("tools_needed", ["google_search"])

    declarations, handlers, unavailable = get_tools_for_execution_agent(tools_needed, project_name)

    unavailable_note = ""
    if unavailable:
        unavailable_note = (
            "\nTOOLS YOU ASKED FOR BUT ARE NOT ACTUALLY CONNECTED YET:\n"
            + "\n".join(f"- {u}" for u in unavailable)
            + "\nDo NOT claim to have used these or fabricate results for them. Say plainly in your "
              "findings/recommendation that this part is blocked and why.\n"
        )

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

{"RELEVANT PAST PATTERNS FROM MEMORY:\n" + memory_context if memory_context else ""}

{"APPROVED RESEARCH FROM PRIOR CYCLES (use as established context):\n" + prior_context if prior_context else ""}

TOOLS: You have real tools available (write_file, read_file, list_deliverables, and any research
connectors listed below such as arxiv_search / web_search). USE them to gather real information —
do not invent findings, sources, or data. If your brief calls for searching arXiv or the web, you
MUST actually call the corresponding tool before answering.
{unavailable_note}
CRITICAL RULES:
1. Focus ONLY on your brief. Do not go beyond it.
2. Actually call your tools to gather real evidence before answering. Do not just describe what you would search for.
3. Your FINAL response (after tool calls are done) must be a JSON object only, no markdown fences.
4. Include an "agent_id" field set to "{agent_id}" in your output.
5. Include a "confidence" field from 0.0 to 1.0 rating how certain you are.
6. Every claim must be backed by real evidence returned from a tool call, or explicitly marked as your own reasoning.
7. If your research reveals specific APIs, services, or tools that would be valuable for executing this task, include them in your output under "recommended_tools".
8. Save critical discoveries, formulas, constants, or key technical specifications under "high_value_memory" as a key-value dictionary.
9. Save general notes, minor facts, background logs, or broad summaries under "general_memory" as a key-value dictionary.
10. If a needed tool is unavailable (see above), set "status": "partial" and explain what's missing in "blocked_reason" — never fabricate as if you had real access.

Output format:
{{
  "agent_id": "{agent_id}",
  "role": "{role}",
  "status": "ok",
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
                "phase": "research",
                "message": f"{role} ({agent_id}) is now investigating: {brief[:100]}...",
                "icon": "🔍"
            }
        })

    client = genai.Client(api_key=GEMINI_API_KEY)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[{"function_declarations": declarations}] if declarations else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True) if declarations else None,
    )

    try:
        loop = asyncio.get_running_loop()

        if event_logger:
            event_logger({
                "event_type": "prompt_sent",
                "source": agent_id,
                "data": {
                    "role": role,
                    "content": f"Execute your research brief now. Task context: {brief}"
                }
            })

        chat = client.chats.create(model="gemini-2.5-flash", config=config)
        current_message = f"Execute your research brief now. Task context: {brief}"

        final_text = None
        for turn in range(MAX_TOOL_TURNS):
            response = await loop.run_in_executor(
                None, lambda m=current_message: chat.send_message(m)
            )

            function_calls = response.function_calls
            if not function_calls:
                final_text = response.text or ""
                if on_chunk_callback:
                    loop.call_soon_threadsafe(on_chunk_callback, agent_id, final_text)
                break

            tool_response_parts = []
            for fc in function_calls:
                tool_args = dict(fc.args) if fc.args else {}
                if event_logger:
                    event_logger({
                        "event_type": "narrative",
                        "source": agent_id,
                        "data": {
                            "phase": "research",
                            "message": f"{role} ({agent_id}) is calling tool `{fc.name}`...",
                            "icon": "🛠️"
                        }
                    })
                result = run_tool(handlers, project_name, agent_id, fc.name, tool_args)
                if event_logger:
                    event_logger({
                        "event_type": "tool_result",
                        "source": agent_id,
                        "data": {"tool": fc.name, "args": tool_args, "result": result}
                    })
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result if isinstance(result, dict) else {"result": result}
                    )
                )
            current_message = tool_response_parts

        if final_text is None:
            final_text = json.dumps({
                "agent_id": agent_id, "role": role, "status": "partial", "confidence": 0.0,
                "findings": {}, "sources": [], "recommendation": "",
                "blocked_reason": "Exceeded max tool-call turns without a final answer.",
            })
            if event_logger:
                event_logger({"event_type": "error", "source": agent_id, "data": "Exceeded max tool-call turns without a final answer."})

        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": agent_id,
                "data": {"role": role, "content": final_text}
            })

        cleaned = final_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        if not cleaned:
            cleaned = "{}"

        result = json.loads(cleaned)
        result["agent_id"] = agent_id  # ensure it's always set
        result.setdefault("status", "ok")
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e),
            "findings": {}
        }
