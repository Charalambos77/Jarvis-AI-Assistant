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
    if not service_name:
        return "unknown"
    config = API_REGISTRY.get(service_name)
    if not config:
        s_clean = service_name.lower().replace("-", "_").replace(" ", "_")
        config = API_REGISTRY.get(s_clean, {})
    return config.get("status", "unknown")


def mark_service_status(service_name: str, status: str):
    """Update a service's status (called when API errors occur)."""
    load_registry()
    if service_name in API_REGISTRY:
        API_REGISTRY[service_name]["status"] = status
        save_registry()


def get_all_configured_services() -> dict[str, dict]:
    """Returns all services in the global registry that are marked as 'up' or configured."""
    load_registry()
    return {svc: cfg for svc, cfg in API_REGISTRY.items() if cfg.get("status") == "up"}


def get_fallback_for(service_name: str) -> str | None:
    """Return the fallback service name, if configured."""
    load_registry()
    config = API_REGISTRY.get(service_name, {})
    return config.get("fallback")


ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")


def save_tool_credentials(service_name: str, credentials: dict, method_id: str | None = None) -> bool:
    """
    Saves tool credentials to .env file, sets os.environ live,
    and updates api_registry.json marking service status as 'up'.
    """
    if not service_name or not credentials:
        return False

    load_registry()

    # Normalize service name
    s_clean = service_name.lower().replace("-", "_").replace(" ", "_")

    # Map credential keys to environment variable names
    lines_to_append = []
    
    for key, value in credentials.items():
        if not value:
            continue
        # Format env key name based on key name or method_id
        if key in ("api_key", "key", "token", "value") and (not method_id or method_id == "api_key"):
            env_key = s_clean.upper() + "_API_KEY"
        else:
            env_key = f"{s_clean.upper()}_{key.upper()}"
            
        # Update live environment
        os.environ[env_key] = str(value)
        lines_to_append.append(f"{env_key}={value}")

    # Append to .env file
    if lines_to_append:
        try:
            # Read existing .env to avoid duplicates
            existing_lines = []
            if os.path.exists(ENV_FILE_PATH):
                with open(ENV_FILE_PATH, "r", encoding="utf-8") as f:
                    existing_lines = f.readlines()

            # Filter out existing lines for keys we are setting
            keys_set = {line.split("=")[0].strip() for line in lines_to_append}
            new_lines = [line for line in existing_lines if line.split("=")[0].strip() not in keys_set]

            # Ensure ending newline
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"

            for line in lines_to_append:
                new_lines.append(f"{line}\n")

            with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            print(f"[save_tool_credentials] Error writing .env: {e}")

    # Update API registry (store both exact service_name and normalized key)
    import time
    if service_name not in API_REGISTRY:
        API_REGISTRY[service_name] = {}
    API_REGISTRY[service_name]["status"] = "up"
    API_REGISTRY[service_name]["method_id"] = method_id or "api_key"
    API_REGISTRY[service_name]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

    if s_clean not in API_REGISTRY:
        API_REGISTRY[s_clean] = {}
    API_REGISTRY[s_clean]["status"] = "up"
    API_REGISTRY[s_clean]["method_id"] = method_id or "api_key"
    API_REGISTRY[s_clean]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_registry()

    # Sync every project's apis_mcps.json on disk so anything reading that file
    # directly sees the new status too.
    #
    # NOTE: this deliberately does NOT try to reach into jarvis.py's live
    # PLAN_STORE / DB rows anymore. jarvis.py is normally run as __main__, so
    # `from jarvis import PLAN_STORE` here would silently re-import jarvis.py
    # as a second, disconnected module instance with its own stale copy of
    # PLAN_STORE — mutations to it never reach the actually-running Flask
    # server, and writing that stale plan dict back to the DB risked
    # clobbering newer state. The live "is this tool connected" status is
    # instead resolved fresh from this registry at read-time by
    # jarvis.py's merge_live_tool_status() (see /plans, /plans/<id>), which
    # is correct regardless of what's cached in memory anywhere.
    try:
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Let Jarvis Handle It")
        if os.path.isdir(base_dir):
            for proj_name in os.listdir(base_dir):
                mem_file = os.path.join(base_dir, proj_name, "memory", "apis_mcps.json")
                if not os.path.exists(mem_file):
                    continue
                try:
                    with open(mem_file, "r", encoding="utf-8") as f:
                        apis_data = json.load(f)
                    updated = False
                    for sec in ("brain", "agents"):
                        for rec in apis_data.get(sec, []):
                            r_name = rec.get("service", "")
                            r_clean = r_name.lower().replace("-", "_").replace(" ", "_")
                            if r_name == service_name or r_clean == s_clean or s_clean in r_clean or r_clean in s_clean:
                                rec["current_status"] = "up"
                                rec["configured"] = True
                                updated = True
                    if updated:
                        with open(mem_file, "w", encoding="utf-8") as f:
                            json.dump(apis_data, f, indent=2, ensure_ascii=False)
                except Exception as ex:
                    print(f"[save_tool_credentials] Error updating memory file for '{proj_name}': {ex}")
    except Exception as e:
        print(f"[save_tool_credentials] Error syncing apis_mcps.json files: {e}")

    return True


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
