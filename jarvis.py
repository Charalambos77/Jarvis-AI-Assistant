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


def push_message(role, text):
    with STATE_LOCK:
        CONVO.append({"role": role, "text": text})


def set_orb(state):
    with STATE_LOCK:
        global ORB_STATE
        ORB_STATE = state


def speak(text: str):
    print(f"[Jarvis] {text}")
    set_orb("speaking")
    
    eleven_key = os.getenv("ELEVENLABS_API_KEY")
    eleven_voice = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    
    played_via_eleven = False
    if eleven_key:
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
                ctypes.windll.winmm.mciSendStringW('play jarvis_voice wait', None, 0, 0)
                ctypes.windll.winmm.mciSendStringW('close jarvis_voice', None, 0, 0)
                
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                played_via_eleven = True
            else:
                print(f"[ElevenLabs Error] {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[ElevenLabs Exception] {e}")

    if not played_via_eleven:
        try:
            engine = pyttsx3.init()
            with SETTINGS_LOCK:
                speed = SETTINGS.get("voice_speed", 175)
            engine.setProperty('rate', int(speed))
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            print(f"[Local TTS Error] {e}")
            
    set_orb("idle")


import coordinator

def jarvis_tool_listener(name, args, result):
    if name == "focus_tasks":
        global FOCUS_TASK_IDS
        with STATE_LOCK:
            task_ids = result.get("task_ids", [])
            if isinstance(task_ids, (int, float)):
                FOCUS_TASK_IDS = [int(task_ids)]
            elif isinstance(task_ids, list):
                FOCUS_TASK_IDS = [int(x) for x in task_ids]
            else:
                FOCUS_TASK_IDS = []

coordinator.register_tool_listener(jarvis_tool_listener)

def handle_request(transcript: str) -> str:
    return coordinator.handle_request(transcript)


SAMPLE_RATE = 16000
CHUNK_SIZE = 1280

# ---------------------------------------------------------------------------
# Local-only web server (127.0.0.1 - not reachable from your network)
# ---------------------------------------------------------------------------

app = Flask(__name__)
import logging
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # quiet the request logs


@app.route("/command-center")
def command_center():
    return send_from_directory(BASE_DIR, "command_center.html")


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
        for k in ["theme", "voice_speed", "wake_word_threshold"]:
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


@app.route("/state", methods=["GET"])
def state():
    global FOCUS_TASK_IDS
    with STATE_LOCK:
        res = jsonify({
            "orb": ORB_STATE,
            "messages": CONVO,
            "focus_task_ids": FOCUS_TASK_IDS
        })
        # Clear focused task IDs after serving so it only triggers once in UI
        FOCUS_TASK_IDS = []
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


def run_server():
    app.run(host=HOST, port=PORT, use_reloader=False)


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

def record_and_transcribe():
    recognizer = sr.Recognizer()
    mic = sr.Microphone(sample_rate=SAMPLE_RATE)
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.3)
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

                # Determine whether they said "Hey Jarvis" or just "Jarvis"
                greeting = "Hello Sir, how can I help you?"
                try:
                    wake_audio = bytes(buffer)
                    recognizer = sr.Recognizer()
                    sr_audio = sr.AudioData(wake_audio, 16000, 2)
                    text = recognizer.recognize_google(sr_audio).lower()
                    print(f"[wake analysis] {text}")
                    if "hey" in text:
                        greeting = "Hello Sir, how can I help you?"
                    elif "jarvis" in text:
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
                        speak("Sorry, I didn't catch that.")
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

    # Create the app window hidden - it only appears when "Hey Jarvis" triggers.
    window = webview.create_window("Jarvis", URL, width=1100, height=720, hidden=True, fullscreen=True)

    mic_thread = threading.Thread(target=mic_loop, args=(window,), daemon=True)
    mic_thread.start()

    # pywebview needs to run on the main thread - this blocks here until
    # the window is closed.
    webview.start()
