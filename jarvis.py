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
import sections as section_store

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

# --- Section Drafts (the same gate, for turning a pipeline into a section) ---
# Everything the user has told us about a section BEFORE the section exists:
# the name and brief they typed, the files they dropped, and the running Q&A.
# Not persisted either — cancelling must leave no section and no files behind.
SECTION_DRAFTS: dict[str, dict] = {}
SECTION_DRAFTS_LOCK = threading.Lock()

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
    # Inside a section the snapshot is that section's: showing the brain's task
    # list here would have Jarvis answering about work that is not in front of
    # the user, and acting on the wrong ids.
    section = coordinator.get_active_section()
    section_id = section["id"] if section else None

    conn = db.get_connection(DB_PATH)
    try:
        tasks_list = db.get_tasks(conn, section_id=section_id)
        if section_id:
            notes_rows = conn.execute(
                "SELECT * FROM notes WHERE status = 'open' AND section_id = ?", (section_id,)
            ).fetchall()
        else:
            notes_rows = conn.execute(
                "SELECT * FROM notes WHERE status = 'open' AND section_id IS NULL"
            ).fetchall()
        notes_list = [dict(r) for r in notes_rows]
    finally:
        conn.close()

    with JARVIS_STATE_LOCK:
        snapshot = JARVIS_UI_SNAPSHOT.copy()

    with STATE_LOCK:
        snapshot["messages"] = CONVO[-10:]

    snapshot["tasks"] = tasks_list
    snapshot["notes"] = notes_list
    snapshot["section"] = {"id": section["id"], "name": section["name"]} if section else None
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

    # Snapshot under the lock (a fast C-level copy), then filter outside it.
    # This runs on every agent event and the log only grows, so scanning it while
    # holding the lock stalled the observability endpoints the execution page polls.
    with AGENT_OBS_LOCK:
        all_events = list(AGENT_EVENT_LOG)
    plan_events = [e for e in all_events if e.get("plan_id") == plan_id]

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

def load_section(section_id: str) -> dict | None:
    """A section by id, or None. Opens and closes its own connection."""
    if not section_id:
        return None
    conn = db.get_connection(DB_PATH)
    try:
        return db.get_section(conn, section_id)
    except Exception as e:
        print(f"[Sections] Could not load {section_id}: {e}")
        return None
    finally:
        conn.close()


def _section_for_draft(draft: dict) -> dict | None:
    return load_section(draft.get("section_id"))


def _draft_project_name(draft: dict) -> str:
    """The draft's project folder name, derived on first use.

    get_project_name() is a synchronous Gemini round-trip. Doing it while the
    user is waiting for the "want to give me more details?" prompt — itself
    nested inside another Gemini call — stalls the UI for seconds before
    anything appears. Nothing needs the name until a file is uploaded or the
    brief is written, so pay for it then.
    """
    with INTAKE_DRAFTS_LOCK:
        name = draft.get("project_name")
    if name:
        return name

    # Inside a section, everything belongs in that section's folder — a new
    # pipeline must not scatter its work into a folder of its own.
    section = _section_for_draft(draft)
    if section:
        with INTAKE_DRAFTS_LOCK:
            draft["project_name"] = section["folder"]
        return section["folder"]

    name = get_project_name(draft["task"])
    with INTAKE_DRAFTS_LOCK:
        draft["project_name"] = name
    return name


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


def _delete_draft_uploads(draft: dict):
    """Delete only the files THIS draft uploaded.

    Never wipe the project's Inputs/ folder wholesale: get_project_name derives
    that folder from the task text, so a second, similar request lands in the
    same folder as an earlier pipeline. Cancelling the new draft must not take
    the older pipeline's files with it \u2014 it may still be running on them.
    """
    for f in draft.get("files", []):
        try:
            if os.path.exists(f["path"]):
                os.remove(f["path"])
        except Exception as e:
            print(f"[Intake] Could not delete {f.get('name')}: {e}")

    # Tidy up the folders this draft created, but only while they are empty.
    project_name = draft.get("project_name")
    if not project_name:
        return          # never named, so nothing was ever written for it
    project = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name)
    for path in (os.path.join(project, "Inputs"), project):
        try:
            if os.path.isdir(path) and not os.listdir(path):
                os.rmdir(path)
        except Exception as e:
            print(f"[Intake] Could not remove empty folder {path}: {e}")


def _prune_intake_drafts():
    """Drop drafts nobody came back to, along with the files they uploaded."""
    import time as _time
    cutoff = _time.time() - INTAKE_DRAFT_TTL
    with INTAKE_DRAFTS_LOCK:
        stale = [d for d in INTAKE_DRAFTS.values() if d.get("touched", 0) < cutoff]
        for draft in stale:
            INTAKE_DRAFTS.pop(draft["draft_id"], None)
    for draft in stale:
        if not draft.get("approved"):
            _delete_draft_uploads(draft)


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
    lines = []
    # Questions and the painted plan must not re-ask what the section already
    # established, so the gate sees the section's standing knowledge first.
    section = _section_for_draft(draft)
    if section:
        lines.append(
            "THIS PIPELINE RUNS INSIDE AN EXISTING SECTION. Everything below is already "
            "known \u2014 never ask the user about it, and do not plan to research it "
            "again:\n" + section_store.knowledge_digest(section, max_chars=4000)
        )
    lines.append(f"ORIGINAL REQUEST:\n{draft.get('task', '')}")
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


def _ask_model_json(instruction: str, context_text: str, parts: list | None = None):
    """One Gemini call: an instruction, the context, and any readable files.

    Shared by both clarification gates — this one, and the section gate further
    down — because they differ only in what they put in the context.
    """
    contents = [instruction, context_text] + (parts or [])
    response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
    return _intake_parse_json(response.text)


def _normalise_questions(data) -> list[dict]:
    """Accept a bare list or {"questions": [...]}, of strings or of objects."""
    questions = data.get("questions", []) if isinstance(data, dict) else data
    cleaned = []
    for q in questions or []:
        if isinstance(q, str) and q.strip():
            cleaned.append({"question": q, "gist": q})
        elif isinstance(q, dict) and q.get("question"):
            cleaned.append({"question": q["question"], "gist": q.get("gist") or q["question"]})
    return cleaned


def _intake_ask_gemini(draft: dict, instruction: str):
    """One Gemini call carrying the whole draft (text + readable files)."""
    return _ask_model_json(instruction, _intake_context_text(draft), _intake_file_parts(draft))


_INTAKE_BRIEF_RULE = (
    "The brief is authoritative for WHAT the user wants. It is not a limit on HOW you work: "
    "research and search the web freely for anything the brief does not cover. Follow the brief "
    "exactly where it constrains you \u2014 if it names a specific source, tool, or approach, use that "
    "one instead of choosing your own. Attached files live at the paths listed in the brief; open "
    "them when they are relevant."
)


# What separates a question worth asking from a waste of the user's time. Shared
# by both gates, because a bad question is bad for the same reasons either way.
_QUESTION_RULES = (
    "- Only ask about gaps that would actually change what gets done. Test every question "
    "before you write it down: if each plausible answer leads to the same work, drop it.\n"
    "- Never ask about how this system works or where anything is kept. The app, its pages, "
    "its folders, its file formats and its tools are already decided and are not the user's "
    "business here. No questions about platforms, hosting, storage, naming, formats, or how "
    "you will organise or track the work.\n"
    "- Never ask what you can reasonably infer, and never ask a preference that would not "
    "alter the work.\n"
    "- Never re-ask something already answered, and never ask for what the attached files "
    "already contain — read them first.\n"
    "- Short, plain, one topic each. No compound questions. There is no limit on how many you "
    "ask, but every one has to earn its place.\n"
    "- If nothing material is missing, return an empty list. An empty list is the correct "
    "answer whenever you could proceed without guessing.\n"
)


def _intake_next_questions(draft: dict) -> list[dict]:
    """Ask Gemini for the gaps that would genuinely change what gets built."""
    instruction = (
        "You are Jarvis, about to hand this job to a team of autonomous agents. Before they start, "
        "find what you genuinely do not know.\n\n"
        "RULES:\n" + _QUESTION_RULES + "\n"
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

    return _normalise_questions(data)


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
        "- Now that you have read the answers, ask again if they opened something material you "
        "still cannot settle: do NOT guess, return questions instead of a plan. The rules on what "
        "makes a question worth asking apply here exactly as they did before:\n"
        + _QUESTION_RULES + "\n"
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
        questions = _normalise_questions(data)
        if questions:
            return {"questions": questions}

    plan_text = (data or {}).get("plan_text") if isinstance(data, dict) else None
    if not plan_text:
        plan_text = _intake_brief_markdown(draft, include_plan=False)
    return {"plan_text": plan_text}


def _clean_edited_text(context_text: str, parts: list, edited_text: str,
                       noun: str = "plan") -> str:
    """Tidy the user's edit without changing a single one of their decisions."""
    instruction = (
        f"The user edited the {noun} below. Return it in a clean, consistent format.\n\n"
        "RULES:\n"
        "- Preserve every decision they made. Change no meaning.\n"
        "- Add no requirements they did not write, and remove none that they did.\n"
        "- Fix only structure, headings and wording.\n\n"
        f"Reply with JSON only: {{\"plan_text\": \"the cleaned {noun} in markdown\"}}\n\n"
        "THE USER\u2019S EDITED TEXT:\n" + (edited_text or "")
    )
    try:
        data = _ask_model_json(instruction, context_text, parts)
        cleaned = (data or {}).get("plan_text")
        if cleaned:
            return cleaned
    except Exception as e:
        print(f"[Intake] Cleaning the edit failed: {e}")
    # Never lose the user's words — keep their version verbatim if cleaning fails.
    return edited_text


def _intake_clean_edit(draft: dict, edited_text: str) -> str:
    return _clean_edited_text(_intake_context_text(draft), _intake_file_parts(draft), edited_text)


def _intake_brief_markdown(draft: dict, include_plan: bool = True) -> str:
    """The complete record handed to the agents."""
    out = [f"# Clarified Brief \u2014 {draft.get('project_name') or 'Project'}", ""]
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

    section = _section_for_draft(draft)
    if section:
        out += ["---", "", section_store.knowledge_digest(section), ""]

    out += ["---", "", "## How to use this brief", _INTAKE_BRIEF_RULE, ""]
    return "\n".join(out)


def _intake_write_brief(draft: dict) -> str:
    brief_dir = _intake_project_dir(_draft_project_name(draft), "Brief")
    # Two similar requests derive the same project name, so never overwrite a brief
    # an earlier pipeline is still working from — its plan points at that file.
    brief_path = os.path.join(brief_dir, "clarified_brief.md")
    n = 2
    while os.path.exists(brief_path):
        brief_path = os.path.join(brief_dir, f"clarified_brief ({n}).md")
        n += 1
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(_intake_brief_markdown(draft))
    return brief_path


def _intake_discard(draft: dict):
    """Cancel means it never happened: forget the draft, delete its uploads."""
    with INTAKE_DRAFTS_LOCK:
        INTAKE_DRAFTS.pop(draft["draft_id"], None)
    _delete_draft_uploads(draft)


def create_intake_draft(task: str, section_id: str | None = None) -> dict:
    """Start a draft. No pipeline, no DB row, nothing persistent yet."""
    import time as _time
    import uuid
    _prune_intake_drafts()
    draft = {
        "draft_id": uuid.uuid4().hex[:8],
        "task": task,
        # A pipeline started inside a section belongs to it: its folder, its
        # knowledge, its tasks. None means the pipeline stands on its own.
        "section_id": section_id,
        "project_name": None,          # derived lazily by _draft_project_name()
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
            "completed_stages": [],
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

    section_id = (settings_dict.get("section_id") or "").strip() or None

    if settings_dict.get("skip_intake"):
        # Even without the gate, work started inside a section belongs to it.
        section = load_section(section_id)
        plan_id = initiate_pipeline(
            task, project_name=section["folder"] if section else None
        )
        if section:
            attach_pipeline_to_section(section, plan_id)
        return {"status": "pipeline_started", "task": task, "plan_id": plan_id}

    draft = create_intake_draft(task, section_id=section_id)
    with STATE_LOCK:
        UI_ACTION = {
            "type": "pipeline_intake_ask",
            "draft_id": draft["draft_id"],
            "task": task,
            "section_id": section_id,
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

def is_stop_command(transcript_lower: str, stop_words: list[str]) -> bool:
    """True only when the user actually said goodbye, not merely used the word.

    Two guards: whole-word matching (so "quite" is not "quit"), and a length limit
    (so "exit the loop in that script, please" is a request, not a farewell).
    """
    import re
    words = transcript_lower.split()
    if len(words) > 6:
        return False
    for phrase in stop_words:
        if re.search(r"\b" + re.escape(phrase) + r"\b", transcript_lower):
            return True
    return False


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


@app.after_request
def no_cache_html(response):
    """Never let the webview serve a stale page.

    The UI is plain HTML files on disk. When one of them changes, the running
    app must show the new version \u2014 otherwise a fix that is genuinely in the
    file looks like it did nothing, and the bug hunt goes looking in the wrong
    place entirely. Only pages are covered; API responses are untouched.
    """
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        # Drop the validators too. Left in place, the browser stores them and
        # asks "still unchanged?" on the next load — and Flask answers 304, which
        # sends it right back to the cached copy. With nothing to revalidate
        # against, every load fetches the file as it is on disk.
        response.headers.pop("ETag", None)
        response.headers.pop("Last-Modified", None)
    return response


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


@app.route("/section.html")
def section_page():
    return send_from_directory(BASE_DIR, "section.html")


@app.route("/sections_ui.js")
def sections_ui_script():
    return send_from_directory(BASE_DIR, "sections_ui.js")


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
    """The brain's tasks, or one section's when asked for by id.

    Tasks made inside a section belong to it, so the brain's list no longer
    mixes them in.
    """
    section_id = (request.args.get("section_id") or "").strip() or None
    conn = db.get_connection(DB_PATH)
    try:
        return jsonify({"tasks": db.get_tasks(conn, section_id=section_id)})
    finally:
        conn.close()


@app.route("/notes", methods=["GET"])
def get_notes():
    conn = db.get_connection(DB_PATH)
    try:
        query = request.args.get("query", "").strip()
        section_id = (request.args.get("section_id") or "").strip() or None
        if query:
            notes = db.search_notes(conn, query, section_id=section_id)
        elif section_id:
            rows = conn.execute(
                "SELECT * FROM notes WHERE section_id = ? ORDER BY created_at DESC",
                (section_id,),
            ).fetchall()
            notes = [dict(r) for r in rows]
        else:
            rows = conn.execute(
                "SELECT * FROM notes WHERE section_id IS NULL ORDER BY created_at DESC"
            ).fetchall()
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
    # Last line of defence: an empty reply would render as a blank "JARVIS:" bubble
    # and be spoken as silence, which reads as the app being broken with no clue why.
    if not (reply or "").strip():
        print("[Jarvis] handle_request returned an empty reply.")
        reply = "I could not produce a reply for that, Sir. Please try again."
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
                    # Stages are recorded only once they genuinely finish, so a
                    # pipeline resumed after the app was closed re-enters the stage
                    # it was actually sitting in instead of skipping past it.
                    completed = plan.setdefault("completed_stages", [])
                    def mark_done(stage: str):
                        if stage and stage not in completed:
                            completed.append(stage)
                    if event_type == "gate_waiting":
                        plan["current_gate"] = event.get("source")
                        plan["gate_status"] = "waiting"
                        plan["gate_data"] = event.get("data")
                    elif event_type == "gate_resolved":
                        plan["current_gate"] = None
                        approved = bool(event.get("data", {}).get("approved"))
                        plan["gate_status"] = "approved" if approved else "rejected"
                        if approved:
                            mark_done(event.get("source"))
                    elif event_type == "blueprint_compiled":
                        plan["master_blueprint"] = event.get("data")
                        plan["phase"] = "execution"
                    elif event_type == "execution_completed":
                        plan["exec_results"] = event.get("data")
                        # The same event is emitted when QA gave up, to show the user
                        # what the agents produced. That is not a completed stage.
                        if event.get("qa_passed") is not False:
                            plan["phase"] = "qa"
                            mark_done("execution")
                    elif event_type == "completed" and event.get("source") == "DeploymentAgent":
                        plan["status"] = "complete"
                        plan["deploy_result"] = event.get("data")
                        mark_done("deploy")
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
    draft = create_intake_draft(task, section_id=(data.get("section_id") or "").strip() or None)
    return jsonify({
        "draft_id": draft["draft_id"],
        "task": draft["task"],
        "section_id": draft.get("section_id"),
        # Deliberately not resolved here: naming the project costs a model call
        # and the UI does not need it until files are attached.
        "project_name": draft.get("project_name"),
    })


@app.route("/pipeline/intake/draft", methods=["GET"])
def intake_draft_route():
    """Fetch a draft by id — used when the ask has to move to another page."""
    draft = _get_intake_draft(request.args.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404
    return jsonify({
        "draft_id": draft["draft_id"],
        "task": draft["task"],
        "project_name": draft.get("project_name"),
        "details": draft.get("details", ""),
        "files": [{k: v for k, v in f.items() if k != "path"} for f in draft.get("files", [])],
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

    inputs_dir = _intake_project_dir(_draft_project_name(draft), "Inputs")
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
        project_name=_draft_project_name(draft),
        brief_path=brief_path,
        task_summary=draft["task"],
    )

    section = _section_for_draft(draft)
    if section:
        attach_pipeline_to_section(section, plan_id)

    with INTAKE_DRAFTS_LOCK:
        draft["approved"] = True
        draft["stage"] = "done"
        INTAKE_DRAFTS.pop(draft["draft_id"], None)

    return jsonify({
        "status": "pipeline_started",
        "plan_id": plan_id,
        "task": draft["task"],
        "project_name": draft.get("project_name"),
        "section_id": draft.get("section_id"),
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


# ---------------------------------------------------------------------------
# Sections
#
# A section is a lasting workspace grown out of one finished pipeline. The
# founding pipeline stops being a one-off run and becomes standing knowledge;
# new pipelines started inside the section build on top of it instead of
# researching the same ground again.
#
# Everything about a section lives in its folder under "Let Jarvis Handle It" —
# the founding pipeline's own folder, so nothing is copied or moved.
# ---------------------------------------------------------------------------

def _section_summariser(brief: str, material: str, previous: str) -> str:
    """Write the living summary. Returns "" if the model is unreachable.

    sections.refresh_summary() falls back to assembling the summary from disk,
    so a dead model call costs polish, never knowledge.
    """
    if not GEMINI_API_KEY:
        return ""
    prompt = (
        "You maintain the standing knowledge of a long-running workspace called a "
        "section. Write the document titled \"What this section knows\".\n\n"
        "RULES:\n"
        "- Plain markdown, no title heading (one is added for you).\n"
        "- State what is established, concretely. This is read by agents starting "
        "new work, so it must be usable, not a table of contents.\n"
        "- Invent nothing. Only what the material below supports.\n"
        "- Keep everything from the previous version that the new material does not "
        "contradict, including anything the user edited in by hand.\n"
        "- Link related topics as [[wikilinks]] using the note names given.\n\n"
        f"WHAT THIS SECTION IS ABOUT:\n{brief or '(not written)'}\n\n"
        f"PREVIOUS VERSION:\n{previous or '(none yet)'}\n\n"
        f"MATERIAL FROM THE SECTION\u2019S PIPELINES:\n{material or '(none yet)'}"
    )
    try:
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return (response.text or "").strip()
    except Exception as e:
        print(f"[Sections] Summary model call failed: {e}")
        return ""


def refresh_section_knowledge(section: dict):
    """Harvest the section's pipeline memory and rewrite its summary and note."""
    try:
        section_store.refresh_summary(section, summarise=_section_summariser)
        conn = db.get_connection(DB_PATH)
        try:
            plan_ids = set(db.get_section_plan_ids(conn, section["id"]))
            pipelines = [p for p in db.get_pipelines(conn) if p["id"] in plan_ids]
        finally:
            conn.close()
        section_store.write_section_note(section, pipelines)
    except Exception as e:
        print(f"[Sections] Could not refresh knowledge for {section.get('id')}: {e}")


def attach_pipeline_to_section(section: dict, plan_id: str):
    """File a newly started pipeline under the section it was started inside."""
    conn = db.get_connection(DB_PATH)
    try:
        db.add_pipeline_to_section(conn, section["id"], plan_id)
    except Exception as e:
        print(f"[Sections] Could not attach {plan_id}: {e}")
        return
    finally:
        conn.close()
    # Refreshing here keeps Section.md listing every pipeline, but the knowledge
    # itself cannot change until the new pipeline has actually produced anything.
    try:
        section_store.write_section_note(section, _section_pipelines(section["id"]))
    except Exception as e:
        print(f"[Sections] Could not update the section note: {e}")


def _section_pipelines(section_id: str) -> list[dict]:
    conn = db.get_connection(DB_PATH)
    try:
        plan_ids = db.get_section_plan_ids(conn, section_id)
        by_id = {p["id"]: p for p in db.get_pipelines(conn)}
    finally:
        conn.close()
    return [by_id[pid] for pid in plan_ids if pid in by_id]


def _section_card(section: dict) -> dict:
    """What the sidebar needs to draw one block."""
    pipelines = _section_pipelines(section["id"])
    return {
        "id": section["id"],
        "name": section["name"],
        "folder": section["folder"],
        "brief": section.get("brief", ""),
        "created_at": section.get("created_at"),
        "founding_plan_id": section.get("founding_plan_id"),
        "plan_ids": [p["id"] for p in pipelines],
        "pipeline_count": len(pipelines),
        "running": any(p.get("status") == "running" for p in pipelines),
        "latest_plan_id": pipelines[-1]["id"] if pipelines else None,
    }


# ---------------------------------------------------------------------------
# The section clarification gate
#
# Turning a finished pipeline into a section is a commitment, so it goes through
# the same gate a pipeline does: you write the brief and drop the files, Jarvis
# asks only what it genuinely does not know, and then paints the section brief
# back for you to correct. Nothing is created until "Create section" is pressed.
#
# It never asks about what the founding pipeline already established — that
# material is read straight off disk and put in front of the model first.
# ---------------------------------------------------------------------------

def create_section_draft(plan: dict, name: str, brief: str) -> dict:
    """Open a draft for a section. No section, no DB row, nothing persistent."""
    import time as _time
    import uuid
    _prune_section_drafts()
    folder = plan.get("project_name") or "Default Project"
    draft = {
        "draft_id": uuid.uuid4().hex[:8],
        "plan_id": plan["id"],
        "folder": folder,
        "task": plan.get("task_summary") or plan.get("task") or "",
        "name": (name or "").strip() or folder,
        "brief": (brief or "").strip(),
        "files": [],
        "qa": [],
        "pending_questions": [],
        "rounds": 0,
        "brief_text": None,          # the painted section brief, once written
        "stage": "brief",
        "created": _time.time(),
        "touched": _time.time(),
    }
    with SECTION_DRAFTS_LOCK:
        SECTION_DRAFTS[draft["draft_id"]] = draft
    return draft


def _get_section_draft(draft_id: str):
    import time as _time
    with SECTION_DRAFTS_LOCK:
        draft = SECTION_DRAFTS.get((draft_id or "").strip())
        if draft:
            draft["touched"] = _time.time()
        return draft


def _delete_section_draft_uploads(draft: dict):
    """Delete only the files THIS draft uploaded.

    The folder belongs to the founding pipeline and is full of its work, so
    unlike the pipeline gate this never removes directories — abandoning a
    section must not touch anything the pipeline put there.
    """
    for f in draft.get("files", []):
        try:
            if os.path.exists(f["path"]):
                os.remove(f["path"])
        except Exception as e:
            print(f"[Sections] Could not delete {f.get('name')}: {e}")


def _prune_section_drafts():
    """Drop drafts nobody came back to, along with the files they uploaded."""
    import time as _time
    cutoff = _time.time() - INTAKE_DRAFT_TTL
    with SECTION_DRAFTS_LOCK:
        stale = [d for d in SECTION_DRAFTS.values() if d.get("touched", 0) < cutoff]
        for draft in stale:
            SECTION_DRAFTS.pop(draft["draft_id"], None)
    for draft in stale:
        if not draft.get("created_section"):
            _delete_section_draft_uploads(draft)


def _section_draft_discard(draft: dict):
    """Cancel means it never happened: forget the draft, delete its uploads."""
    with SECTION_DRAFTS_LOCK:
        SECTION_DRAFTS.pop(draft["draft_id"], None)
    _delete_section_draft_uploads(draft)


def _section_draft_context_text(draft: dict) -> str:
    """Everything Jarvis knows about this section-to-be, as prompt text."""
    lines = [
        "A finished pipeline is about to become a SECTION: a lasting workspace that "
        "later pipelines start inside, already knowing what this one learned.",
        "",
        # Without this the gate asks where the section will live and what will host
        # it — questions this app answered long ago, and which waste the user's time.
        "WHAT A SECTION ALREADY IS. All of this is decided. Never ask about any of it:\n"
        "- It lives inside this application: on the sections sidebar, with its own dashboard "
        "page, reachable from every page.\n"
        "- Its folder is the founding pipeline's own folder on this machine. Its brief, its "
        "knowledge notes and any dropped files are written there as markdown.\n"
        "- It holds the pipelines started inside it, plus its own tasks, notes, and its own "
        "remembered conversation with Jarvis.\n"
        "- Its knowledge is harvested automatically from the pipelines that run in it, and "
        "handed to every new pipeline started inside it.\n"
        "So there is nothing to ask about hosting, platforms, storage, tooling, file formats, "
        "naming, or how the section will be organised. Ask only about the work itself.",
        "",
        f"THE FOUNDING PIPELINE:\n{draft.get('task', '')}",
    ]

    material = section_store.pipeline_material(draft["folder"])
    if material:
        lines.append(
            "\nWHAT THAT PIPELINE ALREADY FOUND — this is established knowledge the "
            "section inherits. Never ask the user about anything in here:\n" + material
        )
    else:
        lines.append("\nWHAT THAT PIPELINE ALREADY FOUND:\n(it left nothing in its memory)")

    lines.append(f"\nSECTION NAME THE USER GAVE:\n{draft.get('name', '')}")
    written = (draft.get("brief") or "").strip()
    lines.append(
        "\nWHAT THE USER WROTE THIS SECTION IS FOR:\n" + (written if written else "(nothing written)")
    )

    files = draft.get("files", [])
    if files:
        listed = "\n".join(f"- {f['name']} ({f.get('mime', 'unknown type')})" for f in files)
        lines.append(
            "\nFILES THEY DROPPED (readable ones are included with this message):\n" + listed
        )
    else:
        lines.append("\nFILES THEY DROPPED:\n(none)")

    qa = draft.get("qa", [])
    if qa:
        answered = "\n".join(f"Q: {item['question']}\nA: {item['answer']}" for item in qa)
        lines.append("\nCLARIFICATIONS ALREADY ANSWERED — never ask these again:\n" + answered)
    return "\n".join(lines)


def _section_draft_ask(draft: dict, instruction: str):
    return _ask_model_json(instruction, _section_draft_context_text(draft),
                           _intake_file_parts(draft))


def _section_draft_questions(draft: dict) -> list[dict]:
    """The gaps that would change what this section is, and what it inherits."""
    instruction = (
        "You are Jarvis. The user is turning a finished pipeline into a lasting section. "
        "Before you write down what this section is, find what you genuinely do not know.\n\n"
        "RULES:\n" + _QUESTION_RULES +
        "- Ask only about the work: what this section is for, where the user is taking it, what "
        "belongs inside it, and which of the pipeline's findings actually matter going forward.\n"
        "- Never ask about anything the founding pipeline already established — that material "
        "is above and is inherited whether or not you ask.\n\n"
        "For each question also write \"gist\": a single short spoken line (under 15 words) that "
        "conveys the question aloud.\n\n"
        "Reply with JSON only: {\"questions\": [{\"question\": \"...\", \"gist\": \"...\"}]}"
    )
    try:
        data = _section_draft_ask(draft, instruction)
    except Exception as e:
        # A dead question round must not trap the user — fall through to the brief.
        print(f"[Sections] Question generation failed: {e}")
        return []
    return _normalise_questions(data)


def _section_draft_fallback_brief(draft: dict) -> str:
    """The section brief assembled by hand, for when the model is unreachable."""
    out = []
    written = (draft.get("brief") or "").strip()
    if written:
        out += [written, ""]
    qa = [item for item in draft.get("qa", []) if (item.get("answer") or "").strip()]
    if qa:
        out.append("## Clarifications")
        out.append("")
        for item in qa:
            out += [f"**{item['question']}**", "", item["answer"], ""]
    return "\n".join(out).strip() or written


def _section_draft_paint(draft: dict) -> dict:
    """Either the section brief, or another round of questions — Jarvis decides."""
    instruction = (
        "You are Jarvis. Using everything below, write the SECTION BRIEF: what this workspace "
        "is for and where it is going. Every pipeline started inside the section begins by "
        "reading it, so it has to stand on its own.\n\n"
        "RULES:\n"
        "- Fold in what the user wrote, every clarification, and everything the dropped files "
        "tell you.\n"
        "- Say what the section is for and what work belongs in it. Do not summarise the "
        "founding pipeline's findings — those are already the section's knowledge.\n"
        "- Plain language and concrete. Invent nothing the user never gave you.\n"
        "- Short: a few paragraphs at most, markdown, no heading above the top level.\n"
        "- Now that you have read the answers, ask again if they opened something material you "
        "still cannot settle: do NOT guess, return questions instead. The rules on what makes a "
        "question worth asking apply here exactly as they did before:\n" + _QUESTION_RULES + "\n"
        "Reply with JSON only, one of:\n"
        "{\"brief_text\": \"the section brief in markdown\"}\n"
        "{\"questions\": [{\"question\": \"...\", \"gist\": \"...\"}]}"
    )
    try:
        data = _section_draft_ask(draft, instruction)
    except Exception as e:
        # Degrade to what the user wrote so they can still edit and create.
        print(f"[Sections] Writing the section brief failed: {e}")
        return {
            "brief_text": _section_draft_fallback_brief(draft),
            "degraded": "Jarvis could not reach the model to write this up, so this is what "
                        "you wrote. You can edit it and create the section from it.",
        }

    if isinstance(data, dict) and data.get("questions"):
        questions = _normalise_questions(data)
        if questions:
            return {"questions": questions}

    brief_text = (data or {}).get("brief_text") if isinstance(data, dict) else None
    return {"brief_text": brief_text or _section_draft_fallback_brief(draft)}


def _section_draft_record(draft: dict, final_brief: str) -> str | None:
    """Write the full clarification record into the section's Brief/ folder.

    The brief that ends up in the database is the painted one; this keeps the
    original wording, the questions and the answers next to the pipeline's own
    brief, where the agents can read them.
    """
    out = [f"# Section brief — {draft.get('name') or draft.get('folder')}", ""]
    out += ["## Founding pipeline", draft.get("task", ""), ""]

    written = (draft.get("brief") or "").strip()
    out += ["## What the user wrote", written if written else "_(nothing written)_", ""]

    qa = draft.get("qa", [])
    if qa:
        out.append("## Clarifications")
        out.append("")
        for item in qa:
            out += [f"**Q:** {item['question']}", "", f"**A:** {item['answer'] or '(no answer)'}", ""]

    files = draft.get("files", [])
    if files:
        out.append("## Files dropped at creation")
        for f in files:
            out.append(f"- `Inputs/{f['name']}` — {f.get('mime', 'unknown type')}")
        out.append("")

    out += ["## The section brief", final_brief, ""]

    try:
        brief_dir = section_store.section_dir(draft["folder"], "Brief")
        path = os.path.join(brief_dir, "section_brief.md")
        n = 2
        while os.path.exists(path):
            path = os.path.join(brief_dir, f"section_brief ({n}).md")
            n += 1
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        return path
    except Exception as e:
        # The record is a convenience; never let it stop a section being created.
        print(f"[Sections] Could not write the section brief record: {e}")
        return None


@app.route("/sections", methods=["GET"])
def sections_list_route():
    """Every section, for the sidebar."""
    conn = db.get_connection(DB_PATH)
    try:
        rows = db.get_sections(conn)
    finally:
        conn.close()
    return jsonify({"sections": [_section_card(row) for row in rows]})


@app.route("/sections/create", methods=["POST"])
def sections_create_route():
    """Turn a finished pipeline into a section.

    The brief the user writes here is what tells Jarvis what the section is for —
    the founding pipeline alone only says what was researched, not where it is
    going. Creating is the commit point: nothing exists until this is called.

    Two ways in, and this is still the only path that creates a section:
    `draft_id` finishes a clarified draft (its questions answered, its brief
    painted and corrected), while a bare `plan_id` is the skip — create it now
    from what was typed, no questions asked.
    """
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(str(data.get("draft_id") or "").strip())
    plan_id = str(data.get("plan_id") or (draft or {}).get("plan_id") or "").strip()
    brief = (data.get("brief") or "").strip()
    if draft:
        # The draft's own text wins: it is what the user read and corrected.
        brief = (draft.get("brief_text") or "").strip() or _section_draft_fallback_brief(draft)
    if not plan_id:
        return jsonify({"error": "plan_id is required"}), 400

    conn = db.get_connection(DB_PATH)
    try:
        plan = next((p for p in db.get_pipelines(conn) if p["id"] == plan_id), None)
        if not plan:
            return jsonify({"error": f"pipeline '{plan_id}' not found"}), 404

        existing = db.get_section_for_pipeline(conn, plan_id)
        if existing:
            return jsonify({"error": "That pipeline is already part of a section.",
                            "section_id": existing["id"]}), 409

        folder = plan.get("project_name") or "Default Project"
        name = ((data.get("name") or (draft or {}).get("name") or "").strip() or folder)
        import uuid
        section_id = uuid.uuid4().hex[:8]
        db.create_section(conn, section_id, name, folder, brief, plan_id)
        section = db.get_section(conn, section_id)
    finally:
        conn.close()

    record_path = None
    if draft:
        with SECTION_DRAFTS_LOCK:
            draft["name"] = name
            # The draft's uploads now belong to the section, so retiring it must
            # not delete them.
            draft["created_section"] = section_id
        record_path = _section_draft_record(draft, brief)
        with SECTION_DRAFTS_LOCK:
            SECTION_DRAFTS.pop(draft["draft_id"], None)

    # The founding pipeline's memory becomes the section's first knowledge.
    refresh_section_knowledge(section)
    return jsonify({"status": "created", "section": _section_card(section),
                    "brief_path": record_path})


@app.route("/sections/intake/start", methods=["POST"])
def section_intake_start_route():
    """Open a draft for a section. Nothing is persisted and no section exists yet."""
    data = request.get_json(force=True) or {}
    plan_id = str(data.get("plan_id") or "").strip()
    if not plan_id:
        return jsonify({"error": "plan_id is required"}), 400

    conn = db.get_connection(DB_PATH)
    try:
        plan = next((p for p in db.get_pipelines(conn) if p["id"] == plan_id), None)
        if not plan:
            return jsonify({"error": f"pipeline '{plan_id}' not found"}), 404
        # Checked here as well as at creation, so the questions are never asked
        # about a pipeline that could not become a section anyway.
        existing = db.get_section_for_pipeline(conn, plan_id)
        if existing:
            return jsonify({"error": "That pipeline is already part of a section.",
                            "section_id": existing["id"]}), 409
    finally:
        conn.close()

    draft = create_section_draft(plan, data.get("name") or "", data.get("brief") or "")
    return jsonify({"draft_id": draft["draft_id"], "name": draft["name"],
                    "folder": draft["folder"]})


@app.route("/sections/intake/upload", methods=["POST"])
def section_intake_upload_route():
    """Store the dropped files in the section's Inputs/ folder.

    They go to their final home rather than a staging area, so creating the
    section moves nothing; cancelling deletes exactly these files and nothing
    the founding pipeline put there.
    """
    draft = _get_section_draft((request.form.get("draft_id") or "").strip())
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify({"error": "no files uploaded"}), 400

    inputs_dir = section_store.section_dir(draft["folder"], "Inputs")
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        name = _intake_safe_filename(upload.filename)
        base, ext = os.path.splitext(name)
        candidate, n = name, 2
        existing = {f["name"] for f in draft["files"]}
        while candidate in existing or os.path.exists(os.path.join(inputs_dir, candidate)):
            candidate = f"{base} ({n}){ext}"
            n += 1
        path = os.path.join(inputs_dir, candidate)
        upload.save(path)
        with SECTION_DRAFTS_LOCK:
            draft["files"].append({
                "name": candidate,
                "path": path,
                "mime": upload.mimetype or "application/octet-stream",
                "size": os.path.getsize(path),
            })

    return jsonify({"files": [
        {k: v for k, v in f.items() if k != "path"} for f in draft["files"]
    ]})


@app.route("/sections/intake/questions", methods=["POST"])
def section_intake_questions_route():
    """Store the brief as written, then ask for the gaps that would change it."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    with SECTION_DRAFTS_LOCK:
        if "brief" in data:
            draft["brief"] = (data.get("brief") or "").strip()
        if "name" in data:
            draft["name"] = (data.get("name") or "").strip() or draft["name"]

    set_orb("thinking")
    questions = _section_draft_questions(draft)
    set_orb("idle")
    with SECTION_DRAFTS_LOCK:
        draft["pending_questions"] = questions
        draft["rounds"] += 1
        draft["stage"] = "questions" if questions else "brief_text"

    return jsonify({"questions": questions, "round": draft["rounds"]})


@app.route("/sections/intake/answer", methods=["POST"])
def section_intake_answer_route():
    """Record one answer and hand back the next question, if any."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    with SECTION_DRAFTS_LOCK:
        draft["qa"].append({"question": question, "answer": (data.get("answer") or "").strip()})
        draft["pending_questions"] = [
            q for q in draft["pending_questions"] if q.get("question") != question
        ]
        remaining = list(draft["pending_questions"])

    return jsonify({"next": remaining[0] if remaining else None,
                    "remaining": len(remaining), "done": not remaining})


@app.route("/sections/intake/skip", methods=["POST"])
def section_intake_skip_route():
    """'Skip the rest': drop the unanswered questions and write it up anyway."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404
    with SECTION_DRAFTS_LOCK:
        draft["pending_questions"] = []
    return jsonify({"status": "skipped"})


@app.route("/sections/intake/picture", methods=["POST"])
def section_intake_picture_route():
    """Jarvis decides: more questions, or the section brief."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    set_orb("thinking")
    result = _section_draft_paint(draft)
    set_orb("idle")

    if result.get("questions"):
        with SECTION_DRAFTS_LOCK:
            draft["pending_questions"] = result["questions"]
            draft["rounds"] += 1
            draft["stage"] = "questions"
        return jsonify({"questions": result["questions"], "round": draft["rounds"]})

    with SECTION_DRAFTS_LOCK:
        draft["brief_text"] = result.get("brief_text", "")
        draft["stage"] = "brief_text"
    return jsonify({"brief_text": draft["brief_text"], "degraded": result.get("degraded")})


@app.route("/sections/intake/edit", methods=["POST"])
def section_intake_edit_route():
    """Clean up the user's edit and hand it back — never create."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"error": "draft not found"}), 404

    edited = data.get("edited_text") or ""
    if not edited.strip():
        return jsonify({"error": "edited_text is required"}), 400

    set_orb("thinking")
    cleaned = _clean_edited_text(_section_draft_context_text(draft),
                                _intake_file_parts(draft), edited, noun="section brief")
    set_orb("idle")
    with SECTION_DRAFTS_LOCK:
        draft["brief_text"] = cleaned
        draft["stage"] = "brief_text"
    return jsonify({"brief_text": cleaned})


@app.route("/sections/intake/cancel", methods=["POST"])
def section_intake_cancel_route():
    """Cancel means it never happened: no section, and the drops are deleted."""
    data = request.get_json(force=True) or {}
    draft = _get_section_draft(data.get("draft_id", ""))
    if not draft:
        return jsonify({"status": "already_gone"})
    _section_draft_discard(draft)
    return jsonify({"status": "cancelled"})


@app.route("/sections/<section_id>", methods=["GET"])
def section_detail_route(section_id):
    """Everything the section dashboard shows."""
    conn = db.get_connection(DB_PATH)
    try:
        section = db.get_section(conn, section_id)
        if not section:
            return jsonify({"error": "section not found"}), 404
        tasks = db.get_tasks(conn, section_id=section_id)
        note_rows = conn.execute(
            "SELECT * FROM notes WHERE section_id = ? ORDER BY created_at DESC", (section_id,)
        ).fetchall()
        notes = [dict(r) for r in note_rows]
        messages = db.get_section_messages(conn, section_id)
    finally:
        conn.close()

    return jsonify({
        "section": _section_card(section),
        "summary": section_store.summary_body(section["folder"]),
        "knowledge": [
            {"name": n["name"], "preview": n["preview"]}
            for n in section_store.knowledge_notes(section["folder"])
        ],
        "pipelines": [
            {
                "id": p["id"],
                "task": p.get("task_summary") or p.get("task"),
                "status": p.get("status"),
                "phase": p.get("phase"),
                "timestamp": p.get("timestamp"),
                "founding": p["id"] == section.get("founding_plan_id"),
            }
            for p in _section_pipelines(section_id)
        ],
        "tasks": tasks,
        "notes": notes,
        "messages": messages,
        "folder": os.path.join("Let Jarvis Handle It", section["folder"]),
    })


@app.route("/sections/<section_id>/enter", methods=["POST"])
def section_enter_route(section_id):
    """Work inside this section: scoped tools, its own remembered conversation."""
    section = load_section(section_id)
    if not section:
        return jsonify({"error": "section not found"}), 404
    coordinator.set_active_section(section)
    return jsonify({"status": "entered", "section": _section_card(section)})


@app.route("/sections/exit", methods=["POST"])
def section_exit_route():
    """Back out to the brain."""
    coordinator.set_active_section(None)
    return jsonify({"status": "exited"})


@app.route("/sections/active", methods=["GET"])
def section_active_route():
    section = coordinator.get_active_section()
    return jsonify({"section": _section_card(section) if section else None})


@app.route("/sections/<section_id>/chat", methods=["POST"])
def section_chat_route(section_id):
    """Talk to Jarvis inside a section. The conversation persists to the section."""
    section = load_section(section_id)
    if not section:
        return jsonify({"error": "section not found"}), 404

    text = ((request.get_json(force=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text provided"}), 400

    # Entering on every message keeps the focus right even if the user opened the
    # section in one window and left another on the brain.
    coordinator.set_active_section(section)

    conn = db.get_connection(DB_PATH)
    try:
        db.add_section_message(conn, section_id, "user", text)
    finally:
        conn.close()

    try:
        reply = handle_request(text)
    except Exception as e:
        reply = f"Something went wrong: {e}"
    if not (reply or "").strip():
        reply = "I could not produce a reply for that, Sir. Please try again."

    conn = db.get_connection(DB_PATH)
    try:
        db.add_section_message(conn, section_id, "jarvis", reply)
    finally:
        conn.close()

    threading.Thread(target=speak, args=(reply,), daemon=True).start()
    return jsonify({"reply": reply})


@app.route("/sections/<section_id>/update", methods=["POST"])
def section_update_route(section_id):
    """Edit the section's name, its brief, or the living summary by hand."""
    data = request.get_json(force=True) or {}
    conn = db.get_connection(DB_PATH)
    try:
        section = db.get_section(conn, section_id)
        if not section:
            return jsonify({"error": "section not found"}), 404
        if "name" in data or "brief" in data:
            db.update_section(conn, section_id,
                              name=(data.get("name") or "").strip() or None,
                              brief=data.get("brief"))
            section = db.get_section(conn, section_id)
    finally:
        conn.close()

    if "summary" in data:
        # Written back verbatim: this file is yours to edit.
        section_store.write_summary(section["folder"], data.get("summary") or "",
                                    section.get("name", ""))
    section_store.write_section_note(section, _section_pipelines(section_id))

    # The section's identity changed, so the chat session built on it is stale.
    coordinator.clear_section_chat(section_id)
    if (coordinator.get_active_section() or {}).get("id") == section_id:
        coordinator.set_active_section(section)
    return jsonify({"status": "updated", "section": _section_card(section)})


@app.route("/sections/<section_id>/refresh", methods=["POST"])
def section_refresh_route(section_id):
    """Re-read the section's pipelines and rewrite what it knows."""
    section = load_section(section_id)
    if not section:
        return jsonify({"error": "section not found"}), 404
    refresh_section_knowledge(section)
    coordinator.clear_section_chat(section_id)
    return jsonify({"status": "refreshed",
                    "summary": section_store.summary_body(section["folder"])})


@app.route("/sections/<section_id>/upload", methods=["POST"])
def section_upload_route(section_id):
    """Drop files into the section's folder."""
    section = load_section(section_id)
    if not section:
        return jsonify({"error": "section not found"}), 404

    uploaded = request.files.getlist("files") or []
    if not uploaded:
        return jsonify({"error": "no files"}), 400

    inputs = section_store.section_dir(section["folder"], "Inputs")
    saved = []
    for f in uploaded:
        name = _intake_safe_filename(f.filename)
        path = os.path.join(inputs, name)
        # Never silently overwrite a file an earlier pipeline may be working from.
        stem, ext = os.path.splitext(name)
        n = 2
        while os.path.exists(path):
            path = os.path.join(inputs, f"{stem} ({n}){ext}")
            n += 1
        try:
            f.save(path)
            saved.append({"name": os.path.basename(path), "mime": f.mimetype})
        except Exception as e:
            print(f"[Sections] Could not save {name}: {e}")
    return jsonify({"status": "uploaded", "files": saved})


@app.route("/sections/<section_id>/delete", methods=["POST"])
def section_delete_route(section_id):
    """Forget the section. Its folder and its pipelines are left untouched —
    closing a workspace must never destroy the work done inside it."""
    conn = db.get_connection(DB_PATH)
    try:
        if not db.get_section(conn, section_id):
            return jsonify({"error": "section not found"}), 404
        db.delete_section(conn, section_id)
    finally:
        conn.close()
    coordinator.clear_section_chat(section_id)
    if (coordinator.get_active_section() or {}).get("id") == section_id:
        coordinator.set_active_section(None)
    return jsonify({"status": "deleted"})


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


@app.route("/api/mcp/servers", methods=["GET"])
def mcp_servers_route():
    """Every configured MCP server with its live status.

    An enabled server is actually started to answer this — status here means a
    real handshake succeeded, never that a config file said so.
    """
    from connectors.mcp_connector import list_available_mcps
    try:
        return jsonify({"servers": list_available_mcps()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mcp/toggle", methods=["POST"])
def mcp_toggle_route():
    """Enable or disable one MCP server, then report what actually happened.

    Enabling starts the server immediately so the answer carries its real tool
    list — or the reason it refused to start, instead of a hopeful "up".
    """
    from connectors.mcp_client import load_mcp_registry, save_mcp_registry, ensure_server_running

    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    enabled = bool(data.get("enabled"))
    if not name:
        return jsonify({"error": "name is required"}), 400

    registry = load_mcp_registry()
    if name not in registry:
        return jsonify({"error": f"No MCP server called '{name}' is configured."}), 404
    if not registry[name].get("command"):
        return jsonify({"error": f"'{name}' has no command to run — add one to mcp_registry.json first."}), 400

    registry[name]["enabled"] = enabled
    save_mcp_registry(registry)

    if not enabled:
        push_message("system", f"MCP server '{name}' disabled.")
        return jsonify({"name": name, "enabled": False, "status": "disabled", "tools": []})

    info = ensure_server_running(name)
    if info["status"] == "up":
        push_message("system", f"MCP server '{name}' connected — {len(info['tools'])} tool(s) available.")
    else:
        push_message("system", f"MCP server '{name}' could not start: {info.get('error')}")
    return jsonify({
        "name": name,
        "enabled": True,
        "status": info["status"],
        "tools": [t["name"] for t in info.get("tools", [])],
        "error": info.get("error"),
    })


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


@app.route("/api/tools/overview", methods=["GET"])
def tools_overview_route():
    """Everything the Connected panel shows: APIs and MCP servers, side by side.

    Two things worth being blunt about here, because both were previously
    invisible:
      * `has_handler` — an API can sit in api_registry.json marked "up" while
        no agent can call it, because no real handler exists for it. Connected
        and usable are not the same thing.
      * MCP status is measured, not read. An enabled server is started to find
        out whether it works.
    """
    from connectors.api_connector import load_registry
    from connectors.oauth_flow import OAUTH_PROVIDERS
    from agents.tool_executor import REGISTRY_TOOLS, ALWAYS_ON_TOOLS, _resolve_tool_key

    registry = load_registry() or {}
    apis = []
    # Everything Jarvis has a real connector for, plus anything already in the
    # registry — so services with a handler show up even before they're set up.
    names = sorted(set(registry) | set(REGISTRY_TOOLS))
    for name in names:
        cfg = registry.get(name) or {}
        status = cfg.get("status", "unknown")
        # Resolve rather than test membership: google_drive_api has no entry of
        # its own but aliases onto google_docs_api's handler, and calling that
        # "no connector" would be plainly wrong. No model call here — the
        # deterministic tiers are enough for a name already in the registry.
        resolved = _resolve_tool_key(name, allow_llm=False)
        spec = REGISTRY_TOOLS.get(resolved)
        apis.append({
            "service": name,
            "configured": bool(cfg) and status != "unknown",
            "status": status if cfg else "unknown",
            "method_id": cfg.get("method_id"),
            "last_updated": cfg.get("last_updated"),
            "has_handler": spec is not None,
            "handled_by": resolved if (spec and resolved != name) else None,
            "auth": "oauth" if name in OAUTH_PROVIDERS else "api_key",
            "tool_name": spec["declaration"]["name"] if spec else None,
        })

    try:
        from connectors.mcp_connector import list_available_mcps
        mcps = list_available_mcps()
    except Exception as e:
        mcps = []
        print(f"[Tools] Could not list MCP servers: {e}")

    always_on = [
        {"name": spec["declaration"]["name"], "description": spec["declaration"]["description"]}
        for spec in ALWAYS_ON_TOOLS.values()
    ]

    return jsonify({"apis": apis, "mcps": mcps, "always_on": always_on})


@app.route("/api/tools/disconnect", methods=["POST"])
def disconnect_tool_route():
    """Mark an API as not connected.

    Deliberately does NOT delete anything from .env — flipping a switch in the
    UI should not silently destroy credentials that were awkward to obtain.
    The service stops being offered to agents; reconnecting re-uses whatever
    is still stored unless you overwrite it.
    """
    # Mutate the dict load_registry() returns, never a name imported earlier:
    # load_registry() rebinds the module global to a freshly-parsed dict, so an
    # imported API_REGISTRY reference goes stale and writes land on a detached
    # copy that save_registry() never sees.
    from connectors.api_connector import load_registry, save_registry

    data = request.get_json(force=True) or {}
    service = (data.get("service_name") or "").strip()
    if not service:
        return jsonify({"error": "service_name is required"}), 400

    registry = load_registry()
    if service not in registry:
        return jsonify({"error": f"'{service}' is not in the registry."}), 404

    registry[service]["status"] = "unknown"
    save_registry()
    push_message("system", f"{service} disconnected. Stored credentials were left in place.")
    return jsonify({"service": service, "status": "unknown", "configured": False})


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
                    # Match whole words on a SHORT utterance only. Plain substring
                    # matching hid the window mid-request: "quit" is inside "quite",
                    # "exit" inside "exits", and any long instruction that happened to
                    # contain one was swallowed as a goodbye instead of being answered.
                    should_stop = is_stop_command(transcript_lower, stop_words)
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
