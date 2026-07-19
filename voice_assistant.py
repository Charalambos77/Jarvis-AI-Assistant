"""
Second Brain Voice Assistant
-----------------------------
Runs in the background, listens for the wake word "Hey Jarvis", then:
  1. Records what you say
  2. Transcribes it to text
  3. Sends it to Gemini, which decides whether to call a second-brain
     tool (add_task, get_tasks, complete_task, add_note, search_notes)
  4. Executes that tool directly against your local second_brain.db
  5. Speaks Gemini's response back to you

This does NOT go through Claude Desktop or MCP at all - it's a fully
standalone script using the free Gemini API.
"""

import os
import sys
import json
import time
import wave
import tempfile

import numpy as np
import pyaudio
import openwakeword
from openwakeword.model import Model as WakeModel
import speech_recognition as sr
import pyttsx3
import google.generativeai as genai
from dotenv import load_dotenv

import db

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("ERROR: GEMINI_API_KEY not found. Copy .env.example to .env and fill it in.")
    sys.exit(1)

genai.configure(api_key=GEMINI_API_KEY)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "second_brain.db")

WAKE_WORD_THRESHOLD = 0.5   # lower = more sensitive (more false triggers)
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280           # openWakeWord expects 80ms chunks at 16kHz

def speak(text: str):
    print(f"[Jarvis] {text}")
    # pyttsx3 on Windows can silently stop working after the first call if the
    # engine instance is reused, so a fresh engine is created for each utterance.
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()
    engine.stop()


# ---------------------------------------------------------------------------
# Gemini tool definitions - mirrors db.py functions
# ---------------------------------------------------------------------------

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
            "properties": {
                "status": {"type": "string", "enum": ["open", "in_progress", "done"]},
            },
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
]

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
    "focus_tasks": lambda conn, **kw: {"status": "focused", "task_ids": kw.get("task_ids", [])},
}

from google import genai
from google.genai import types

client = genai.Client(api_key=GEMINI_API_KEY)

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
        "so the UI can highlight them and let the user narrow it down. Keep replies short "
        "and conversational since they'll be read aloud."
    ),
    tools=[
        {"function_declarations": TOOLS},
        {"google_search": {}}
    ],
    tool_config=types.ToolConfig(
        include_server_side_tool_invocations=True
    ),
    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
)

CHAT_SESSION = None

def get_chat_session():
    global CHAT_SESSION
    if CHAT_SESSION is None:
        CHAT_SESSION = client.chats.create(model="gemini-3.5-flash", config=config)
    return CHAT_SESSION


def handle_request(transcript: str) -> str:
    """Send transcript to Gemini, execute any tool calls, return final spoken text."""
    conn = db.get_connection(DB_PATH)
    try:
        chat = get_chat_session()
        response = chat.send_message(transcript)

        # Gemini may request one or more tool calls before giving a final answer
        for _ in range(5):  # safety cap on back-and-forth turns
            if not response.function_calls:
                break

            response_parts = []
            for call in response.function_calls:
                fname = call.name
                fargs = call.args
                print(f"[tool call] {fname}({fargs})")

                try:
                    result = TOOL_IMPL[fname](conn, **fargs)
                except Exception as e:
                    result = {"error": str(e)}

                response_parts.append(
                    types.Part.from_function_response(
                        name=fname,
                        response={"result": json.dumps(result, default=str)},
                    )
                )

            response = chat.send_message(response_parts)

        return response.text
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Speech-to-text (records a few seconds after wake word, transcribes)
# ---------------------------------------------------------------------------

def record_and_transcribe() -> str | None:
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
# Wake word listener loop
# ---------------------------------------------------------------------------

def main():
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

    print("Second Brain voice assistant running. Say 'Hey Jarvis' to activate. Ctrl+C to quit.")

    try:
        while True:
            audio_chunk = np.frombuffer(stream.read(CHUNK_SIZE, exception_on_overflow=False), dtype=np.int16)
            prediction = oww_model.predict(audio_chunk)

            triggered = any(score > WAKE_WORD_THRESHOLD for score in prediction.values())
            if triggered:
                print("\nWake word detected!")
                oww_model.reset()  # avoid immediate re-trigger
            triggered = any(score > WAKE_WORD_THRESHOLD for score in prediction.values())
            if triggered:
                print("\nWake word detected!")
                oww_model.reset()  # avoid immediate re-trigger
                speak("Hello Harry, how can I help you?")

                # Conversation mode: once woken up, keep listening for
                # follow-up requests directly - no need to repeat "Hey Jarvis"
                # each time - until the user says the end phrase below.
                in_conversation = True
                while in_conversation:
                    transcript = record_and_transcribe()
                    if not transcript:
                        speak("Sorry, I didn't catch that.")
                        continue

                    transcript_lower = transcript.lower()
                    if "jarvis exit completely" in transcript_lower or "exit completely" in transcript_lower:
                        speak("Shutting down completely. Goodbye.")
                        import os
                        os._exit(0)

                    stop_words = [
                        "end the conversation", "end conversation", "goodbye",
                        "go to sleep", "exit", "quit", "stop listening"
                    ]
                    should_stop = any(w in transcript_lower for w in stop_words)
                    if should_stop:
                        speak("Okay, going back to sleep.")
                        in_conversation = False
                        break

                    try:
                        reply = handle_request(transcript)
                        speak(reply)
                    except Exception as e:
                        print(f"Error handling request: {e}")
                        speak("Something went wrong, sorry.")
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    main()