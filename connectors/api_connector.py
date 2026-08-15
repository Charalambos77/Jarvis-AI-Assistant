"""
API Provider Configuration Registry.
Maps service names to their API keys, endpoints, status, and fallback chains.
Users plug in APIs/MCPs via the API/MCP Plugging Gate before execution.
"""
import os
import json
import sqlite3
from dotenv import load_dotenv

load_dotenv()

# Registry: service_name -> config
API_REGISTRY: dict[str, dict] = {}
REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_registry.json")


def load_registry() -> dict:
    """Load registry from disk or return defaults."""
    global API_REGISTRY
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r") as f:
                API_REGISTRY = json.load(f)
        except Exception:
            API_REGISTRY = {}
    return API_REGISTRY


def save_registry():
    """Persist registry to disk."""
    with open(REGISTRY_PATH, "w") as f:
        json.dump(API_REGISTRY, f, indent=2)


def get_service_config(service_name: str) -> dict | None:
    """Get config for a specific service."""
    load_registry()
    return API_REGISTRY.get(service_name)


def get_service_status(service_name: str) -> str:
    """Returns 'up', 'down', or 'rate_limited'."""
    load_registry()
    config = API_REGISTRY.get(service_name, {})
    return config.get("status", "unknown")


def mark_service_status(service_name: str, status: str):
    """Update a service's status (called when API errors occur)."""
    load_registry()
    if service_name in API_REGISTRY:
        API_REGISTRY[service_name]["status"] = status
        save_registry()


def get_fallback_for(service_name: str) -> str | None:
    """Return the fallback service name, if configured."""
    load_registry()
    config = API_REGISTRY.get(service_name, {})
    return config.get("fallback")


def register_service(service_name: str, config: dict):
    """Register or update a service in the registry (called during plugging gate)."""
    load_registry()
    API_REGISTRY[service_name] = config
    save_registry()


def get_required_services(master_blueprint: dict) -> list[dict]:
    """
    Analyzes the master blueprint to determine which APIs/MCPs are needed.
    Returns a list of {"service": str, "purpose": str, "status": str}.
    """
    required = []
    # Extract tools_needed from execution agents in the blueprint
    for agent in master_blueprint.get("execution_agents", []):
        for tool in agent.get("tools_needed", []):
            status = get_service_status(tool)
            required.append({
                "service": tool,
                "purpose": f"Used by {agent.get('role', 'agent')}",
                "status": status,
                "configured": status != "unknown",
            })
    return required


def get_tools_for_agent(tools_needed: list[str]) -> list[dict]:
    """
    Dynamically construct a Gemini tools list based on the agent's tools_needed config.
    This replaces hardcoded tools_list in research_agent.py.
    """
    tools = []
    for tool_name in tools_needed:
        if tool_name == "google_search":
            tools.append({"google_search": {}})
        # Future: map tool_name to API connector calls
    return tools or [{"google_search": {}}]


def call_external_api(
    conn: sqlite3.Connection,
    service_name: str,
    endpoint: str,
    method: str = "GET",
    params: dict | None = None,
    body: dict | None = None,
    headers: dict | None = None,
) -> dict:
    """
    Placeholder for future external API connector integration.

    This function is intentionally left as a no-op implementation so Jarvis can
    later be extended to call third-party APIs without changing the core
    coordinator logic.
    """
    return {
        "status": "not_configured",
        "service_name": service_name,
        "endpoint": endpoint,
        "method": method,
        "params": params,
        "body": body,
        "headers": headers,
        "message": "External API connector is not configured yet.",
    }
