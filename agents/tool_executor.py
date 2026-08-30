"""
Tool Executor — real, callable actions execution agents can invoke via Gemini
function-calling, instead of just narrating JSON about what they "did".

Design: tools are bound to an execution agent AUTOMATICALLY based on the
`tools_needed` list the Brain put in its brief, cross-checked against
api_registry.json (the same registry the API/MCP Plugging Gate writes to).
No separate manual wiring step — approve a service at the Plugging Gate
(or it ships pre-configured, like google_search/arxiv_api) and any agent
that lists it in tools_needed gets a real handler for it automatically.

Two tiers:
  1. ALWAYS_ON_TOOLS   — no credentials required, always bound (write_file,
                          read_file, list_deliverables). This is what turns
                          "the agent wrote prose describing a file" into an
                          actual file landing on disk.
  2. REGISTRY_TOOLS    — bound only when connectors/api_connector.py reports
                          the backing service as configured/up. If a brief
                          asks for a tool that has no real handler yet, we do
                          NOT fabricate one — we tell the agent explicitly so
                          it reports the limitation instead of hallucinating
                          success (see build_unavailable_notice below).
"""
import os
import re
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _deliverables_dir(project_name: str) -> str:
    d = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Deliverables")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_join(project_name: str, relative_path: str) -> str:
    """Resolve relative_path under the project's Deliverables dir, blocking traversal."""
    root = os.path.abspath(_deliverables_dir(project_name))
    relative_path = (relative_path or "output.txt").lstrip("/\\")
    candidate = os.path.abspath(os.path.join(root, relative_path))
    if not (candidate == root or candidate.startswith(root + os.sep)):
        raise ValueError(f"Path '{relative_path}' escapes the project's Deliverables directory.")
    return candidate


# ---------------------------------------------------------------------------
# ALWAYS-ON TOOLS — no API key / registry entry required
# ---------------------------------------------------------------------------

def write_file_impl(project_name: str, agent_id: str, relative_path: str, content: str) -> dict:
    path = _safe_join(project_name, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    rel = os.path.relpath(path, BASE_DIR)
    return {"status": "ok", "action": "write_file", "path": rel, "bytes_written": len(content or "")}


def read_file_impl(project_name: str, agent_id: str, relative_path: str) -> dict:
    path = _safe_join(project_name, relative_path)
    if not os.path.exists(path):
        return {"status": "error", "action": "read_file", "error": f"File not found: {relative_path}"}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return {"status": "ok", "action": "read_file", "path": relative_path, "content": content}


def list_deliverables_impl(project_name: str, agent_id: str) -> dict:
    root = _deliverables_dir(project_name)
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return {"status": "ok", "action": "list_deliverables", "files": files}


ALWAYS_ON_TOOLS = {
    "write_file": {
        "declaration": {
            "name": "write_file",
            "description": (
                "Write a real deliverable file to disk under this project's Deliverables folder. "
                "Use this for any code file, report, script, or document you produce — do not just "
                "describe the file's contents in your final answer, actually write it with this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative path/filename, e.g. 'index.html' or 'src/app.py'"},
                    "content": {"type": "string", "description": "Full file contents to write."},
                },
                "required": ["relative_path", "content"],
            },
        },
        "handler": write_file_impl,
    },
    "read_file": {
        "declaration": {
            "name": "read_file",
            "description": "Read back a deliverable file previously written in this project (e.g. one written by another agent).",
            "parameters": {
                "type": "object",
                "properties": {"relative_path": {"type": "string"}},
                "required": ["relative_path"],
            },
        },
        "handler": read_file_impl,
    },
    "list_deliverables": {
        "declaration": {
            "name": "list_deliverables",
            "description": "List every deliverable file written so far in this project.",
            "parameters": {"type": "object", "properties": {}},
        },
        "handler": list_deliverables_impl,
    },
}


# ---------------------------------------------------------------------------
# REGISTRY-GATED TOOLS — real handlers, only offered when the backing
# service is configured (status != "unknown" in api_registry.json)
# ---------------------------------------------------------------------------

def arxiv_search_impl(project_name: str, agent_id: str, query: str, max_results: int = 5) -> dict:
    """Real call to the public arXiv API (no key required)."""
    try:
        resp = requests.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": f"all:{query}", "start": 0, "max_results": max(1, min(int(max_results or 5), 20))},
            timeout=15,
        )
        resp.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.text)
        papers = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            link = ""
            for l in entry.findall("atom:link", ns):
                if l.attrib.get("type") == "text/html":
                    link = l.attrib.get("href", "")
                    break
            authors = [a.findtext("atom:name", default="", namespaces=ns) for a in entry.findall("atom:author", ns)]
            papers.append({"title": title, "summary": summary, "url": link, "authors": authors})
        return {"status": "ok", "action": "arxiv_search", "query": query, "results": papers}
    except Exception as e:
        return {"status": "error", "action": "arxiv_search", "error": str(e)}


def web_search_impl(project_name: str, agent_id: str, query: str) -> dict:
    """Real Gemini-grounded web search (same mechanism coordinator.py uses for
    the voice assistant's own google_search tool), exposed as a normal
    function tool so agents can combine it with write_file/arxiv_search/etc.
    in one function-calling loop (Gemini won't let a single call mix its
    built-in google_search grounding tool with custom function declarations,
    so this wraps grounding in its own inner call instead)."""
    if not GEMINI_API_KEY:
        return {"status": "error", "action": "web_search", "error": "GEMINI_API_KEY not configured."}
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GEMINI_API_KEY)
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction="Search the web for the query and return a factual, concise summary with sources if available.",
        )
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=query, config=config)
        return {"status": "ok", "action": "web_search", "query": query, "summary": resp.text or ""}
    except Exception as e:
        return {"status": "error", "action": "web_search", "error": str(e)}


# Alternate names Brain has been observed using for the same real capability.
# Brain isn't guaranteed to phrase tools_needed consistently run to run — it's
# an LLM generating a fresh name most times (arxiv_api, arxiv_search,
# arxiv_search_api, arxiv_api_client, ... have all shown up for the exact
# same underlying arXiv capability in different pipeline runs). An exact-match
# alias table can't keep up with that, so exact aliases are just a fast path;
# _resolve_tool_key() below falls back to keyword matching for anything not
# in this table, which is what actually makes this durable.
TOOL_ALIASES = {
    "arxiv_search": "arxiv_api",
    "arxiv": "arxiv_api",
    "arxiv_api_client": "arxiv_api",
    "web_search": "google_search",
    "websearch": "google_search",
    "internet_search": "google_search",
    "search": "google_search",
    "google_drive_api": "google_docs_api",  # same real handler creates+writes a Doc either way
}


def google_docs_create_impl(project_name: str, agent_id: str, title: str, content: str) -> dict:
    """
    Real Google Docs creation, using the OAuth tokens produced by the
    connect-tool flow (connectors/oauth_flow.py). Honest on failure — never
    fabricates a doc_id/url if the API call didn't actually succeed.
    """
    from connectors.oauth_flow import refresh_access_token, get_oauth_provider

    # Brain has named this capability "google_docs_api" in some plans and
    # "google_drive_api" in others (they're the same real handler here —
    # see TOOL_ALIASES); credentials could have been saved under either
    # prefix depending on which name the Plugging Gate showed. Check both.
    client_id = os.getenv("GOOGLE_DOCS_API_CLIENT_ID") or os.getenv("GOOGLE_DRIVE_API_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_DOCS_API_CLIENT_SECRET") or os.getenv("GOOGLE_DRIVE_API_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_DOCS_API_REFRESH_TOKEN") or os.getenv("GOOGLE_DRIVE_API_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        return {
            "status": "error", "action": "google_docs_create",
            "error": "Google Docs/Drive is not actually authorized yet (missing OAuth tokens) — "
                     "connect it at the API/MCP Plugging Gate first.",
        }

    provider = get_oauth_provider("google_docs_api")
    refreshed = refresh_access_token(provider["token_url"], client_id, client_secret, refresh_token)
    if refreshed.get("status") != "ok":
        return {"status": "error", "action": "google_docs_create", "error": f"token refresh failed: {refreshed.get('error')}"}
    access_token = refreshed["access_token"]
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    try:
        create_resp = requests.post(
            "https://docs.googleapis.com/v1/documents",
            headers=headers,
            json={"title": title or "Untitled Document"},
            timeout=20,
        )
        create_resp.raise_for_status()
        doc = create_resp.json()
        doc_id = doc["documentId"]

        if content:
            update_resp = requests.post(
                f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                headers=headers,
                json={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
                timeout=20,
            )
            update_resp.raise_for_status()

        return {
            "status": "ok", "action": "google_docs_create",
            "google_doc_id": doc_id,
            "google_doc_url": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
    except Exception as e:
        return {"status": "error", "action": "google_docs_create", "error": str(e)}

# Fallback keyword groups, checked in order, first match wins. A key matches
# a group if it contains ANY of the group's keywords. Keep "arxiv" ahead of
# the generic search/web group so "arxiv_search_api" resolves to arxiv_api,
# not google_search.
_FUZZY_TOOL_GROUPS = [
    ("arxiv_api", ("arxiv",)),
    ("google_search", ("search", "web", "internet", "browse")),
]


def _resolve_tool_key(raw_name: str) -> str:
    """Best-effort mapping from whatever Brain called a tool to the canonical
    REGISTRY_TOOLS key, so the exact wording Brain used this run doesn't
    matter as long as the intent is recognizable."""
    key = re.sub(r"[\s-]+", "_", (raw_name or "").strip().lower())
    if key in REGISTRY_TOOLS:
        return key
    if key in TOOL_ALIASES:
        return TOOL_ALIASES[key]
    for canonical, keywords in _FUZZY_TOOL_GROUPS:
        if any(kw in key for kw in keywords):
            return canonical
    return key

REGISTRY_TOOLS = {
    "arxiv_api": {
        "declaration": {
            "name": "arxiv_search",
            "description": "Search arXiv.org for real papers matching a query. Returns real titles, abstracts, authors, and URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        "handler": arxiv_search_impl,
    },
    "google_search": {
        "declaration": {
            "name": "web_search",
            "description": "Search the live web for a query and get back a real, grounded, factual summary with sources.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        "handler": web_search_impl,
    },
    "google_docs_api": {
        "declaration": {
            "name": "create_google_doc",
            "description": (
                "Create a real Google Doc with the given title and content, using the OAuth "
                "connection made at the API/MCP Plugging Gate. Returns the real document ID and URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Full text body to write into the doc."},
                },
                "required": ["title", "content"],
            },
        },
        "handler": google_docs_create_impl,
    },
    # Real handlers land here as connectors are built (github, youtube_api,
    # ...). Until a service has one, it stays out of REGISTRY_TOOLS on
    # purpose — anything requested but unresolved is
    # reported to the agent as unavailable instead of silently faked.
}


def get_tools_for_execution_agent(tools_needed: list[str], project_name: str):
    """
    Automatically resolves an execution agent's `tools_needed` (from the Brain's
    brief) into (gemini_function_declarations, handler_map, unavailable) —
    no manual per-agent wiring required.

    A tool is bound for real only if:
      - it's an always-on tool (write_file/read_file/list_deliverables), or
      - its backing service is configured/up in api_registry.json (i.e. the
        user approved it at the API/MCP Plugging Gate) AND a real handler
        exists in REGISTRY_TOOLS.

    Anything requested but not resolvable is returned in `unavailable` so the
    agent's system prompt can tell it honestly, instead of it fabricating a
    fake success for a tool that doesn't actually exist.
    """
    from connectors.api_connector import get_service_status

    declarations = []
    handlers = {}
    seen = set()

    for name, spec in ALWAYS_ON_TOOLS.items():
        declarations.append(spec["declaration"])
        handlers[spec["declaration"]["name"]] = spec["handler"]
        seen.add(spec["declaration"]["name"])

    unavailable = []
    for raw_name in (tools_needed or []):
        raw_key = re.sub(r"[\s-]+", "_", (raw_name or "").strip().lower())
        if raw_key in ("", "none", "n/a"):
            continue
        if raw_key in ("write_file", "read_file", "list_deliverables"):
            continue  # already bound above, always-on
        if raw_key in ("document_reader", "file_reader"):
            continue  # covered by the always-on read_file tool

        key = _resolve_tool_key(raw_key)

        if key in REGISTRY_TOOLS:
            # Check BOTH the exact name Brain used (raw_key) and the resolved
            # canonical name (key) — a service can be connected at the
            # Plugging Gate under either (e.g. plan's tool_recommendations
            # says "google_drive_api", which resolves to the canonical
            # "google_docs_api" handler, but the user connected it under
            # whichever exact name the gate showed). Checking only the
            # resolved key missed genuinely-connected services named
            # differently from their canonical handler key.
            status = get_service_status(raw_key)
            if status == "unknown":
                status = get_service_status(key)
            if status != "unknown":
                spec = REGISTRY_TOOLS[key]
                fn_name = spec["declaration"]["name"]
                if fn_name not in seen:
                    declarations.append(spec["declaration"])
                    handlers[fn_name] = spec["handler"]
                    seen.add(fn_name)
                continue
            else:
                unavailable.append(f"{raw_name} (service '{key}' not yet configured at the Plugging Gate)")
        else:
            unavailable.append(f"{raw_name} (no real connector implemented yet — do not claim to have used it)")

    return declarations, handlers, unavailable


def run_tool(handler_map: dict, project_name: str, agent_id: str, tool_name: str, tool_args: dict) -> dict:
    handler = handler_map.get(tool_name)
    if not handler:
        return {"status": "error", "error": f"Unknown or unbound tool '{tool_name}'."}
    try:
        return handler(project_name=project_name, agent_id=agent_id, **(tool_args or {}))
    except Exception as e:
        return {"status": "error", "error": str(e)}
