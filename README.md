# Jarvis: AI-Powered Second Brain & Voice Assistant

An advanced, local-first personal AI assistant and cognitive engine built directly into your desktop workspace. By combining a 3D WebGL Constellation Map, an active local SQLite database, and voice/chat commands processed via Google Gemini or Ollama, Jarvis acts as a first-class app user with complete control over your productivity interface.

---

## 🚀 Key Architectural Features

### 1. First-Class Identity & Autonomous Commands
Jarvis is not just a passive listener; he is a session-level authenticated app user.
- **Dedicated Communications**: Features a secure command bus (`POST /jarvis/command`) authenticated via a custom `JARVIS_SESSION_TOKEN`.
- **Teal Identity UI**: Autonomous actions and thoughts from Jarvis are displayed in the chat log with a distinct teal glow and a `⚡ JARVIS:` prefix.

### 2. Live UI Snapshot Feed (Jarvis "Eyes")
Through the `read_app_snapshot` tool, Jarvis obtains real-time context of your desktop UI before deciding on any actions.
- **Visual Grounding**: Queries `GET /jarvis/snapshot` to discover which page, side panel, task drawer, and orb states are currently visible.
- **Contextual Execution**: Avoids redundant navigation commands (e.g. will not try to open settings if the snapshot indicates settings are already open).

### 3. Command Confirmation Bus (ACK Loop)
A bidirectional acknowledgement loop guarantees action delivery.
- When Jarvis triggers a UI action (such as focusing a 3D task node or opening notes), the frontend immediately posts back a `POST /jarvis/ack`.
- Jarvis monitors this queue to confirm success or pivot to a retry strategy if the action was blocked.

### 4. Interactive 3D WebGL Constellation Map
Your tasks, subtasks, and notes are rendered as a glowing 3D particle nebula in real-time.
- **Dynamic Animations**: Changes to tasks/notes trigger vector-driven WebGL animations:
  - *Task Creation*: Particle assembly stream and growing priority-colored laser lines.
  - *Subtask Decomposition*: Parent node overcharge pulse branching out children.
  - *Deletion*: A supernova flare collapse leading to a dispersing particle explosion.
  - *Completion*: Rapid Z-axis camera screen-suck warp.

### 5. Multi-Agent Pipeline & Metric Monitoring
- **Automated Pipelines**: Launch autonomous multi-agent sprint pipelines (`start_pipeline`) containing research, synthesis, and execution phases, complete with human-in-the-loop gate approvals.
- **Telemetry Alerts**: Instruct Jarvis to track KPI metrics (`update_metric`). A background thread monitors these values every 5 minutes and commands the coordinator to spawn corrective tasks if thresholds are breached.

---

## 🛠️ Setup & Installation (Windows)

### 1. Get a Free Gemini API Key
Go to [Google AI Studio](https://aistudio.google.com/apikey) and create a free API key.

### 2. Configure Environment
Copy `.env.example` to `.env` and fill in your details:
```env
GEMINI_API_KEY=your_gemini_key_here
JARVIS_PORT=5000
MODEL_PROVIDER=ollama  # or 'gemini'
```

### 3. Install Dependencies
Double-click `install.bat` inside the folder. This sets up a virtual environment (`venv`) and installs the required speech-to-text, text-to-speech, openwakeword, and rendering libraries.

---

## 🏃 Running Jarvis

Launch the assistant by running:
```bash
python jarvis.py
```
Or double-click the `run_jarvis.bat` shortcut.
Once you see `Jarvis is running`, say **"Hey Jarvis"** or **"Jar"** to wake him, and tell him what you need!

---

## 🎙️ Sample Voice Commands
- *"Hey Jarvis, add three tasks: write unit tests, complete implementation plan, buy milk"* (Batch Creation)
- *"focus on task 3"* (3D Zoom & Detail Drawer)
- *"change voice speed to 200"* (Settings change with verification confirmation prompt)
- *"track youtube CTR at 0.045 with threshold 0.05"* (Metric Tracking)
- *"switch to gem"* (Prompts confirmation to swap model provider from Ollama to Gemini)
