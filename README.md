# Second Brain Voice Assistant

A background voice assistant for your second brain. Say **"Hey Jarvis"**, then
speak a request like "add a task: call the dentist tomorrow" or "what's on my
list?" - it transcribes your speech, sends it to Gemini (free API), Gemini
calls the right tool against your local `second_brain.db`, and the reply is
spoken back to you.

This runs completely independently of Claude Desktop. It's a separate script
using the free Gemini API instead.

## Setup (Windows)

**1. Get a free Gemini API key**
Go to https://aistudio.google.com/apikey, sign in, click "Create API key".

**2. Extract this folder** on your PC.

**3. Run the automated installer**
Double-click `install.bat` inside the folder. This will automatically check for Python, set up the virtual environment, and install all required dependencies.

*(Alternatively, you can manually open PowerShell, run `python -m venv venv`, activate it via `venv\Scripts\activate`, and run `pip install -r requirements.txt`.)*

> **Note on PyAudio:** this is the package most likely to give trouble on
> Windows. If `pip install -r requirements.txt` fails specifically on
> `pyaudio`, run this instead:
> ```
> pip install pipwin
> pipwin install pyaudio
> ```

**6. Set your API key**
Copy `.env.example` to a new file named `.env` in the same folder, then open
`.env` and replace `your_key_here` with your actual Gemini API key. This file
stays on your PC only - it's never sent anywhere except directly to Google's
API when making requests.

**7. Run it**
```
python voice_assistant.py
```

First run downloads the wake-word model (small, one-time). Once you see
`Second Brain voice assistant running.`, say **"Hey Jarvis"** out loud, wait
for "Wake word detected!" in the console, then speak your request.

## Notes

- This creates its **own** `second_brain.db` in this folder - separate from
  any database used by the Claude Desktop MCP version. Copy the file over
  manually if you want them to share data.
- Speech-to-text uses Google's free web speech API (via the
  `SpeechRecognition` library) - this sends short audio clips to Google's
  servers to transcribe, same as most other free options.
- If "Hey Jarvis" triggers too often by accident, raise
  `WAKE_WORD_THRESHOLD` in `voice_assistant.py` (default `0.5`, try `0.6-0.7`).
- To run this automatically in the background on startup, you can later set
  it up as a Windows Scheduled Task - ask if you want help with that.
