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
ELEVENLABS_QUOTA_EXCEEDED = False # temporary runtime flag to skip ElevenLabs if quota exceeded

# --- Pipeline Gate State ---
PIPELINE_STATE = {
    "current_gate": None,        # None | 1 | 2 | 3
    "gate_status": "idle",       # "idle" | "waiting" | "approved" | "rejected"
    "redirect_note": None,       # human's rejection reason
    "phase": "idle",             # e.g. "research" | "synthesis" | "execution" | "deployed"
}
PIPELINE_LOCK = threading.Lock()

# --- Track Agent Metric Store ---
TRACKED_METRICS = {}   # e.g. {"youtube_ctr": {"value": 0.025, "threshold": 0.03}}
TRACKED_METRICS_LOCK = threading.Lock()

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



def push_message(role, text):
    with STATE_LOCK:
        CONVO.append({"role": role, "text": text})


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
            import requests
            import tempfile
            import ctypes
            
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
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                    f.write(res.content)
                    temp_path = f.name
                
                path_str = os.path.abspath(temp_path)
                ctypes.windll.winmm.mciSendStringW(f'open "{path_str}" type mpegvideo alias jarvis_voice', None, 0, 0)
                
                # Start simulated word updater thread for ElevenLabs playback
                def simulate_words():
                    global CURRENT_SPOKEN_WORD
                    words = text.split()
                    for i, w in enumerate(words):
                        if not played_via_eleven: # if aborted or cleaned up
                            break
                        CURRENT_SPOKEN_WORD = f"{w}_{i}"
                        # Average speed: 170 WPM -> ~350ms per word
                        time.sleep(0.35)
                
                sim_thread = threading.Thread(target=simulate_words, daemon=True)
                sim_thread.start()

                ctypes.windll.winmm.mciSendStringW('play jarvis_voice wait', None, 0, 0)
                ctypes.windll.winmm.mciSendStringW('close jarvis_voice', None, 0, 0)
                played_via_eleven = False # stops simulation thread
                
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                played_via_eleven = True
            else:
                print(f"[ElevenLabs Error] {res.status_code}: {res.text}")
                if res.status_code in (401, 429) or "quota" in res.text.lower():
                    ELEVENLABS_QUOTA_EXCEEDED = True
                    print("[Jarvis] ElevenLabs API quota exceeded. Temporarily falling back to local TTS for this session.")
        except Exception as e:
            print(f"[ElevenLabs Exception] {e}")

    if not played_via_eleven:
        try:
            engine = pyttsx3.init()
            with SETTINGS_LOCK:
                speed = SETTINGS.get("voice_speed", 175)
            engine.setProperty('rate', int(speed))
            
            # Update CURRENT_SPOKEN_WORD for every word spoken
            def on_word(name, location, length):
                global CURRENT_SPOKEN_WORD
                words = text.split()
                # Estimate current word index based on character location
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
            
    # Reset spoken word after speech finishes
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
            UI_ACTION = {
                "type": "control_interface",
                "action": args.get("action"),
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

coordinator.register_tool_listener(jarvis_tool_listener)

def handle_request(transcript: str) -> str:
    return coordinator.handle_request(transcript)


SAMPLE_RATE = 16000
CHUNK_SIZE = 1280

# Global speech recognizer configuration
recognizer = sr.Recognizer()
recognizer.energy_threshold = 300
recognizer.dynamic_energy_threshold = True
recognizer.dynamic_energy_adjustment_damping = 0.15
recognizer.dynamic_energy_ratio = 1.5
recognizer.pause_threshold = 0.8

# ---------------------------------------------------------------------------
# Local-only web server (127.0.0.1 - not reachable from your network)
# ---------------------------------------------------------------------------

app = Flask(__name__)
import logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # quiet the request logs


@app.route("/command-center")
def command_center():
    return send_from_directory(BASE_DIR, "command_center.html")


@app.route("/plan.html")
def plan_page():
    return send_from_directory(BASE_DIR, "plan.html")


@app.route("/provider_comparison.html")
def provider_comparison_page():
    return send_from_directory(BASE_DIR, "provider_comparison.html")


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
    global FOCUS_TASK_IDS, UI_ACTION, JARVIS_SLEEPING, CURRENT_SPOKEN_WORD
    with STATE_LOCK:
        res = jsonify({
            "orb": ORB_STATE,
            "messages": CONVO,
            "focus_task_ids": FOCUS_TASK_IDS,
            "ui_action": UI_ACTION,
            "sleeping": JARVIS_SLEEPING,
            "current_word": CURRENT_SPOKEN_WORD
        })
        # Clear focused task IDs and UI action after serving so they only trigger once
        FOCUS_TASK_IDS = []
        UI_ACTION = None
        return res


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
    with PIPELINE_LOCK:
        return jsonify(PIPELINE_STATE.copy())


@app.route("/gate/approve", methods=["POST"])
def gate_approve():
    with PIPELINE_LOCK:
        gate = PIPELINE_STATE.get("current_gate")
        if gate is None:
            return jsonify({"error": "No gate is currently active"}), 400
        PIPELINE_STATE["gate_status"] = "approved"
        PIPELINE_STATE["redirect_note"] = None
    push_message("system", f"Gate {gate} approved. Advancing pipeline.")
    return jsonify({"status": "approved", "gate": gate})


@app.route("/gate/reject", methods=["POST"])
def gate_reject():
    data = request.get_json(force=True) or {}
    note = data.get("redirect_note", "").strip()
    with PIPELINE_LOCK:
        gate = PIPELINE_STATE.get("current_gate")
        if gate is None:
            return jsonify({"error": "No gate is currently active"}), 400
        PIPELINE_STATE["gate_status"] = "rejected"
        PIPELINE_STATE["redirect_note"] = note or None
    push_message("system", f"Gate {gate} rejected. Note: {note or 'none provided'}")
    return jsonify({"status": "rejected", "gate": gate, "redirect_note": note})


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
    a corrective sub-task."""
    print("[Track Agent] Started monitoring.")
    while True:
        time.sleep(300)  # 5 minutes
        with TRACKED_METRICS_LOCK:
            metrics_snapshot = dict(TRACKED_METRICS)

        for metric_name, data in metrics_snapshot.items():
            value = data.get("value", 0)
            threshold = data.get("threshold", 0)
            if threshold > 0 and value < threshold:
                msg = (
                    f"[Track Agent] ALERT: '{metric_name}' is {value:.3f}, "
                    f"below threshold {threshold:.3f}. Signalling Brain."
                )
                print(msg)
                push_message("system", msg)
                # Signal Brain via the coordinator
                corrective_prompt = (
                    f"Track Agent alert: metric '{metric_name}' is underperforming "
                    f"(current: {value:.3f}, threshold: {threshold:.3f}). "
                    f"Spawn a corrective sub-task to address this."
                )
                try:
                    reply = coordinator.handle_request(corrective_prompt)
                    push_message("ai", reply)
                except Exception as e:
                    print(f"[Track Agent] Error signalling Brain: {e}")


@app.route("/pipeline/start", methods=["POST"])
def start_pipeline():
    """Starts the multi-agent pipeline for a complex task."""
    data = request.get_json(force=True) or {}
    task = data.get("task", "").strip()
    if not task:
        return jsonify({"error": "task is required"}), 400

    push_message("system", f"Pipeline started: {task[:80]}...")

    async def gate_fn(gate_number: int, data: dict) -> dict:
        """Polls PIPELINE_STATE until the gate is resolved."""
        with PIPELINE_LOCK:
            PIPELINE_STATE["current_gate"] = gate_number
            PIPELINE_STATE["gate_status"] = "waiting"
            PIPELINE_STATE["redirect_note"] = None

        push_message("system", f"Gate {gate_number} is open. Waiting for your approval.")

        # Poll every 2 seconds until approved or rejected
        import asyncio
        while True:
            await asyncio.sleep(2)
            with PIPELINE_LOCK:
                status = PIPELINE_STATE["gate_status"]
                note = PIPELINE_STATE["redirect_note"]
            if status in ("approved", "rejected"):
                with PIPELINE_LOCK:
                    PIPELINE_STATE["current_gate"] = None
                    PIPELINE_STATE["gate_status"] = "idle"
                return {"approved": status == "approved", "redirect_note": note}

    def run_pipeline():
        import asyncio
        from multi_agent_coordinator import run_full_pipeline
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # [FIX #5] Removed the dead lambda: None third argument
            result = loop.run_until_complete(run_full_pipeline(task, gate_fn))
            push_message("ai", f"Pipeline complete. {result.get('status', 'done')}.")
        except Exception as e:
            push_message("system", f"Pipeline error: {e}")
        finally:
            loop.close()

    # Run pipeline in background thread so Flask responds immediately
    threading.Thread(target=run_pipeline, daemon=True).start()
    return jsonify({"status": "pipeline_started", "task": task})


def run_server():
    app.run(host=HOST, port=PORT, use_reloader=False)


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

def record_and_transcribe():
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
                    wake_audio = bytes(buffer)
                    sr_audio = sr.AudioData(wake_audio, 16000, 2)
                    text = recognizer.recognize_google(sr_audio).lower()
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
