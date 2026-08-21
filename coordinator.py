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

_state_providers = {}

def register_state_provider(name, func):
    _state_providers[name] = func

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
                        "go_to_execution",
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
                        "reset_camera",
                        "exec_open_task_panel",
                        "exec_close_panel",
                        "exec_open_agent_panel",
                        "exec_drill_department",
                        "exec_exit_drill",
                        "exec_show_idle",
                        "exec_show_active",
                        "exec_open_console"
                    ],
                    "description": (
                        "go_to_plan: Navigate to the Plan page. "
                        "go_to_brain: Return to Brain Core (3D map home). "
                        "go_to_apis: Navigate to the APIs/MCPs comparison page. "
                        "go_to_execution: Navigate to the Execution page dashboard. "
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
                        "reset_camera: Reset the 3D camera to default position. "
                        "exec_open_task_panel: Open the task constellation side panel (execution idle view). "
                        "exec_close_panel: Close any open execution side panel. "
                        "exec_open_agent_panel: Open agent detail side panel — requires payload.agent_id. "
                        "exec_drill_department: Drill into a specific department cycle — requires payload.department. "
                        "exec_exit_drill: Exit department drill-down back to full constellation. "
                        "exec_show_idle: Switch to idle/task constellation view. "
                        "exec_show_active: Switch to active pipeline constellation view. "
                        "exec_open_console: Navigate to the Task Logs/Chat page."
                    )
                },
                "payload": {
                    "type": "object",
                    "description": "Extra data. task_id for focus/detail; message for notifications; department for drill-down; agent_id for agent panel.",
                    "properties": {
                        "task_id": {"type": "integer", "description": "Task ID to focus on or open detail for"},
                        "message": {"type": "string", "description": "Message text for flash_notification"},
                        "department": {"type": "string", "description": "Department/cycle name for exec_drill_department"},
                        "agent_id": {"type": "string", "description": "Agent ID for exec_open_agent_panel"}
                    }
                }
            },
        }
    },
    {
        "name": "read_app_snapshot",
        "description": "Read the live state of the app UI: which page is open, which panel is open, the orb state, sleeping state, and the last 10 conversation messages. Call this BEFORE any control_interface action.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "update_task",
        "description": "Update an existing task. Change its content, priority, effort estimate, scheduled time, due date, or status. Cannot change parent_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "The ID of the task to update."},
                "content": {"type": "string", "description": "New content/title of the task. Only provide this if renaming the task."},
                "priority": {"type": "string", "enum": ["low", "medium", "high"], "description": "New priority level. Only provide this if changing the task's priority."},
                "effort_estimate": {"type": "string", "enum": ["small", "medium", "large"], "description": "New effort estimate. Only provide this if changing effort."},
                "scheduled_at": {"type": "string", "description": "New ISO datetime scheduled commitment. Only provide this if changing schedule."},
                "due_date": {"type": "string", "description": "New ISO date soft deadline. Only provide this if changing due date."},
                "status": {"type": "string", "enum": ["open", "in_progress", "done"], "description": "New status. Only provide this if changing task status."}
            },
            "required": ["task_id"]
        }
    },
    {
        "name": "add_subtasks",
        "description": "Break a task into multiple subtasks at once. Each step becomes a child task.",
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "integer"},
                "steps": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["parent_id", "steps"]
        }
    },
    {
        "name": "batch_create_tasks",
        "description": "Create multiple tasks in one call. Each item needs at least 'content'.",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                            "effort_estimate": {"type": "string", "enum": ["small", "medium", "large"]},
                            "scheduled_at": {"type": "string"},
                            "due_date": {"type": "string"},
                            "parent_id": {"type": "integer"}
                        },
                        "required": ["content"]
                    }
                }
            },
            "required": ["tasks"]
        }
    },
    {
        "name": "batch_delete_tasks",
        "description": "Delete multiple tasks at once by their IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "integer"}}
            },
            "required": ["task_ids"]
        }
    },
    {
        "name": "batch_create_notes",
        "description": "Create multiple notes in one call. Each item needs at least 'content'.",
        "parameters": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "tags": {"type": "string"},
                            "task_id": {"type": "integer"}
                        },
                        "required": ["content"]
                    }
                }
            },
            "required": ["notes"]
        }
    },
    {
        "name": "batch_delete_notes",
        "description": "Delete multiple notes at once by their IDs.",
        "parameters": {
            "type": "object",
            "properties": {
                "note_ids": {"type": "array", "items": {"type": "integer"}}
            },
            "required": ["note_ids"]
        }
    },
    {
        "name": "read_settings",
        "description": "Read the current app settings: AI provider, voice speed, wake word threshold, and theme.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "change_settings",
        "description": "Change app settings. IMPORTANT: Always confirm with the user before applying. Say what you're about to change and ask 'Shall I proceed?'",
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["gemini", "ollama"]},
                "voice_speed": {"type": "integer", "description": "WPM (100-300)"},
                "wake_word_threshold": {"type": "number", "description": "0.1-0.9"},
                "theme": {"type": "string", "enum": ["cyberpunk", "dark", "light"]}
            }
        }
    },
    {
        "name": "start_pipeline",
        "description": "Launch the multi-agent pipeline for a complex task. The pipeline runs through research, synthesis, human gate review, execution, and deploy phases.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description for the pipeline"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "resume_pipeline",
        "description": "Resume a paused, active, or unfinished pipeline using its plan_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "The unique 8-character ID of the pipeline plan to resume"}
            },
            "required": ["plan_id"]
        }
    },
    {
        "name": "delete_pipeline",
        "description": "Delete a pipeline project permanently by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "The unique ID of the pipeline plan to delete"}
            },
            "required": ["plan_id"]
        }
    },
    {
        "name": "get_gate_status",
        "description": "Check the current pipeline gate status: which gate is active, waiting/approved/rejected, and the pipeline phase.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "get_pipelines",
        "description": "Retrieve all active and past pipeline projects, including their IDs, tasks, project names, phases, and statuses.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "update_metric",
        "description": "Set or update a tracked KPI metric with a name, current value, and threshold.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Metric name (e.g. youtube_ctr)"},
                "value": {"type": "number", "description": "Current value"},
                "threshold": {"type": "number", "description": "Alert threshold"}
            },
            "required": ["name", "value"]
        }
    },
    {
        "name": "read_metrics",
        "description": "Read all currently tracked metrics and their values/thresholds.",
        "parameters": {"type": "object", "properties": {}}
    }
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
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction="You are a search grounding assistant. Search the web for the user's query and provide a factual, concise summary of the results with references if appropriate."
        )
        res = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=query,
            config=search_config
        )
        return res.text or "No results found."
    except Exception as e:
        print(f"Error in outsourced Google Search: {e}. Falling back to general knowledge...")
        try:
            fallback_res = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Summarize what you know about: '{query}'. Keep your response extremely brief, under 15 words, factual and spoken-friendly."
            )
            return fallback_res.text or "Search service is currently unavailable."
        except Exception as fallback_err:
            return f"Search service is currently unavailable: {fallback_err}"


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
    "read_app_snapshot":      lambda conn, **kw: _state_providers["read_app_snapshot"]() if "read_app_snapshot" in _state_providers else (requests.get(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/jarvis/snapshot",
        headers={"X-Jarvis-Token": os.getenv("JARVIS_SESSION_TOKEN", "jarvis-auth-token-xyz-789")},
        timeout=2
    ).json() if requests else {"error": "requests library missing"}),
    "update_task":        lambda conn, **kw: db.update_task(conn, **kw),
    "add_subtasks":       lambda conn, **kw: db.add_subtasks(conn, **kw),
    "batch_create_tasks": lambda conn, **kw: db.batch_create_tasks(conn, **kw),
    "batch_delete_tasks": lambda conn, **kw: db.batch_delete_tasks(conn, **kw),
    "batch_create_notes": lambda conn, **kw: db.batch_create_notes(conn, **kw),
    "batch_delete_notes": lambda conn, **kw: db.batch_delete_notes(conn, **kw),
    "read_settings":      lambda conn, **kw: _state_providers["read_settings"]() if "read_settings" in _state_providers else (requests.get(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/settings",
        timeout=2
    ).json().get("settings", {}) if requests else {}),
    "change_settings":    lambda conn, **kw: _state_providers["change_settings"](kw) if "change_settings" in _state_providers else (requests.post(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/settings",
        json=kw,
        timeout=2
    ).json().get("settings", {}) if requests else {}),
    "start_pipeline":     lambda conn, **kw: _state_providers["start_pipeline"](kw) if "start_pipeline" in _state_providers else (requests.post(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/pipeline/start",
        json=kw,
        timeout=2
    ).json() if requests else {}),
    "resume_pipeline":    lambda conn, **kw: _state_providers["resume_pipeline"](kw) if "resume_pipeline" in _state_providers else (requests.post(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/pipeline/resume",
        json=kw,
        timeout=2
    ).json() if requests else {}),
    "delete_pipeline":    lambda conn, **kw: _state_providers["delete_pipeline"](kw) if "delete_pipeline" in _state_providers else (requests.post(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/pipeline/delete",
        json=kw,
        timeout=2
    ).json() if requests else {}),
    "get_gate_status":    lambda conn, **kw: _state_providers["get_gate_status"]() if "get_gate_status" in _state_providers else (requests.get(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/gate/status",
        timeout=2
    ).json() if requests else {}),
    "get_pipelines":      lambda conn, **kw: _state_providers["get_pipelines"]() if "get_pipelines" in _state_providers else (requests.get(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/plans",
        timeout=2
    ).json().get("plans", []) if requests else {}),
    "update_metric":      lambda conn, **kw: _state_providers["update_metric"](kw) if "update_metric" in _state_providers else (requests.post(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/metrics/update",
        json=kw,
        timeout=2
    ).json() if requests else {}),
    "read_metrics":       lambda conn, **kw: _state_providers["read_metrics"]() if "read_metrics" in _state_providers else (requests.get(
        "http://127.0.0.1:" + os.getenv("JARVIS_PORT", "5000") + "/metrics/get",
        timeout=2
    ).json().get("metrics", {}) if requests else {}),
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
    "  - Execution Page (execution.html): Real-time execution stream and task logs. "
    "    Action: go_to_execution\n"
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
    "EXECUTION PAGE (execution.html):\n"
    "  Sub-views:\n"
    "    - Idle View: Task Constellation Map (when no pipeline running). Shows task nodes orbiting core.\n"
    "    - Active View: Department Execution Constellation (when pipeline running). Shows agent department cycles orbiting core.\n"
    "  Actions (only when current_page is 'Execution'):\n"
    "    - exec_open_task_panel: Open task database side panel in idle view.\n"
    "    - exec_close_panel: Close any open side panel.\n"
    "    - exec_drill_department + payload.department: Drill into a department cycle (e.g. 'research').\n"
    "    - exec_exit_drill: Exit drill-down, return to full constellation.\n"
    "    - exec_open_agent_panel + payload.agent_id: Open agent detail panel.\n"
    "    - exec_show_idle: Switch to idle task constellation view.\n"
    "    - exec_show_active: Switch to active pipeline constellation view.\n"
    "    - exec_open_console: Navigate to Task Logs/Chat page.\n"
    "VOICE EXAMPLES (always invoke control_interface for these):\n"
    "  'open settings' → open_settings_panel\n"
    "  'show the plan' / 'go to plan' → go_to_plan\n"
    "  'go to APIs' / 'show providers' → go_to_apis\n"
    "  'open notes' / 'show my notes' → open_notes_panel\n"
    "  'show task 5' / 'open task 5' → open_task_detail, payload={task_id:5}\n"
    "  'close the panel' / 'close everything' → close_side_panel\n"
    "  'reset the view' → reset_camera\n"
    "  'go home' / 'back to brain' → go_to_brain\n"
    "  'update task 3 priority to high' → update_task\n"
    "  'change voice speed to 200' → change_settings (with confirmation)\n"
    "  'break task 5 down into steps' → add_subtasks\n"
    "  'add 3 tasks: buy milk, clean house, call mom' → batch_create_tasks\n"
    "  'delete tasks 4, 5, and 6' → batch_delete_tasks\n"
    "  'save these 3 notes...' → batch_create_notes\n"
    "  'delete notes 1, 2, and 3' → batch_delete_notes\n"
    "  'start a pipeline for X' → start_pipeline\n"
    "  'resume project Y' / 'resume pipeline Y' → resume_pipeline\n"
    "  'delete project Y' / 'delete pipeline Y' → delete_pipeline\n"
    "  'check pipeline status' → get_gate_status\n"
    "  'show all projects' / 'list pipelines' → get_pipelines\n"
    "  'what are my current settings?' → read_settings\n"
    "  'track youtube CTR at 0.03' → update_metric\n"
    "  'show all metrics' → read_metrics\n"
    "  'switch to gem' → change_settings(provider='gemini')\n"
    "  'drill into research' (on execution page) → exec_drill_department, payload={department:'research'}\n"
    "  'zoom out' (on execution page) → exec_exit_drill\n"
    "  'show the pipeline' (on execution page) → exec_show_active\n"
    "  'open console' / 'show logs' → exec_open_console\n"
    "  'show task database' (on execution page) → exec_open_task_panel\n"
)

SYSTEM_PROMPT = (
    "You are Jarvis, a helpful personal AI assistant with full control over the user's app. "
    "You have access to their task and note database, Google Search for real-time info, "
    "and complete control over the app's user interface.\n\n"
    + UI_MAP +
    "\nRULES:\n"
    "1. Before performing any navigation or UI action, ALWAYS call `read_app_snapshot` first to see what page and panel is currently open. "
    "Never navigate somewhere that is already open (e.g. do not open settings if the snapshot shows settings panel is open).\n"
    "2. For any navigation or UI action, ALWAYS invoke 'control_interface' — never just describe it.\n"
    "3. When the user mentions a specific task by name or ID, call 'focus_tasks' with its ID.\n"
    "4. When you take a UI action, briefly confirm it aloud in 1 sentence (e.g. 'Opening settings, Sir.').\n"
    "5. Use database tools to add/list/complete/delete tasks and notes as requested.\n"
    "6. Keep all spoken replies extremely brief (strictly 1 sentence max, under 15 words) since they are read aloud to the user. Never output more than one sentence.\n"
    "7. Never give explanations or long introductions. State only what you did or will do immediately and concisely.\n"
    "8. Before changing any settings (change_settings), ALWAYS tell the user what you're about to change "
    "and ask 'Shall I proceed, Sir?' — only apply after explicit confirmation.\n"
    "9. You CANNOT approve or reject pipeline gates. Those are human-only review steps.\n"
    "10. Never print, output, or speak raw code blocks, JSON, programming functions (such as AnimationFrame, update_task, etc.), or technical code arguments in your replies. Keep your responses strictly conversational, spoken English (e.g. 'I have updated the priority to medium, Sir.').\n"
    "11. If the user refers to a task by name, alias, or description (e.g. 'delete the beta task'), ALWAYS check `get_tasks` or `read_app_snapshot` first to find its ID. Do not ask the user for the ID unless it is not present in the list.\n"
    "12. If the user says 'switch to gem' or references 'gem', interpret it as 'switch to gemini' and invoke `change_settings` with provider='gemini' (always ask 'Shall I switch from Ollama to Gemini, Sir?' first to confirm settings change).\n"
    "13. If the user refers to you as 'Jar' or says 'Jar', understand that they are addressing you as 'Jarvis'.\n"
    "14. If the user asks to resume, track, delete, or check a pipeline by project name or task, ALWAYS check `get_pipelines` first to identify its ID.\n"
    "15. Before deleting a pipeline (delete_pipeline), you MUST request double confirmation from the user conversationally. First ask: 'Are you sure you want to delete pipeline Y, Sir?'. If they confirm, ask a second time: 'Please confirm once more, Sir: this action is permanent. Should I delete Y?'. Do NOT invoke the delete_pipeline tool until they have confirmed BOTH times.\n"
    "16. When the user is on the Execution Page (current_page contains 'Execution'), use exec_* actions for "
    "in-page navigation. For cross-page navigation from execution, the system will automatically append "
    "'?from=execution' to preserve context.\n"
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
        # Prune history to keep context clean for small local models (keeps system prompt + last 10 messages)
        if len(OLLAMA_CHAT_HISTORY) > 12:
            system_msg = OLLAMA_CHAT_HISTORY[0]
            OLLAMA_CHAT_HISTORY = [system_msg] + OLLAMA_CHAT_HISTORY[-10:]

        # If history is empty, add system prompt
        if not any(m.get("role") == "system" for m in OLLAMA_CHAT_HISTORY):
            OLLAMA_CHAT_HISTORY.append({
                "role": "system",
                "content": SYSTEM_PROMPT
            })

        # Append user message
        OLLAMA_CHAT_HISTORY.append({"role": "user", "content": transcript})

        tool_called_in_session = False
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
                reply = (message.content or "").strip()
                if not reply and tool_called_in_session:
                    reply = "Done, Sir."
                return proofread_text(reply)

            # We have tool calls, process them
            tool_called_in_session = True
            instant_reply = None
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

                # Instant confirmation mappings to avoid slow second LLM turn under local Ollama CPU inference
                if fname == "control_interface":
                    action = fargs.get("action")
                    if action not in ("focus_task", "flash_notification"):
                        action_map = {
                            "go_to_plan": "Navigating to the Plan page, Sir.",
                            "go_to_brain": "Returning to the Brain Core, Sir.",
                            "go_to_apis": "Going to the APIs page, Sir.",
                            "go_to_execution": "Going into execution mode, Sir.",
                            "open_side_panel": "Opening the side panel, Sir.",
                            "close_side_panel": "Closing the side panel, Sir.",
                            "open_notes_panel": "Opening notes panel, Sir.",
                            "open_settings_panel": "Opening settings, Sir.",
                            "open_task_detail": "Opening task details, Sir.",
                            "exit_completely": "Shutting down completely, goodbye.",
                            "wake_up": "I am awake, Sir.",
                            "go_to_sleep": "Going to sleep, Sir.",
                            "reset_camera": "Camera view reset, Sir.",
                            "exec_open_task_panel": "Opening task database, Sir.",
                            "exec_close_panel": "Closing the panel, Sir.",
                            "exec_drill_department": "Drilling into that department, Sir.",
                            "exec_exit_drill": "Zooming back out, Sir.",
                            "exec_show_idle": "Switching to idle view, Sir.",
                            "exec_show_active": "Showing pipeline constellation, Sir.",
                            "exec_open_console": "Opening the console, Sir.",
                            "exec_open_agent_panel": "Opening agent details, Sir."
                        }
                        if action in action_map:
                            instant_reply = action_map[action]
                elif fname == "complete_task":
                    instant_reply = "Task marked as completed, Sir."
                elif fname == "delete_task":
                    instant_reply = "Task deleted, Sir."
                elif fname == "update_task":
                    instant_reply = "Task updated, Sir."
                elif fname == "add_subtasks":
                    instant_reply = "Subtasks added, Sir."
                elif fname == "batch_create_tasks":
                    instant_reply = "Tasks created, Sir."
                elif fname == "batch_delete_tasks":
                    instant_reply = "Tasks deleted, Sir."
                elif fname == "complete_note":
                    instant_reply = "Note marked as completed, Sir."
                elif fname == "delete_note":
                    instant_reply = "Note deleted, Sir."
                elif fname == "add_task":
                    instant_reply = "Task added, Sir."
                elif fname == "add_note":
                    instant_reply = "Note saved, Sir."
                elif fname == "update_note":
                    instant_reply = "Note updated, Sir."
                elif fname == "batch_create_notes":
                    instant_reply = "Notes saved, Sir."
                elif fname == "batch_delete_notes":
                    instant_reply = "Notes deleted, Sir."
                elif fname == "save_memory_pattern":
                    instant_reply = "Pattern saved, Sir."

            if instant_reply and len(message.tool_calls) == 1:
                OLLAMA_CHAT_HISTORY.append({
                    "role": "assistant",
                    "content": instant_reply
                })
                return instant_reply

        # Fallback if loop exceeded
        return "I processed your request but took too many steps."
    finally:
        conn.close()

