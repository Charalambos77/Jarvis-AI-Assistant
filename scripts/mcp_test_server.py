"""A minimal real MCP server, used to verify connectors/mcp_client.py.

Speaks the actual protocol over stdio via the official SDK, so a successful
call through it proves the client's handshake, discovery and invocation path
work — not just that our own code agrees with itself.

Run indirectly: the client spawns it per mcp_registry.json.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("jarvis-test")


@mcp.tool()
def echo(message: str) -> str:
    """Echo a message back, prefixed, to prove a real round trip."""
    return f"echoed: {message}"


@mcp.tool()
def add(a: float, b: float) -> str:
    """Add two numbers and return the sum."""
    return str(a + b)


if __name__ == "__main__":
    mcp.run(transport="stdio")
