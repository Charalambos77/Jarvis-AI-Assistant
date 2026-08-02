import json
import os
from typing import Any

from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

import db
from connectors.api_connector import call_external_api
from connectors.mcp_connector import call_mcp

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")

TOOLS = [
    {
        "name": "add_task",
        "description": "Add a new task to the second brain.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What the task is"},
                "effort_estimate": {"type": "string", "enum": ["small", "medium", "large"]},
                "priority": {"type": "string", "enum": ["low", "medium", "high"], "description": "Priority level of the task"},
                "scheduled_at": {"type": "string", "description": "ISO datetime if this is a fixed commitment"},
                "due_date": {"type": "string", "description": "ISO date if this has a soft deadline"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "get_tasks",
        "description": "Get all tasks, optionally filtered by status (open, in_progress, done).",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["open", "in_progress", "done"]}},
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task as done, given its task_id.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": "Permanently delete a task, given its task_id. Use this when the user wants a task removed entirely, not just marked done.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "add_note",
        "description": "Save a free-form note to the second brain.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "tags": {"type": "string", "description": "comma-separated tags"},
                "task_id": {"type": "integer", "description": "Optional task ID to connect this note to"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "search_notes",
        "description": "Search saved notes by content or tag.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "delete_note",
        "description": "Permanently delete a note, given its note_id.",
        "parameters": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
    {
        "name": "update_note",
        "description": "Modify the content, tags, status, or task_id of an existing note.",
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {"type": "integer"},
                "content": {"type": "string", "description": "New note text"},
                "tags": {"type": "string", "description": "New comma-separated tags"},
                "status": {"type": "string", "enum": ["open", "done"]},
                "task_id": {"type": "integer", "description": "New task ID connection for this note"},
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "complete_note",
        "description": "Mark a note as completed, given its note_id.",
        "parameters": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
    {
        "name": "focus_tasks",
        "description": "Highlight or open one or more tasks in the UI matching the user's focus request.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "The IDs of tasks to focus on"
                }
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "call_external_api",
        "description": "Call an external API through a future connector. This is a placeholder for later integration.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
                "endpoint": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                "params": {"type": "object"},
                "body": {"type": "object"},
                "headers": {"type": "object"},
            },
            "required": ["service_name", "endpoint"],
        },
    },
    {
        "name": "call_mcp",
        "description": "Call an MCP or Claude Desktop connector for external workflows.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["action"],
        },
    },
]


def focus_tasks_impl(task_ids):
    if isinstance(task_ids, (int, float)):
        task_ids = [int(task_ids)]
    elif isinstance(task_ids, list):
        task_ids = [int(x) for x in task_ids]
    else:
        task_ids = []
    return {"status": "focused", "task_ids": task_ids}


TOOL_IMPL = {
    "add_task": lambda conn, **kw: db.add_task(conn, **kw),
    "get_tasks": lambda conn, **kw: db.get_tasks(conn, **kw),
    "complete_task": lambda conn, **kw: db.complete_task(conn, **kw),
    "delete_task": lambda conn, **kw: db.delete_task(conn, **kw),
    "add_note": lambda conn, **kw: db.add_note(conn, **kw),
    "search_notes": lambda conn, **kw: db.search_notes(conn, **kw),
    "delete_note": lambda conn, **kw: db.delete_note(conn, **kw),
    "update_note": lambda conn, **kw: db.update_note(conn, **kw),
    "complete_note": lambda conn, **kw: db.complete_note(conn, **kw),
    "focus_tasks": lambda conn, **kw: focus_tasks_impl(**kw),
    "call_external_api": lambda conn, **kw: call_external_api(conn, **kw),
    "call_mcp": lambda conn, **kw: call_mcp(conn, **kw),
}

def has_gemini() -> bool:
    return bool(GEMINI_API_KEY and genai is not None and types is not None)

client = genai.Client(api_key=GEMINI_API_KEY) if has_gemini() else None

config = None
if has_gemini():
    config = types.GenerateContentConfig(
        system_instruction=(
            "You are a helpful personal assistant with access to the user's task "
            "and note database, as well as Google Search for real-time information. "
            "Use the database tools when the user asks you to add, list, complete, "
            "delete or search tasks and notes. If the user asks about anything else "
            "(like the weather, news, math, general knowledge, etc.), use the Google Search tool "
            "to find the answer. You can perform multiple operations at once if the user requests it. "
            "You can set the priority of a task to 'low', 'medium', or 'high' if the user requests it. "
            "If the user speaks about a specific task, or asks to show/open/focus on it, "
            "you MUST invoke 'focus_tasks' with its ID. If multiple tasks seem to match "
            "or if there is ambiguity, invoke 'focus_tasks' with all potential matching task IDs "
            "so the UI can highlight them and let the user narrow it down. "
            "CRITICAL: Keep replies extremely brief, concise, and conversational (ideally under 1-2 sentences) "
            "since they'll be read aloud. Avoid long explanations."
            "When the user asks to interact with an external service or connect to a system "
            "outside the local database, you may use call_external_api or call_mcp, but only if the user explicitly requires it."
        ),
        tools=[
            {"function_declarations": TOOLS},
            {"google_search": {}},
        ],
        tool_config=types.ToolConfig(
            include_server_side_tool_invocations=True,
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

CHAT_SESSION = None


def get_chat_session():
    if not has_gemini() or client is None:
        raise RuntimeError("Gemini is not configured. Set GEMINI_API_KEY in .env to use the assistant.")

    global CHAT_SESSION
    if CHAT_SESSION is None:
        CHAT_SESSION = client.chats.create(model="gemini-3.5-flash", config=config)
    return CHAT_SESSION


def format_tool_response(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        return str(result)


def perform_tool_action(conn: Any, tool_name: str, **kwargs: Any) -> Any:
    if tool_name not in TOOL_IMPL:
        return {
            "status": "error",
            "error": f"Unknown tool '{tool_name}'. Available tools: {', '.join(sorted(TOOL_IMPL.keys()))}",
        }
    return TOOL_IMPL[tool_name](conn, **kwargs)


def parse_tool_request(text: str) -> tuple[str, dict] | None:
    text = text.strip()
    if not text.lower().startswith("tool:"):
        return None

    payload = text.split(":", 1)[1].strip()
    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict) and "name" in parsed and "args" in parsed:
            return parsed["name"], parsed["args"]
    except json.JSONDecodeError:
        return None
    return None


tool_listeners = []

def register_tool_listener(listener):
    tool_listeners.append(listener)


def fallback_message() -> str:
    available = ", ".join(sorted(TOOL_IMPL.keys()))
    return (
        "Gemini is not configured. Set GEMINI_API_KEY in .env to use the assistant, "
        "or send a direct tool request like: tool: {\"name\": \"get_tasks\", \"args\": {}}\n"
        f"Available local tools: {available}"
    )


def proofread_text(text: str) -> str:
    """
    Checks the given text for spelling and grammatical errors, correcting them
    to ensure smooth delivery without changing the meaning or style.
    """
    if not text or not has_gemini() or client is None:
        return text

    prompt = (
        "You are an assistant's output proofreader. Review the following text for spelling, "
        "grammatical errors, and typos. Correct them so it reads naturally and is ready to be spoken. "
        "Do NOT change the style, tone, or information content. Keep it short. "
        "Return ONLY the corrected text, with no preamble, comments, or quotes.\n\n"
        f"Text: {text}"
    )
    try:
        res = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
        )
        if res.text:
            cleaned = res.text.strip()
            if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
                cleaned = cleaned[1:-1].strip()
            if cleaned != text:
                print(f"[Proofreader] Corrected: '{text}' -> '{cleaned}'")
            return cleaned
    except Exception as e:
        print(f"Error proofreading text: {e}")
    return text


def handle_request(transcript: str) -> str:
    conn = db.get_connection(DB_PATH)
    try:
        tool_call = parse_tool_request(transcript)
        if tool_call is not None:
            name, args = tool_call
            result = perform_tool_action(conn, name, **args)
            for listener in tool_listeners:
                try:
                    listener(name, args, result)
                except Exception as e:
                    print(f"Error in tool listener: {e}")
            return format_tool_response(result)

        if not has_gemini():
            return fallback_message()

        chat = get_chat_session()
        response = chat.send_message(transcript)

        for _ in range(5):
            if not response.function_calls:
                break

            response_parts = []
            for call in response.function_calls:
                fname = call.name
                fargs = call.args
                print(f"[tool call] {fname}({fargs})")
                try:
                    result = perform_tool_action(conn, fname, **fargs)
                    for listener in tool_listeners:
                        try:
                            listener(fname, fargs, result)
                        except Exception as e:
                            print(f"Error in tool listener: {e}")
                except Exception as e:
                    result = {"error": str(e)}
                response_parts.append(
                    types.Part.from_function_response(
                        name=fname,
                        response={"result": json.dumps(result, default=str)},
                    )
                )

            response = chat.send_message(response_parts)

        return proofread_text(response.text)
    finally:
        conn.close()

