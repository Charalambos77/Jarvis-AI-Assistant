def run_synthesis_agent(agent_results: list[dict]) -> dict:
    """
    Takes N research agent result dicts.
    1. Detects conflicts between any two agents on the same key.
    2. If conflicts found, returns {"status": "conflict", "conflicts": [...]}
       for the Brain to adjudicate.
    3. If no conflicts, returns {"status": "ok", "blueprint": {...}}
    """
    if not agent_results:
        return {"status": "error", "message": "No agent results to synthesise."}

    # Step 1: Merge all results into one flat dict, tracking sources
    merged = {}
    for result in agent_results:
        for key, value in result.items():
            if key not in merged:
                merged[key] = [{"agent": result.get("agent_id", "unknown"), "value": value}]
            else:
                merged[key].append({"agent": result.get("agent_id", "unknown"), "value": value})

    # Step 2: Conflict detection — multiple agents disagreeing on the same key.
    # [FIX #1] Skip meta-keys that are EXPECTED to differ per agent.
    # Without this, agent_id, confidence, and status would ALWAYS be flagged as conflicts.
    SKIP_KEYS = {"agent_id", "status", "confidence", "sources", "recommendation", "role"}
    conflicts = []
    for key, entries in merged.items():
        if key in SKIP_KEYS:
            continue
        unique_values = set(str(e["value"]) for e in entries)
        if len(unique_values) > 1:
            conflicts.append({
                "key": key,
                "disagreements": entries
            })

    if conflicts:
        return {
            "status": "conflict",
            "conflicts": conflicts,
            "message": (
                f"{len(conflicts)} conflict(s) detected. "
                "Brain must adjudicate before proceeding to Gate 1."
            )
        }

    # Step 3: No conflicts — compress into blueprint
    blueprint = {key: entries[0]["value"] for key, entries in merged.items()}
    return {
        "status": "ok",
        "blueprint": blueprint,
        "agent_count": len(agent_results),
        "key_count": len(blueprint)
    }
