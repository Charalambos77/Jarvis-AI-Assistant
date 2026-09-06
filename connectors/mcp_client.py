"""
Real MCP client — speaks the Model Context Protocol over stdio to server
processes, discovers the tools each one offers, and calls them for real.

Replaces the old placeholder that answered every request with
{"status": "not_configured"} while mcp_registry.json advertised servers as
"up". Nothing claims a capability here unless a server process actually
started, answered the initialize handshake, and listed the tool.

Threading model
---------------
Sessions live on ONE dedicated background thread with its own event loop.
Everything else in Jarvis calls in synchronously through
`_run(coro)`, which submits to that loop and blocks on the result.

This matters: agents already run inside an asyncio loop, and tool handlers
are plain sync functions called from within it. Driving an async session
directly from there would deadlock the agent's own loop. A separate loop
also lets a session stay alive across many calls instead of paying server
startup on every tool invocation.
"""
import asyncio
import json
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(BASE_DIR, "mcp_registry.json")

# A server gets this long to start and complete the initialize handshake.
STARTUP_TIMEOUT = 60.0
# And this long to answer a single tools/call.
CALL_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Background event loop
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _loop_lock:
        if _loop and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()

        def _run_forever():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run_forever, name="mcp-client-loop", daemon=True)
        thread.start()
        _loop, _loop_thread = loop, thread
        return loop


def _run(coro, timeout: float):
    """Run a coroutine on the MCP loop and block for its result."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError(f"MCP operation timed out after {timeout}s")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Servers ship disabled with a command spelled out, rather than being claimed
# as "up" with nothing behind them. Turn one on by setting "enabled": true.
DEFAULT_REGISTRY = {
    "fetch": {
        "enabled": False,
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "description": "Fetches a URL and returns its content as readable text.",
    },
    "filesystem": {
        "enabled": False,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        "description": "Read and write files under an allowed directory.",
    },
}


def load_mcp_registry() -> dict:
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[MCP] Could not read {REGISTRY_PATH}: {e}")
    return dict(DEFAULT_REGISTRY)


def save_mcp_registry(registry: dict) -> None:
    try:
        with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
    except Exception as e:
        print(f"[MCP] Could not write {REGISTRY_PATH}: {e}")


def enabled_servers() -> dict:
    """Servers the user has switched on AND that name a command to run."""
    out = {}
    for name, cfg in load_mcp_registry().items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("enabled") and cfg.get("command"):
            out[name] = cfg
    return out


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class _Server:
    """One live stdio server: its process, session, and discovered tools."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.tools: list[dict] = []
        self.error: str | None = None
        self._session = None
        self._stack = None
        self._ready = asyncio.Event()

    async def start(self) -> bool:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from contextlib import AsyncExitStack

        params = StdioServerParameters(
            command=self.config["command"],
            args=list(self.config.get("args") or []),
            env={**os.environ, **(self.config.get("env") or {})},
            cwd=self.config.get("cwd") or BASE_DIR,
        )
        self._stack = AsyncExitStack()
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
            self._session = session
            self.tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": (t.inputSchema or {"type": "object", "properties": {}}),
                }
                for t in listed.tools
            ]
            self.error = None
            print(f"[MCP] '{self.name}' started — {len(self.tools)} tool(s): {[t['name'] for t in self.tools]}")
            return True
        except Exception as e:
            self.error = str(e)
            print(f"[MCP] '{self.name}' failed to start: {e}")
            await self.stop()
            return False

    async def call(self, tool_name: str, arguments: dict):
        if not self._session:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        return await self._session.call_tool(tool_name, arguments or {})

    async def stop(self):
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass  # a server that already died has nothing left to close
        self._stack = None
        self._session = None


_servers: dict[str, _Server] = {}
_servers_lock = threading.Lock()


async def _get_or_start(name: str, config: dict) -> _Server | None:
    server = _servers.get(name)
    if server and server._session:
        return server
    server = _Server(name, config)
    _servers[name] = server
    ok = await server.start()
    return server if ok else None


def start_enabled_servers() -> dict:
    """Start every enabled server and report what each one actually offers.

    Returns {name: {"status": "up"|"down", "tools": [...], "error": ...}} —
    "up" only after a real handshake, never from a config file's say-so.
    """
    servers = enabled_servers()
    report: dict[str, dict] = {}

    async def _boot():
        out = {}
        for name, cfg in servers.items():
            try:
                server = await _get_or_start(name, cfg)
            except Exception as e:
                out[name] = {"status": "down", "tools": [], "error": str(e)}
                continue
            if server and server._session:
                out[name] = {"status": "up", "tools": server.tools, "error": None}
            else:
                out[name] = {
                    "status": "down",
                    "tools": [],
                    "error": (server.error if server else "could not start"),
                }
        return out

    if not servers:
        return report
    try:
        report = _run(_boot(), timeout=STARTUP_TIMEOUT * max(1, len(servers)))
    except Exception as e:
        print(f"[MCP] Startup failed: {e}")
    return report


def server_status(server_name: str) -> dict:
    """Report a server's state WITHOUT starting anything.

    The Connected panel polls every few seconds; if reporting status started
    servers, a misconfigured one would be respawned forever and a working one
    would be launched by merely looking at the page. Starting is an explicit
    act: toggling it on, or an agent/gate actually needing it.

    "up" = a live session right now. "enabled" = switched on but not started
    yet. "disabled" = off.
    """
    server = _servers.get(server_name)
    if server and server._session:
        return {"status": "up", "tools": server.tools, "error": None}
    cfg = load_mcp_registry().get(server_name) or {}
    if not cfg.get("enabled") or not cfg.get("command"):
        return {"status": "disabled", "tools": [], "error": None}
    # Enabled, not running. If a start was already tried and failed, say so.
    if server and server.error:
        return {"status": "down", "tools": [], "error": server.error}
    return {"status": "enabled", "tools": [], "error": None}


def ensure_server_running(server_name: str) -> dict:
    """Start one server if needed and report what it really offers.

    {"status": "up"|"down", "tools": [...], "error": ...} — "up" means the
    handshake succeeded and the tool list came back, nothing weaker.
    """
    server = _servers.get(server_name)
    if server and server._session:
        return {"status": "up", "tools": server.tools, "error": None}

    cfg = enabled_servers().get(server_name)
    if not cfg:
        return {"status": "down", "tools": [], "error": f"'{server_name}' is not enabled in mcp_registry.json"}
    try:
        server = _run(_get_or_start(server_name, cfg), timeout=STARTUP_TIMEOUT)
    except Exception as e:
        return {"status": "down", "tools": [], "error": str(e)}
    if server and server._session:
        return {"status": "up", "tools": server.tools, "error": None}
    return {"status": "down", "tools": [], "error": (server.error if server else "could not start")}


def list_live_tools() -> list[dict]:
    """Every tool from every currently-running server."""
    out = []
    with _servers_lock:
        for name, server in _servers.items():
            if not server._session:
                continue
            for tool in server.tools:
                out.append({**tool, "server": name})
    return out


def call_mcp_tool(server_name: str, tool_name: str, arguments: dict) -> dict:
    """Call one tool on one server. Honest about every failure."""
    server = _servers.get(server_name)
    if not server or not server._session:
        cfg = enabled_servers().get(server_name)
        if not cfg:
            return {"status": "error", "error": f"MCP server '{server_name}' is not enabled."}
        try:
            server = _run(_get_or_start(server_name, cfg), timeout=STARTUP_TIMEOUT)
        except Exception as e:
            return {"status": "error", "error": f"Could not start MCP server '{server_name}': {e}"}
        if not server or not server._session:
            return {"status": "error", "error": f"MCP server '{server_name}' did not start."}

    try:
        result = _run(server.call(tool_name, arguments), timeout=CALL_TIMEOUT)
    except Exception as e:
        return {"status": "error", "action": tool_name, "error": str(e)}

    # Flatten MCP content blocks into something an agent can read directly.
    text_parts, other = [], []
    for block in (getattr(result, "content", None) or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        else:
            other.append(btype or "unknown")

    if getattr(result, "isError", False):
        return {
            "status": "error",
            "action": tool_name,
            "server": server_name,
            "error": "\n".join(text_parts) or "the MCP server reported an error",
        }

    payload = {
        "status": "ok",
        "action": tool_name,
        "server": server_name,
        "content": "\n".join(text_parts),
    }
    structured = getattr(result, "structuredContent", None)
    if structured:
        payload["structured"] = structured
    if other:
        payload["non_text_blocks"] = other
    return payload


def shutdown_all() -> None:
    async def _stop_all():
        for server in list(_servers.values()):
            await server.stop()
        _servers.clear()

    try:
        _run(_stop_all(), timeout=30)
    except Exception:
        pass
