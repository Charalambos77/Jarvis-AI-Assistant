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
import json
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


def memory_patterns_search_impl(project_name: str, agent_id: str, query: str,
                                task_type: str | None = None, outcome: str | None = None) -> dict:
    """Read-only lookup over the memory_patterns table.

    Brain asks for this on nearly every plan. It used to resolve to web search,
    so an agent asking what Jarvis had learned before got search results off the
    internet instead. This is the real thing: past wins and losses, nothing else.
    """
    try:
        import db as _db
        conn = _db.get_connection(os.path.join(BASE_DIR, "second_brain.db"))
        try:
            rows = _db.search_memory_patterns(conn, query or "", task_type=task_type, outcome=outcome)
        finally:
            conn.close()
        return {
            "status": "ok",
            "action": "search_memory_patterns",
            "query": query,
            "count": len(rows),
            "patterns": [
                {
                    "pattern": r.get("pattern"),
                    "task_type": r.get("task_type"),
                    "metric_name": r.get("metric_name"),
                    "metric_value": r.get("metric_value"),
                    "outcome": r.get("outcome"),
                    "created_at": r.get("created_at"),
                }
                for r in rows
            ],
        }
    except Exception as e:
        return {"status": "error", "action": "search_memory_patterns", "error": str(e)}


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
    # Internal, read-only, no credentials — so it is always on rather than
    # sitting behind the Plugging Gate asking for a key that cannot exist.
    "memory_patterns": {
        "declaration": {
            "name": "search_memory_patterns",
            "description": (
                "Search Jarvis's own memory of what worked and what failed on past pipelines. "
                "Returns real recorded patterns with their metrics and win/loss outcome. "
                "This searches internal memory only — it does NOT search the web."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to match against remembered patterns."},
                    "task_type": {"type": "string", "description": "Optional filter, e.g. 'research', 'video', 'code'."},
                    "outcome": {"type": "string", "description": "Optional filter: 'win' or 'loss'."},
                },
                "required": ["query"],
            },
        },
        "handler": memory_patterns_search_impl,
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
# same underlying arXiv capability in different pipeline runs). This exact
# table is the fast, free, deterministic path; _resolve_tool_key() adds token
# rules and, only for names nothing else recognises, one cached model call.
TOOL_ALIASES = {
    "arxiv_search": "arxiv_api",
    "arxiv": "arxiv_api",
    "arxiv_api_client": "arxiv_api",
    "web_search": "google_search",
    "websearch": "google_search",
    "internet_search": "google_search",
    "search": "google_search",
    "google_drive_api": "google_docs_api",  # same real handler creates+writes a Doc either way
    "gdocs": "google_docs_api",
    "gdrive": "google_docs_api",
    "document_reader": "read_file",
    "file_reader": "read_file",
    "memory_search": "memory_patterns",
    "search_memory_patterns": "memory_patterns",
    "memory_query": "memory_patterns",
    "memory_patterns_search": "memory_patterns",
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
# Token rules, checked in order. A rule matches only when EVERY clause is
# satisfied by at least one whole token of the requested name.
#
# Whole tokens, never substrings. The old matcher tested `"web" in key`, which
# read "web" out of the middle of `text_generation_webui` and handed an agent
# asking to drive a local LLM UI a Google web-search tool instead — and matched
# "search" inside `search_memory_patterns`, so the memory lookup also silently
# became a web search. A wrong tool bound quietly is worse than no tool: an
# unresolved name is reported honestly, a mis-resolved one is used with
# confidence. Anything a rule doesn't clearly claim falls through.
_TOKEN_RULES = [
    # (canonical key, [clause, clause, ...]) — most distinctive first.
    ("arxiv_api", [{"arxiv"}]),
    ("memory_patterns", [{"memory", "memories", "recall", "learnings"}]),
    ("google_docs_api", [
        {"google", "gdocs", "gdrive"},
        {"doc", "docs", "document", "documents", "drive"},
    ]),
    # "search" alone is NOT distinctive — it appears in plenty of names that
    # have nothing to do with the web, so a web word is required too. Word
    # forms are listed out rather than stemmed: `web_browsing` has to land
    # here deterministically instead of falling through to a model call.
    ("google_search", [
        {"web", "internet", "google", "online", "www"},
        {"search", "searches", "searching", "browse", "browsing", "browser",
         "query", "queries", "lookup", "retrieval", "scrape", "scraper", "scraping", "crawl"},
    ]),
]

# Generic filler an LLM tacks onto a capability name. Stripping these collapses
# text_analysis_tool / text_analysis_tools / text_analysis_library onto one
# name, so the same concept can't get three different answers on three runs.
_GENERIC_SUFFIX_TOKENS = {
    "tool", "tools", "api", "apis", "library", "libraries", "service", "services",
    "framework", "frameworks", "sdk", "client", "access", "integration", "integrations",
    "connector", "plugin", "software", "utility", "utilities", "module",
}


def _strip_generic_suffixes(key: str) -> str:
    tokens = [t for t in key.split("_") if t]
    while len(tokens) > 1 and tokens[-1] in _GENERIC_SUFFIX_TOKENS:
        tokens.pop()
    return "_".join(tokens)

_NORMALIZE_RE = re.compile(r"[\s\-./\\:]+")


def _normalize_tool_name(raw_name: str) -> str:
    key = _NORMALIZE_RE.sub("_", (raw_name or "").strip().lower())
    return re.sub(r"_+", "_", key).strip("_")


def _token_rule_match(key: str) -> str | None:
    tokens = set(t for t in key.split("_") if t)
    if not tokens:
        return None
    for canonical, clauses in _TOKEN_RULES:
        if all(tokens & clause for clause in clauses):
            return canonical
    return None


# One model call per never-before-seen name, then remembered. Names repeat
# constantly across agents, cycles and runs, so this stays cheap — and the
# decision stays the same on a resume instead of being re-rolled each time.
_LLM_RESOLUTION_CACHE: dict[str, str] = {}
_RESOLUTION_CACHE_PATH = os.path.join(BASE_DIR, "data", "tool_resolution_cache.json")
_RESOLUTION_CACHE_LOADED = False


def _load_resolution_cache() -> None:
    global _RESOLUTION_CACHE_LOADED
    if _RESOLUTION_CACHE_LOADED:
        return
    _RESOLUTION_CACHE_LOADED = True
    try:
        with open(_RESOLUTION_CACHE_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            # Only keep entries that still point at a connector that exists —
            # a cached answer must never outlive the connector it names.
            known = set(REGISTRY_TOOLS) | set(ALWAYS_ON_TOOLS)
            for k, v in stored.items():
                if v == "" or v in known:
                    _LLM_RESOLUTION_CACHE.setdefault(k, v)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[ToolResolver] Could not read resolution cache: {e}")


def _save_resolution_cache() -> None:
    try:
        os.makedirs(os.path.dirname(_RESOLUTION_CACHE_PATH), exist_ok=True)
        with open(_RESOLUTION_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_LLM_RESOLUTION_CACHE, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[ToolResolver] Could not write resolution cache: {e}")


def _llm_resolve_tool_key(raw_name: str, context: str | None = None) -> str | None:
    """Ask the model which real connector a name means — or none of them.

    The answer is only ever accepted if it names a connector that actually
    exists, so the model can decline but never invent a capability.
    """
    key = _normalize_tool_name(raw_name)
    _load_resolution_cache()
    if key in _LLM_RESOLUTION_CACHE:
        cached = _LLM_RESOLUTION_CACHE[key]
        return cached or None
    if not GEMINI_API_KEY:
        return None

    # Always-on tools are offered too. Without them the model could only pick a
    # credentialed service, so a request to "edit text" became Google Docs and
    # the gate then asked for Google credentials on that agent's behalf — when
    # write_file was right there, free, and already bound.
    catalogue = "\n".join(
        [f"- {name}: {spec['declaration']['description']} (already available, no setup)"
         for name, spec in ALWAYS_ON_TOOLS.items()]
        + [f"- {name}: {spec['declaration']['description']}"
           for name, spec in REGISTRY_TOOLS.items()]
    )
    prompt = (
        "An agent asked for a tool by name. Decide which of the real connectors "
        "below actually provides that capability.\n\n"
        f"REQUESTED TOOL NAME: {raw_name}\n"
        + (f"WHAT THE AGENT IS DOING: {context}\n" if context else "")
        + f"\nREAL CONNECTORS AVAILABLE:\n{catalogue}\n\n"
        "Answer with the connector's exact name on a single line, or the single "
        "word none.\n"
        "Answer none when the request is not one of these capabilities — including "
        "when it names a local program, a desktop app, a model runtime, a "
        "programming library, or a general category of software rather than a "
        "service these connectors reach. Guessing is worse than none: a wrong "
        "match makes the agent act as though it has a capability it does not have."
    )
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        answer = _normalize_tool_name((resp.text or "").strip().splitlines()[0] if resp.text else "")
    except Exception as e:
        print(f"[ToolResolver] Could not resolve '{raw_name}' via model: {e}")
        return None

    known = set(REGISTRY_TOOLS) | set(ALWAYS_ON_TOOLS)
    resolved = answer if answer in known else ""
    if answer and not resolved and answer != "none":
        print(f"[ToolResolver] Model suggested '{answer}' for '{raw_name}', which is not a real connector — treating as unavailable.")
    _LLM_RESOLUTION_CACHE[key] = resolved
    _save_resolution_cache()
    return resolved or None


def _resolve_tool_key(raw_name: str, context: str | None = None, allow_llm: bool = True) -> str:
    """Map whatever an agent called a tool onto a canonical connector key.

    Exact match, then the curated alias table, then whole-token rules — all
    free and deterministic. Only a name none of those recognise reaches the
    model, and its answer must name a connector that exists. Unresolvable
    names come back unchanged so the caller reports them as unavailable.
    """
    key = _normalize_tool_name(raw_name)
    if not key:
        return key
    if key in REGISTRY_TOOLS:
        return key
    # Always-on tools need no credentials and must never reach the model
    # fallback — match them by their key and by the name agents actually call.
    if key in ALWAYS_ON_TOOLS:
        return key
    for always_key, spec in ALWAYS_ON_TOOLS.items():
        if key == spec["declaration"]["name"]:
            return always_key
    if key in TOOL_ALIASES:
        return TOOL_ALIASES[key]
    # An enabled MCP server, by name — exact match only. MCP servers are
    # user-configured, so their names are known strings, not something to guess at.
    mcp_servers = _mcp_server_names()
    if key in mcp_servers:
        return MCP_KEY_PREFIX + key
    ruled = _token_rule_match(key)
    if ruled:
        return ruled

    # Same three tiers again on the name minus its generic filler, so
    # "arxiv_search_api" and "arxiv_search" can't diverge.
    stem = _strip_generic_suffixes(key)
    if stem != key:
        if stem in REGISTRY_TOOLS or stem in ALWAYS_ON_TOOLS:
            return stem
        if stem in TOOL_ALIASES:
            return TOOL_ALIASES[stem]
        ruled = _token_rule_match(stem)
        if ruled:
            return ruled

    if allow_llm:
        # Ask about the stem, so every variant of one concept shares an answer.
        guessed = _llm_resolve_tool_key(stem, context)
        if guessed:
            return guessed
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


def get_tools_for_execution_agent(tools_needed: list[str], project_name: str, context: str | None = None):
    """
    Automatically resolves an execution agent's `tools_needed` (from the Brain's
    brief) into (gemini_function_declarations, handler_map, unavailable) —
    no manual per-agent wiring required.

    A tool is bound for real only if:
      - it's an always-on tool (files, internal memory search), or
      - its backing service is configured/up in api_registry.json (i.e. the
        user approved it at the API/MCP Plugging Gate) AND a real handler
        exists in REGISTRY_TOOLS.

    Anything requested but not resolvable is returned in `unavailable` so the
    agent's system prompt can tell it honestly, instead of it fabricating a
    fake success for a tool that doesn't actually exist. `context` (the asking
    agent's role/brief) only sharpens the model fallback for unfamiliar names.
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
        raw_key = _normalize_tool_name(raw_name)
        if raw_key in ("", "none", "n/a", "na"):
            continue
        if raw_key in ("document_reader", "file_reader"):
            continue  # covered by the always-on read_file tool

        key = _resolve_tool_key(raw_key, context=context)

        if key in ALWAYS_ON_TOOLS:
            continue  # bound above for every agent, no credentials involved

        if key.startswith(MCP_KEY_PREFIX):
            server_name = key[len(MCP_KEY_PREFIX):]
            if _bind_mcp_server(server_name, declarations, handlers, seen):
                continue
            unavailable.append(
                f"{raw_name} (MCP server '{server_name}' is enabled but did not start — do not claim to have used it)"
            )
            continue

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


# ---------------------------------------------------------------------------
# MCP SERVERS — discovered at runtime, not declared here
# ---------------------------------------------------------------------------

MCP_KEY_PREFIX = "mcp:"

# Gemini accepts only a subset of JSON Schema. MCP servers send whatever they
# like ($schema, additionalProperties, anyOf, defaults...), and an unknown key
# makes Gemini reject the whole declaration — which would silently cost the
# agent every tool on that server.
_SCHEMA_KEEP = {"type", "properties", "required", "items", "enum", "description"}


def _sanitize_schema(schema):
    if not isinstance(schema, dict):
        return {"type": "string"}
    out = {}
    for k, v in schema.items():
        if k not in _SCHEMA_KEEP:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            out[k] = _sanitize_schema(v)
        else:
            out[k] = v
    if "type" not in out:
        out["type"] = "object" if "properties" in out else "string"
    if out["type"] == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def _mcp_function_name(server: str, tool: str) -> str:
    """Namespaced so an MCP tool can never shadow a built-in.

    The filesystem server offers its own read_file and write_file; without a
    prefix they would overwrite the always-on handlers that keep agents
    sandboxed inside the project's Deliverables folder.
    """
    raw = f"mcp_{server}_{tool}"
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    return safe[:64]


def _mcp_server_names() -> dict:
    try:
        from connectors.mcp_client import enabled_servers
        return enabled_servers()
    except Exception:
        return {}


def _bind_mcp_server(server_name: str, declarations: list, handlers: dict, seen: set) -> bool:
    """Add every tool a running MCP server offers. False if it isn't running."""
    try:
        from connectors.mcp_client import ensure_server_running, call_mcp_tool
    except Exception as e:
        print(f"[ToolResolver] MCP client unavailable: {e}")
        return False

    info = ensure_server_running(server_name)
    if not info or info.get("status") != "up":
        return False

    for tool in info.get("tools", []):
        fn_name = _mcp_function_name(server_name, tool["name"])
        if fn_name in seen:
            continue
        declarations.append({
            "name": fn_name,
            "description": f"[MCP:{server_name}] {tool.get('description') or tool['name']}",
            "parameters": _sanitize_schema(tool.get("input_schema")),
        })

        def _make_handler(srv=server_name, tname=tool["name"]):
            def _handler(project_name: str, agent_id: str, **kwargs):
                return call_mcp_tool(srv, tname, kwargs)
            return _handler

        handlers[fn_name] = _make_handler()
        seen.add(fn_name)
    return True


def classify_requested_tools(names, context: str | None = None) -> dict:
    """Sort raw tool names into what the Plugging Gate should actually ask about.

    Returns {"connectable": {canonical: [raw names]}, "always_on": [...],
             "not_a_service": [...]}.

    `connectable` is the only bucket that can need a credential. `not_a_service`
    is everything no connector provides — local binaries, model runtimes,
    software categories. Asking the user for an API key for `nvidia-smi` was
    never going to lead anywhere.
    """
    connectable: dict[str, list[str]] = {}
    always_on: list[str] = []
    not_a_service: list[str] = []

    for raw in (names or []):
        raw_key = _normalize_tool_name(raw)
        if raw_key in ("", "none", "n/a", "na"):
            continue
        key = _resolve_tool_key(raw_key, context=context)
        if key in ALWAYS_ON_TOOLS:
            if raw not in always_on:
                always_on.append(raw)
        elif key in REGISTRY_TOOLS or key.startswith(MCP_KEY_PREFIX):
            connectable.setdefault(key, [])
            if raw not in connectable[key]:
                connectable[key].append(raw)
        elif raw not in not_a_service:
            not_a_service.append(raw)

    return {"connectable": connectable, "always_on": always_on, "not_a_service": not_a_service}


def describe_connectable(key: str) -> dict:
    """What the gate needs to render one required item: is it an API or an MCP
    server, and is it actually usable right now (verified, not asserted)."""
    if key.startswith(MCP_KEY_PREFIX):
        server = key[len(MCP_KEY_PREFIX):]
        try:
            from connectors.mcp_client import ensure_server_running
            info = ensure_server_running(server)
        except Exception as e:
            info = {"status": "down", "tools": [], "error": str(e)}
        return {
            "kind": "mcp",
            "service": server,
            "configured": info.get("status") == "up",
            "current_status": info.get("status", "down"),
            "error": info.get("error"),
            "tools": [t["name"] for t in info.get("tools", [])],
        }

    from connectors.api_connector import get_service_status
    status = get_service_status(key)
    return {
        "kind": "api",
        "service": key,
        "configured": status != "unknown",
        "current_status": status,
    }


def run_tool(handler_map: dict, project_name: str, agent_id: str, tool_name: str, tool_args: dict) -> dict:
    handler = handler_map.get(tool_name)
    if not handler:
        return {"status": "error", "error": f"Unknown or unbound tool '{tool_name}'."}
    try:
        return handler(project_name=project_name, agent_id=agent_id, **(tool_args or {}))
    except Exception as e:
        return {"status": "error", "error": str(e)}
