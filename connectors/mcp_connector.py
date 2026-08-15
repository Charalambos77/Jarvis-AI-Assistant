import os
import json
import sqlite3

# Registry: mcp_name -> config
MCP_REGISTRY: dict[str, dict] = {}
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mcp_registry.json")


def load_mcp_registry() -> dict:
    """Load registry from disk or return defaults."""
    global MCP_REGISTRY
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r") as f:
                MCP_REGISTRY = json.load(f)
        except Exception:
            MCP_REGISTRY = {}
    else:
        # Default fallback
        MCP_REGISTRY = {
            "fetch": {"status": "up", "description": "Fetches web page content"},
            "sqlite": {"status": "up", "description": "SQLite DB operations"}
        }
    return MCP_REGISTRY


def save_mcp_registry():
    """Persist registry to disk."""
    with open(REGISTRY_PATH, "w") as f:
        json.dump(MCP_REGISTRY, f, indent=2)


def get_mcp_config(mcp_name: str) -> dict | None:
    """Get config for a specific MCP server."""
    load_mcp_registry()
    return MCP_REGISTRY.get(mcp_name)


def list_available_mcps() -> list[dict]:
    """Returns a list of all configured MCP servers."""
    load_mcp_registry()
    return [{"name": name, **config} for name, config in MCP_REGISTRY.items()]


def call_mcp(
    conn: sqlite3.Connection,
    action: str,
    payload: dict | None = None,
) -> dict:
    """
    Placeholder for future MCP or Claude Desktop connector integration.

    This function exists so the coordinator can route MCP-style requests
    without needing the connector implemented immediately.
    """
    return {
        "status": "not_configured",
        "action": action,
        "payload": payload,
        "message": "MCP connector is not configured yet.",
    }
