"""
The tool catalogue — every tool Jarvis has discovered on a connected service,
what the user decided about it, and how risky it is.

Before this, an MCP server's tools reached an agent only if the Brain happened
to name that server in the agent's tools_needed, nothing about them was kept
between runs, and a tool that deletes or sends things was bound exactly like
one that reads. Now a service's tools are written here the moment they are
listed, and nothing is offered to an agent until the catalogue says so.

Each tool carries:
  * risk   — "read", "write", "destructive" (deletes, sends, publishes) or
             "costs_money", decided by classify_risk below;
  * status — "pending"  waiting for the review on the Commands page;
             "held"     costs money or is destructive, waiting in the Control room;
             "enabled"  agents may use it;
             "disabled" rejected or blocked;
  * rule   — for risky tools once enabled: "always_ask" (every call is held in
             the Control room) or "always_allow".

The file lives in data/, next to the tool resolution cache, and is rewritten
whole under a lock: it is small and changes only when a person decides something
or a server's tool list changes.
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(BASE_DIR, "data", "tool_catalog.json")

RISKY = ("destructive", "costs_money")

_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load() -> dict:
    with _LOCK:
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[Tool catalogue] Could not read {CATALOG_PATH}: {e}")
            return {}


def save(catalog: dict) -> None:
    with _LOCK:
        os.makedirs(os.path.dirname(CATALOG_PATH), exist_ok=True)
        tmp = CATALOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(catalog, f, indent=2, sort_keys=True)
        os.replace(tmp, CATALOG_PATH)


def get(service: str) -> dict | None:
    return load().get(service)


def update(fn) -> dict:
    """Load, let fn change the catalogue in place, save. Returns the saved catalogue."""
    with _LOCK:
        catalog = load()
        fn(catalog)
        save(catalog)
        return catalog


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------

# Matched against whole words of the tool's name. Money is checked first, then
# destructive, so a tool that both lists and deletes is destructive.
#
# Action words count wherever they appear in a name. Nouns only count when the
# name does not start with a reading verb: create_order costs money, list_orders
# does not.
_MONEY_ACTIONS = {
    "pay", "charge", "purchase", "buy", "checkout", "refund", "subscribe", "payout", "withdraw",
    "donate", "tip", "bid", "trade", "transfer", "book", "reserve", "spend", "topup",
}
_MONEY_NOUNS = {
    "payment", "payments", "order", "orders", "invoice", "invoices", "subscription",
    "subscriptions", "billing", "bill", "booking", "bookings", "reservation", "charges",
}
# Matched against whole words of the description, whatever the name says: a
# search that "costs 1 credit per call" still costs money.
_MONEY_TEXT = {
    "pay", "pays", "payment", "payments", "charge", "charged", "charges", "purchase", "purchases",
    "buy", "checkout", "invoice", "refund", "billing", "billed", "payout", "paid", "cost", "costs",
    "credit", "credits", "spend", "spends", "money", "price", "fee", "fees",
}
_DESTRUCTIVE_NAME = {
    "delete", "remove", "drop", "destroy", "purge", "erase", "wipe", "truncate", "kill",
    "terminate", "revoke", "cancel", "overwrite", "reset", "uninstall", "send", "post", "publish",
    "tweet", "reply", "deploy", "push", "merge", "unpublish", "ban", "execute", "exec", "shell",
}
# Only when the name does not start with a reading verb, so "get_file: returns a
# file that can later be deleted" stays a read.
_DESTRUCTIVE_TEXT = {
    "delete", "deletes", "deleted", "permanently", "irreversible", "irreversibly", "destroy",
    "destroys", "erase", "erases", "wipe", "wipes", "send", "sends", "publish", "publishes",
    "overwrite", "overwrites", "deploy", "deploys",
}
_READ_VERBS = {
    "get", "list", "read", "search", "fetch", "find", "query", "describe", "show", "view",
    "lookup", "count", "status", "check", "inspect", "echo", "browse", "retrieve", "download",
    "explore", "info", "summarize", "summarise", "preview", "resolve", "validate",
}

_WORD_RE = re.compile(r"[A-Za-z]+")


def _words(text: str) -> list[str]:
    # snake_case, kebab-case and camelCase all split into lower-case words.
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    return [w.lower() for w in _WORD_RE.findall(spaced)]


def classify_risk(name: str, description: str = "", annotations: dict | None = None) -> str:
    """read, write, destructive or costs_money — cautious by design.

    A wrong "read" hands every agent a tool nobody reviewed; a wrong
    "destructive" only means one extra click in the Control room. So money and
    destructive words win over everything, a server's own readOnlyHint can only
    lower a tool to "read" when nothing else says otherwise, and anything
    unclear is "write".
    """
    annotations = annotations or {}
    ordered = _words(name)
    name_words = set(ordered)
    text_words = set(_words(description))
    # Google's API documents name operations resource first ("subscriptions_list"),
    # so for a tool the service itself marks read-only the verb may come last.
    reads_first = bool(ordered) and (ordered[0] in _READ_VERBS
                                     or (annotations.get("readOnlyHint") is True and ordered[-1] in _READ_VERBS))

    if name_words & _MONEY_ACTIONS or text_words & _MONEY_TEXT \
            or (not reads_first and name_words & _MONEY_NOUNS):
        return "costs_money"
    if annotations.get("destructiveHint") is True or name_words & _DESTRUCTIVE_NAME \
            or (not reads_first and text_words & _DESTRUCTIVE_TEXT):
        return "destructive"
    if reads_first or annotations.get("readOnlyHint") is True:
        return "read"
    return "write"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _tool_hash(tool: dict) -> str:
    parts = [tool.get("name"), tool.get("description") or "", tool.get("input_schema") or {}]
    if tool.get("call"):
        # An API tool pointed at a different address is a different tool.
        parts.append(tool["call"])
    raw = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tools_hash(tools: list[dict]) -> str:
    return hashlib.sha256("|".join(sorted(_tool_hash(t) for t in tools)).encode("utf-8")).hexdigest()[:16]


def record_inventory(service: str, kind: str, tools: list[dict], fn_name) -> tuple[dict, bool]:
    """Write a service's real tool list into the catalogue.

    Returns (entry, changed). A tool that is exactly as it was keeps whatever the
    user already decided about it; a new or changed tool starts over as pending
    (or held, if it is risky). Tools the server no longer offers are dropped.
    `fn_name(service, tool_name)` gives the name agents call it by.
    """
    digest = tools_hash(tools)
    result = {}

    def _apply(catalog):
        entry = catalog.get(service)
        if entry and entry.get("tools_hash") == digest:
            result["entry"], result["changed"] = entry, False
            return
        previous = (entry or {}).get("tools", {})
        new_tools = {}
        for tool in tools:
            name = tool["name"]
            digest_one = _tool_hash(tool)
            old = previous.get(name)
            if old and old.get("schema_hash") == digest_one:
                new_tools[name] = old
                continue
            risk = classify_risk(name, tool.get("description") or "", tool.get("annotations"))
            new_tools[name] = {
                "fn_name": fn_name(service, name),
                "description": tool.get("description") or name,
                # What the server itself said. "description" is replaced by the
                # research card's text once the research phase has run.
                "server_description": tool.get("description") or "",
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                "risk": risk,
                "status": "held" if risk in RISKY else "pending",
                "rule": "always_ask",
                "schema_hash": digest_one,
                "discovered_at": now_iso(),
            }
            if tool.get("call"):
                # How to make the real request: API tools only (see openapi_tools).
                new_tools[name]["call"] = tool["call"]
        entry = {
            **(entry or {}),
            "kind": kind,
            "tools_hash": digest,
            "discovered_at": now_iso(),
            "tools": new_tools,
        }
        entry.setdefault("review", None)
        catalog[service] = entry
        result["entry"], result["changed"] = entry, True

    update(_apply)
    return result["entry"], result["changed"]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def enabled_tools(service: str | None = None) -> list[tuple[str, str, dict]]:
    """(service, tool name, tool) for every tool agents may use, optionally for one service."""
    out = []
    for name, entry in load().items():
        if service and name != service:
            continue
        for tool_name, tool in (entry.get("tools") or {}).items():
            if tool.get("status") == "enabled":
                out.append((name, tool_name, tool))
    return out


def find_by_fn_name(fn_name: str) -> tuple[str, str, dict] | None:
    """(service, tool name, tool) for the name an agent called."""
    for service, entry in load().items():
        for name, tool in (entry.get("tools") or {}).items():
            if tool.get("fn_name") == fn_name:
                return service, name, tool
    return None


def set_tool(service: str, tool_name: str, **fields) -> dict | None:
    """Change one tool's fields. Returns the tool, or None if there is no such tool."""
    found = {}

    def _apply(catalog):
        tool = ((catalog.get(service) or {}).get("tools") or {}).get(tool_name)
        if tool is not None:
            tool.update(fields)
            found["tool"] = tool

    update(_apply)
    return found.get("tool")
