def run_quality_checker(execution_results: list[dict], spec: dict) -> dict:
    """
    Validates each execution agent's output against the blueprint spec.
    Returns per-agent pass/fail with reasons.

    spec example:
    {
        "min_word_count": 500,
        "required_keys": ["title", "body", "cta"],
        "task_type": "content"
    }
    """
    results = []
    all_passed = True

    for agent_result in execution_results:
        agent_id = agent_result.get("agent_id", "unknown")
        issues = []

        # Check required keys
        for key in spec.get("required_keys", []):
            if key not in agent_result:
                issues.append(f"Missing required key: '{key}'")

        # Check min word count (for content tasks)
        if spec.get("min_word_count"):
            body = str(agent_result.get("body", ""))
            wc = len(body.split())
            if wc < spec["min_word_count"]:
                issues.append(
                    f"Word count {wc} is below minimum {spec['min_word_count']}"
                )

        # Check for error signals from the agent itself
        if agent_result.get("status") == "error":
            issues.append(f"Agent self-reported error: {agent_result.get('error', 'unknown')}")

        passed = len(issues) == 0
        if not passed:
            all_passed = False

        results.append({
            "agent_id": agent_id,
            "passed": passed,
            "issues": issues
        })

    return {
        "all_passed": all_passed,
        "results": results,
        "failed_agents": [r["agent_id"] for r in results if not r["passed"]]
    }
