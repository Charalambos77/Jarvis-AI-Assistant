"""
Deployment Agent — takes final execution results and performs actual deployment.

Currently a pluggable stub. Real deployment logic is added per-connector:
- YouTube: upload video via YouTube Data API
- GitHub: push code via GitHub API
- Stripe: create products/prices via Stripe API
- etc.

The user confirms which APIs/MCPs are plugged in at the API/MCP Plugging Gate
(Step 6) BEFORE execution begins. This agent reads the registry to know
which connectors are available.
"""
import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv()


async def run_deployment_agent(
    exec_results: list[dict],
    master_blueprint: dict,
    target_platform: str | None = None,
) -> dict:
    """
    Executes deployment based on the execution results and blueprint.
    
    Checks the API registry to see which connectors are available.
    For each execution result, routes to the appropriate connector.
    If no connector is available, logs what WOULD be deployed.
    """
    from connectors.api_connector import load_registry, get_service_status
    registry = load_registry()
    
    deployment_log = []
    
    for result in exec_results:
        agent_id = result.get("agent_id", "unknown")
        status = result.get("status", "unknown")
        
        # Check if a target connector exists in registry
        target = target_platform or "local"
        connector_status = get_service_status(target) if target != "local" else "local"
        
        # Real artifacts (a created Google Doc, a written file, ...) come
        # from the execution agent's actual tool calls (see execution_agent's
        # _extract_artifact) — they survive even if that agent's own status
        # ended up "error" from an unrelated wrap-up formatting issue, since
        # the tool call itself already did real, irreversible work.
        artifacts = result.get("artifacts", [])

        if connector_status in ("up", "local"):
            # Future: call the actual connector here
            deployment_log.append({
                "agent_id": agent_id,
                "status": "deployed" if (status == "ok" or artifacts) else "skipped",
                "platform": target,
                "connector_status": connector_status,
                "output_keys": list(result.keys()),
                "artifacts": artifacts,
                "note": "Stub — would deploy here when connector is implemented",
            })
        else:
            deployment_log.append({
                "agent_id": agent_id,
                "status": "blocked",
                "platform": target,
                "connector_status": connector_status,
                "artifacts": artifacts,
                "note": f"Connector '{target}' is {connector_status}. Cannot deploy.",
            })
    
    all_artifacts = [a for entry in deployment_log for a in entry.get("artifacts", [])]

    return {
        "status": "deployed",
        "platform": target_platform or "local",
        "deployment_log": deployment_log,
        "artifacts": all_artifacts,
        "artifacts_count": len(exec_results),
        "registry_snapshot": {k: v.get("status", "unknown") for k, v in registry.items()} if registry else {},
    }
