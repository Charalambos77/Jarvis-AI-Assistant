# ⚡ Jarvis: AI-Powered Second Brain & Autonomous Multi-Agent Voice Assistant

**Jarvis** is a local-first, voice-controlled personal AI assistant, multi-agent engine, and workspace manager built directly into your desktop environment. Combining a **3D WebGL Constellation Map**, real-time SQLite storage, local and cloud LLM providers (Google Gemini & Ollama), and an autonomous multi-agent sprint pipeline, Jarvis serves as an active collaborator with full command over your personal productivity interface.

---

## 🌟 Key Architectural Features

### 1. First-Class Identity & Autonomous Command Bus
- **Authenticated Command Bus**: Jarvis communicates with the frontend via authenticated endpoints (`POST /jarvis/command`) using a dedicated `JARVIS_SESSION_TOKEN`.
- **Teal Identity UI**: Autonomous actions and thoughts from Jarvis are displayed with a distinct teal glow and `⚡ JARVIS:` prefix in the chat logs.
- **Bi-Directional Acknowledgement Loop (ACK)**: When Jarvis triggers UI actions (e.g. focusing a 3D node, opening notes, navigating pages), the frontend responds via `POST /jarvis/ack` to confirm completion.

### 2. Live UI Snapshot Feed ("Jarvis Eyes")
- **Contextual Grounding**: Queries `GET /jarvis/snapshot` to view real-time state: current open page, side panels, active task drawer, and orb states.
- **Smart Execution**: Prevents duplicate UI operations and contextual missteps.

### 3. Interactive 3D WebGL Constellation Map
- **Real-Time Nebula Rendering**: Rendered with Three.js WebGL, tasks, subtasks, and notes float as interactive glowing nodes in space.
- **Dynamic Physics & Vector Animations**:
  - **Task Creation**: Particle assembly stream and priority-coded laser connections.
  - **Subtask Decomposition**: Parent node overcharge pulse branching out to child nodes.
  - **Task Deletion**: Supernova flare collapse with dispersing particle burst.
  - **Task Completion**: Z-axis camera warp effect.

### 4. Autonomous Multi-Agent Sprint Pipeline
- **Multi-Agent Orchestration**: Handles complex goals via `agents/` (Brain, Research, Synthesis, Execution, Quality Checker, Deployment).
- **Human-In-The-Loop Gate Approval**: Pauses at critical milestones (Blueprint, Execution, Final QA) allowing human review, per-step approvals, or redirect instructions via interactive modals on `execution.html` and `plan.html`.
- **Live Task Logs**: Automatically creates and updates project task logs under `Let Jarvis Handle It/<Project_Name>/Task Logs/pipeline_<id>.md`.

### 5. Telemetry Metric Tracking & Self-Healing Agent Loop
- **Metric Monitoring**: Tracks KPIs (`POST /metrics/update`).
- **Autonomous Remediation**: A background monitoring loop inspects metrics every 5 minutes:
  - **Above Threshold**: Extracts winning execution patterns to the SQLite memory store (`db.save_memory_pattern`).
  - **Below Threshold**: Spawns an automated corrective pipeline sub-task to fix underperforming metrics.

### 6. Flexible Model & Tool Ecosystem (Gemini + Ollama + MCP)
- **Dual LLM Engines**: Seamlessly switch between Google Gemini (`gemini-2.5-flash`) and local Ollama models (`qwen2.5:3b`).
- **External Connectors & MCP**: Integrated REST API connector (`connectors/api_connector.py`) and Model Context Protocol support (`mcp_server.py`, `connectors/mcp_connector.py`).

---

## 📁 Repository Structure

```
second-brain-voice/
├── jarvis.py                   # Main entry point (Flask server, PyWebView UI, voice listener & loops)
├── coordinator.py              # LLM routing, tool execution engine, system prompts
├── multi_agent_coordinator.py  # Multi-agent sprint pipeline manager
├── db.py                       # SQLite database manager (tasks, notes, pipelines, memory)
├── voice_assistant.py          # Standalone voice helper utilities
├── mcp_server.py               # Model Context Protocol server implementation
├── settings.json               # Persisted user settings (theme, voice speed, wake threshold)
├── api_registry.json           # External API service configuration registry
├── mcp_registry.json           # MCP tool registry configuration
├── requirements.txt            # Python dependencies list
├── .env                        # Environment credentials & API keys
│
├── agents/                     # Multi-Agent Sub-Agents
│   ├── brain.py                # Central planner & task breakdown
│   ├── research_agent.py       # Information gathering & search agent
│   ├── synthesis.py            # Conflict resolution & master blueprint generator
│   ├── execution_agent.py      # Blueprint task execution agent
│   ├── quality_checker.py      # Quality assurance & verification agent
│   └── deployment_agent.py     # Artifact deployment agent
│
├── connectors/                 # API & MCP Connectors
│   ├── api_connector.py        # REST API integration & credentials saver
│   └── mcp_connector.py        # MCP protocol client connector
│
├── scripts/                    # Utilities & Helper Scripts
│   └── record_samples.py       # Audio calibration & mic sample recorder
│
├── HTML UI Views/              # Desktop Web Interfaces
│   ├── command_center.html     # Main 3D WebGL Constellation Map & Command Center
│   ├── execution.html          # Multi-Agent execution stream & Gate Approval view
│   ├── plan.html               # Sprint & pipeline plan management dashboard
│   ├── provider_comparison.html# Provider selection (Gemini/Ollama) & API/MCP registry
│   └── agent_talk.task_log.html# Inter-agent chat & console log viewer
│
├── Batch & Shell Scripts/
│   ├── install.bat             # Automated environment installer (venv & pip)
│   ├── install_ollama.bat      # Silent Ollama installer & model pull script
│   ├── run_jarvis.bat          # One-click launcher script
│   └── start_jarvis_silent.vbs # Hidden/background Windows startup script
│
└── Let Jarvis Handle It/       # Output folder for generated project deliverables & logs
```

---

## 🛠️ Requirements & Installation (Windows)

### Prerequisites
- **Python**: Python 3.10 – 3.13 added to system `PATH`.
- **Microphone**: Standard input device for voice recognition.
- **Gemini API Key**: Free key from [Google AI Studio](https://aistudio.google.com/apikey).

### 1. Automated Installation
Run the included setup script:
```cmd
install.bat
```
*This creates a Python virtual environment (`venv`), upgrades `pip`, and installs all dependencies from `requirements.txt`.*

### 2. (Optional) Local Ollama Installation
To run fully offline with local models:
```cmd
install_ollama.bat
```
*This downloads Ollama silently and pulls the lightweight `qwen2.5:3b` model.*

### 3. Environment Configuration
Create or edit `.env` in the root folder:
```env
GEMINI_API_KEY=your_gemini_api_key_here
JARVIS_PORT=5000
JARVIS_HOST=0.0.0.0
MODEL_PROVIDER=gemini  # Options: 'gemini' or 'ollama'
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_URL=http://localhost:11434/api/generate
ELEVENLABS_API_KEY=optional_elevenlabs_key
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
```

---

## 🏃 Running Jarvis

### Launching the Application
- **Standard Mode**: Double-click `run_jarvis.bat` or run:
  ```cmd
  python jarvis.py
  ```
- **Silent Background Mode**: Double-click `start_jarvis_silent.vbs`.

Once started, Jarvis will calibrate the microphone. Say **"Hey Jarvis"** or **"Jarvis"** to wake him!

---

## 🎙️ Voice & Control Commands

| Voice Command | Action / Effect |
| :--- | :--- |
| **"Hey Jarvis"** / **"Jarvis"** | Activates the assistant voice listener |
| **"Add tasks: [Task 1], [Task 2]"** | Batch creates new tasks in 3D constellation |
| **"Focus on task [ID]"** | Zooms 3D WebGL camera onto task node |
| **"Open execution"** / **"Go to execution"** | Navigates UI to `execution.html` agent stream |
| **"Open plan"** / **"Go to plan page"** | Navigates UI to `plan.html` pipeline view |
| **"Open brain"** / **"Command center"** | Navigates UI to 3D `command_center.html` |
| **"Track metric [Name] at [Value] threshold [Val]"** | Registers metric threshold monitoring |
| **"Change voice speed to 200"** | Adjusts TTS speaking rate and persists to `settings.json` |
| **"Jarvis end the conversation"** | Puts Jarvis back to sleep & hides window |
| **"Jarvis end the conversation without exiting"** | Puts Jarvis to sleep while keeping window open |
| **"Jarvis exit completely"** | Terminates the application process completely |

---

## 🧪 Verification & Tooling

### Record Audio Calibration Samples
```cmd
python scripts/record_samples.py
```

### Inspect Database & Logs
- Database: `second_brain.db` (SQLite)
- Project Logs: `Let Jarvis Handle It/<Project_Name>/Task Logs/pipeline_<id>.md`
- Console Logs: Exposed via `GET /api/console_logs` and `GET /api/agent_chat`

