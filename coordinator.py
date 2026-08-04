import json
import os
import subprocess
import time
import threading
from typing import Any

import requests
from dotenv import load_dotenv

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

import db
from connectors.api_connector import call_external_api
from connectors.mcp_connector import call_mcp

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")

def get_ollama_config():
    try:
        settings_path = os.path.join(BASE_DIR, "settings.json")
        if os.path.exists(settings_path):
            with open(settings_path, "r") as f:
                sett = json.load(f)
        else:
            sett = {}
    except Exception:
        sett = {}
    
    url = sett.get("ollama_url") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434"
    model = sett.get("ollama_model") or os.getenv("OLLAMA_MODEL") or "qwen2.5:3b"
    force_cpu = sett.get("ollama_force_cpu", False) or (os.getenv("OLLAMA_FORCE_CPU", "false").lower() == "true")
    return url, model, force_cpu

def ensure_ollama_running(url, model, force_cpu):
    try:
        res = requests.get(f"{url}/api/tags", timeout=1)
        if res.status_code == 200:
            print("[Ollama] Server is already running.")
            return
    except Exception:
        pass

    def start_server(cpu_only=False):
        if cpu_only:
            print("[Ollama] Starting programmatically in CPU-only mode...")
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = ""
        else:
            print("[Ollama] Starting programmatically (GPU enabled)...")
            env = None

        try:
            return subprocess.Popen(
                ["ollama", "serve"],
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"[Ollama] Failed to spawn Ollama server process: {e}")
            return None

    proc = start_server(cpu_only=force_cpu)
    if not proc:
        return

    success = False
    for _ in range(10):
        time.sleep(1)
        # Check if the process died (crashed on GPU start)
        if proc.poll() is not None:
            print(f"[Ollama] Server process terminated prematurely with exit code: {proc.returncode}")
            break
        try:
            res = requests.get(f"{url}/api/tags", timeout=1)
            if res.status_code == 200:
                # Try loading the model to force-check CUDA driver compatibility
                try:
                    show_res = requests.post(f"{url}/api/show", json={"name": model}, timeout=3)
                    if show_res.status_code == 200:
                        success = True
                        print("[Ollama] Server started and model loaded successfully.")
                        break
                    else:
                        print(f"[Ollama] Model load returned error {show_res.status_code}: {show_res.text}")
                except Exception as e:
                    print(f"[Ollama] Exception while loading model: {e}")
                    break
        except Exception:
            pass

    if not success and not force_cpu:
        print("[Ollama] GPU mode failed or crashed during model check. Initiating CPU-only fallback...")
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass

        if os.name == 'nt':
            subprocess.run(["taskkill", "/f", "/im", "ollama.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["taskkill", "/f", "/im", "ollama app.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        proc_cpu = start_server(cpu_only=True)
        if proc_cpu:
            for _ in range(10):
                time.sleep(1)
                try:
                    if requests.get(f"{url}/api/tags", timeout=1).status_code == 200:
                        print("[Ollama] Server started successfully on CPU fallback.")
                        try:
                            requests.post(f"{url}/api/show", json={"name": model}, timeout=2)
                        except Exception:
                            pass
                        break
                except Exception:
                    pass

threading.Thread(target=lambda: ensure_ollama_running(*get_ollama_config()), daemon=True).start()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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
    {
        "name": "google_search",
        "description": "Search the web for real-time information, weather, news, current events, or general knowledge questions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query to send to Google."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory_patterns",
        "description": (
            "Query Long-Term Memory for past winning or losing patterns relevant to a task. "
            "Use this BEFORE starting research or execution to avoid repeating past mistakes "
            "and to leverage proven strategies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What pattern to search for"},
                "task_type": {"type": "string", "description": "Filter by task type: video, code, marketing, etc."},
                "outcome": {"type": "string", "enum": ["win", "loss"], "description": "Filter by outcome"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_memory_pattern",
        "description": (
            "Save a discovered pattern or insight into Long-Term Memory after a task completes. "
            "Use this when the Track Agent identifies a high-performing or low-performing pattern."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "task_type": {"type": "string"},
                "metric_name": {"type": "string"},
                "metric_value": {"type": "number"},
                "outcome": {"type": "string", "enum": ["win", "loss"]}
            },
            "required": ["pattern"]
        }
    },
    {
        "name": "control_interface",
        "description": (
            "Control every part of the app's UI: navigate between pages, open/close panels, "
            "focus tasks, show notifications, and control sleep mode. "
            "ALWAYS call this tool when the user asks to go somewhere, open/close something, "
            "or control the interface. Never just describe navigation — actually invoke this tool. "
            "Pages: Brain Core (home/3D map), Plan Page, APIs/MCPs Page. "
            "Side Panels: Tasks list, Notes list, Settings, specific Task Detail. "
            "Use payload.task_id (int) with focus_task or open_task_detail. "
            "Use payload.message (str) with flash_notification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "go_to_plan",
                        "go_to_brain",
                        "go_to_apis",
                        "open_side_panel",
                        "close_side_panel",
                        "open_notes_panel",
                        "open_settings_panel",
                        "focus_task",
                        "open_task_detail",
                        "exit_completely",
                        "wake_up",
                        "go_to_sleep",
                        "flash_notification",
                        "reset_camera"
                    ],
                    "description": (
                        "go_to_plan: Navigate to the Plan page. "
                        "go_to_brain: Return to Brain Core (3D map home). "
                        "go_to_apis: Navigate to the APIs/MCPs comparison page. "
                        "open_side_panel: Open the left side panel showing tasks. "
                        "close_side_panel: Close/hide the left side panel. "
                        "open_notes_panel: Open side panel showing all saved notes. "
                        "open_settings_panel: Open side panel showing app settings (voice speed, theme, provider). "
                        "focus_task: Highlight/zoom to a specific task node — requires payload.task_id. "
                        "open_task_detail: Open task detail view in side panel — requires payload.task_id. "
                        "exit_completely: Shut down the app with a collapse animation. "
                        "wake_up: Wake Jarvis from sleep mode. "
                        "go_to_sleep: Put UI into sleep/dim mode. "
                        "flash_notification: Show a temporary floating notification — use payload.message. "
                        "reset_camera: Reset the 3D camera to default position."
                    )
                },
                "payload": {
                    "type": "object",
                    "description": "Extra data for the action. task_id (int) for focus/detail actions; message (str) for notifications.",
                    "properties": {
                        "task_id": {"type": "integer", "description": "Task ID to focus on or open detail for"},
                        "message": {"type": "string", "description": "Message text for flash_notification"}
                    }
                }
            },
            "required": ["action"]
        }
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


def outsource_google_search(query: str) -> str:
    if not has_gemini() or client is None:
        return "Google Search is not configured because the Gemini API key is missing."
    try:
        print(f"[Gemini Search Outsource] Searching: '{query}'")
        search_config = types.GenerateContentConfig(
            tools=[{"google_search": {}}],
            system_instruction="You are a search grounding assistant. Search the web for the user's query and provide a factual, concise summary of the results with references if appropriate."
        )
        res = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=query,
            config=search_config
        )
        return res.text or "No results found."
    except Exception as e:
        print(f"Error in outsourced Google Search: {e}")
        return f"Failed to perform search: {e}"

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
    "google_search": lambda conn, query: outsource_google_search(query),
    "search_memory_patterns": lambda conn, **kw: db.search_memory_patterns(conn, **kw),
    "save_memory_pattern":    lambda conn, **kw: db.save_memory_pattern(conn, **kw),
    "control_interface":      lambda conn, action, payload=None: {"status": "success", "action": action, "payload": payload or {}},
}

def has_gemini() -> bool:
    return bool(GEMINI_API_KEY and genai is not None and types is not None)

client = genai.Client(api_key=GEMINI_API_KEY) if has_gemini() else None

UI_MAP = (
    "=== APP UI MAP (memorise this) ===\n"
    "PAGES:\n"
    "  - Brain Core (default, command_center): 3D nebula showing tasks as glowing nodes. Home screen. "
    "    Action: go_to_brain\n"
    "  - Plan Page (plan.html): Project plan with phases and timelines. "
    "    Action: go_to_plan\n"
    "  - APIs/MCPs Page (provider_comparison.html): Compare AI providers. "
    "    Action: go_to_apis\n"
    "SIDE PANEL (left drawer, slides in/out):\n"
    "  - Tasks list view: All open tasks. Action: open_side_panel\n"
    "  - Notes list view: All saved notes sorted by date. Action: open_notes_panel\n"
    "  - Settings view: Voice speed, theme, wake-word threshold, AI provider. Action: open_settings_panel\n"
    "  - Task Detail view: Subtasks, notes, actions for one task. Action: open_task_detail + payload.task_id\n"
    "  - Close panel. Action: close_side_panel\n"
    "TASK FOCUS:\n"
    "  - Zoom/highlight a specific 3D task node. Action: focus_task + payload.task_id\n"
    "  - Open task detail panel for a task. Action: open_task_detail + payload.task_id\n"
    "SLEEP / WAKE:\n"
    "  - Dim UI to sleep mode. Action: go_to_sleep\n"
    "  - Wake from sleep. Action: wake_up\n"
    "MISC:\n"
    "  - Show floating toast message. Action: flash_notification + payload.message\n"
    "  - Reset 3D camera. Action: reset_camera\n"
    "  - Shut down entirely with collapse animation. Action: exit_completely\n"
    "VOICE EXAMPLES (always invoke control_interface for these):\n"
    "  'open settings' → open_settings_panel\n"
    "  'show the plan' / 'go to plan' → go_to_plan\n"
    "  'go to APIs' / 'show providers' → go_to_apis\n"
    "  'open notes' / 'show my notes' → open_notes_panel\n"
    "  'show task 5' / 'open task 5' → open_task_detail, payload={task_id:5}\n"
    "  'close the panel' / 'close everything' → close_side_panel\n"
    "  'reset the view' → reset_camera\n"
    "  'go home' / 'back to brain' → go_to_brain\n"
)

SYSTEM_PROMPT = (
    "You are Jarvis, a helpful personal AI assistant with full control over the user's app. "
    "You have access to their task and note database, Google Search for real-time info, "
    "and complete control over the app's user interface.\n\n"
    + UI_MAP +
    "\nRULES:\n"
    "1. For any navigation or UI action, ALWAYS invoke 'control_interface' — never just describe it.\n"
    "2. When the user mentions a specific task by name or ID, call 'focus_tasks' with its ID.\n"
    "3. When you take a UI action, briefly confirm it aloud in 1 sentence (e.g. 'Opening settings, Sir.').\n"
    "4. Use database tools to add/list/complete/delete tasks and notes as requested.\n"
    "5. Keep all spoken replies extremely brief (1-2 sentences max) since they are read aloud.\n"
    "6. Never give long explanations — be concise and conversational like a real assistant."
)

config = None
if has_gemini():
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[
            {"function_declarations": TOOLS},
        ],
        tool_config=types.ToolConfig(
            include_server_side_tool_invocations=False,
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

CHAT_SESSION = None


def get_chat_session():
    if not has_gemini() or client is None:
        raise RuntimeError("Gemini is not configured. Set GEMINI_API_KEY in .env to use the assistant.")

    global CHAT_SESSION
    if CHAT_SESSION is None:
        CHAT_SESSION = client.chats.create(model="gemini-2.5-flash", config=config)
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
    Returns the text directly. The local model is responsible for generating
    grammatically correct, speech-ready output.
    """
    return text


OLLAMA_CHAT_HISTORY = []

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

        # Check settings for preferred model provider (Gemini vs Ollama)
        provider = "ollama"
        try:
            settings_path = os.path.join(BASE_DIR, "settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r") as f:
                    sett = json.load(f)
                    provider = sett.get("provider") or os.getenv("MODEL_PROVIDER") or "ollama"
        except Exception:
            pass

        # Fallback to Ollama if Gemini is selected but not configured
        if provider == "gemini" and not has_gemini():
            print("[Coordinator] Gemini requested but not configured (missing key). Falling back to Ollama.")
            provider = "ollama"

        if provider == "gemini":
            chat = get_chat_session()
            current_message = transcript
            for _ in range(5):
                try:
                    response = chat.send_message(current_message)
                except Exception as e:
                    print(f"Error calling Gemini API: {e}")
                    return f"Sorry, I had trouble talking to the Gemini model: {e}"
                
                # Check for function calls
                function_calls = response.function_calls
                if not function_calls:
                    return response.text or ""
                
                # Execute all function calls
                tool_responses = []
                for fc in function_calls:
                    fname = fc.name
                    fargs = fc.args
                    print(f"[Gemini tool call] {fname}({fargs})")
                    try:
                        result = perform_tool_action(conn, fname, **fargs)
                        for listener in tool_listeners:
                            try:
                                listener(fname, fargs, result)
                            except Exception as e:
                                print(f"Error in tool listener: {e}")
                    except Exception as e:
                        result = {"error": str(e)}
                    
                    tool_responses.append(
                        types.Part.from_function_response(
                            name=fname,
                            response=result if isinstance(result, dict) else {"result": result}
                        )
                    )
                
                # Send the function responses back to progress the chat
                current_message = tool_responses
            
            return "I processed your request but took too many steps."

        ollama_url, ollama_model, _ = get_ollama_config()

        if not OpenAI:
            return "OpenAI library not installed. Please run pip install -r requirements.txt."

        openai_tools = []
        for t in TOOLS:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"]
                }
            })

        client_ollama = OpenAI(base_url=f"{ollama_url}/v1", api_key="ollama")

        global OLLAMA_CHAT_HISTORY
        # If history is empty, add system prompt
        if not any(m.get("role") == "system" for m in OLLAMA_CHAT_HISTORY):
            OLLAMA_CHAT_HISTORY.append({
                "role": "system",
                "content": SYSTEM_PROMPT
            })

        # Append user message
        OLLAMA_CHAT_HISTORY.append({"role": "user", "content": transcript})

        for _ in range(5):
            try:
                response = client_ollama.chat.completions.create(
                    model=ollama_model,
                    messages=OLLAMA_CHAT_HISTORY,
                    tools=openai_tools,
                    tool_choice="auto"
                )
            except Exception as e:
                print(f"Error calling Ollama API: {e}")
                return f"Sorry, I had trouble talking to the local model: {e}"
            
            choice = response.choices[0]
            message = choice.message
            
            # Convert choice.message to a dict format compatible with history
            assistant_msg = {
                "role": "assistant",
                "content": message.content or ""
            }
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    } for tc in message.tool_calls
                ]
            
            OLLAMA_CHAT_HISTORY.append(assistant_msg)

            if not message.tool_calls:
                # No more tool calls, we have our final text reply
                reply = message.content or ""
                return proofread_text(reply)

            # We have tool calls, process them
            for tool_call in message.tool_calls:
                fname = tool_call.function.name
                try:
                    fargs = json.loads(tool_call.function.arguments or "{}")
                except Exception:
                    fargs = {}
                print(f"[Ollama tool call] {fname}({fargs})")
                try:
                    result = perform_tool_action(conn, fname, **fargs)
                    for listener in tool_listeners:
                        try:
                            listener(fname, fargs, result)
                        except Exception as e:
                            print(f"Error in tool listener: {e}")
                except Exception as e:
                    result = {"error": str(e)}

                # Append tool response
                OLLAMA_CHAT_HISTORY.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": fname,
                    "content": json.dumps(result, default=str)
                })

        # Fallback if loop exceeded
        return "I processed your request but took too many steps."
    finally:
        conn.close()

