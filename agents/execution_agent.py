"""
Execution Agent — builds a specific deliverable based on the approved blueprint.

Unlike the old version, this agent doesn't just ask Gemini to narrate JSON
about what it supposedly did. It gets real function-calling tools (bound
automatically from its `tools_needed` brief via agents/tool_executor.py) and
actually calls them — writing real files, hitting real APIs where a
connector exists — before producing its final JSON summary.
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

# Keys, in priority order, that a tool result might carry the "thing that got
# created" under. Generic on purpose — any current or future tool (Docs,
# Drive, video generation, website deploy, ...) is picked up automatically
# as long as its handler returns one of these, no per-tool wiring needed.
_ARTIFACT_URL_KEYS = ("google_doc_url", "doc_url", "url", "web_url", "video_url", "website_url", "deploy_url")
_ARTIFACT_PATH_KEYS = ("path", "file_path", "output_path", "video_path")


def _extract_artifact(tool_name: str, tool_args: dict, result) -> dict | None:
    """Pulls a clickable artifact (a URL or a local file path) out of a real
    tool result, if that call actually produced one. Returns None for tool
    calls that didn't create anything openable (e.g. read_file, a search)."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    label = tool_args.get("relative_path") or tool_args.get("title") or tool_args.get("query") or tool_name
    for key in _ARTIFACT_URL_KEYS:
        if result.get(key):
            return {"type": "url", "value": result[key], "label": str(label), "tool": tool_name}
    # write_file's own bookkeeping (list_deliverables, read_file) also uses
    # "path" but isn't something the user asked to "open" — only count it as
    # an artifact when this call actually wrote something.
    if result.get("action") == "write_file":
        for key in _ARTIFACT_PATH_KEYS:
            if result.get(key):
                return {"type": "path", "value": result[key], "label": str(label), "tool": tool_name}
    return None


async def run_execution_agent(
    agent_config: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None,
    event_logger=None,                  # NEW
    project_name: str = "Default Project",
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
    tools_needed = agent_config.get("tools_needed", [])

    blueprint_str = json.dumps(blueprint, indent=2)

    declarations, handlers, unavailable = get_tools_for_execution_agent(tools_needed, project_name)

    unavailable_note = ""
    if unavailable:
        unavailable_note = (
            "\nTOOLS YOU ASKED FOR BUT ARE NOT ACTUALLY CONNECTED YET:\n"
            + "\n".join(f"- {u}" for u in unavailable)
            + "\nDo NOT claim you used these or fabricate URLs/IDs for them. Instead, produce the "
              "best deliverable you can with write_file, and explicitly note in your output which "
              "part could not be completed and why.\n"
        )

    rejection_block = f"GATE REJECTION NOTE (address this specifically in your output):\n{gate_redirect_note}\n" if gate_redirect_note else ""

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

APPROVED RESEARCH BLUEPRINT (use this as your source of truth):
{blueprint_str}

{rejection_block}

REQUIRED OUTPUT KEYS: {json.dumps(output_spec.get("required_keys", []))}
MINIMUM WORD COUNT: {output_spec.get("min_word_count", 0)}

TOOLS: You have real tools available (write_file, read_file, list_deliverables, and any
connectors listed below). USE write_file to actually save any code, report, script, or
document you produce — a deliverable that only exists in your final JSON text is not real work.
{unavailable_note}
RULES:
1. Stay strictly within your brief.
2. Actually call your tools to do real work before answering. Do not just describe actions.
3. Your FINAL response (after tool calls are done) must include ALL required keys.
4. Include "agent_id": "{agent_id}" and "status": "ok" in your final response.
5. If you cannot complete part of the task (e.g. a tool isn't connected), set "status": "partial"
   and explain exactly what's missing in a "blocked_reason" key — never fabricate success.
6. Your final response must be valid JSON only, no markdown code fences.
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
    collected_artifacts = []

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
                    "content": "Execute your deliverable now according to your brief and blueprint."
                }
            })

        chat = client.chats.create(model="gemini-2.5-flash", config=config)
        current_message = "Execute your deliverable now according to your brief and blueprint. Use your tools to do real work, then give your final JSON summary."

        final_text = None
        for turn in range(MAX_TOOL_TURNS):
            response = await loop.run_in_executor(
                None, lambda m=current_message: chat.send_message(m)
            )

            function_calls = response.function_calls
            if not function_calls:
                final_text = response.text or ""
                break

            tool_response_parts = []
            for fc in function_calls:
                tool_args = dict(fc.args) if fc.args else {}
                if event_logger:
                    event_logger({
                        "event_type": "narrative",
                        "source": agent_id,
                        "data": {
                            "phase": "execution",
                            "message": f"{role} ({agent_id}) is calling tool `{fc.name}`...",
                            "icon": "🛠️"
                        }
                    })
                result = run_tool(handlers, project_name, agent_id, fc.name, tool_args)
                artifact = _extract_artifact(fc.name, tool_args, result)
                if artifact:
                    collected_artifacts.append(artifact)
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
            final_text = "{}"
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
        result["agent_id"] = agent_id
        result.setdefault("status", "ok")
        if collected_artifacts:
            result["artifacts"] = collected_artifacts
        return result
    except Exception as e:
        # Even if the agent's final wrap-up JSON failed to parse, any tools
        # it actually called already did real, irreversible work (a Doc got
        # created, a file got written) — don't let a text-formatting slip
        # hide that from the user.
        error_result = {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e)
        }
        if collected_artifacts:
            error_result["artifacts"] = collected_artifacts
        return error_result
