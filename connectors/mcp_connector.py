import os
import json
import sqlite3

# Registry: mcp_name -> config
MCP_REGISTRY: dict[str, dict] = {}
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp_registry.json")


def load_mcp_registry() -> dict:
    """Load the registry — one source of truth, shared with mcp_client.

    The old fallback here declared `fetch` and `sqlite` as "up" out of thin air,
    with no command to run and no client to run it. Nothing is reported up now
    unless a server process really started; see mcp_client.ensure_server_running.
    """
    global MCP_REGISTRY
    from .mcp_client import load_mcp_registry as _load
    MCP_REGISTRY = _load()
    return MCP_REGISTRY


def save_mcp_registry():
    """Persist registry to disk."""
    with open(REGISTRY_PATH, "w") as f:
        json.dump(MCP_REGISTRY, f, indent=2)


def get_mcp_config(mcp_name: str) -> dict | None:
    """Get config for a specific MCP server."""
    load_mcp_registry()
    return MCP_REGISTRY.get(mcp_name)


def list_available_mcps(probe: bool = False) -> list[dict]:
    """Every configured MCP server and its real state.

    probe=False (default) observes without starting anything — safe to poll.
    probe=True actually starts enabled servers to find out whether they work;
    use it where the answer has to be authoritative, like a gate deciding
    whether a pipeline can proceed.
    """
    from .mcp_client import ensure_server_running, server_status
    load_mcp_registry()
    out = []
    for name, config in MCP_REGISTRY.items():
        entry = {"name": name, **(config if isinstance(config, dict) else {})}
        info = ensure_server_running(name) if probe else server_status(name)
        entry["status"] = info["status"]
        entry["tools"] = [t["name"] if isinstance(t, dict) else t for t in info.get("tools", [])]
        entry["error"] = info.get("error")
        out.append(entry)
    return out


def call_mcp(
    conn: sqlite3.Connection,
    action: str,
    payload: dict | None = None,
) -> dict:
    """Call a tool on a running MCP server.

    `action` is either "server.tool" or just "tool", in which case the tool is
    looked up across every running server. Used to return a hardcoded
    "not_configured" for everything; it now goes through the real stdio client
    in connectors/mcp_client.py, and says honestly when a server isn't up.
    """
    from .mcp_client import call_mcp_tool, list_live_tools, start_enabled_servers

    payload = payload or {}
    if "." in action:
        server_name, tool_name = action.split(".", 1)
        return call_mcp_tool(server_name, tool_name, payload)

    live = list_live_tools()
    if not live:
        start_enabled_servers()
        live = list_live_tools()

    matches = [t for t in live if t["name"] == action]
    if not matches:
        available = sorted({f"{t['server']}.{t['name']}" for t in live})
        return {
            "status": "error",
            "action": action,
            "error": (
                f"No running MCP server offers a tool called '{action}'."
                + (f" Available: {', '.join(available)}" if available else
                   " No MCP servers are enabled in mcp_registry.json.")
            ),
        }
    if len(matches) > 1:
        owners = ", ".join(f"{t['server']}.{t['name']}" for t in matches)
        return {
            "status": "error",
            "action": action,
            "error": f"'{action}' is offered by several servers — name one: {owners}",
        }
    return call_mcp_tool(matches[0]["server"], action, payload)
