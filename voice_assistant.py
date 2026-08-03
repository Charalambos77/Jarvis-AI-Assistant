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
from google import genai
from google.genai import types
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

client = genai.Client(api_key=GEMINI_API_KEY)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "second_brain.db")

WAKE_WORD_THRESHOLD = 0.5   # lower = more sensitive (more false triggers)
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280           # openWakeWord expects 80ms chunks at 16kHz

# Global speech recognizer configuration
recognizer = sr.Recognizer()
recognizer.energy_threshold = 300
recognizer.dynamic_energy_threshold = True
recognizer.dynamic_energy_adjustment_damping = 0.15
recognizer.dynamic_energy_ratio = 1.5
recognizer.pause_threshold = 0.8

def speak(text: str):
    print(f"[Jarvis] {text}")
    
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
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception as e:
            print(f"[Local TTS Error] {e}")


# ---------------------------------------------------------------------------
# Gemini tool definitions - mirrors db.py functions
# ---------------------------------------------------------------------------

import coordinator

def handle_request(transcript: str) -> str:
    """Send transcript to Gemini, execute any tool calls, return final spoken text."""
    return coordinator.handle_request(transcript)


# ---------------------------------------------------------------------------
# Speech-to-text (records a few seconds after wake word, transcribes)
# ---------------------------------------------------------------------------

def record_and_transcribe() -> str | None:
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
# Wake word listener loop
# ---------------------------------------------------------------------------

def main():
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

    print("Second Brain voice assistant running. Say 'Hey Jarvis' to activate. Ctrl+C to quit.")

    try:
        while True:
            audio_chunk = np.frombuffer(stream.read(CHUNK_SIZE, exception_on_overflow=False), dtype=np.int16)
            prediction = oww_model.predict(audio_chunk)

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