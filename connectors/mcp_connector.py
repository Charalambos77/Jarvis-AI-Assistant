import sqlite3


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
