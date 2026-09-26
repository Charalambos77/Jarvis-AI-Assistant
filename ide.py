"""
The Workbench — Jarvis's in-app IDE.

The owner can already hand software work to Claude or Antigravity through their
CLIs. This is the place to do that work *with* Jarvis instead: a file explorer,
an editor and a terminal on one of Jarvis's projects, with Jarvis working
alongside as an agent. It still works with no CLI connected at all, because
Jarvis can do the work with his own model (Gemini, or a local Ollama model).

It is agent-first. The unit of work is a *mission*, not a keystroke: the owner
says what they want, Jarvis writes down what he understood and how he will do
it, and — depending on the mission's mode — waits for that plan to be approved,
then proposes the edits as diffs the owner accepts or rejects file by file.
Commands Jarvis wants run are never run by him: they are listed on the mission
with a Run button, so the click is the approval.

Three engines can do a mission:

  * "jarvis"      — Jarvis's own model. Plans, then proposes edits. Nothing
                    touches disk until it is accepted (or the mission is on
                    autopilot).
  * "antigravity" — the Antigravity CLI (connectors/antigravity.py). It edits
                    files itself, so its changes arrive already applied; each
                    one can still be looked at as a diff and reverted.
  * "claude"      — the Claude coding agent (agents/code_agent.py) when that
                    module is present and set up. Same shape as Antigravity.

Everything here is confined to one project folder under "Let Jarvis Handle It".
Paths are resolved and checked before every read and write, so neither the
owner's typo nor a model's invention can reach Jarvis's own source.
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECTS_ROOT = os.path.join(BASE_DIR, "Let Jarvis Handle It")
STORE_DIR = os.path.join(BASE_DIR, "data", "ide")
MISSIONS_FILE = os.path.join(STORE_DIR, "missions.json")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# Folders that are someone else's output, not the project's source. Listing a
# node_modules tree would bury the files that matter under tens of thousands
# that do not.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
             ".next", ".cache", ".pytest_cache", ".mypy_cache", ".idea", ".vscode"}
MAX_TREE_ENTRIES = 4000
MAX_OPEN_BYTES = 1_500_000          # the editor will not open anything bigger
MAX_CONTEXT_CHARS = 140_000         # file text handed to the model in one mission round
MAX_FILE_CONTEXT_CHARS = 40_000     # ...and from any one file
MAX_STORED_CHARS = 400_000          # before/after kept per change, for diffs and reverts
MAX_MISSIONS = 200
SNAPSHOT_TEXT_LIMIT = 300_000       # per file, when watching a CLI engine work

GEMINI_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash"]

_LOCK = threading.RLock()
_MISSIONS: dict[str, dict] = {}
_STOP: set[str] = set()
_loaded = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Paths — every one of them goes through here
# ---------------------------------------------------------------------------

class IDEError(ValueError):
    """Something the owner can read and act on; the route turns it into a 400."""


def _clean_project(project: str) -> str:
    name = (project or "").strip()
    if not name or name in (".", "..") or re.search(r"[\\/:*?\"<>|]", name):
        raise IDEError("Pick a project first.")
    return name


def project_root(project: str, create: bool = False) -> str:
    root = os.path.realpath(os.path.join(PROJECTS_ROOT, _clean_project(project)))
    if not root.startswith(os.path.realpath(PROJECTS_ROOT) + os.sep):
        raise IDEError("That project is outside Jarvis's projects folder.")
    if create:
        os.makedirs(root, exist_ok=True)
    elif not os.path.isdir(root):
        raise IDEError(f"There is no project called {project!r}.")
    return root


def resolve(project: str, rel: str, must_exist: bool = False) -> str:
    """The absolute path of `rel` inside the project, or IDEError if it would
    land anywhere else (.., absolute paths, symlinks pointing out)."""
    root = project_root(project)
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not rel:
        raise IDEError("No file path given.")
    if re.match(r"^[a-zA-Z]:", rel):
        raise IDEError("Use a path inside the project, not a drive path.")
    target = os.path.realpath(os.path.join(root, rel))
    if not (target == root or target.startswith(root + os.sep)):
        raise IDEError(f"{rel} is outside the project folder.")
    if target == root:
        raise IDEError("That is the project folder itself, not a file in it.")
    if must_exist and not os.path.exists(target):
        raise IDEError(f"{rel} does not exist.")
    return target


def _rel(project: str, path: str) -> str:
    return os.path.relpath(path, project_root(project)).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# Projects and files
# ---------------------------------------------------------------------------

def list_projects() -> list[dict]:
    if not os.path.isdir(PROJECTS_ROOT):
        return []
    out = []
    for name in sorted(os.listdir(PROJECTS_ROOT), key=str.lower):
        path = os.path.join(PROJECTS_ROOT, name)
        if os.path.isdir(path) and not name.startswith("."):
            out.append({"name": name, "modified": os.path.getmtime(path)})
    return out


def create_project(name: str) -> dict:
    project_root(name, create=True)
    return {"name": _clean_project(name)}


def tree(project: str) -> dict:
    """Flat list of the project's folders and files, in explorer order (each
    folder's subfolders first, then its files). Flat is easier on the page,
    which builds the nesting, and on the model, which reads a list of paths."""
    root = project_root(project)
    entries: list[dict] = []

    def walk(folder: str, rel: str) -> bool:
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError:
            return True
        dirs = [n for n in names if n not in SKIP_DIRS and os.path.isdir(os.path.join(folder, n))]
        files = [n for n in names if os.path.isfile(os.path.join(folder, n))]
        for d in dirs:
            path = f"{rel}{d}"
            entries.append({"path": path, "type": "dir"})
            if len(entries) >= MAX_TREE_ENTRIES or not walk(os.path.join(folder, d), path + "/"):
                return False
        for f in files:
            try:
                size = os.path.getsize(os.path.join(folder, f))
            except OSError:
                continue
            entries.append({"path": f"{rel}{f}", "type": "file", "size": size})
            if len(entries) >= MAX_TREE_ENTRIES:
                return False
        return True

    complete = walk(root, "")
    # The absolute folder is what Sections are made from (and matched against).
    return {"project": project, "root": root, "entries": entries, "truncated": not complete}


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if _looks_binary(data):
        return None
    return data.decode("utf-8", errors="replace")


def read_file(project: str, rel: str) -> dict:
    path = resolve(project, rel, must_exist=True)
    if os.path.isdir(path):
        raise IDEError(f"{rel} is a folder.")
    size = os.path.getsize(path)
    if size > MAX_OPEN_BYTES:
        return {"path": rel, "too_large": True, "size": size}
    text = _read_text(path)
    if text is None:
        return {"path": rel, "binary": True, "size": size}
    return {"path": rel, "content": text, "size": size, "hash": _hash(text)}


def _hash(text: str | None) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def write_file(project: str, rel: str, content: str, expected_hash: str | None = None) -> dict:
    """Save from the editor. `expected_hash` is the hash the editor loaded; if
    the file changed on disk since (Jarvis, a CLI, another window), the save is
    refused rather than silently throwing that change away."""
    path = resolve(project, rel)
    if os.path.isdir(path):
        raise IDEError(f"{rel} is a folder.")
    if expected_hash and os.path.exists(path):
        current = _read_text(path)
        if current is not None and _hash(current) != expected_hash:
            return {"status": "conflict", "path": rel, "content": current, "hash": _hash(current)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content or "")
    return {"status": "saved", "path": rel, "hash": _hash(content)}


def create_entry(project: str, rel: str, kind: str) -> dict:
    path = resolve(project, rel)
    if os.path.exists(path):
        raise IDEError(f"{rel} already exists.")
    if kind == "dir":
        os.makedirs(path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").close()
    return {"status": "created", "path": _rel(project, path)}


def rename_entry(project: str, rel: str, new_rel: str) -> dict:
    src = resolve(project, rel, must_exist=True)
    dst = resolve(project, new_rel)
    if os.path.exists(dst):
        raise IDEError(f"{new_rel} already exists.")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    return {"status": "renamed", "path": _rel(project, dst)}


def delete_entry(project: str, rel: str) -> dict:
    path = resolve(project, rel, must_exist=True)
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    return {"status": "deleted", "path": rel}


# ---------------------------------------------------------------------------
# Engines and models
# ---------------------------------------------------------------------------

def _settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_setting(key: str, value) -> None:
    data = _settings()
    data[key] = value
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def _ollama_config() -> tuple[str, str]:
    s = _settings()
    url = s.get("ollama_url") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434"
    model = s.get("ollama_model") or os.getenv("OLLAMA_MODEL") or "qwen2.5:3b"
    return url.rstrip("/"), model


def _ollama_models() -> list[str]:
    url, default = _ollama_config()
    try:
        import requests
        tags = requests.get(f"{url}/api/tags", timeout=1.5).json().get("models", [])
        names = [t.get("name") for t in tags if t.get("name")]
        return names or [default]
    except Exception:
        return []


def _antigravity():
    try:
        from connectors import antigravity
        return antigravity
    except Exception:
        return None


def _code_agent():
    """The Claude coding agent, when its module is in this checkout. It lives
    on its own branch for now, so its absence is normal, not an error."""
    try:
        from agents import code_agent
        return code_agent
    except Exception:
        return None


def engines() -> dict:
    """What can do a mission right now, and which model Jarvis's own engine
    would use. The page greys out what is not available and says why."""
    s = _settings()
    have_gemini = bool(os.getenv("GEMINI_API_KEY"))
    ollama = _ollama_models()
    models = []
    if have_gemini:
        models += [{"id": f"gemini:{m}", "label": m, "provider": "gemini"} for m in GEMINI_MODELS]
    models += [{"id": f"ollama:{m}", "label": f"{m} (local)", "provider": "ollama"} for m in ollama]

    default_model = s.get("ide_model") or ""
    if default_model not in {m["id"] for m in models}:
        preferred = "gemini" if (s.get("provider") or "gemini") == "gemini" and have_gemini else "ollama"
        pick = [m for m in models if m["provider"] == preferred] or models
        default_model = pick[0]["id"] if pick else ""

    out = [{
        "id": "jarvis", "label": "Jarvis",
        "available": bool(models),
        "reason": "" if models else "No model: add GEMINI_API_KEY to .env or start Ollama.",
        "detail": "Plans first, then proposes edits you accept or reject.",
    }]

    agy = _antigravity()
    agy_ok = bool(agy and agy.is_available())
    out.append({
        "id": "antigravity", "label": "Antigravity CLI", "available": agy_ok,
        "reason": "" if agy_ok else "The Antigravity CLI (agy) is not installed on this PC.",
        "detail": "Edits files itself; its commands still go to the Commands page.",
    })

    ca = _code_agent()
    if ca is None:
        claude_ok, why = False, "The Claude coding agent is not in this version of Jarvis yet."
    else:
        try:
            claude_ok, why = ca.describe_availability()
        except Exception as e:
            claude_ok, why = False, str(e)
    out.append({
        "id": "claude", "label": "Claude", "available": bool(claude_ok),
        "reason": "" if claude_ok else why,
        "detail": "Claude's coding agent, working in the project's Deliverables folder.",
    })
    engine = s.get("ide_engine") or "jarvis"
    if not any(e["id"] == engine and e["available"] for e in out):
        engine = next((e["id"] for e in out if e["available"]), "jarvis")
    return {"engines": out, "models": models, "engine": engine, "model": default_model}


def remember_choice(engine: str | None, model: str | None) -> None:
    if engine:
        _save_setting("ide_engine", engine)
    if model:
        _save_setting("ide_model", model)


def call_model(model_id: str, system: str, prompt: str) -> str:
    """One JSON-mode completion from Jarvis's own model. Tests replace this."""
    provider, _, name = (model_id or "").partition(":")
    if provider == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        resp = client.models.generate_content(
            model=name or "gemini-2.5-flash", contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system,
                                               response_mime_type="application/json",
                                               temperature=0.2))
        return resp.text or ""
    if provider == "ollama":
        from openai import OpenAI
        url, default = _ollama_config()
        client = OpenAI(base_url=f"{url}/v1", api_key="ollama")
        resp = client.chat.completions.create(
            model=name or default, temperature=0.2,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
        return resp.choices[0].message.content or ""
    raise IDEError("No model picked for Jarvis. Choose one in the engine menu.")


def parse_json(text: str) -> dict:
    """Models wrap JSON in fences or chatter often enough that this matters."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise IDEError("The model's answer was not valid JSON.")


# ---------------------------------------------------------------------------
# Missions — storage
# ---------------------------------------------------------------------------

def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(MISSIONS_FILE, "r", encoding="utf-8") as f:
            for m in json.load(f):
                # A mission that was mid-step when Jarvis closed will never
                # finish that step; say so instead of showing it working forever.
                if m.get("status") in ("planning", "working"):
                    m["status"] = "failed"
                    m["error"] = "Jarvis was closed while this mission was running."
                _MISSIONS[m["id"]] = m
    except (OSError, json.JSONDecodeError):
        pass


def _persist() -> None:
    os.makedirs(STORE_DIR, exist_ok=True)
    items = sorted(_MISSIONS.values(), key=lambda m: m["created_at"])[-MAX_MISSIONS:]
    tmp = MISSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
    os.replace(tmp, MISSIONS_FILE)


def _log(m: dict, kind: str, text: str) -> None:
    m.setdefault("log", []).append({"at": _now(), "kind": kind, "text": text})
    m["updated_at"] = _now()


def _save(m: dict) -> None:
    with _LOCK:
        m["updated_at"] = _now()
        _MISSIONS[m["id"]] = m
        _persist()


def get_mission(mission_id: str) -> dict:
    with _LOCK:
        _load()
        m = _MISSIONS.get(mission_id)
        if not m:
            raise IDEError("That mission no longer exists.")
        return m


def list_missions(project: str | None = None) -> list[dict]:
    """Newest first, without the file contents (the page asks for a diff when it
    opens one)."""
    with _LOCK:
        _load()
        items = [m for m in _MISSIONS.values() if not project or m["project"] == project]
    items.sort(key=lambda m: m["created_at"], reverse=True)
    return [summarise(m) for m in items]


def summarise(m: dict) -> dict:
    out = {k: v for k, v in m.items() if k != "changes"}
    out["changes"] = [{k: v for k, v in c.items() if k not in ("before", "after")}
                      for c in m.get("changes", [])]
    return out


# ---------------------------------------------------------------------------
# Missions — Jarvis's own engine
# ---------------------------------------------------------------------------

PLAN_SYSTEM = """You are Jarvis, working as a software engineer inside the owner's IDE on one project folder.
Before doing anything, understand the task deeply: what the owner actually wants, what already exists
in the project, and what "done" looks like. Then plan.

Reply with ONE JSON object and nothing else:
{
  "kind": "change" | "answer",
  "understanding": "one short paragraph: what you understood the owner wants and what done looks like",
  "simple": true | false,
  "answer": "only when kind is answer: the full answer to the owner's question (markdown allowed)",
  "steps": ["short imperative step", ...],
  "read": ["relative/path", ...],
  "change": ["relative/path", ...],
  "questions": ["only questions whose answer would change the work; usually none"]
}

Rules:
- kind "answer" is for questions about the code that need no edits.
- "read" lists files you need to see before editing (existing files only, from the listing).
- "change" lists files you expect to create, edit or delete.
- Only paths inside the project, relative, with forward slashes.
- If the task is not simple, the steps must include investigating the existing code first."""

EDIT_SYSTEM = """You are Jarvis, carrying out an approved plan inside the owner's IDE on one project folder.
Reply with ONE JSON object and nothing else:
{
  "edits": [
    {"path": "relative/path", "action": "write", "content": "the complete new file"},
    {"path": "relative/path", "action": "replace", "find": "exact existing text", "replace": "new text"},
    {"path": "relative/path", "action": "delete"}
  ],
  "commands": [{"command": "a shell command to run in the project folder", "why": "what it is for"}],
  "summary": "what you did and anything the owner should check, in two or three sentences"
}

Rules:
- Use "replace" for small edits to big existing files; "find" must match the file exactly, once.
- Use "write" for new files and for rewrites; give the whole file, never a fragment or placeholder.
- Commands are suggestions the owner runs with a click. Never include destructive commands.
- Stay inside the project folder. Do not invent files you were not shown unless you are creating them."""


def _listing(project: str, limit: int = 600) -> str:
    entries = [e for e in tree(project)["entries"] if e["type"] == "file"]
    lines = [f"{e['path']} ({e['size']} bytes)" for e in entries[:limit]]
    if len(entries) > limit:
        lines.append(f"... and {len(entries) - limit} more files")
    return "\n".join(lines) or "(the project folder is empty)"


def _file_context(project: str, paths: list[str]) -> str:
    parts, used = [], 0
    for rel in dict.fromkeys(p for p in paths if p):
        try:
            path = resolve(project, rel)
        except IDEError:
            continue
        if not os.path.isfile(path):
            continue
        text = _read_text(path)
        if text is None:
            continue
        if len(text) > MAX_FILE_CONTEXT_CHARS:
            text = text[:MAX_FILE_CONTEXT_CHARS] + "\n...[truncated]"
        if used + len(text) > MAX_CONTEXT_CHARS:
            parts.append(f"=== {rel} === (left out: context budget used up)")
            continue
        used += len(text)
        parts.append(f"=== {rel} ===\n{text}")
    return "\n\n".join(parts)


def _history(m: dict) -> str:
    lines = []
    for turn in m.get("conversation", []):
        lines.append(f"{turn['role'].upper()}: {turn['text']}")
    return "\n".join(lines)


def _plan_prompt(m: dict) -> str:
    ctx = m.get("context") or {}
    parts = [f"PROJECT: {m['project']}", "FILES:\n" + _listing(m["project"])]
    if ctx.get("open_file"):
        parts.append(f"THE OWNER HAS THIS FILE OPEN: {ctx['open_file']}")
        opened = _file_context(m["project"], [ctx["open_file"]])
        if opened:
            parts.append(opened)
    if ctx.get("selection"):
        parts.append("THE OWNER SELECTED:\n" + ctx["selection"][:6000])
    parts.append("CONVERSATION SO FAR:\n" + _history(m))
    return "\n\n".join(parts)


def _edit_prompt(m: dict) -> str:
    plan = m.get("plan") or {}
    wanted = list(plan.get("read", [])) + list(plan.get("change", []))
    ctx = m.get("context") or {}
    if ctx.get("open_file"):
        wanted.append(ctx["open_file"])
    parts = [
        f"PROJECT: {m['project']}",
        "FILES:\n" + _listing(m["project"]),
        "CONVERSATION:\n" + _history(m),
        "UNDERSTANDING:\n" + (plan.get("understanding") or ""),
        "APPROVED STEPS:\n" + "\n".join(f"- {s}" for s in plan.get("steps", [])),
    ]
    if m.get("plan_feedback"):
        parts.append("OWNER'S NOTES ON THE PLAN (follow these):\n" + m["plan_feedback"])
    if ctx.get("selection"):
        parts.append("THE OWNER SELECTED:\n" + ctx["selection"][:6000])
    parts.append("CURRENT FILE CONTENTS:\n" + (_file_context(m["project"], wanted) or "(none)"))
    return "\n\n".join(parts)


def _stopped(m: dict) -> bool:
    if m["id"] in _STOP:
        m["status"] = "stopped"
        _log(m, "stop", "Stopped by the owner.")
        _save(m)
        return True
    return False


def _cap(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_STORED_CHARS else None


def _stage_edits(m: dict, edits: list) -> None:
    """Turn the model's edits into reviewable changes: each gets the file as it
    is now and as it would be, so the page can draw a diff before anything is
    written."""
    project = m["project"]
    staged = {}          # path -> change, so two edits to one file stack
    for e in edits or []:
        if not isinstance(e, dict):
            continue
        rel = (e.get("path") or "").replace("\\", "/").strip().lstrip("/")
        action = e.get("action") or "write"
        try:
            path = resolve(project, rel)
        except IDEError as err:
            _log(m, "warn", f"Skipped an edit: {err}")
            continue
        change = staged.get(rel)
        if change is None:
            before = _read_text(path) if os.path.isfile(path) else None
            change = {"id": uuid.uuid4().hex[:10], "path": rel, "before": before,
                      "after": before, "status": "pending", "error": ""}
            staged[rel] = change
        if action == "delete":
            change["after"] = None
        elif action == "replace":
            current = change["after"] or ""
            find = e.get("find") or ""
            count = current.count(find) if find else 0
            if count != 1:
                change["error"] = (f"Could not apply one edit: the text to replace was "
                                   f"{'not found' if count == 0 else 'found more than once'}.")
                continue
            change["after"] = current.replace(find, e.get("replace") or "", 1)
        else:
            change["after"] = e.get("content") or ""
    changes = []
    for c in staged.values():
        if c["before"] == c["after"]:
            if c["error"]:
                _log(m, "warn", f"{c['path']}: {c['error']}")
            continue
        c["kind"] = "create" if c["before"] is None else ("delete" if c["after"] is None else "edit")
        c["stats"] = _stats(c["before"], c["after"])
        changes.append(c)
    m["changes"] = m.get("changes", []) + changes


def _stats(before: str | None, after: str | None) -> dict:
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    added = removed = 0
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return {"added": added, "removed": removed}


def _apply(m: dict, change: dict) -> None:
    path = resolve(m["project"], change["path"])
    current = _read_text(path) if os.path.isfile(path) else None
    if current != change["before"]:
        raise IDEError(f"{change['path']} changed on disk since Jarvis proposed this edit. "
                       "Reject it and ask again, or edit it by hand.")
    if change["after"] is None:
        if os.path.exists(path):
            os.remove(path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(change["after"])


def _plan_round(m: dict) -> None:
    m["status"] = "planning"
    _log(m, "phase", "Understanding the task and planning.")
    _save(m)
    raw = call_model(m["model"], PLAN_SYSTEM, _plan_prompt(m))
    plan = parse_json(raw)
    if _stopped(m):
        return
    plan["steps"] = [str(s) for s in plan.get("steps") or []]
    plan["read"] = [str(p) for p in plan.get("read") or []]
    plan["change"] = [str(p) for p in plan.get("change") or []]
    plan["questions"] = [str(q) for q in plan.get("questions") or []]
    m["plan"] = plan
    if plan.get("kind") == "answer":
        answer = plan.get("answer") or plan.get("understanding") or ""
        m["conversation"].append({"role": "jarvis", "text": answer})
        m["result"] = answer
        m["status"] = "done"
        _log(m, "answer", "Answered without changing any files.")
        _save(m)
        return
    _log(m, "plan", plan.get("understanding") or "Plan ready.")
    if m["mode"] == "review" or plan["questions"]:
        m["status"] = "awaiting_plan"
        _save(m)
        return
    _edit_round(m)


def _edit_round(m: dict) -> None:
    m["status"] = "working"
    _log(m, "phase", "Writing the changes.")
    _save(m)
    raw = call_model(m["model"], EDIT_SYSTEM, _edit_prompt(m))
    result = parse_json(raw)
    if _stopped(m):
        return
    first_new = len(m.get("changes", []))
    _stage_edits(m, result.get("edits"))
    new = m["changes"][first_new:]
    m["commands"] = m.get("commands", []) + [
        {"id": uuid.uuid4().hex[:10], "command": str(c.get("command") or "").strip(),
         "why": str(c.get("why") or ""), "status": "suggested"}
        for c in (result.get("commands") or []) if isinstance(c, dict) and c.get("command")
    ]
    summary = result.get("summary") or f"Proposed {len(new)} change(s)."
    m["conversation"].append({"role": "jarvis", "text": summary})
    m["result"] = summary
    if m["mode"] == "autopilot":
        for c in new:
            if c["error"]:
                # Part of this file's edit could not be placed; applying the
                # rest blind could leave it half-changed, so it waits for a look.
                continue
            try:
                _apply(m, c)
                c["status"] = "applied"
            except (IDEError, OSError) as err:
                c["status"] = "failed"
                c["error"] = str(err)
        held = sum(c["status"] == "pending" for c in new)
        _log(m, "applied", f"Applied {sum(c['status'] == 'applied' for c in new)} change(s)."
             + (f" {held} held for review because part of the edit did not fit." if held else ""))
    else:
        _log(m, "review", f"{len(new)} change(s) ready for review.")
    _finish_if_settled(m)
    _save(m)


def _finish_if_settled(m: dict) -> None:
    open_changes = [c for c in m.get("changes", []) if c["status"] == "pending"]
    m["status"] = "review" if open_changes else "done"


# ---------------------------------------------------------------------------
# Missions — CLI engines (they edit files themselves)
# ---------------------------------------------------------------------------

def _snapshot(root: str) -> dict:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and d != ".agents"]
        for f in filenames:
            full = os.path.join(dirpath, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            text = _read_text(full) if st.st_size <= SNAPSHOT_TEXT_LIMIT else None
            snap[os.path.relpath(full, root).replace(os.sep, "/")] = (st.st_mtime, st.st_size, text)
    return snap


def _diff_snapshots(m: dict, before: dict, after: dict) -> None:
    changes = []
    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        if b and a and b[:2] == a[:2]:
            continue
        b_text = b[2] if b else None
        a_text = a[2] if a else None
        if b and a and b_text == a_text:
            continue
        changes.append({"id": uuid.uuid4().hex[:10], "path": rel,
                        "before": _cap(b_text), "after": _cap(a_text),
                        "kind": "create" if not b else ("delete" if not a else "edit"),
                        "stats": _stats(b_text, a_text), "status": "applied", "error": ""})
    m["changes"] = m.get("changes", []) + changes


def _cli_round(m: dict) -> None:
    m["status"] = "working"
    engine = m["engine"]
    root = project_root(m["project"])
    task = _history(m)
    ctx = m.get("context") or {}
    if ctx.get("open_file"):
        task += f"\n\n(The owner has {ctx['open_file']} open in the editor.)"
    if ctx.get("selection"):
        task += "\n\nThe owner selected:\n" + ctx["selection"][:6000]
    _log(m, "phase", f"Handed to {'Antigravity' if engine == 'antigravity' else 'Claude'}.")
    _save(m)
    before = _snapshot(root)
    if engine == "antigravity":
        agy = _antigravity()
        if not agy:
            raise IDEError("The Antigravity connector could not be loaded.")
        result = agy.run(task, m["project"])
    else:
        ca = _code_agent()
        if not ca:
            raise IDEError("The Claude coding agent is not in this version of Jarvis.")
        result = ca.run_coding_task(m["project"], "ide", task)
    _diff_snapshots(m, before, _snapshot(root))
    text = (result.get("output") or result.get("summary") or result.get("error")
            or "Finished.")
    m["conversation"].append({"role": "jarvis", "text": text})
    m["result"] = text
    if result.get("status") not in ("success", "ok"):
        m["status"] = "failed"
        m["error"] = result.get("error") or result.get("note") or "The engine reported a failure."
        _log(m, "error", m["error"])
    else:
        if result.get("note"):
            _log(m, "warn", result["note"])
        m["status"] = "done"
        _log(m, "applied", f"{len(m['changes'])} file(s) changed.")
    _save(m)


# ---------------------------------------------------------------------------
# Missions — public actions
# ---------------------------------------------------------------------------

def _run(m: dict, step) -> None:
    def worker():
        try:
            step(m)
        except Exception as err:          # anything, so a mission never hangs "working"
            m["status"] = "failed"
            m["error"] = str(err)
            _log(m, "error", str(err))
            _save(m)
        finally:
            _STOP.discard(m["id"])
    threading.Thread(target=worker, daemon=True, name=f"ide-mission-{m['id']}").start()


def _title(prompt: str) -> str:
    line = prompt.splitlines()[0].strip()
    if len(line) <= 80:
        return line
    cut = line[:80].rsplit(" ", 1)[0]
    return (cut if len(cut) > 40 else line[:80]).rstrip(" ,.;:") + "…"


def start_mission(project: str, prompt: str, engine: str = "jarvis", model: str = "",
                  mode: str = "review", context: dict | None = None, background: bool = True) -> dict:
    project_root(project)
    prompt = (prompt or "").strip()
    if not prompt:
        raise IDEError("Tell Jarvis what you want done.")
    if engine not in ("jarvis", "antigravity", "claude"):
        raise IDEError("Unknown engine.")
    if mode not in ("review", "autopilot"):
        mode = "review"
    if engine == "jarvis" and not model:
        model = engines()["model"]
    remember_choice(engine, model if engine == "jarvis" else None)
    m = {
        "id": uuid.uuid4().hex[:12], "project": project, "engine": engine, "model": model,
        "mode": mode, "title": _title(prompt), "status": "planning",
        "created_at": _now(), "updated_at": _now(),
        "conversation": [{"role": "owner", "text": prompt}],
        "context": {k: v for k, v in (context or {}).items() if k in ("open_file", "selection") and v},
        "plan": None, "changes": [], "commands": [], "log": [], "result": "", "error": "",
    }
    _save(m)
    step = _plan_round if engine == "jarvis" else _cli_round
    if background:
        _run(m, step)
    else:
        step(m)
    return summarise(m)


def approve_plan(mission_id: str, feedback: str = "", background: bool = True) -> dict:
    m = get_mission(mission_id)
    if m["status"] != "awaiting_plan":
        raise IDEError("This mission is not waiting on its plan.")
    m["plan_feedback"] = (feedback or "").strip()
    if m["plan_feedback"]:
        m["conversation"].append({"role": "owner", "text": m["plan_feedback"]})
    _log(m, "approved", "Plan approved.")
    if background:
        _run(m, _edit_round)
    else:
        _edit_round(m)
    return summarise(m)


def follow_up(mission_id: str, message: str, context: dict | None = None,
              background: bool = True) -> dict:
    """Keep talking to Jarvis on the same mission: another plan-and-edit round
    that knows everything said so far."""
    m = get_mission(mission_id)
    if m["status"] in ("planning", "working"):
        raise IDEError("Jarvis is still working on this mission. Stop it first or wait.")
    message = (message or "").strip()
    if not message:
        raise IDEError("Say what you want next.")
    m["conversation"].append({"role": "owner", "text": message})
    if context is not None:
        m["context"] = {k: v for k, v in context.items() if k in ("open_file", "selection") and v}
    m["error"] = ""
    step = _plan_round if m["engine"] == "jarvis" else _cli_round
    if background:
        _run(m, step)
    else:
        step(m)
    return summarise(m)


def stop_mission(mission_id: str) -> dict:
    m = get_mission(mission_id)
    if m["status"] in ("planning", "working"):
        _STOP.add(mission_id)
        _log(m, "stop", "Stop asked for; Jarvis stops after the step in progress.")
    elif m["status"] == "awaiting_plan":
        m["status"] = "stopped"
        _log(m, "stop", "Stopped by the owner.")
    _save(m)
    return summarise(m)


def get_change(mission_id: str, change_id: str) -> dict:
    m = get_mission(mission_id)
    for c in m.get("changes", []):
        if c["id"] == change_id:
            return dict(c)
    raise IDEError("That change is not on this mission.")


def decide_change(mission_id: str, change_id: str, decision: str) -> dict:
    """accept / reject a proposed change, or revert one already applied."""
    done = {"accept": "Accepted", "reject": "Rejected", "revert": "Reverted"}.get(decision)
    if not done:
        raise IDEError("Decide with accept, reject or revert.")
    m = get_mission(mission_id)
    with _LOCK:
        targets = [c for c in m.get("changes", [])
                   if change_id in ("all", c["id"])]
        if not targets:
            raise IDEError("That change is not on this mission.")
        errors = []
        for c in targets:
            try:
                if decision == "accept" and c["status"] == "pending":
                    _apply(m, c)
                    c["status"] = "accepted"
                elif decision == "reject" and c["status"] == "pending":
                    c["status"] = "rejected"
                elif decision == "revert" and c["status"] in ("accepted", "applied"):
                    if c["before"] is None and c["kind"] != "create":
                        raise IDEError(f"{c['path']} was too big to keep a copy of, so it "
                                       "cannot be reverted here.")
                    _apply(m, {"path": c["path"], "before": c["after"], "after": c["before"]})
                    c["status"] = "reverted"
            except (IDEError, OSError) as err:
                errors.append(str(err))
        if m["status"] in ("review", "done"):
            _finish_if_settled(m)
        _log(m, decision, f"{done} {len(targets) - len(errors)} change(s).")
        _save(m)
    out = summarise(m)
    if errors:
        out["errors"] = errors
    return out


def mark_command(mission_id: str, command_id: str, job_id: str) -> None:
    try:
        m = get_mission(mission_id)
    except IDEError:
        return
    for c in m.get("commands", []):
        if c["id"] == command_id:
            c["status"] = "ran"
            c["job_id"] = job_id
    _save(m)


def delete_mission(mission_id: str) -> None:
    with _LOCK:
        _load()
        _MISSIONS.pop(mission_id, None)
        _persist()


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
TERMINAL_TIMEOUT = int(os.getenv("JARVIS_IDE_COMMAND_TIMEOUT", "1800"))
MAX_JOB_OUTPUT = 400_000


def run_command(project: str, command: str, cwd_rel: str = "") -> dict:
    """Run one command in the project folder and stream its output.

    The owner typing (or clicking Run on a command Jarvis suggested) is the
    approval, so this does not go through the Commands page — but the hard
    denylist still applies, because a mis-click on those is not recoverable.
    """
    import command_gate
    command = (command or "").strip()
    if not command:
        raise IDEError("Type a command.")
    blocked = command_gate.denied_reason(command)
    if blocked:
        raise IDEError(f"Refused: {blocked} Run it outside Jarvis if you really mean to.")
    cwd = project_root(project)
    if cwd_rel:
        cwd = resolve(project, cwd_rel, must_exist=True)
        if not os.path.isdir(cwd):
            raise IDEError(f"{cwd_rel} is not a folder.")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("GEMINI_API_KEY", None)       # the project's scripts do not need Jarvis's keys
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen(
        command, shell=True, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
    )
    job = {"id": uuid.uuid4().hex[:10], "project": project, "command": command,
           "cwd": os.path.relpath(cwd, project_root(project)).replace(os.sep, "/"),
           "started": time.time(), "output": "", "exit_code": None, "proc": proc}
    _JOBS[job["id"]] = job

    def pump():
        try:
            for line in proc.stdout:
                job["output"] += line
                if len(job["output"]) > MAX_JOB_OUTPUT:
                    job["output"] = "[...earlier output trimmed...]\n" + job["output"][-MAX_JOB_OUTPUT // 2:]
        finally:
            try:
                job["exit_code"] = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def watchdog():
        try:
            proc.wait(timeout=TERMINAL_TIMEOUT)
        except subprocess.TimeoutExpired:
            job["output"] += f"\n[Stopped after {TERMINAL_TIMEOUT // 60} minutes.]\n"
            _kill(proc)

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    return _job_view(job, 0)


def _kill(proc) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            proc.kill()
    except Exception:
        pass


def _job_view(job: dict, since: int) -> dict:
    out = job["output"]
    return {"id": job["id"], "command": job["command"], "cwd": job["cwd"],
            "output": out[since:] if since <= len(out) else out, "offset": len(out),
            "running": job["exit_code"] is None and job["proc"].poll() is None,
            "exit_code": job["exit_code"] if job["exit_code"] is not None else job["proc"].poll()}


def job_output(job_id: str, since: int = 0) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise IDEError("That command is no longer tracked.")
    return _job_view(job, since)


def stop_job(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise IDEError("That command is no longer tracked.")
    if job["proc"].poll() is None:
        _kill(job["proc"])
        job["output"] += "\n[Stopped.]\n"
    return _job_view(job, 0)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

blueprint = Blueprint("ide", __name__)

_LOCAL = {"127.0.0.1", "::1", "localhost"}


@blueprint.before_request
def _local_only():
    """The Workbench reads, writes and runs things on this PC. Jarvis's server
    listens on every interface by default, so these routes answer only this
    PC unless the owner deliberately opens them up."""
    if os.getenv("JARVIS_IDE_ALLOW_REMOTE") == "1":
        return None
    if (request.remote_addr or "") not in _LOCAL:
        return jsonify({"error": "The Workbench only answers on this PC."}), 403
    return None


@blueprint.errorhandler(IDEError)
def _ide_error(err):
    return jsonify({"error": str(err)}), 400


def _body() -> dict:
    return request.get_json(silent=True) or {}


@blueprint.route("/ide/projects", methods=["GET"])
def r_projects():
    return jsonify({"projects": list_projects()})


@blueprint.route("/ide/projects", methods=["POST"])
def r_create_project():
    return jsonify(create_project(_body().get("name", "")))


@blueprint.route("/ide/engines", methods=["GET"])
def r_engines():
    return jsonify(engines())


@blueprint.route("/ide/tree", methods=["GET"])
def r_tree():
    return jsonify(tree(request.args.get("project", "")))


@blueprint.route("/ide/file", methods=["GET"])
def r_read():
    return jsonify(read_file(request.args.get("project", ""), request.args.get("path", "")))


@blueprint.route("/ide/file", methods=["POST"])
def r_write():
    b = _body()
    return jsonify(write_file(b.get("project", ""), b.get("path", ""), b.get("content", ""),
                              b.get("hash")))


@blueprint.route("/ide/entry", methods=["POST"])
def r_entry():
    b = _body()
    op, project = b.get("op"), b.get("project", "")
    if op == "create":
        return jsonify(create_entry(project, b.get("path", ""), b.get("kind", "file")))
    if op == "rename":
        return jsonify(rename_entry(project, b.get("path", ""), b.get("new_path", "")))
    if op == "delete":
        return jsonify(delete_entry(project, b.get("path", "")))
    raise IDEError("Unknown file operation.")


@blueprint.route("/ide/missions", methods=["GET"])
def r_missions():
    return jsonify({"missions": list_missions(request.args.get("project") or None)})


@blueprint.route("/ide/missions", methods=["POST"])
def r_start():
    b = _body()
    return jsonify(start_mission(b.get("project", ""), b.get("prompt", ""),
                                 b.get("engine", "jarvis"), b.get("model", ""),
                                 b.get("mode", "review"), b.get("context")))


@blueprint.route("/ide/missions/<mission_id>", methods=["GET"])
def r_mission(mission_id):
    return jsonify(summarise(get_mission(mission_id)))


@blueprint.route("/ide/missions/<mission_id>", methods=["DELETE"])
def r_delete_mission(mission_id):
    delete_mission(mission_id)
    return jsonify({"status": "deleted"})


@blueprint.route("/ide/missions/<mission_id>/approve", methods=["POST"])
def r_approve(mission_id):
    return jsonify(approve_plan(mission_id, _body().get("feedback", "")))


@blueprint.route("/ide/missions/<mission_id>/followup", methods=["POST"])
def r_followup(mission_id):
    b = _body()
    return jsonify(follow_up(mission_id, b.get("message", ""), b.get("context")))


@blueprint.route("/ide/missions/<mission_id>/stop", methods=["POST"])
def r_stop(mission_id):
    return jsonify(stop_mission(mission_id))


@blueprint.route("/ide/missions/<mission_id>/changes/<change_id>", methods=["GET"])
def r_change(mission_id, change_id):
    return jsonify(get_change(mission_id, change_id))


@blueprint.route("/ide/missions/<mission_id>/changes/<change_id>", methods=["POST"])
def r_decide(mission_id, change_id):
    return jsonify(decide_change(mission_id, change_id, _body().get("decision", "")))


@blueprint.route("/ide/terminal", methods=["POST"])
def r_run():
    b = _body()
    job = run_command(b.get("project", ""), b.get("command", ""), b.get("cwd", ""))
    if b.get("mission_id") and b.get("command_id"):
        mark_command(b["mission_id"], b["command_id"], job["id"])
    return jsonify(job)


@blueprint.route("/ide/terminal/<job_id>", methods=["GET"])
def r_job(job_id):
    return jsonify(job_output(job_id, int(request.args.get("since", "0") or 0)))


@blueprint.route("/ide/terminal/<job_id>/stop", methods=["POST"])
def r_stop_job(job_id):
    return jsonify(stop_job(job_id))
