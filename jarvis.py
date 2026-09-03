"""
Jarvis - Second Brain (all-in-one, local only)
------------------------------------------------
One script, one process, runs entirely on your PC:

  - Listens for the wake word "Hey Jarvis" through your microphone
  - When triggered, opens the Command Center screen in your browser
  - Everything you say gets transcribed, sent to Gemini (which can
    add/list/complete/delete tasks and notes), and spoken back to you
  - The Command Center screen updates live to match the conversation

The web server only binds to 127.0.0.1 (localhost) - it is NOT reachable
from your home network or the internet. Nothing here is exposed outside
this one PC.
"""

import os
import sys
import json
import time
import subprocess
import threading

import numpy as np
import pyaudio
import openwakeword
from openwakeword.model import Model as WakeModel
import speech_recognition as sr
import pyttsx3
from google import genai
from google.genai import types
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
import webview

import db

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not found. Copy .env.example to .env and fill it in.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")

PORT = int(os.getenv("JARVIS_PORT", "5000"))
HOST = os.getenv("JARVIS_HOST", "0.0.0.0")
URL = f"http://127.0.0.1:{PORT}/command-center"

# ---------------------------------------------------------------------------
# Settings & Settings persistence
# ---------------------------------------------------------------------------

SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "theme": "cyberpunk",
    "voice_speed": 175,
    "wake_word_threshold": 0.5
}
SETTINGS_LOCK = threading.Lock()
SETTINGS = DEFAULT_SETTINGS.copy()

def load_settings():
    global SETTINGS
    with SETTINGS_LOCK:
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        SETTINGS[k] = v
            except Exception as e:
                print(f"Error reading settings: {e}")
        return SETTINGS.copy()

def save_settings(new_settings):
    global SETTINGS
    with SETTINGS_LOCK:
        for k, v in new_settings.items():
            SETTINGS[k] = v
        try:
            with open(SETTINGS_PATH, "w") as f:
                json.dump(SETTINGS, f, indent=2)
        except Exception as e:
            print(f"Error saving settings: {e}")
        return SETTINGS.copy()

# Initialize settings
load_settings()

def get_wake_word_threshold():
    with SETTINGS_LOCK:
        return SETTINGS.get("wake_word_threshold", 0.5)

# ---------------------------------------------------------------------------
# Shared state between the mic thread and the browser page.
# The browser polls /state and re-renders whenever this changes.
# ---------------------------------------------------------------------------

STATE_LOCK = threading.Lock()
CONVO = []            # list of {"role": "user"|"ai"|"system", "text": ...}
ORB_STATE = "idle"     # "idle" | "thinking" | "speaking"
FOCUS_TASK_IDS = []   # list of task IDs to highlight/focus on in UI
UI_ACTION = None      # e.g. {"type": "task_created", "task_id": 5, "priority": "high"}
JARVIS_SLEEPING = False # tracks sleep/wake visual state of UI/nebula
MIC_MUTED = False # tracks whether microphone is muted

ELEVENLABS_QUOTA_EXCEEDED = False # temporary runtime flag to skip ElevenLabs if quota exceeded

# --- Pipeline Gate State ---
# Keyed by plan_id — NOT a single shared dict. Multiple pipelines can be
# running (and gated) concurrently; a global PIPELINE_STATE meant every
# pipeline thread's gate_fn polled the SAME slot, so approving one plan's
# gate in the UI could silently resolve a completely different plan's gate
# instead (whichever one happened to occupy the shared slot at that moment).
PIPELINE_STATES: dict[str, dict] = {}
PIPELINE_LOCK = threading.Lock()


def _default_gate_state() -> dict:
    return {
        "current_gate": None,        # None | "cycle_1_research" | "execution_blueprint" | "final_qa"
        "gate_status": "idle",       # "idle" | "waiting" | "approved" | "rejected"
        "redirect_note": None,       # human's rejection reason
        "gate_data": None,           # the full data payload passed to the gate
        "rejected_steps": None,      # step IDs or indices that were rejected by the user
    }


def _get_gate_state(plan_id: str) -> dict:
    """Must be called while holding PIPELINE_LOCK."""
    return PIPELINE_STATES.setdefault(plan_id, _default_gate_state())

# --- Track Agent Metric Store ---
TRACKED_METRICS = {}   # e.g. {"youtube_ctr": {"value": 0.025, "threshold": 0.03}}
TRACKED_METRICS_LOCK = threading.Lock()

# --- Agent Observability State ---
AGENT_EVENT_LOG: list[dict] = []       # append-only log of all agent lifecycle events
AGENT_REGISTRY: dict[str, dict] = {}   # maps agent_id -> full config + status + output
AGENT_OBS_LOCK = threading.Lock()

# --- Pipeline Plan Storage ---
PLAN_STORE: list[dict] = []            # list of all active/past plans
PLAN_STORE_LOCK = threading.Lock()

# --- Pipeline Intake Drafts (clarification gate) ---
# A draft is everything the user has told us about a pipeline BEFORE the
# pipeline exists: their typed details, uploaded files, and the running Q&A.
# Deliberately NOT persisted to SQLite — cancelling an intake must leave no
# trace at all, so an abandoned draft dies with the process (or with the
# janitor below).
INTAKE_DRAFTS: dict[str, dict] = {}
INTAKE_DRAFTS_LOCK = threading.Lock()
INTAKE_DRAFT_TTL = 6 * 3600            # seconds before an untouched draft is swept

# Gemini can read these natively; everything else is stored and referenced by
# path so the agents can open it themselves.
INTAKE_READABLE_MIMES = ("image/", "application/pdf", "text/")
INTAKE_MAX_INLINE_BYTES = 15 * 1024 * 1024   # per-file cap on what we inline into a prompt


ACTIVE_PIPELINE_THREADS = set()
ACTIVE_PIPELINE_LOCK = threading.Lock()

def load_pipelines_from_db():
    global PLAN_STORE
    conn = db.get_connection(DB_PATH)
    try:
        loaded = db.get_pipelines(conn)
        with PLAN_STORE_LOCK:
            PLAN_STORE = loaded
    except Exception as e:
        print(f"Error loading pipelines from DB: {e}")
    finally:
        conn.close()

# Load persisted plans on startup
load_pipelines_from_db()


# --- Jarvis User & Identity Layer ---
import collections
JARVIS_SESSION_TOKEN = os.getenv("JARVIS_SESSION_TOKEN", "jarvis-auth-token-xyz-789")
JARVIS_UI_SNAPSHOT = {
    "current_page": "Brain Core",
    "panel_open": None,
    "orb": "idle",
    "sleeping": False
}
JARVIS_ACK_QUEUE = collections.deque(maxlen=20)
JARVIS_STATE_LOCK = threading.Lock()

# --- Real-Time Console & Inter-Agent Log Storage ---
CONSOLE_LOGS = []
CONSOLE_LOGS_LOCK = threading.Lock()

AGENT_CHAT_LOGS = []
AGENT_CHAT_LOGS_LOCK = threading.Lock()

# --- Actual Agent Conversation Logs (prompts sent + responses received) ---
AGENT_CONVERSATION_LOGS = []
AGENT_CONVERSATION_LOGS_LOCK = threading.Lock()

# --- Brain/Agent Thinking Logs (system prompts + input construction) ---
AGENT_THINKING_LOGS = []
AGENT_THINKING_LOGS_LOCK = threading.Lock()

# --- Narrative Progress Log (human-readable pipeline story) ---
NARRATIVE_LOGS = []
NARRATIVE_LOGS_LOCK = threading.Lock()


def append_console_log(level: str, text: str, source: str = "System"):
    entry = {"timestamp": time.time(), "level": level, "text": text, "source": source}
    with CONSOLE_LOGS_LOCK:
        CONSOLE_LOGS.append(entry)
        if len(CONSOLE_LOGS) > 500:
            CONSOLE_LOGS.pop(0)

def append_agent_conversation(agent_id: str, direction: str, role: str, content: str):
    """direction: 'prompt_sent' or 'response_received'"""
    entry = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "role": role,
        "direction": direction,
        "content": content
    }
    with AGENT_CONVERSATION_LOGS_LOCK:
        AGENT_CONVERSATION_LOGS.append(entry)
        if len(AGENT_CONVERSATION_LOGS) > 500:
            AGENT_CONVERSATION_LOGS.pop(0)

def append_agent_thinking(agent_id: str, role: str, thinking_type: str, content: str):
    """thinking_type: 'system_prompt' | 'user_prompt' | 'config_construction' | 'decision'"""
    entry = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "role": role,
        "thinking_type": thinking_type,
        "content": content
    }
    with AGENT_THINKING_LOGS_LOCK:
        AGENT_THINKING_LOGS.append(entry)
        if len(AGENT_THINKING_LOGS) > 500:
            AGENT_THINKING_LOGS.pop(0)

def append_narrative(phase: str, message: str, icon: str = "➡️"):
    entry = {
        "timestamp": time.time(),
        "phase": phase,
        "message": message,
        "icon": icon
    }
    with NARRATIVE_LOGS_LOCK:
        NARRATIVE_LOGS.append(entry)
        if len(NARRATIVE_LOGS) > 200:
            NARRATIVE_LOGS.pop(0)

def append_agent_chat(sender: str, receiver: str, message: str):
    entry = {"timestamp": time.time(), "sender": sender, "receiver": receiver, "message": message}
    with AGENT_CHAT_LOGS_LOCK:
        AGENT_CHAT_LOGS.append(entry)
        if len(AGENT_CHAT_LOGS) > 300:
            AGENT_CHAT_LOGS.pop(0)



def push_message(role, text):
    with STATE_LOCK:
        CONVO.append({"role": role, "text": text})
    if role in ("system", "ai", "jarvis"):
        append_console_log("info", text, source=role.capitalize())
        if any(k in text.lower() for k in ["pipeline", "gate", "agent", "task"]):
            append_agent_chat(sender=role.capitalize(), receiver="All Agents", message=text)


def set_orb(state):
    with STATE_LOCK:
        global ORB_STATE
        ORB_STATE = state


def speak(text: str):
    global ELEVENLABS_QUOTA_EXCEEDED
    print(f"[Jarvis] {text}")
    set_orb("speaking")
    
    eleven_key = os.getenv("ELEVENLABS_API_KEY")
    eleven_voice = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    
    played_via_eleven = False
    if eleven_key and not ELEVENLABS_QUOTA_EXCEEDED:
        try:
            import hashlib
            import requests
            import ctypes
            
            cache_dir = os.path.join(BASE_DIR, "data", "tts_cache")
            os.makedirs(cache_dir, exist_ok=True)
            text_hash = hashlib.md5((text + "_" + eleven_voice).encode("utf-8")).hexdigest()
            cache_path = os.path.join(cache_dir, f"{text_hash}.mp3")
            
            use_cached = os.path.exists(cache_path)
            success = True
            
            if not use_cached:
                url = f"https://api.elevenlabs.io/v1/text-to-speech/{eleven_voice}"
                headers = {
                    "xi-api-key": eleven_key,
                    "Content-Type": "application/json"
                }
                payload = {
                    "text": text,
                    "model_id": "eleven_turbo_v2",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75
                    }
                }
                res = requests.post(url, json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    with open(cache_path, "wb") as f:
                        f.write(res.content)
                else:
                    print(f"[ElevenLabs Error] {res.status_code}: {res.text}")
                    if res.status_code in (401, 429) or "quota" in res.text.lower():
                        ELEVENLABS_QUOTA_EXCEEDED = True
                        print("[Jarvis] ElevenLabs API quota exceeded. Temporarily falling back to local TTS for this session.")
                    success = False
            
            if success and os.path.exists(cache_path):
                path_str = os.path.abspath(cache_path)
                ctypes.windll.winmm.mciSendStringW(f'open "{path_str}" type mpegvideo alias jarvis_voice', None, 0, 0)
                
                updater_active = [True]
                def simulate_words():
                    global CURRENT_SPOKEN_WORD
                    words = text.split()
                    for i, w in enumerate(words):
                        if not updater_active[0]:
                            break
                        CURRENT_SPOKEN_WORD = f"{w}_{i}"
                        time.sleep(0.35)
                
                sim_thread = threading.Thread(target=simulate_words, daemon=True)
                sim_thread.start()

                ctypes.windll.winmm.mciSendStringW('play jarvis_voice wait', None, 0, 0)
                ctypes.windll.winmm.mciSendStringW('close jarvis_voice', None, 0, 0)
                updater_active[0] = False
                played_via_eleven = True
        except Exception as e:
            print(f"[ElevenLabs Exception] {e}")

    if not played_via_eleven:
        try:
            engine = pyttsx3.init()
            with SETTINGS_LOCK:
                speed = SETTINGS.get("voice_speed", 175)
            engine.setProperty('rate', int(speed))
            
            def on_word(name, location, length):
                global CURRENT_SPOKEN_WORD
                words = text.split()
                char_idx = 0
                for i, w in enumerate(words):
                    char_idx += len(w) + 1
                    if char_idx >= location:
                        CURRENT_SPOKEN_WORD = f"{w}_{i}"
                        break

            engine.connect('started-word', on_word)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            print(f"[Local TTS Error] {e}")
            
    global CURRENT_SPOKEN_WORD
    CURRENT_SPOKEN_WORD = ""
    set_orb("idle")


import coordinator

def jarvis_tool_listener(name, args, result):
    global FOCUS_TASK_IDS, UI_ACTION
    if name == "focus_tasks":
        with STATE_LOCK:
            task_ids = result.get("task_ids", [])
            if isinstance(task_ids, (int, float)):
                FOCUS_TASK_IDS = [int(task_ids)]
            elif isinstance(task_ids, list):
                FOCUS_TASK_IDS = [int(x) for x in task_ids]
            else:
                FOCUS_TASK_IDS = []

    with STATE_LOCK:
        if name == "add_task":
            parent_id = args.get("parent_id")
            new_id = result
            if parent_id:
                UI_ACTION = {
                    "type": "subtask_created",
                    "task_id": new_id,
                    "parent_id": parent_id
                }
            else:
                UI_ACTION = {
                    "type": "task_created",
                    "task_id": new_id,
                    "priority": args.get("priority", "medium")
                }
        elif name == "delete_task":
            task_id = args.get("task_id")
            if task_id:
                UI_ACTION = {
                    "type": "task_deleted",
                    "task_id": task_id
                }
        elif name == "complete_task":
            task_id = args.get("task_id")
            if task_id:
                UI_ACTION = {
                    "type": "task_completed",
                    "task_id": task_id
                }
        elif name == "add_note":
            UI_ACTION = {
                "type": "note_created",
                "task_id": args.get("task_id"),
                "note_id": result
            }
        elif name == "delete_note":
            UI_ACTION = {
                "type": "note_deleted",
                "note_id": args.get("note_id")
            }
        elif name == "search_notes":
            UI_ACTION = {
                "type": "note_search"
            }
        elif name == "google_search":
            UI_ACTION = {
                "type": "google_search"
            }
        elif name == "control_interface":
            action = args.get("action")
            if action == "go_to_execution":
                UI_ACTION = {"type": "navigate", "url": "execution.html"}
            elif action == "go_to_plan":
                with JARVIS_STATE_LOCK:
                    cp = JARVIS_UI_SNAPSHOT.get("current_page", "")
                is_exec = "execution" in cp.lower() or "task logs" in cp.lower()
                url = "plan.html?from=execution" if is_exec else "plan.html"
                UI_ACTION = {"type": "navigate", "url": url}
            elif action == "go_to_brain":
                UI_ACTION = {"type": "navigate", "url": "command_center.html"}
            elif action == "go_to_apis":
                with JARVIS_STATE_LOCK:
                    cp = JARVIS_UI_SNAPSHOT.get("current_page", "")
                is_exec = "execution" in cp.lower() or "task logs" in cp.lower()
                url = "provider_comparison.html?from=execution" if is_exec else "provider_comparison.html"
                UI_ACTION = {"type": "navigate", "url": url}
            elif action and action.startswith("exec_"):
                # Forward execution-specific actions directly
                UI_ACTION = {
                    "type": "execution_control",
                    "action": action,
                    "payload": args.get("payload") or {}
                }
            else:
                UI_ACTION = {
                    "type": "control_interface",
                    "action": action,
                    "payload": args.get("payload") or {}
                }
        elif name == "update_task":
            changed_fields = {k: v for k, v in args.items() if k != "task_id"}
            UI_ACTION = {
                "type": "task_updated",
                "task_id": args.get("task_id"),
                "changed_fields": changed_fields
            }
        elif name == "add_subtasks":
            UI_ACTION = {"type": "subtasks_batch_created", "parent_id": args.get("parent_id"),
                         "count": len(result) if isinstance(result, list) else 0}
        elif name == "batch_create_tasks":
            tasks_list = args.get("tasks", [])
            priorities = [t.get("priority", "medium") for t in tasks_list]
            UI_ACTION = {
                "type": "tasks_batch_created",
                "task_ids": result if isinstance(result, list) else [],
                "priorities": priorities,
                "count": len(result) if isinstance(result, list) else 0
            }
        elif name == "batch_delete_tasks":
            UI_ACTION = {
                "type": "tasks_batch_deleted",
                "task_ids": args.get("task_ids", []),
                "count": result if isinstance(result, int) else 0
            }
        elif name == "batch_create_notes":
            notes_in = args.get("notes", [])
            notes_payload = []
            if isinstance(result, list):
                for i, nid in enumerate(result):
                    task_id = notes_in[i].get("task_id") if i < len(notes_in) else None
                    notes_payload.append({"note_id": nid, "task_id": task_id})
            UI_ACTION = {
                "type": "notes_batch_created",
                "notes": notes_payload,
                "count": len(result) if isinstance(result, list) else 0
            }
        elif name == "batch_delete_notes":
            UI_ACTION = {
                "type": "notes_batch_deleted",
                "note_ids": args.get("note_ids", []),
                "count": result if isinstance(result, int) else 0
            }

def get_snapshot_local():
    conn = db.get_connection(DB_PATH)
    try:
        tasks_list = db.get_tasks(conn)
        notes_rows = conn.execute("SELECT * FROM notes WHERE status = 'open'").fetchall()
        notes_list = [dict(r) for r in notes_rows]
    finally:
        conn.close()

    with JARVIS_STATE_LOCK:
        snapshot = JARVIS_UI_SNAPSHOT.copy()

    with STATE_LOCK:
        snapshot["messages"] = CONVO[-10:]

    snapshot["tasks"] = tasks_list
    snapshot["notes"] = notes_list
    snapshot["acks"] = list(JARVIS_ACK_QUEUE)
    return snapshot

def change_settings_local(settings_dict):
    current = load_settings()
    for k in ["theme", "voice_speed", "wake_word_threshold", "provider", "ollama_model", "ollama_url"]:
        if k in settings_dict:
            if k == "voice_speed":
                current[k] = int(settings_dict[k])
            elif k == "wake_word_threshold":
                current[k] = float(settings_dict[k])
            else:
                current[k] = str(settings_dict[k])
    save_settings(current)
    return current

def get_project_name(task: str) -> str:
    import re
    if GEMINI_API_KEY:
        try:
            prompt = (
                "Given this task description, generate a short, clean, descriptive project name "
                "(2-4 words maximum, capitalize the first letter of each word, do not use special characters or punctuation, simple spaces only). "
                f"Task: {task}\nProject Name:"
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            name = response.text.strip().replace("\"", "").replace("'", "").replace("/", "_").replace("\\", "_")
            name = re.sub(r'[^a-zA-Z0-9\s_-]', '', name).strip()
            if name:
                return name
        except Exception as e:
            print(f"Failed to generate project name via LLM: {e}")
    # Fallback to cleaning the task name
    cleaned = re.sub(r'[^a-zA-Z0-9\s_-]', '', task[:40]).strip()
    return cleaned if cleaned else "Default Project"

def create_initial_task_log(plan_id: str, task: str, project_name: str):
    import time
    log_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Task Logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"pipeline_{plan_id}.md")
    
    content = f"# Pipeline Task Log - Plan ID: {plan_id}\n"
    content += f"- **Task:** {task}\n"
    content += f"- **Start Time:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
    content += f"- **Status:** Running\n\n"
    content += "## Execution Progress\n\n"
    content += "- [ ] Central Brain generating agent plan...\n"
    
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(content)
        
    try:
        os.startfile(log_file)
    except Exception as e:
        print(f"Failed to automatically open log file: {e}")

def update_task_log_file(plan_id: str, event: dict):
    if not plan_id:
        return
    import time
    
    plan_data = None
    with PLAN_STORE_LOCK:
        for p in PLAN_STORE:
            if p["id"] == plan_id:
                plan_data = p.copy()
                break
                
    if not plan_data:
        return

    project_name = plan_data.get("project_name", "Default Project")
    log_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Task Logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"pipeline_{plan_id}.md")

    plan_events = []
    with AGENT_OBS_LOCK:
        for e in AGENT_EVENT_LOG:
            if e.get("plan_id") == plan_id:
                plan_events.append(e)

    content = f"# Pipeline Task Log - Plan ID: {plan_id}\n"
    content += f"- **Task:** {plan_data.get('task')}\n"
    content += f"- **Phase:** {plan_data.get('phase', 'N/A').upper()}\n"
    content += f"- **Status:** {plan_data.get('status', 'running').upper()}\n\n"
    
    content += "## Current Execution Flow\n"
    phase = plan_data.get('phase', 'research')
    status = plan_data.get('status')
    
    def get_chk(cond):
        return "[x]" if cond else "[ ]"
        
    content += f"- {get_chk(phase in ('execution', 'qa', 'deploy', 'complete'))} central brain planned and research cycles executed\n"
    content += f"- {get_chk(phase in ('execution', 'qa', 'deploy', 'complete') and plan_data.get('current_gate') != 'execution_blueprint')} master blueprint compiled and approved\n"
    content += f"- {get_chk(phase in ('qa', 'deploy', 'complete'))} execution agents completed\n"
    content += f"- {get_chk(status == 'complete')} quality checker passed and deployed\n\n"
    
    content += "## Detailed Process Logs\n"
    for e in plan_events:
        ts = time.strftime('%H:%M:%S', time.localtime(e.get('timestamp', time.time())))
        etype = e.get('event_type')
        src = e.get('source') or e.get('agent_id') or 'System'
        
        if etype == "spawned":
            content += f"- `{ts}` 🚀 **{src}** spawned\n"
        elif etype == "running":
            content += f"- `{ts}` ⏳ **{src}** is running...\n"
        elif etype == "completed":
            content += f"- `{ts}` ✅ **{src}** completed\n"
        elif etype == "error":
            content += f"- `{ts}` ❌ **{src}** failed: {e.get('data')}\n"
        elif etype == "gate_waiting":
            content += f"- `{ts}` 🚧 **Gate waiting** on: `{src}`\n"
        elif etype == "gate_resolved":
            gate_status = "Approved" if e.get('data', {}).get('approved') else "Rejected"
            content += f"- `{ts}` ⚖️ **Gate resolved** (`{src}`): **{gate_status}**\n"
        elif etype == "conflict":
            content += f"- `{ts}` ⚠️ **Conflict** in Synthesis\n"
        else:
            content += f"- `{ts}` ➡️ **{etype}** ({src})\n"

    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        print(f"Failed to update task log file: {e}")

# ---------------------------------------------------------------------------
# Pipeline Intake — the clarification gate that runs BEFORE a pipeline exists
#
# Flow: details + files  ->  gap questions, one at a time  ->  a written plan
# the user can edit  ->  approval.  Only the approval step creates a pipeline.
# ---------------------------------------------------------------------------

def _intake_project_dir(project_name: str, *parts: str) -> str:
    """Path inside this project's folder, creating it on the way."""
    path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def _intake_safe_filename(name: str) -> str:
    """Strip any directory component so an upload can only land in Inputs/."""
    import re
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r'[^A-Za-z0-9._ -]', "_", base)
    return base[:120] or "upload"


def _prune_intake_drafts():
    """Drop drafts nobody came back to, along with the files they uploaded."""
    import shutil, time as _time
    cutoff = _time.time() - INTAKE_DRAFT_TTL
    with INTAKE_DRAFTS_LOCK:
        stale = [d for d in INTAKE_DRAFTS.values() if d.get("touched", 0) < cutoff]
        for draft in stale:
            INTAKE_DRAFTS.pop(draft["draft_id"], None)
    for draft in stale:
        try:
            inputs = os.path.join(BASE_DIR, "Let Jarvis Handle It", draft["project_name"], "Inputs")
            if os.path.isdir(inputs) and not draft.get("approved"):
                shutil.rmtree(inputs, ignore_errors=True)
        except Exception as e:
            print(f"[Intake] Failed to sweep stale draft files: {e}")


def _get_intake_draft(draft_id: str):
    import time as _time
    with INTAKE_DRAFTS_LOCK:
        draft = INTAKE_DRAFTS.get((draft_id or "").strip())
        if draft:
            draft["touched"] = _time.time()
        return draft


def _intake_file_parts(draft: dict) -> list:
    """Inline the files Gemini can actually read (images, PDFs, text)."""
    parts = []
    for f in draft.get("files", []):
        mime = f.get("mime") or "application/octet-stream"
        if not mime.startswith(INTAKE_READABLE_MIMES):
            continue
        try:
            if os.path.getsize(f["path"]) > INTAKE_MAX_INLINE_BYTES:
                continue
            with open(f["path"], "rb") as fh:
                parts.append(types.Part.from_bytes(data=fh.read(), mime_type=mime))
        except Exception as e:
            print(f"[Intake] Could not inline {f.get('name')}: {e}")
    return parts


def _intake_context_text(draft: dict) -> str:
    """Everything the user has told us so far, as plain text for the prompt."""
    lines = [f"ORIGINAL REQUEST:\n{draft.get('task', '')}"]
    details = (draft.get("details") or "").strip()
    lines.append(f"\nDETAILS THE USER WROTE:\n{details if details else '(none given)'}")

    files = draft.get("files", [])
    if files:
        listed = "\n".join(f"- {f['name']} ({f.get('mime', 'unknown type')})" for f in files)
        lines.append(
            "\nATTACHED FILES (readable ones are included with this message):\n" + listed
        )
    else:
        lines.append("\nATTACHED FILES:\n(none)")

    qa = draft.get("qa", [])
    if qa:
        answered = "\n".join(f"Q: {item['question']}\nA: {item['answer']}" for item in qa)
        lines.append("\nCLARIFICATIONS ALREADY ANSWERED — never ask these again:\n" + answered)
    return "\n".join(lines)


def _intake_parse_json(text: str):
    """Gemini likes to wrap JSON in code fences; unwrap before parsing."""
    import re
    cleaned = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    return json.loads(cleaned)


def _intake_ask_gemini(draft: dict, instruction: str):
    """One Gemini call carrying the whole draft (text + readable files)."""
    contents = [instruction, _intake_context_text(draft)] + _intake_file_parts(draft)
    response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
    return _intake_parse_json(response.text)


_INTAKE_BRIEF_RULE = (
    "The brief is authoritative for WHAT the user wants. It is not a limit on HOW you work: "
    "research and search the web freely for anything the brief does not cover. Follow the brief "
    "exactly where it constrains you \u2014 if it names a specific source, tool, or approach, use that "
    "one instead of choosing your own. Attached files live at the paths listed in the brief; open "
    "them when they are relevant."
)


def _intake_next_questions(draft: dict) -> list[dict]:
    """Ask Gemini for the gaps that would genuinely change what gets built."""
    instruction = (
        "You are Jarvis, about to hand this job to a team of autonomous agents. Before they start, "
        "find what you genuinely do not know.\n\n"
        "RULES:\n"
        "- Only ask about gaps that would actually change what gets built. Skip anything you can "
        "reasonably infer, and skip preferences that would not alter the work.\n"
        "- Never re-ask something already answered below.\n"
        "- Read the attached files first; do not ask for information they already contain.\n"
        "- Ask at most 5 questions. Short, plain, one topic each. No compound questions.\n"
        "- If nothing material is missing, return an empty list. An empty list is the correct "
        "answer whenever you could brief the team without guessing.\n\n"
        "For each question also write \"gist\": a single short spoken line (under 15 words) that "
        "conveys the question aloud.\n\n"
        "Reply with JSON only: {\"questions\": [{\"question\": \"...\", \"gist\": \"...\"}]}"
    )
    try:
        data = _intake_ask_gemini(draft, instruction)
    except Exception as e:
        # A dead question round must not trap the user — fall through to the plan.
        print(f"[Intake] Question generation failed: {e}")
        return []

    questions = data.get("questions", []) if isinstance(data, dict) else data
    cleaned = []
    for q in questions or []:
        if isinstance(q, str):
            cleaned.append({"question": q, "gist": q})
        elif isinstance(q, dict) and q.get("question"):
            cleaned.append({"question": q["question"], "gist": q.get("gist") or q["question"]})
    return cleaned[:5]


def _intake_paint_picture(draft: dict) -> dict:
    """Either the plan, or another round of questions — Jarvis decides."""
    instruction = (
        "You are Jarvis. Using everything below, paint a picture of what the user asked for: a "
        "written plan they can read and correct.\n\n"
        "RULES:\n"
        "- Fold in every clarification and everything you can see in the attached files.\n"
        "- Plain language and concrete. Describe what will be built and for whom, not how you "
        "will manage the work.\n"
        "- Invent nothing. Do not add requirements the user never gave you.\n"
        "- If something material is still unclear, do NOT guess: return questions instead of a "
        "plan.\n\n"
        "Reply with JSON only, one of:\n"
        "{\"plan_text\": \"the plan in markdown\"}\n"
        "{\"questions\": [{\"question\": \"...\", \"gist\": \"...\"}]}"
    )
    try:
        data = _intake_ask_gemini(draft, instruction)
    except Exception as e:
        # Degrade to the raw brief so the user can still edit and approve something.
        print(f"[Intake] Painting the picture failed: {e}")
        return {
            "plan_text": _intake_brief_markdown(draft, include_plan=False),
            "degraded": "Jarvis could not reach the model to write the plan, so this is the raw "
                        "brief. You can edit it and build from it.",
        }

    if isinstance(data, dict) and data.get("questions"):
        questions = []
        for q in data["questions"]:
            if isinstance(q, str):
                questions.append({"question": q, "gist": q})
            elif isinstance(q, dict) and q.get("question"):
                questions.append({"question": q["question"], "gist": q.get("gist") or q["question"]})
        if questions:
            return {"questions": questions[:5]}

    plan_text = (data or {}).get("plan_text") if isinstance(data, dict) else None
    if not plan_text:
        plan_text = _intake_brief_markdown(draft, include_plan=False)
    return {"plan_text": plan_text}


def _intake_clean_edit(draft: dict, edited_text: str) -> str:
    """Tidy the user's edit without changing a single one of their decisions."""
    instruction = (
        "The user edited the plan below. Return it in a clean, consistent format.\n\n"
        "RULES:\n"
        "- Preserve every decision they made. Change no meaning.\n"
        "- Add no requirements they did not write, and remove none that they did.\n"
        "- Fix only structure, headings and wording.\n\n"
        "Reply with JSON only: {\"plan_text\": \"the cleaned plan in markdown\"}\n\n"
        "THE USER\u2019S EDITED PLAN:\n" + (edited_text or "")
    )
    try:
        data = _intake_ask_gemini(draft, instruction)
        cleaned = (data or {}).get("plan_text")
        if cleaned:
            return cleaned
    except Exception as e:
        print(f"[Intake] Cleaning the edit failed: {e}")
    # Never lose the user's words — keep their version verbatim if cleaning fails.
    return edited_text


def _intake_brief_markdown(draft: dict, include_plan: bool = True) -> str:
    """The complete record handed to the agents."""
    out = [f"# Clarified Brief \u2014 {draft.get('project_name', 'Project')}", ""]
    out += ["## Original request", draft.get("task", ""), ""]

    details = (draft.get("details") or "").strip()
    out += ["## Details from the user", details if details else "_(none given)_", ""]

    qa = draft.get("qa", [])
    if qa:
        out.append("## Clarifications")
        for item in qa:
            out += [f"**Q:** {item['question']}", "", f"**A:** {item['answer']}", ""]

    files = draft.get("files", [])
    if files:
        out.append("## Attached files")
        for f in files:
            out.append(f"- `Inputs/{f['name']}` \u2014 {f.get('mime', 'unknown type')}")
        out.append("")

    if include_plan and draft.get("plan_text"):
        out += ["## Approved plan", draft["plan_text"], ""]

    out += ["---", "", "## How to use this brief", _INTAKE_BRIEF_RULE, ""]
    return "\n".join(out)


def _intake_write_brief(draft: dict) -> str:
    brief_dir = _intake_project_dir(draft["project_name"], "Brief")
    brief_path = os.path.join(brief_dir, "clarified_brief.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(_intake_brief_markdown(draft))
    return brief_path


def _intake_discard(draft: dict):
    """Cancel means it never happened: forget the draft, delete its uploads."""
    import shutil
    with INTAKE_DRAFTS_LOCK:
        INTAKE_DRAFTS.pop(draft["draft_id"], None)
    try:
        inputs = os.path.join(BASE_DIR, "Let Jarvis Handle It", draft["project_name"], "Inputs")
        if os.path.isdir(inputs):
            shutil.rmtree(inputs, ignore_errors=True)
        project = os.path.join(BASE_DIR, "Let Jarvis Handle It", draft["project_name"])
        if os.path.isdir(project) and not os.listdir(project):
            os.rmdir(project)
    except Exception as e:
        print(f"[Intake] Failed to remove cancelled draft files: {e}")


def create_intake_draft(task: str) -> dict:
    """Start a draft. No pipeline, no DB row, nothing persistent yet."""
    import time as _time
    import uuid
    _prune_intake_drafts()
    draft = {
        "draft_id": uuid.uuid4().hex[:8],
        "task": task,
        "project_name": get_project_name(task),
        "details": "",
        "files": [],
        "qa": [],
        "pending_questions": [],
        "rounds": 0,
        "plan_text": None,
        "stage": "intake",
        "created": _time.time(),
        "touched": _time.time(),
    }
    with INTAKE_DRAFTS_LOCK:
        INTAKE_DRAFTS[draft["draft_id"]] = draft
    return draft


def initiate_pipeline(task: str, project_name: str | None = None,
                      brief_path: str | None = None,
                      task_summary: str | None = None) -> str:
    """Create the pipeline and start the agents.

    `task` may now be a full clarified brief rather than a one-liner, so:
      - `project_name` is passed in when the intake flow already derived one, so the
        Inputs/ folder the user uploaded into is the same folder the pipeline uses;
      - `task_summary` keeps the original short request for UI labels;
      - `brief_path` points the agents at the complete brief on disk.
    Called with none of them, this behaves exactly as it always did.
    """
    import time
    
    conn = db.get_connection(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM pipelines")
        ids = [row[0] for row in cursor.fetchall()]
        int_ids = []
        for val in ids:
            try:
                int_ids.append(int(val))
            except ValueError:
                pass
        next_id = max(int_ids) + 1 if int_ids else 1
        plan_id = str(next_id)
    except Exception as e:
        print(f"Error generating integer ID: {e}")
        plan_id = str(len(PLAN_STORE) + 1)
    finally:
        conn.close()
    
    project_name = project_name or get_project_name(task)
    task_summary = task_summary or task

    with PLAN_STORE_LOCK:
        plan_entry = {
            "id": plan_id,
            "task": task,
            "task_summary": task_summary,
            "brief_path": brief_path,
            "project_name": project_name,
            "status": "running",
            "current_gate": None,
            "gate_status": "idle",
            "phase": "research",
            "timestamp": time.time(),
            "cycles": [],
            "master_blueprint": {},
            "exec_results": [],
            "deploy_result": {}
        }
        PLAN_STORE.append(plan_entry)
        
        # Save to database
        conn = db.get_connection(DB_PATH)
        try:
            db.save_pipeline(conn, plan_entry)
        except Exception as e:
            print(f"Error persisting initial pipeline: {e}")
        finally:
            conn.close()


    push_message("system", f"Pipeline started [Plan ID: {plan_id}]: {task_summary[:80]}...")
    create_initial_task_log(plan_id, task_summary, project_name)

    async def gate_fn(gate_id: str, data: dict) -> dict:
        with PIPELINE_LOCK:
            state = _get_gate_state(plan_id)
            state["current_gate"] = gate_id
            state["gate_status"] = "waiting"
            state["redirect_note"] = None
            state["gate_data"] = data
            state["rejected_steps"] = None
        push_message("system", f"Gate {gate_id} is open. Waiting for your approval.")
        import asyncio
        while True:
            await asyncio.sleep(2)
            with PIPELINE_LOCK:
                state = _get_gate_state(plan_id)
                status = state["gate_status"]
                note = state["redirect_note"]
                rejected = state["rejected_steps"]
            if status in ("approved", "rejected"):
                with PIPELINE_LOCK:
                    state = _get_gate_state(plan_id)
                    state["current_gate"] = None
                    state["gate_status"] = "idle"
                    state["gate_data"] = None
                    state["rejected_steps"] = None
                return {"approved": status == "approved", "redirect_note": note, "rejected_steps": rejected}

    def run_pipeline():
        import asyncio
        from multi_agent_coordinator import run_full_pipeline
        with ACTIVE_PIPELINE_LOCK:
            ACTIVE_PIPELINE_THREADS.add(plan_id)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_full_pipeline(task, gate_fn, event_logger=pipeline_event_logger, plan_id=plan_id, project_name=project_name, brief_path=brief_path))
            status = result.get('status', 'done')
            if status == "escalated_to_human":
                reason = result.get('message', 'Max retries exceeded.')
                push_message("ai", f"Pipeline failed: {reason}")
            else:
                push_message("ai", f"Pipeline complete. Status: {status}.")
        except Exception as e:
            push_message("system", f"Pipeline error: {e}")
        finally:
            loop.close()
            with ACTIVE_PIPELINE_LOCK:
                ACTIVE_PIPELINE_THREADS.discard(plan_id)

    threading.Thread(target=run_pipeline, daemon=True).start()
    return plan_id

def normalize_spoken_id(plan_id: str) -> str:
    cleaned = str(plan_id).lower().strip()
    if cleaned.startswith("id"):
        cleaned = cleaned[2:].strip()
    number_map = {
        "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"
    }
    return number_map.get(cleaned, cleaned)

def start_pipeline_local(settings_dict):
    """Opens the clarification gate instead of launching the pipeline outright.

    Nothing is created here: no plan id, no DB row, no agent thread. We hand the
    UI a draft and ask whether the user wants to give more details first. If they
    say no, the frontend calls /pipeline/start and we build on the one-liner
    exactly as before.
    """
    global UI_ACTION
    task = settings_dict.get("task", "").strip()
    if not task:
        return {"error": "task is required"}

    if settings_dict.get("skip_intake"):
        plan_id = initiate_pipeline(task)
        return {"status": "pipeline_started", "task": task, "plan_id": plan_id}

    draft = create_intake_draft(task)
    with STATE_LOCK:
        UI_ACTION = {
            "type": "pipeline_intake_ask",
            "draft_id": draft["draft_id"],
            "task": task,
            "project_name": draft["project_name"],
        }
    push_message("ai", "Do you want to give me more details before I start?")
    return {
        "status": "awaiting_details",
        "task": task,
        "draft_id": draft["draft_id"],
        "note": "The pipeline has NOT started. Jarvis asked the user whether they want to add "
                "details first; the answer decides what happens next.",
    }

def resume_pipeline_local(settings_dict):
    plan_id = normalize_spoken_id(settings_dict.get("plan_id", ""))
    if not plan_id:
        return {"error": "plan_id is required"}
    force_reexecute = bool(settings_dict.get("force_reexecute", False))
    
    plan_entry = None
    with PLAN_STORE_LOCK:
        for plan in PLAN_STORE:
            if plan["id"] == plan_id:
                plan_entry = plan
                break
                
    if not plan_entry:
        load_pipelines_from_db()
        with PLAN_STORE_LOCK:
            for plan in PLAN_STORE:
                if plan["id"] == plan_id:
                    plan_entry = plan
                    break
                    
    if not plan_entry:
        return {"error": f"Pipeline plan '{plan_id}' not found"}

    task = plan_entry.get("task")
    project_name = plan_entry.get("project_name", "Default Project")
    # Pipelines started through the clarification gate carry a brief on disk.
    brief_path = plan_entry.get("brief_path")
    task_summary = plan_entry.get("task_summary") or task
    
    with PLAN_STORE_LOCK:
        plan_entry["status"] = "running"
    
    conn = db.get_connection(DB_PATH)
    try:
        db.save_pipeline(conn, plan_entry)
    except Exception as e:
        print(f"Error saving pipeline status on resume: {e}")
    finally:
        conn.close()

    # Automatically reopen the task log file on resumption
    log_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Task Logs")
    log_file = os.path.join(log_dir, f"pipeline_{plan_id}.md")
    if os.path.exists(log_file):
        try:
            os.startfile(log_file)
        except Exception as e:
            print(f"Failed to automatically open log file: {e}")

    if force_reexecute:
        push_message("system", f"Resuming pipeline [Plan ID: {plan_id}] with FORCE RE-EXECUTE: {task_summary[:80]}...")
    else:
        push_message("system", f"Resuming pipeline [Plan ID: {plan_id}]: {task_summary[:80]}...")

    async def gate_fn(gate_id: str, data: dict) -> dict:
        with PIPELINE_LOCK:
            state = _get_gate_state(plan_id)
            state["current_gate"] = gate_id
            state["gate_status"] = "waiting"
            state["redirect_note"] = None
            state["gate_data"] = data
            state["rejected_steps"] = None
        push_message("system", f"Gate {gate_id} is open. Waiting for your approval.")
        import asyncio
        while True:
            await asyncio.sleep(2)
            with PIPELINE_LOCK:
                state = _get_gate_state(plan_id)
                status = state["gate_status"]
                note = state["redirect_note"]
                rejected = state["rejected_steps"]
            if status in ("approved", "rejected"):
                with PIPELINE_LOCK:
                    state = _get_gate_state(plan_id)
                    state["current_gate"] = None
                    state["gate_status"] = "idle"
                    state["gate_data"] = None
                    state["rejected_steps"] = None
                return {"approved": status == "approved", "redirect_note": note, "rejected_steps": rejected}

    with ACTIVE_PIPELINE_LOCK:
        if plan_id in ACTIVE_PIPELINE_THREADS:
            return {"status": "already_running", "plan_id": plan_id}

    def run_pipeline():
        import asyncio
        from multi_agent_coordinator import run_full_pipeline
        with ACTIVE_PIPELINE_LOCK:
            ACTIVE_PIPELINE_THREADS.add(plan_id)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_full_pipeline(task, gate_fn, event_logger=pipeline_event_logger, plan_id=plan_id, project_name=project_name, force_reexecute=force_reexecute, brief_path=brief_path))
            status = result.get('status', 'done')
            if status == "escalated_to_human":
                reason = result.get('message', 'Max retries exceeded.')
                push_message("ai", f"Pipeline failed: {reason}")
            else:
                push_message("ai", f"Pipeline complete. Status: {status}.")
        except Exception as e:
            push_message("system", f"Pipeline error: {e}")
        finally:
            loop.close()
            with ACTIVE_PIPELINE_LOCK:
                ACTIVE_PIPELINE_THREADS.discard(plan_id)

    threading.Thread(target=run_pipeline, daemon=True).start()
    return {"status": "pipeline_resumed", "plan_id": plan_id}

def get_gate_status_local(plan_id: str | None = None):
    with PIPELINE_LOCK:
        if plan_id:
            return _get_gate_state(plan_id).copy()
        # No specific plan given (e.g. a generic voice "check pipeline status" query) —
        # best-effort: report the first gate currently waiting on approval, if any.
        for pid, state in PIPELINE_STATES.items():
            if state.get("gate_status") == "waiting":
                return {**state.copy(), "plan_id": pid}
        return _default_gate_state()

def update_metric_local(settings_dict):
    name = settings_dict.get("name")
    value = settings_dict.get("value")
    threshold = settings_dict.get("threshold")
    if not name or value is None:
        return {"error": "name and value required"}
    with TRACKED_METRICS_LOCK:
        TRACKED_METRICS[name] = {"value": float(value), "threshold": float(threshold or 0)}
    return {"status": "updated", "metric": name, "value": value}

def read_metrics_local():
    with TRACKED_METRICS_LOCK:
        return dict(TRACKED_METRICS)

def get_pipelines_local():
    """List plans for the assistant. `task` now holds the full clarified brief on
    pipelines that came through the intake gate, which would flood the model's
    context, so hand back the short summary and point at the brief instead."""
    with PLAN_STORE_LOCK:
        plans = list(PLAN_STORE)
    trimmed = []
    for plan in plans:
        view = dict(plan)
        summary = view.get("task_summary") or view.get("task") or ""
        view["task"] = summary
        view.pop("task_summary", None)
        trimmed.append(view)
    return trimmed

def delete_pipeline_local(settings_dict):
    plan_id = normalize_spoken_id(settings_dict.get("plan_id", ""))
    if not plan_id:
        return {"error": "plan_id is required"}
    
    deleted = False
    with PLAN_STORE_LOCK:
        for i, plan in enumerate(PLAN_STORE):
            if plan["id"] == plan_id:
                PLAN_STORE.pop(i)
                deleted = True
                break
                
    if not deleted:
        return {"error": f"Pipeline plan '{plan_id}' not found"}
        
    conn = db.get_connection(DB_PATH)
    try:
        db.delete_pipeline(conn, plan_id)
    except Exception as e:
        print(f"Error deleting pipeline from DB: {e}")
    finally:
        conn.close()
        
    push_message("system", f"Deleted pipeline project [Plan ID: {plan_id}].")
    return {"status": "pipeline_deleted", "plan_id": plan_id}

coordinator.register_state_provider("read_app_snapshot", get_snapshot_local)
coordinator.register_state_provider("read_settings", load_settings)
coordinator.register_state_provider("change_settings", change_settings_local)
coordinator.register_state_provider("start_pipeline", start_pipeline_local)
coordinator.register_state_provider("resume_pipeline", resume_pipeline_local)
coordinator.register_state_provider("delete_pipeline", delete_pipeline_local)
coordinator.register_state_provider("get_pipelines", get_pipelines_local)
coordinator.register_state_provider("get_gate_status", get_gate_status_local)
coordinator.register_state_provider("update_metric", update_metric_local)
coordinator.register_state_provider("read_metrics", read_metrics_local)

coordinator.register_tool_listener(jarvis_tool_listener)

def _extract_after_keyword(text: str, keywords: list) -> str:
    """Extract the text that follows any of the keywords in the transcript."""
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            remainder = text[idx + len(kw):].strip()
            # Clean up common filler words
            for prefix in ["the ", "a ", "my "]:
                if remainder.startswith(prefix):
                    remainder = remainder[len(prefix):]
            return remainder
    return text

def check_navigation_intent(transcript: str) -> str:
    global UI_ACTION
    t_lower = transcript.lower()

    # Determine current page context
    with JARVIS_STATE_LOCK:
        current_page = JARVIS_UI_SNAPSHOT.get("current_page", "Brain Core")

    is_on_execution = "execution" in current_page.lower()
    is_on_task_logs = "task logs" in current_page.lower()

    # Execution-specific commands (only when on execution page)
    if is_on_execution:
        if any(k in t_lower for k in ["show tasks", "open tasks", "task database", "show task constellation"]):
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "open_task_panel"}
            return "exec_tasks"
        elif any(k in t_lower for k in ["drill into", "zoom into", "enter department", "go into"]):
            dept_name = _extract_after_keyword(t_lower, ["drill into", "zoom into", "enter department", "go into"])
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "drill_down", "department": dept_name}
            return "exec_drill"
        elif any(k in t_lower for k in ["zoom out", "exit drill", "go back to constellation", "back out"]):
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "exit_drill"}
            return "exec_exit_drill"
        elif any(k in t_lower for k in ["close panel", "close everything", "hide panel"]):
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "close_panel"}
            return "exec_close"
        elif any(k in t_lower for k in ["show pipeline", "active view", "show agents", "show constellation"]):
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "show_active"}
            return "exec_active"
        elif any(k in t_lower for k in ["idle view", "show idle"]):
            with STATE_LOCK:
                UI_ACTION = {"type": "execution_control", "action": "show_idle"}
            return "exec_idle"

    # Standard cross-page navigation (with execution context preservation)
    if any(k in t_lower for k in ["console", "console stream", "task console", "chat stream", "console logs", "agent chat"]):
        with STATE_LOCK:
            UI_ACTION = {"type": "navigate", "url": "agent_talk.task_log.html"}
        return "console"
    elif any(k in t_lower for k in ["execution mode", "execution map", "go to execution", "open execution"]):
        with STATE_LOCK:
            UI_ACTION = {"type": "navigate", "url": "execution.html"}
        return "execution"
    elif any(k in t_lower for k in ["go back", "previous page", "navigate back"]):
        with STATE_LOCK:
            UI_ACTION = {"type": "control_interface", "action": "go_back"}
        return "back"
    elif any(k in t_lower for k in [
        "open brain", "back to brain", "brain page", "go to brain", 
        "go to the brain", "take me to brain", "take me to the brain", 
        "switch to brain", "switch to the brain", "show brain", "show the brain",
        "command center", "open command center", "go to command center"
    ]):
        with STATE_LOCK:
            UI_ACTION = {"type": "navigate", "url": "command_center.html"}
        return "brain"
    elif any(k in t_lower for k in ["open plan", "go to plan", "plan page"]):
        # Context-aware: preserve execution context in URL
        url = "plan.html?from=execution" if (is_on_execution or is_on_task_logs) else "plan.html"
        with STATE_LOCK:
            UI_ACTION = {"type": "navigate", "url": url}
        return "plan"
    elif any(k in t_lower for k in ["go to api", "open api", "go to providers", "show providers", "go to mcp"]):
        url = "provider_comparison.html?from=execution" if (is_on_execution or is_on_task_logs) else "provider_comparison.html"
        with STATE_LOCK:
            UI_ACTION = {"type": "navigate", "url": url}
        return "apis"
    return ""

def handle_request(transcript: str) -> str:
    nav_target = check_navigation_intent(transcript)
    if nav_target == "execution":
        return "Going into execution mode."
    elif nav_target == "console":
        return "Opening task console stream."
    elif nav_target == "plan":
        return "Navigating to the plan page."
    elif nav_target == "brain":
        return "Navigating to the brain core map."
    elif nav_target == "back":
        return "Going back to the previous page."
    elif nav_target == "apis":
        return "Going to the APIs page."
    # Execution-specific commands
    elif nav_target == "exec_tasks":
        return "Opening task database."
    elif nav_target == "exec_drill":
        return "Drilling into that department."
    elif nav_target == "exec_exit_drill":
        return "Zooming back out."
    elif nav_target == "exec_close":
        return "Closing the panel."
    elif nav_target == "exec_active":
        return "Showing pipeline constellation."
    elif nav_target == "exec_idle":
        return "Switching to idle view."
    return coordinator.handle_request(transcript)


SAMPLE_RATE = 16000
CHUNK_SIZE = 1280

# Global speech recognizer configuration
recognizer = sr.Recognizer()
recognizer.energy_threshold = 300
recognizer.dynamic_energy_threshold = True
recognizer.dynamic_energy_adjustment_damping = 0.15
recognizer.dynamic_energy_ratio = 1.5
recognizer.pause_threshold = 1.0

# ---------------------------------------------------------------------------
# Local-only web server (127.0.0.1 - not reachable from your network)
# ---------------------------------------------------------------------------

app = Flask(__name__)
import logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # quiet the request logs


@app.route("/")
def root_page():
    return send_from_directory(BASE_DIR, "command_center.html")


@app.route("/command-center")
def command_center():
    return send_from_directory(BASE_DIR, "command_center.html")


@app.route("/command_center.html")
def command_center_html_page():
    return send_from_directory(BASE_DIR, "command_center.html")


@app.route("/agent_map_final.html")
def agent_map_final_page():
    return send_from_directory(BASE_DIR, "command_center.html")


@app.route("/agent_map_demo.html")
def agent_map_demo_page():
    return send_from_directory(os.path.join(BASE_DIR, "Previews"), "agent_map_demo.html")


@app.route("/plan.html")
def plan_page():
    return send_from_directory(BASE_DIR, "plan.html")


@app.route("/execution.html")
def execution_page():
    return send_from_directory(BASE_DIR, "execution.html")


@app.route("/execution_preview.html")
def execution_preview_page():
    return send_from_directory(os.path.join(BASE_DIR, "Previews"), "execution_preview.html")



@app.route("/agent_talk.task_log.html")
def agent_talk_task_log_page():
    return send_from_directory(BASE_DIR, "agent_talk.task_log.html")


@app.route("/provider_comparison.html")
def provider_comparison_page():
    return send_from_directory(BASE_DIR, "provider_comparison.html")


@app.route("/api/console_logs", methods=["GET"])
def get_console_logs():
    with CONSOLE_LOGS_LOCK:
        return jsonify({"logs": list(CONSOLE_LOGS)})


@app.route("/api/agent_chat", methods=["GET"])
def get_agent_chat_logs():
    with AGENT_CHAT_LOGS_LOCK:
        return jsonify({"chat": list(AGENT_CHAT_LOGS)})


@app.route("/tasks", methods=["GET"])
def tasks():
    conn = db.get_connection(DB_PATH)
    try:
        return jsonify({"tasks": db.get_tasks(conn)})
    finally:
        conn.close()


@app.route("/notes", methods=["GET"])
def get_notes():
    conn = db.get_connection(DB_PATH)
    try:
        query = request.args.get("query", "").strip()
        if query:
            notes = db.search_notes(conn, query)
        else:
            rows = conn.execute("SELECT * FROM notes ORDER BY created_at DESC").fetchall()
            notes = [dict(r) for r in rows]
        return jsonify({"notes": notes})
    finally:
        conn.close()


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        current = load_settings()
        for k in ["theme", "voice_speed", "wake_word_threshold", "provider", "ollama_model", "ollama_url"]:
            if k in data:
                if k == "voice_speed":
                    current[k] = int(data[k])
                elif k == "wake_word_threshold":
                    current[k] = float(data[k])
                else:
                    current[k] = str(data[k])
        save_settings(current)
        return jsonify({"settings": current})
    else:
        return jsonify({"settings": load_settings()})


# Global variable to track the currently spoken word
CURRENT_SPOKEN_WORD = ""

@app.route("/state", methods=["GET"])
def state():
    global FOCUS_TASK_IDS, UI_ACTION, JARVIS_SLEEPING, CURRENT_SPOKEN_WORD, MIC_MUTED
    with STATE_LOCK:
        res = jsonify({
            "orb": ORB_STATE,
            "messages": CONVO,
            "focus_task_ids": FOCUS_TASK_IDS,
            "ui_action": UI_ACTION,
            "sleeping": JARVIS_SLEEPING,
            "mic_muted": MIC_MUTED,
            "current_word": CURRENT_SPOKEN_WORD
        })
        # Clear focused task IDs and UI action after serving so they only trigger once
        FOCUS_TASK_IDS = []
        UI_ACTION = None
        return res

@app.route("/jarvis/mute-mic", methods=["POST"])
def mute_mic():
    global MIC_MUTED
    data = request.get_json(force=True) or {}
    if "muted" in data:
        MIC_MUTED = bool(data["muted"])
    else:
        MIC_MUTED = not MIC_MUTED
    return jsonify({"mic_muted": MIC_MUTED})

@app.route("/ask", methods=["POST"])
def ask():
    """Lets you type/click-mic directly in the browser too, not just via wake word."""
    data = request.get_json(force=True)
    text = (data or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "no text provided"}), 400

    push_message("user", text)
    set_orb("thinking")
    try:
        reply = handle_request(text)
    except Exception as e:
        reply = f"Something went wrong: {e}"
    push_message("ai", reply)

    # Speak it out loud on a background thread so the UI updates immediately.
    threading.Thread(target=speak, args=(reply,), daemon=True).start()

    return jsonify({"reply": reply})


@app.route("/jarvis/state-update", methods=["POST"])
def jarvis_state_update():
    """Endpoint for frontend to report UI page, panel, or orb state changes."""
    data = request.get_json(force=True) or {}
    with JARVIS_STATE_LOCK:
        if "current_page" in data:
            JARVIS_UI_SNAPSHOT["current_page"] = str(data["current_page"])
        if "panel_open" in data:
            JARVIS_UI_SNAPSHOT["panel_open"] = data["panel_open"]
        if "orb" in data:
            JARVIS_UI_SNAPSHOT["orb"] = str(data["orb"])
        if "sleeping" in data:
            JARVIS_UI_SNAPSHOT["sleeping"] = bool(data["sleeping"])
    return jsonify({"status": "updated"})


@app.route("/jarvis/snapshot", methods=["GET"])
def jarvis_snapshot():
    """Returns the full UI state and active task/note database snapshot."""
    token = request.headers.get("X-Jarvis-Token")
    if token != JARVIS_SESSION_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    conn = db.get_connection(DB_PATH)
    try:
        tasks_list = db.get_tasks(conn)
        notes_rows = conn.execute("SELECT * FROM notes WHERE status = 'open'").fetchall()
        notes_list = [dict(r) for r in notes_rows]
    finally:
        conn.close()

    with JARVIS_STATE_LOCK:
        snapshot = JARVIS_UI_SNAPSHOT.copy()

    with STATE_LOCK:
        snapshot["messages"] = CONVO[-10:]

    snapshot["tasks"] = tasks_list
    snapshot["notes"] = notes_list
    snapshot["acks"] = list(JARVIS_ACK_QUEUE)
    return jsonify(snapshot)


@app.route("/jarvis/command", methods=["POST"])
def jarvis_command():
    """Receives structured Jarvis instructions, simulating Jarvis acting as a user."""
    token = request.headers.get("X-Jarvis-Token")
    if token != JARVIS_SESSION_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "no text provided"}), 400

    push_message("jarvis", text)
    set_orb("thinking")
    try:
        reply = handle_request(text)
    except Exception as e:
        reply = f"Something went wrong: {e}"
    push_message("ai", reply)

    # Speak it on a background thread
    threading.Thread(target=speak, args=(reply,), daemon=True).start()

    return jsonify({"reply": reply})


@app.route("/jarvis/ack", methods=["POST"])
def jarvis_ack():
    """Let the UI acknowledge to Jarvis that a control_interface command succeeded or failed."""
    data = request.get_json(force=True) or {}
    action = data.get("action")
    status = data.get("status", "ok")
    if not action:
        return jsonify({"error": "action required"}), 400

    JARVIS_ACK_QUEUE.append({"action": action, "status": status, "timestamp": time.time()})
    return jsonify({"status": "acknowledged"})


@app.route("/gate/status", methods=["GET"])
def gate_status():
    plan_id = request.args.get("plan_id")
    return jsonify(get_gate_status_local(plan_id))


@app.route("/gate/approve", methods=["POST"])
def gate_approve():
    data = request.get_json(force=True) or {}
    plan_id = (data.get("plan_id") or "").strip()
    if not plan_id:
        return jsonify({"error": "plan_id is required — approving without it could resolve the wrong pipeline's gate when more than one is running."}), 400
    with PIPELINE_LOCK:
        state = _get_gate_state(plan_id)
        gate = state.get("current_gate")
        if gate is None:
            return jsonify({"error": f"No gate is currently active for plan '{plan_id}'"}), 400
        state["gate_status"] = "approved"
        state["redirect_note"] = None
        # accept per-step approvals
        approved_steps = data.get("approved_steps")  # optional list of step IDs
    push_message("system", f"Gate '{gate}' approved for plan {plan_id}. Advancing pipeline.")
    return jsonify({"status": "approved", "gate": gate, "plan_id": plan_id})


@app.route("/gate/reject", methods=["POST"])
def gate_reject():
    data = request.get_json(force=True) or {}
    plan_id = (data.get("plan_id") or "").strip()
    if not plan_id:
        return jsonify({"error": "plan_id is required — rejecting without it could resolve the wrong pipeline's gate when more than one is running."}), 400
    note = data.get("redirect_note", "").strip()
    rejected = data.get("rejected_steps") or []
    with PIPELINE_LOCK:
        state = _get_gate_state(plan_id)
        gate = state.get("current_gate")
        if gate is None:
            return jsonify({"error": f"No gate is currently active for plan '{plan_id}'"}), 400
        state["gate_status"] = "rejected"
        state["redirect_note"] = note or None
        state["rejected_steps"] = rejected
    push_message("system", f"Gate '{gate}' rejected for plan {plan_id}. Note: {note or 'none provided'}, steps: {rejected}")
    return jsonify({"status": "rejected", "gate": gate, "plan_id": plan_id, "redirect_note": note, "rejected_steps": rejected})


@app.route("/gate/data", methods=["GET"])
def gate_data():
    """Returns the full data payload for the currently active gate."""
    plan_id = request.args.get("plan_id")
    state = get_gate_status_local(plan_id)
    return jsonify({
        "gate": state.get("current_gate"),
        "status": state.get("gate_status"),
        "data": state.get("gate_data"),
        "plan_id": plan_id or state.get("plan_id"),
    })


@app.route("/registry", methods=["GET"])
def get_registry():
    """Returns the full API/MCP registry."""
    from connectors.api_connector import load_registry
    return jsonify({"registry": load_registry()})


@app.route("/api/oauth-providers", methods=["GET"])
def get_oauth_providers_route():
    """
    Returns the service names we actually know how to auto-connect via a
    real OAuth flow (connectors/oauth_flow.OAUTH_PROVIDERS) — the plugging
    gate UI uses this to know when it can ignore Brain's often-invented
    connection_methods field list (which has hallucinated things like a
    manual "Refresh Token" input — nonsensical, since the whole point of
    the real flow is that the backend obtains that itself) and show only
    what's actually needed: Client ID + Client Secret.
    """
    from connectors.oauth_flow import OAUTH_PROVIDERS
    return jsonify({"providers": list(OAUTH_PROVIDERS.keys())})


@app.route("/registry/update", methods=["POST"])
def update_registry():
    """Register or update a service. Used during API/MCP plugging."""
    data = request.get_json(force=True) or {}
    service_name = data.get("service")
    config = data.get("config", {})
    if not service_name:
        return jsonify({"error": "service name required"}), 400
    from connectors.api_connector import register_service
    register_service(service_name, config)
    return jsonify({"status": "updated", "service": service_name})


@app.route("/agents", methods=["GET"])
def get_agents():
    """Returns the full agent registry."""
    with AGENT_OBS_LOCK:
        return jsonify({"agents": dict(AGENT_REGISTRY)})


@app.route("/agents/<agent_id>", methods=["GET"])
def get_agent_detail(agent_id):
    """Returns a single agent's full detail."""
    with AGENT_OBS_LOCK:
        agent = AGENT_REGISTRY.get(agent_id)
        if not agent:
            return jsonify({"error": f"Agent '{agent_id}' not found"}), 404
        return jsonify(agent)


@app.route("/agents/events", methods=["GET"])
def get_agent_events():
    """Returns the event log. Supports ?since=<timestamp> for polling."""
    since = request.args.get("since", type=float, default=0)
    with AGENT_OBS_LOCK:
        if since > 0:
            events = [e for e in AGENT_EVENT_LOG if e.get("timestamp", 0) > since]
        else:
            events = list(AGENT_EVENT_LOG)
        return jsonify({"events": events})

@app.route("/agents/interactions", methods=["GET"])
def get_agent_interactions():
    """Returns the interaction log (all prompts sent and results received)."""
    with AGENT_OBS_LOCK:
        interactions = [
            e for e in AGENT_EVENT_LOG
            if e.get("event_type") in ("prompt_sent", "result_received", "conflict", "gate_waiting")
        ]
        return jsonify({"interactions": interactions})

@app.route("/api/agent_conversations", methods=["GET"])
def get_agent_conversations():
    with AGENT_CONVERSATION_LOGS_LOCK:
        return jsonify({"conversations": list(AGENT_CONVERSATION_LOGS)})

@app.route("/api/agent_thinking", methods=["GET"])
def get_agent_thinking():
    with AGENT_THINKING_LOGS_LOCK:
        return jsonify({"thinking": list(AGENT_THINKING_LOGS)})

@app.route("/api/narrative", methods=["GET"])
def get_narrative():
    with NARRATIVE_LOGS_LOCK:
        return jsonify({"narrative": list(NARRATIVE_LOGS)})


@app.route("/api/narrative", methods=["POST"])
def post_narrative():
    """
    Lets an external narrator (e.g. Claude watching a pipeline live) push an
    entry into the same Narrative feed the pipeline itself writes to via
    append_narrative() — same store, same timeline, shows up in the
    Narrative tab immediately next to the pipeline's own agent narration.
    """
    token = request.headers.get("X-Jarvis-Token")
    if token != JARVIS_SESSION_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    phase = data.get("phase") or "external"
    icon = data.get("icon") or "🤖"

    append_narrative(phase, message, icon)
    return jsonify({"status": "ok"})

def pipeline_event_logger(event: dict):
    """Called by multi_agent_coordinator at each agent lifecycle point."""
    import time, json
    timestamp = time.time()
    event["timestamp"] = timestamp

    raw_source = event.get("source") or event.get("agent_id") or "TaskConsole"
    target = event.get("target") or event.get("receiver") or "All Agents"
    event_type = event.get("event_type", "info")
    data_val = event.get("data") or event.get("message") or ""

    # Route new event types into their correct store
    if event_type == "prompt_sent":
        append_agent_conversation(raw_source, "prompt_sent", data_val.get("role", "Agent"), data_val.get("content", ""))
    elif event_type == "response_received":
        append_agent_conversation(raw_source, "response_received", data_val.get("role", "Agent"), data_val.get("content", ""))
    elif event_type == "thinking":
        # Skip user_prompt thinking — that content is already in CHAT tab as prompt_sent
        t_type = data_val.get("thinking_type", "thought") if isinstance(data_val, dict) else "thought"
        if t_type != "user_prompt":
            append_agent_thinking(raw_source, data_val.get("role", "Agent"), t_type, data_val.get("content", ""))
    elif event_type == "narrative":
        append_narrative(data_val.get("phase", "running"), data_val.get("message", ""), data_val.get("icon", "➡️"))

    # Store spawned agent configs first so role is available immediately
    agent_id = event.get("agent_id") or event.get("source")
    if agent_id and agent_id not in ("Brain", "System"):
        with AGENT_OBS_LOCK:
            if agent_id not in AGENT_REGISTRY:
                AGENT_REGISTRY[agent_id] = {}
            if event_type == "spawned" and isinstance(data_val, dict):
                AGENT_REGISTRY[agent_id]["config"] = data_val

    # Resolve human-readable Role Name for UI display
    role_name = None
    if isinstance(data_val, dict) and data_val.get("role"):
        role_name = data_val.get("role")
    elif agent_id and agent_id in AGENT_REGISTRY and AGENT_REGISTRY[agent_id].get("config", {}).get("role"):
        role_name = AGENT_REGISTRY[agent_id]["config"].get("role")

    if role_name and raw_source not in ("Brain", "System", "TaskConsole"):
        if role_name.lower() in raw_source.lower():
            source = raw_source
        else:
            source = f"{role_name} ({raw_source})"
    else:
        source = raw_source

    if event_type == "thinking_stream":
        with AGENT_OBS_LOCK:
            if agent_id:
                if agent_id not in AGENT_REGISTRY:
                    AGENT_REGISTRY[agent_id] = {}
                AGENT_REGISTRY[agent_id]["streamed_thoughts"] = data_val
                AGENT_REGISTRY[agent_id]["status"] = "thinking"
                AGENT_REGISTRY[agent_id]["last_update"] = timestamp
        return

    # 1. Push to Console Stream (/api/console_logs)
    log_level = "error" if event_type in ("error", "failed") else ("warn" if event_type == "conflict" else "info")
    data_str = data_val if isinstance(data_val, str) else (json.dumps(data_val) if data_val else event_type)
    if len(data_str) > 200:
        data_str = data_str[:197] + "..."
    append_console_log(log_level, f"[{event_type.upper()}] {data_str}", source=source)

    # 2. Push to Inter-Agent Dialogue Stream (/api/agent_chat)
    chat_text = ""
    if isinstance(data_val, dict):
        chat_text = data_val.get("message") or data_val.get("summary") or data_val.get("prompt") or str(data_val)
    elif data_val:
        chat_text = str(data_val)
    else:
        chat_text = f"Agent status updated: {event_type}"

    append_agent_chat(sender=source, receiver=target, message=chat_text)

    with AGENT_OBS_LOCK:
        AGENT_EVENT_LOG.append(event)
        # Also update registry if this is an agent event
        if agent_id and agent_id not in ("Brain", "System"):
            if agent_id not in AGENT_REGISTRY:
                AGENT_REGISTRY[agent_id] = {}
            AGENT_REGISTRY[agent_id].update({
                "status": event.get("event_type"),
                "last_update": event["timestamp"],
            })
            if event.get("event_type") == "completed":
                AGENT_REGISTRY[agent_id]["output"] = event.get("data")
            if event.get("event_type") == "spawned":
                AGENT_REGISTRY[agent_id]["config"] = event.get("data")

    # Sync with PLAN_STORE
    plan_id = event.get("plan_id")
    if plan_id:
        plan_to_save = None
        with PLAN_STORE_LOCK:
            for plan in PLAN_STORE:
                if plan["id"] == plan_id:
                    event_type = event.get("event_type")
                    if event_type == "gate_waiting":
                        plan["current_gate"] = event.get("source")
                        plan["gate_status"] = "waiting"
                        plan["gate_data"] = event.get("data")
                    elif event_type == "gate_resolved":
                        plan["current_gate"] = None
                        plan["gate_status"] = "approved" if event.get("data", {}).get("approved") else "rejected"
                    elif event_type == "blueprint_compiled":
                        plan["master_blueprint"] = event.get("data")
                        plan["phase"] = "execution"
                    elif event_type == "execution_completed":
                        plan["exec_results"] = event.get("data")
                        plan["phase"] = "qa"
                    elif event_type == "completed" and event.get("source") == "DeploymentAgent":
                        plan["status"] = "complete"
                        plan["deploy_result"] = event.get("data")
                    elif event_type == "running" and event.get("source", "").startswith("Cycle"):
                        plan["phase"] = "research"
                        cycle_name = event.get("source")
                        if cycle_name not in plan["cycles"]:
                            plan["cycles"].append(cycle_name)
                    elif event_type == "conflict":
                        plan["phase"] = "conflict"
                    elif event_type == "agent_plan_compiled":
                        plan["agent_plan"] = event.get("data")
                    elif event_type == "cycle_approved":
                        if "approved_blueprints" not in plan:
                            plan["approved_blueprints"] = []
                        bp = event.get("data", {}).get("blueprint", {})
                        if bp not in plan["approved_blueprints"]:
                            plan["approved_blueprints"].append(bp)
                    plan_to_save = plan.copy()
                    break

        if plan_to_save:
            conn = db.get_connection(DB_PATH)
            try:
                db.save_pipeline(conn, plan_to_save)
            except Exception as e:
                print(f"Error persisting pipeline update: {e}")
            finally:
                conn.close()


    if plan_id:
        update_task_log_file(plan_id, event)


def merge_live_tool_status(plan: dict) -> dict:
    """
    Overlays live api_registry.json status onto a plan's tool_recommendations.

    This is the single source of truth for "is this API/MCP connected" —
    it's read fresh from disk on every call. We do NOT rely on in-memory
    PLAN_STORE mutation for this (connectors/api_connector.py's
    save_tool_credentials tries to patch PLAN_STORE directly, but since
    jarvis.py normally runs as __main__, its `from jarvis import PLAN_STORE`
    actually re-imports this file as a second, disconnected module — that
    patch lands on a throwaway copy, never on the live server state, which
    is exactly why a freshly connected tool would show as disconnected again
    on the next page load/poll). Merging live registry status at read-time
    sidesteps that entirely and is correct regardless.
    """
    from connectors.api_connector import get_service_status
    mb = plan.get("master_blueprint")
    if isinstance(mb, dict) and mb.get("tool_recommendations"):
        for rec in mb["tool_recommendations"]:
            status = get_service_status(rec.get("service"))
            rec["current_status"] = status
            rec["configured"] = status != "unknown"
    return plan


@app.route("/plans", methods=["GET"])
def get_plans():
    """Returns the full plans store with active thread status."""
    with PLAN_STORE_LOCK:
        plans = list(PLAN_STORE)

    with ACTIVE_PIPELINE_LOCK:
        active_ids = list(ACTIVE_PIPELINE_THREADS)

    decorated_plans = []
    for plan in plans:
        p_copy = plan.copy()
        p_copy["active"] = p_copy["id"] in active_ids
        merge_live_tool_status(p_copy)
        decorated_plans.append(p_copy)

    return jsonify({"plans": decorated_plans})


@app.route("/api/pipeline_status", methods=["GET"])
def get_pipeline_status():
    """Returns whether any pipeline is currently active and details about running plans."""
    with ACTIVE_PIPELINE_LOCK:
        active_ids = list(ACTIVE_PIPELINE_THREADS)
    with PLAN_STORE_LOCK:
        running_plans = [p for p in PLAN_STORE if p["id"] in active_ids]
    return jsonify({
        "active": len(active_ids) > 0,
        "active_ids": active_ids,
        "running_count": len(running_plans),
        "plans": running_plans
    })




@app.route("/plans/<plan_id>", methods=["GET"])
def get_plan_detail(plan_id):
    """Returns details for a single plan."""
    with PLAN_STORE_LOCK:
        for plan in PLAN_STORE:
            if plan["id"] == plan_id:
                return jsonify(merge_live_tool_status(plan.copy()))
        return jsonify({"error": f"Plan '{plan_id}' not found"}), 404


@app.route("/api/agent_graph/<plan_id>", methods=["GET"])
def get_agent_graph(plan_id):
    """Returns the full live constellation graph for a pipeline run.
    Structure: { plan_id, phase, task, cycles: [{ cycle_id, domain, goal, agents: [
        { agent_id, role, brief, tools_needed, memory_query, is_lead, status,
          streamed_thoughts, output, config }
    ]}], execution_agents: [...] }
    """
    with PLAN_STORE_LOCK:
        plan = None
        for p in PLAN_STORE:
            if p["id"] == plan_id:
                plan = p
                break
        if not plan:
            return jsonify({"error": "Plan not found"}), 404

    agent_plan = plan.get("agent_plan", {})
    cycles = agent_plan.get("cycles", [])

    graph_cycles = []
    for cycle in cycles:
        cycle_agents = []
        all_agent_configs = [cycle.get("lead_specialist", {})] + cycle.get("advisory_agents", [])

        for agent_cfg in all_agent_configs:
            agent_id = agent_cfg.get("agent_id", "")
            # Get live status from AGENT_REGISTRY
            with AGENT_OBS_LOCK:
                registry_entry = AGENT_REGISTRY.get(agent_id, {})

            cycle_agents.append({
                "agent_id": agent_id,
                "role": agent_cfg.get("role", ""),
                "brief": agent_cfg.get("brief", ""),
                "tools_needed": agent_cfg.get("tools_needed", []),
                "memory_query": agent_cfg.get("memory_query", ""),
                "is_lead": agent_cfg == cycle.get("lead_specialist"),
                "status": registry_entry.get("status", "pending"),
                "streamed_thoughts": registry_entry.get("streamed_thoughts", ""),
                "output": registry_entry.get("output"),
                "config": registry_entry.get("config"),
            })

        graph_cycles.append({
            "cycle_id": cycle.get("cycle_id"),
            "domain": cycle.get("domain", ""),
            "goal": cycle.get("goal", ""),
            "agents": cycle_agents,
        })

    # Execution agents
    exec_agents_cfg = agent_plan.get("execution_agents", [])
    exec_agents = []
    for cfg in exec_agents_cfg:
        agent_id = cfg.get("agent_id", "")
        with AGENT_OBS_LOCK:
            reg = AGENT_REGISTRY.get(agent_id, {})
        exec_agents.append({
            "agent_id": agent_id,
            "role": cfg.get("role", ""),
            "brief": cfg.get("brief", ""),
            "status": reg.get("status", "pending"),
            "output": reg.get("output"),
        })

    return jsonify({
        "plan_id": plan_id,
        "phase": plan.get("phase", "planning"),
        "task": plan.get("task", ""),
        "cycles": graph_cycles,
        "execution_agents": exec_agents,
    })


@app.route("/metrics/update", methods=["POST"])
def update_metric():
    data = request.get_json(force=True) or {}
    name = data.get("name")
    value = data.get("value")
    threshold = data.get("threshold")
    if not name or value is None:
        return jsonify({"error": "name and value required"}), 400
    with TRACKED_METRICS_LOCK:
        TRACKED_METRICS[name] = {"value": float(value), "threshold": float(threshold or 0)}
    return jsonify({"status": "updated", "metric": name, "value": value})


@app.route("/metrics/get", methods=["GET"])
def get_metrics():
    with TRACKED_METRICS_LOCK:
        return jsonify({"metrics": dict(TRACKED_METRICS)})



def track_agent_loop():
    """Background thread. Checks metrics every 5 minutes.
    If any metric is below its threshold, signals the Brain to spawn
    a corrective sub-task. If above, extracts success insights via LLM."""
    print("[Track Agent] Started monitoring.")
    while True:
        time.sleep(300)  # 5 minutes
        with TRACKED_METRICS_LOCK:
            metrics_snapshot = dict(TRACKED_METRICS)

        for metric_name, data in metrics_snapshot.items():
            value = data.get("value", 0)
            threshold = data.get("threshold", 0)
            
            # Change 1: Success pattern extraction
            if threshold > 0 and value >= threshold:
                pattern_prompt = (
                    f"A deployment just performed well on metric '{metric_name}' "
                    f"(value: {value:.3f}, threshold: {threshold:.3f}). "
                    f"Analyze WHY this succeeded based on the execution blueprint and "
                    f"produce an actionable insight pattern. Return JSON: "
                    f'{{"pattern": "...", "metric_name": "{metric_name}", "metric_value": {value}}}'
                )
                try:
                    pattern_response = coordinator.handle_request(pattern_prompt)
                    # Parse and save to memory
                    conn = db.get_connection(DB_PATH)
                    db.save_memory_pattern(conn, pattern=pattern_response,
                                           task_type=None, metric_name=metric_name,
                                           metric_value=value, outcome='win')
                    conn.close()
                    print(f"[Track Agent] Saved success pattern for metric '{metric_name}' to memory.")
                except Exception as e:
                    print(f"[Track Agent] Error extracting pattern: {e}")

            # Change 2: corrective loop spawning pipeline sub-task
            elif threshold > 0 and value < threshold:
                msg = (
                    f"[Track Agent] ALERT: '{metric_name}' is {value:.3f}, "
                    f"below threshold {threshold:.3f}. Spawning corrective sub-task."
                )
                print(msg)
                push_message("system", msg)
                
                corrective_task = f"Corrective sub-task: metric '{metric_name}' underperforming ({value:.3f} < {threshold:.3f})"
                try:
                    # Enters pipeline bypassing research phase via Phase 6 execution target
                    start_pipeline_local({"task": corrective_task})
                except Exception as e:
                    print(f"[Track Agent] Error starting corrective pipeline: {e}")


@app.route("/pipeline/start", methods=["POST"])
def start_pipeline():
    """Starts the multi-agent pipeline for a complex task."""
    data = request.get_json(force=True) or {}
    task = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "task is required"}), 400
    plan_id = initiate_pipeline(task)
    return jsonify({"status": "pipeline_started", "task": task, "plan_id": plan_id})


# ---------------------------------------------------------------------------
# Pipeline Intake routes — the clarification gate.
# Only /pipeline/intake/approve ever creates a pipeline.
# ---------------------------------------------------------------------------

@app.route("/pipeline/intake/start", methods=["POST"])
def intake_start_route():
    """Open a draft for a task. Nothing is persisted and no pipeline exists yet."""
    data = request.get_json(force=True) or {}
    task = (data.get("task") or "").strip()
    if not task:
        return jsonify({"error": "task is required"}), 400
    draft = create_intake_draft(task)
    return jsonify({
        "draft_id": draft["draft_id"],
        "task": draft["task"],
        "project_name": draft["project_name"],
    })


@app.route("/pipeline/intake/upload", methods=["POST"])
def intake_upload_route():
    """Store uploaded files of any type under the project's Inputs/ folder."""
    draft_id = (request.form.get("draft_id") or "").strip()
    draft = _get_intake_draft(draft_id)
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify({"error": "no files uploaded"}), 400

    inputs_dir = _intake_project_dir(draft["project_name"], "Inputs")
    stored = []
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        name = _intake_safe_filename(upload.filename)
        # Never silently overwrite a file the user already added.
        base, ext = os.path.splitext(name)
        candidate, n = name, 2
        existing = {f["name"] for f in draft["files"]}
        while candidate in existing or os.path.exists(os.path.join(inputs_dir, candidate)):
            candidate = f"{base} ({n}){ext}"
            n += 1
        path = os.path.join(inputs_dir, candidate)
        upload.save(path)
        entry = {
            "name": candidate,
            "path": path,
            "mime": upload.mimetype or "application/octet-stream",
            "size": os.path.getsize(path),
        }
        with INTAKE_DRAFTS_LOCK:
            draft["files"].append(entry)
        stored.append(entry)

    return jsonify({"files": [
        {k: v for k, v in f.items() if k != "path"} for f in draft["files"]
    ], "added": len(stored)})


@app.route("/pipeline/intake/remove_file", methods=["POST"])
def intake_remove_file_route():
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    name = data.get("name")
    removed = None
    with INTAKE_DRAFTS_LOCK:
        for f in list(draft["files"]):
            if f["name"] == name:
                draft["files"].remove(f)
                removed = f
                break
    if removed:
        try:
            os.remove(removed["path"])
        except Exception as e:
            print(f"[Intake] Could not delete {removed['name']}: {e}")
    return jsonify({"files": [
        {k: v for k, v in f.items() if k != "path"} for f in draft["files"]
    ]})


@app.route("/pipeline/intake/questions", methods=["POST"])
def intake_questions_route():
    """Store the details, then ask Gemini for the next round of gap questions."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    if "details" in data:
        with INTAKE_DRAFTS_LOCK:
            draft["details"] = (data.get("details") or "").strip()

    set_orb("thinking")
    questions = _intake_next_questions(draft)
    set_orb("idle")
    with INTAKE_DRAFTS_LOCK:
        draft["pending_questions"] = questions
        draft["rounds"] += 1
        draft["stage"] = "questions" if questions else "picture"

    return jsonify({"questions": questions, "round": draft["rounds"]})


@app.route("/pipeline/intake/answer", methods=["POST"])
def intake_answer_route():
    """Record one answer and hand back the next question, if any."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    with INTAKE_DRAFTS_LOCK:
        draft["qa"].append({"question": question, "answer": answer})
        draft["pending_questions"] = [
            q for q in draft["pending_questions"] if q.get("question") != question
        ]
        remaining = list(draft["pending_questions"])

    return jsonify({
        "next": remaining[0] if remaining else None,
        "remaining": len(remaining),
        "done": not remaining,
    })


@app.route("/pipeline/intake/picture", methods=["POST"])
def intake_picture_route():
    """Jarvis decides: more questions, or the plan."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    set_orb("thinking")
    result = _intake_paint_picture(draft)
    set_orb("idle")

    if result.get("questions"):
        with INTAKE_DRAFTS_LOCK:
            draft["pending_questions"] = result["questions"]
            draft["rounds"] += 1
            draft["stage"] = "questions"
        return jsonify({"questions": result["questions"], "round": draft["rounds"]})

    with INTAKE_DRAFTS_LOCK:
        draft["plan_text"] = result.get("plan_text", "")
        draft["stage"] = "picture"
    return jsonify({"plan_text": draft["plan_text"], "degraded": result.get("degraded")})


@app.route("/pipeline/intake/skip", methods=["POST"])
def intake_skip_route():
    """'Skip the rest — build with what you have': drop unanswered questions."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404
    with INTAKE_DRAFTS_LOCK:
        draft["pending_questions"] = []
    return jsonify({"status": "skipped"})


@app.route("/pipeline/intake/edit", methods=["POST"])
def intake_edit_route():
    """Clean up the user's edit and hand it back for approval — never build."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    edited = data.get("edited_text") or ""
    if not edited.strip():
        return jsonify({"error": "edited_text is required"}), 400

    set_orb("thinking")
    cleaned = _intake_clean_edit(draft, edited)
    set_orb("idle")
    with INTAKE_DRAFTS_LOCK:
        draft["plan_text"] = cleaned
        draft["stage"] = "picture"
    return jsonify({"plan_text": cleaned})


@app.route("/pipeline/intake/approve", methods=["POST"])
def intake_approve_route():
    """The only path that creates a pipeline from a draft."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    # "Continue with this info" skips the questions entirely, so there may be no
    # painted plan — the details the user typed are the brief.
    if "details" in data:
        with INTAKE_DRAFTS_LOCK:
            draft["details"] = (data.get("details") or "").strip()

    brief_path = _intake_write_brief(draft)
    brief_text = _intake_brief_markdown(draft)

    plan_id = initiate_pipeline(
        brief_text,
        project_name=draft["project_name"],
        brief_path=brief_path,
        task_summary=draft["task"],
    )

    with INTAKE_DRAFTS_LOCK:
        draft["approved"] = True
        draft["stage"] = "done"
        INTAKE_DRAFTS.pop(draft["draft_id"], None)

    return jsonify({
        "status": "pipeline_started",
        "plan_id": plan_id,
        "task": draft["task"],
        "project_name": draft["project_name"],
        "brief_path": brief_path,
    })


@app.route("/intake-file/<draft_id>/<path:filename>", methods=["GET"])
def intake_file_route(draft_id, filename):
    """Serve an uploaded file back to the modal (thumbnails in the file chips)."""
    draft = _get_intake_draft(draft_id)
    if not draft:
        return jsonify({"error": "draft not found"}), 404
    # Only files this draft actually recorded — never an arbitrary path.
    for f in draft.get("files", []):
        if f["name"] == filename:
            return send_from_directory(os.path.dirname(f["path"]), os.path.basename(f["path"]))
    return jsonify({"error": "file not found"}), 404


@app.route("/pipeline/intake/cancel", methods=["POST"])
def intake_cancel_route():
    """Cancel means it never happened."""
    data = request.get_json(force=True) or {}
    draft = _get_intake_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"status": "already_gone"})
    _intake_discard(draft)
    return jsonify({"status": "cancelled"})


@app.route("/jarvis/say", methods=["POST"])
def jarvis_say_route():
    """Speak a line through the normal voice path (so mute and the orb apply)."""
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    # The only mute this app has is the mic button; treat it as "Jarvis, be quiet"
    # so pressing mute silences the spoken questions too. The question text stays
    # on screen either way, so nothing is lost by staying silent.
    if MIC_MUTED:
        return jsonify({"status": "muted"})
    threading.Thread(target=speak, args=(text,), daemon=True).start()
    return jsonify({"status": "speaking"})


@app.route("/pipeline/resume", methods=["POST"])
def resume_pipeline_route():
    """Resumes a paused or incomplete pipeline."""
    data = request.get_json(force=True) or {}
    plan_id = data.get("plan_id", "").strip()
    if not plan_id:
        return jsonify({"error": "plan_id is required"}), 400
    force_reexecute = bool(data.get("force_reexecute", False))
    res = resume_pipeline_local({"plan_id": plan_id, "force_reexecute": force_reexecute})
    if "error" in res:
        return jsonify(res), 404
    return jsonify(res)


@app.route("/pipeline/delete", methods=["POST"])
def delete_pipeline_route():
    """Deletes a pipeline project permanently."""
    data = request.get_json(force=True) or {}
    plan_id = data.get("plan_id", "").strip()
    if not plan_id:
        return jsonify({"error": "plan_id is required"}), 400
    res = delete_pipeline_local({"plan_id": plan_id})
    if "error" in res:
        return jsonify(res), 404
    return jsonify(res)


@app.route("/api/connect-tool", methods=["POST"])
def connect_tool_route():
    """
    Saves tool credentials and updates status in registry.

    For method_id == "oauth" against a service with a known OAuth provider
    (connectors/oauth_flow.OAUTH_PROVIDERS), this doesn't just store the
    Client ID/Secret text — those alone can never authorize anything. It
    actually runs the real OAuth2 flow: opens the user's browser to Google's
    (or whichever provider's) consent screen, waits for them to approve,
    captures the redirect, and exchanges the code for a real access/refresh
    token — automatically, for any provider registered there, not just
    Google. Only once that succeeds is the service marked genuinely
    connected. Plain API-key services are unaffected — same as before.
    """
    from connectors.api_connector import save_tool_credentials
    from connectors.oauth_flow import run_installed_app_flow, get_oauth_provider
    data = request.get_json(force=True) or {}
    service_name = data.get("service_name", "").strip()
    credentials = data.get("credentials", {})
    method_id = data.get("method_id")

    if not service_name:
        return jsonify({"error": "service_name is required"}), 400
    if not credentials or not isinstance(credentials, dict):
        return jsonify({"error": "credentials dict is required"}), 400

    provider = get_oauth_provider(service_name) if method_id == "oauth" else None

    if provider:
        client_id = credentials.get("client_id", "").strip()
        client_secret = credentials.get("client_secret", "").strip()
        if not client_id or not client_secret:
            return jsonify({"error": "client_id and client_secret are required for OAuth services"}), 400

        push_message("system", f"Opening your browser to authorize {service_name} — please approve access, Sir.")
        result = run_installed_app_flow(
            authorize_url=provider["authorize_url"],
            token_url=provider["token_url"],
            client_id=client_id,
            client_secret=client_secret,
            scopes=provider["scopes"],
        )
        if result.get("status") != "ok":
            return jsonify({"error": f"OAuth authorization failed for {service_name}: {result.get('error')}"}), 502

        # Persist client_id/secret AND the real tokens the flow produced —
        # the tokens are what actually let a connector call the API later.
        credentials = {
            **credentials,
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token"),
        }
        push_message("system", f"{service_name} authorized successfully.")

    success = save_tool_credentials(service_name, credentials, method_id=method_id)
    if success:
        return jsonify({"status": "success", "message": f"Successfully connected {service_name}"})
    else:
        return jsonify({"error": f"Failed to save credentials for {service_name}"}), 500


@app.route("/api/open-artifact", methods=["POST"])
def open_artifact_route():
    """
    Opens a real deliverable produced by an execution agent — a written file
    (via the OS file explorer, at its containing folder) or a created remote
    resource (a Google Doc, a deployed site, ...). URL-type artifacts are
    opened directly by the frontend (window.open) and never reach this route;
    this only handles local paths, since a browser can't open the OS file
    explorer itself for security reasons.
    """
    data = request.get_json(force=True) or {}
    rel_path = (data.get("value") or "").strip()
    if not rel_path:
        return jsonify({"error": "value is required"}), 400

    abs_path = os.path.abspath(os.path.join(BASE_DIR, rel_path))
    if not (abs_path == BASE_DIR or abs_path.startswith(BASE_DIR + os.sep)):
        return jsonify({"error": "Path escapes the project directory."}), 400
    if not os.path.exists(abs_path):
        return jsonify({"error": f"File not found: {rel_path}"}), 404

    try:
        # Select the file in its containing folder rather than trying to
        # "run" it — correct for a script, a doc, a video, or any file type.
        subprocess.run(["explorer", "/select,", os.path.normpath(abs_path)], check=False)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": f"Failed to open: {e}"}), 500


@app.route("/api/configured-tools", methods=["GET"])
def get_configured_tools_route():
    """Returns all globally configured services across all projects."""
    from connectors.api_connector import get_all_configured_services
    configured = get_all_configured_services()
    return jsonify({"status": "success", "tools": configured})


def run_server():
    # threaded=True: /api/connect-tool now blocks for up to 3 minutes while
    # waiting on OAuth browser consent — without this, that would freeze
    # every other request (UI polling, voice commands, etc.) app-wide.
    app.run(host=HOST, port=PORT, use_reloader=False, threaded=True)


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------


def record_and_transcribe():
    global MIC_MUTED
    if MIC_MUTED:
        time.sleep(0.5)
        return None
    mic = sr.Microphone(sample_rate=SAMPLE_RATE)
    with mic as source:
        print("Listening...")
        try:
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
        except sr.WaitTimeoutError:
            return None
    try:
        text = recognizer.recognize_google(audio)
        print(f"[you said] {text}")
        return text
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        print(f"Speech recognition service error: {e}")
        return None


# ---------------------------------------------------------------------------
# Reminder Engine Loop
# ---------------------------------------------------------------------------

def reminder_loop():
    print("Reminder engine started.")
    reminded_task_ids = set()
    while True:
        try:
            conn = db.get_connection(DB_PATH)
            tasks = db.get_tasks(conn)
            conn.close()
            
            now_ts = time.time()
            for t in tasks:
                if t["status"] == "done":
                    continue
                if t["id"] in reminded_task_ids:
                    continue
                
                time_str = t["scheduled_at"] or t["due_date"]
                if not time_str:
                    continue
                
                try:
                    if "T" in time_str:
                        # Parse ISO datetime
                        # Remove timezone offset string representation if present for standard parsing if python < 3.11
                        # standard fromisoformat parses offsets like +03:00 starting from python 3.7
                        dt = datetime.fromisoformat(time_str)
                        dt_ts = dt.timestamp()
                        diff = dt_ts - now_ts
                        # Remind if between 0 and 15 mins (900s)
                        if 0 <= diff <= 900:
                            reminded_task_ids.add(t["id"])
                            msg = f"Reminder: Task #{t['id']} is scheduled soon: {t['content']}"
                            push_message("system", msg)
                            speak(msg)
                    else:
                        # Date only YYYY-MM-DD
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        if time_str == today_str:
                            reminded_task_ids.add(t["id"])
                            msg = f"Reminder: Task #{t['id']} is due today: {t['content']}"
                            push_message("system", msg)
                            speak(msg)
                except Exception as e:
                    print(f"Error parsing date {time_str} in reminder: {e}")
        except Exception as e:
            print(f"Error in reminder loop: {e}")
        time.sleep(60)


# ---------------------------------------------------------------------------
# Wake word listener loop
# ---------------------------------------------------------------------------

def mic_loop(window):
    print("Calibrating microphone for ambient noise...")
    with sr.Microphone(sample_rate=SAMPLE_RATE) as source:
        recognizer.adjust_for_ambient_noise(source, duration=1.0)
    print(f"Calibrated energy threshold: {recognizer.energy_threshold:.2f}")

    print("Loading wake word model (Hey Jarvis)...")
    openwakeword.utils.download_models()
    oww_model = WakeModel(wakeword_models=["hey_jarvis"])

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print("Jarvis is running. Say 'Hey Jarvis' to activate. Ctrl+C to quit.")

    # Sliding buffer of the last 2.0 seconds of audio (16000 samples/sec * 2 bytes/sample * 2.0 sec = 64000 bytes)
    buffer = bytearray()
    buffer_limit = 64000

    try:
        while True:
            raw_data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            buffer.extend(raw_data)
            if len(buffer) > buffer_limit:
                buffer = buffer[-buffer_limit:]
                
            audio_chunk = np.frombuffer(raw_data, dtype=np.int16)
            prediction = oww_model.predict(audio_chunk)

            threshold = get_wake_word_threshold()
            triggered = any(score > threshold for score in prediction.values())
            if triggered:
                print("\nWake word detected!")
                oww_model.reset()
                global JARVIS_SLEEPING
                with STATE_LOCK:
                    JARVIS_SLEEPING = False

                # Determine whether they said "Hey Jarvis" or just "Jarvis"
                greeting = "Hello Sir, how can I help you?"
                try:
                    # Crop to last 1.2 seconds of buffer to speed up API upload/processing
                    wake_audio = bytes(buffer[-38400:])
                    sr_audio = sr.AudioData(wake_audio, 16000, 2)
                    text = recognizer.recognize_google(sr_audio, language="en-US").lower()
                    print(f"[wake analysis] {text}")
                    if "hey" in text:
                        greeting = "Hello Sir, how can I help you?"
                    elif "jarvis" in text or "jar" in text:
                        greeting = "Yes Sir?"
                except Exception as e:
                    # Default fallback
                    pass

                window.show()

                push_message("ai", greeting)
                speak(greeting)

                in_conversation = True
                while in_conversation:
                    transcript = record_and_transcribe()
                    if not transcript:
                        continue

                    transcript_lower = transcript.lower()
                    if "jarvis exit completely" in transcript_lower or "exit completely" in transcript_lower:
                        push_message("user", transcript)
                        speak("Shutting down completely. Goodbye.")
                        window.destroy()
                        import os
                        os._exit(0)

                    stop_words = [
                        "end the conversation", "end conversation", "goodbye",
                        "go to sleep", "exit", "quit", "stop listening"
                    ]
                    should_stop = any(w in transcript_lower for w in stop_words)
                    if should_stop:
                        push_message("user", transcript)
                        speak("Okay, going back to sleep.")
                        in_conversation = False
                        with STATE_LOCK:
                            JARVIS_SLEEPING = True
                        
                        keep_open = (
                            "without closing" in transcript_lower or 
                            "without exiting" in transcript_lower or
                            "keep the app open" in transcript_lower or
                            "keep app open" in transcript_lower
                        )
                        if not keep_open:
                            window.hide()
                        break

                    push_message("user", transcript)
                    set_orb("thinking")
                    try:
                        reply = handle_request(transcript)
                    except Exception as e:
                        print(f"Error handling request: {e}")
                        reply = "Something went wrong, sorry."
                    push_message("ai", reply)
                    speak(reply)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    from datetime import datetime
    import db
    
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(0.5)  # give Flask a moment to bind before the window loads it

    # Start background reminder thread
    reminders = threading.Thread(target=reminder_loop, daemon=True)
    reminders.start()

    # Start background track agent thread
    track_thread = threading.Thread(target=track_agent_loop, daemon=True)
    track_thread.start()

    # Create the app window hidden - it only appears when "Hey Jarvis" triggers.
    window = webview.create_window("Jarvis", URL, width=1100, height=720, hidden=True, fullscreen=True)

    mic_thread = threading.Thread(target=mic_loop, args=(window,), daemon=True)
    mic_thread.start()

    # pywebview needs to run on the main thread - this blocks here until
    # the window is closed.
    webview.start()
