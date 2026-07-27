# Jarvis MCP Server Implementation Plan (Cursor & Claude Integration)

This plan outlines how to expose Jarvis's tools and database to **Claude Desktop** and **Cursor** using the **Model Context Protocol (MCP)**. This allows you to use Claude or Cursor as the host interface for Jarvis, enabling advanced agent workflows and direct interaction with your tasks and notes.

## User Review Required

> [!IMPORTANT]
> **MCP Stdio Server Architecture**
> We will implement Jarvis as an MCP stdio server. This runs as a background process started by Claude Desktop or Cursor. It communicates over standard input/output (stdin/stdout) using JSON-RPC.

## Open Questions

> [!IMPORTANT]
> **1. Python Dependencies**
> We need to install the Anthropic `mcp` Python SDK in your virtual environment to implement the MCP server cleanly. Is it okay to run `venv\Scripts\pip install mcp`?
>
> **2. Environment Variables & DB Location**
> The database is located at `d:/Charalambos/Desktop/AI/second-brain-voice/second_brain.db`. We will configure the MCP server to use this absolute path so that it shares data seamlessly with the existing speech/web interfaces. Do you have any preferences on database sharing?

---

## Proposed Changes

### Core Integration

#### [NEW] [mcp_server.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/mcp_server.py)
A new script that initializes an MCP server using the Python `mcp` library.
- Defines and registers tools:
  - `get_tasks`
  - `add_task`
  - `complete_task`
  - `delete_task`
  - `add_note`
  - `search_notes`
  - `update_note`
  - `complete_note`
  - `delete_note`
- Connects directly to the existing `second_brain.db` database using functions imported from [db.py](file:///d:/Charalambos/Desktop/AI/second-brain-voice/db.py).
- Listens for JSON-RPC commands on `stdin` and writes results to `stdout`.

#### [MODIFY] [requirements.txt](file:///d:/Charalambos/Desktop/AI/second-brain-voice/requirements.txt)
Add `mcp` to requirements for complete local dependency tracking.

---

## Configuration & Run Guide

To run Jarvis on either Claude Desktop or Cursor:

### 1. Claude Desktop Setup
Modify your Claude Desktop configuration file (typically at `%APPDATA%\Claude\claude_desktop_config.json`) to add the Jarvis MCP server:

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "d:/Charalambos/Desktop/AI/second-brain-voice/venv/Scripts/python.exe",
      "args": [
        "d:/Charalambos/Desktop/AI/second-brain-voice/mcp_server.py"
      ]
    }
  }
}
```

### 2. Cursor Setup
1. Open Cursor and go to **Settings** -> **Features** -> **MCP**.
2. Click **+ Add New MCP Server**.
3. Fill in the following details:
   - **Name**: `Jarvis`
   - **Type**: `stdio`
   - **Command**: `d:/Charalambos/Desktop/AI/second-brain-voice/venv/Scripts/python.exe d:/Charalambos/Desktop/AI/second-brain-voice/mcp_server.py`
4. Save and ensure the status shows green (Connected).

---

## Verification Plan

### Manual Verification
1. Install `mcp` package in virtual environment.
2. Build the `mcp_server.py` script.
3. Test locally in command line using stdin/stdout simulator or python script.
4. Configure in Claude Desktop and verify that the "Jarvis tools" appear in the Claude chat UI.
5. Configure in Cursor and verify that the composer/chat can invoke the Jarvis tools (e.g. "Add a task: write code", "Show my tasks").
