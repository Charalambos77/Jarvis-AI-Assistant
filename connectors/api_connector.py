import sqlite3


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
