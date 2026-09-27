"""Finds the local AI apps on this PC and the models they have.

Jarvis looks for them on his own: at startup, every half minute while he runs,
and whenever a model menu opens. Nothing has to be configured first.

  * Running servers are found by asking the usual local ports what models they
    serve (Ollama, LM Studio, Jan, GPT4All, llama.cpp / LocalAI, KoboldCpp,
    text-generation-webui, vLLM). Only an answer shaped like a model list
    counts, so an unrelated program on the same port is not mistaken for one.
  * Installed but closed apps are found on disk (Ollama and LM Studio), so
    Jarvis can say "LM Studio is installed, start its server" instead of
    nothing. Ollama's downloaded models are read from its models folder, and
    Jarvis starts Ollama himself when one of them is picked.
  * Extra servers can be listed in settings.json as
    "local_ai_servers": [{"name": "My box", "url": "http://192.168.1.5:8080/v1"}].

Every model gets an id "<runtime>:<model>", e.g. "ollama:qwen2.5:3b" or
"lmstudio:qwen2.5-7b-instruct", which the IDE and the chat use to call it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
KNOWN_PATH = os.path.join(ROOT, "data", "local_ai_known.json")

CACHE_SECONDS = 10
WATCH_SECONDS = 30
PROBE_TIMEOUT = 0.6

# id, label, default base URL (OpenAI-style, ends in /v1), how to start it.
RUNTIMES = [
    {"id": "ollama", "label": "Ollama", "url": "http://127.0.0.1:11434/v1",
     "start": "Jarvis starts it for you when you pick one of its models."},
    {"id": "lmstudio", "label": "LM Studio", "url": "http://127.0.0.1:1234/v1",
     "start": "Open LM Studio and start its server (Developer tab), or run: lms server start"},
    {"id": "jan", "label": "Jan", "url": "http://127.0.0.1:1337/v1",
     "start": "Open Jan and turn on its Local API Server in Settings."},
    {"id": "gpt4all", "label": "GPT4All", "url": "http://127.0.0.1:4891/v1",
     "start": "Open GPT4All and enable its API server in Settings."},
    {"id": "llamacpp", "label": "llama.cpp / LocalAI", "url": "http://127.0.0.1:8080/v1",
     "start": "Start llama-server (or LocalAI) on port 8080."},
    {"id": "koboldcpp", "label": "KoboldCpp", "url": "http://127.0.0.1:5001/v1",
     "start": "Start KoboldCpp."},
    {"id": "textgen", "label": "text-generation-webui", "url": "http://127.0.0.1:5000/v1",
     "start": "Start text-generation-webui with --api."},
    {"id": "vllm", "label": "vLLM", "url": "http://127.0.0.1:8000/v1",
     "start": "Start vLLM's OpenAI server."},
]
RUNTIME_IDS = {r["id"] for r in RUNTIMES}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "result": None}
_listeners: list = []
_watcher_started = False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _ollama_base(s: dict) -> str:
    url = (s.get("ollama_url") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _jarvis_port() -> int:
    try:
        return int(os.getenv("JARVIS_PORT", "5000"))
    except ValueError:
        return 5000


def _runtimes(s: dict) -> list[dict]:
    """The built-in list plus any servers named in settings. A default port that
    Jarvis himself is using is skipped: asking himself would find nothing."""
    out = []
    own = f":{_jarvis_port()}/"
    for r in RUNTIMES:
        r = dict(r)
        if r["id"] == "ollama":
            r["url"] = _ollama_base(s) + "/v1"
        elif own in r["url"] + "/":
            continue
        out.append(r)
    for i, extra in enumerate(s.get("local_ai_servers") or []):
        url = str((extra or {}).get("url") or "").rstrip("/")
        if not url:
            continue
        if not url.endswith("/v1"):
            url += "/v1"
        out.append({"id": f"custom{i + 1}", "label": str(extra.get("name") or url),
                    "url": url, "start": "Start this server."})
    return out


# ---------------------------------------------------------------------------
# Looking on disk
# ---------------------------------------------------------------------------

def _home() -> str:
    return os.path.expanduser("~")


def _local_appdata() -> str:
    return os.getenv("LOCALAPPDATA") or os.path.join(_home(), "AppData", "Local")


def _first_existing(paths) -> str:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ""


def ollama_executable() -> str:
    found = shutil.which("ollama")
    if found:
        return found
    return _first_existing([
        os.path.join(_local_appdata(), "Programs", "Ollama", "ollama.exe"),
        "/usr/local/bin/ollama", "/usr/bin/ollama", "/opt/homebrew/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
    ])


def lms_executable() -> str:
    found = shutil.which("lms")
    if found:
        return found
    exe = "lms.exe" if sys.platform == "win32" else "lms"
    return _first_existing([os.path.join(_home(), ".lmstudio", "bin", exe),
                            os.path.join(_home(), ".cache", "lm-studio", "bin", exe)])


def _lmstudio_installed() -> str:
    return lms_executable() or _first_existing([
        os.path.join(_local_appdata(), "Programs", "LM Studio", "LM Studio.exe"),
        os.path.join(_local_appdata(), "Programs", "lm-studio", "LM Studio.exe"),
        "/Applications/LM Studio.app",
        os.path.join(_home(), ".lmstudio"),
    ])


def _jan_installed() -> str:
    return _first_existing([os.path.join(_local_appdata(), "Programs", "jan", "Jan.exe"),
                            os.path.join(_local_appdata(), "Programs", "Jan", "Jan.exe"),
                            "/Applications/Jan.app"])


def _gpt4all_installed() -> str:
    return _first_existing([os.path.join(_home(), "gpt4all", "bin", "chat.exe"),
                            os.path.join(_home(), "gpt4all", "bin", "chat"),
                            "/Applications/gpt4all/bin/gpt4all.app"])


def ollama_models_on_disk() -> list[str]:
    """Model names Ollama has downloaded, read from its manifests folder, so
    they are known even while Ollama is closed. Layout:
    <models>/manifests/<registry>/<namespace>/<model>/<tag>."""
    base = os.getenv("OLLAMA_MODELS") or os.path.join(_home(), ".ollama", "models")
    root = os.path.join(base, "manifests")
    names = []
    if not os.path.isdir(root):
        return names
    for registry in sorted(os.listdir(root)):
        rdir = os.path.join(root, registry)
        if not os.path.isdir(rdir):
            continue
        for ns in sorted(os.listdir(rdir)):
            ndir = os.path.join(rdir, ns)
            if not os.path.isdir(ndir):
                continue
            for model in sorted(os.listdir(ndir)):
                mdir = os.path.join(ndir, model)
                if not os.path.isdir(mdir):
                    continue
                for tag in sorted(os.listdir(mdir)):
                    if not os.path.isfile(os.path.join(mdir, tag)):
                        continue
                    name = f"{model}:{tag}"
                    if ns != "library":
                        name = f"{ns}/{name}"
                    if registry != "registry.ollama.ai":
                        name = f"{registry}/{name}"
                    names.append(name)
    return names


_INSTALLED = {
    "ollama": ollama_executable,
    "lmstudio": _lmstudio_installed,
    "jan": _jan_installed,
    "gpt4all": _gpt4all_installed,
}


# ---------------------------------------------------------------------------
# Asking running servers
# ---------------------------------------------------------------------------

def _get_json(url: str):
    import requests
    resp = requests.get(url, timeout=PROBE_TIMEOUT)
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _probe(runtime: dict) -> list[str] | None:
    """The models a running server offers, or None when nothing answered like
    a model server. An empty list means it runs but has no model."""
    try:
        if runtime["id"] == "ollama":
            data = _get_json(runtime["url"][:-3] + "/api/tags")
            if isinstance(data, dict) and isinstance(data.get("models"), list):
                return [m["name"] for m in data["models"] if isinstance(m, dict) and m.get("name")]
            return None
        data = _get_json(runtime["url"] + "/models")
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            ids = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            # LM Studio lists embedding models too; they can't chat.
            return [i for i in ids if "embed" not in i.lower()]
    except Exception:
        return None
    return None


def _scan() -> dict:
    s = _settings()
    runtimes = _runtimes(s)
    with ThreadPoolExecutor(max_workers=len(runtimes) or 1) as pool:
        answers = list(pool.map(_probe, runtimes))

    found, models = [], []
    for r, served in zip(runtimes, answers):
        running = served is not None
        installed_at = ""
        finder = _INSTALLED.get(r["id"])
        if finder:
            try:
                installed_at = finder() or ""
            except Exception:
                installed_at = ""
        names = list(served or [])
        on_disk = False
        if r["id"] == "ollama" and not running and installed_at:
            names = ollama_models_on_disk()
            on_disk = True
        if not running and not installed_at:
            continue
        entry = {
            "id": r["id"], "label": r["label"], "url": r["url"],
            "running": running, "installed": bool(installed_at or running),
            "models": names,
        }
        if not running:
            entry["note"] = f"{r['label']} is installed but not running. {r['start']}"
        elif not names:
            entry["note"] = f"{r['label']} is running but has no model loaded or downloaded."
        found.append(entry)
        # A closed app's models can only be used if Jarvis can start it himself.
        usable = running or (r["id"] == "ollama" and on_disk)
        if usable:
            suffix = "local" if running else "local, starts Ollama"
            models += [{"id": f"{r['id']}:{n}", "label": f"{n} ({r['label']}, {suffix})"
                        if r["id"] != "ollama" else f"{n} ({suffix})",
                        "provider": r["id"], "runtime": r["label"], "running": running}
                       for n in names]
    return {"runtimes": found, "models": models, "checked_at": time.time()}


def detect(refresh: bool = False) -> dict:
    """What local AI this PC has right now. Cached for a few seconds so several
    menus opening at once cost one look."""
    with _lock:
        if not refresh and _cache["result"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["result"]
    result = _scan()
    with _lock:
        _cache.update(at=time.time(), result=result)
    _announce(result)
    return result


def models(refresh: bool = False) -> list[dict]:
    return detect(refresh)["models"]


# ---------------------------------------------------------------------------
# Noticing new ones
# ---------------------------------------------------------------------------

def _load_known() -> dict | None:
    try:
        with open(KNOWN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_known(known: dict) -> None:
    try:
        os.makedirs(os.path.dirname(KNOWN_PATH), exist_ok=True)
        with open(KNOWN_PATH, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=2)
    except OSError:
        pass


def describe_changes(before: dict, after: dict) -> list[str]:
    """Plain sentences about apps or models that appeared since last time."""
    lines = []
    for rid, info in after.items():
        old = before.get(rid)
        label = info["label"]
        if old is None:
            n = len(info["models"])
            what = f" with {n} model{'s' if n != 1 else ''}" if n else ""
            lines.append(f"I found {label} on this PC{what}. Its models are now in the model menus.")
            continue
        if info["running"] and not old.get("running"):
            lines.append(f"{label} is running now.")
        new = [m for m in info["models"] if m not in old.get("models", [])]
        if new:
            shown = ", ".join(new[:3]) + (f" and {len(new) - 3} more" if len(new) > 3 else "")
            lines.append(f"{label} has new model{'s' if len(new) > 1 else ''}: {shown}.")
    return lines


def _announce(result: dict) -> None:
    after = {r["id"]: {"label": r["label"], "running": r["running"], "models": r["models"]}
             for r in result["runtimes"]}
    with _lock:
        before = _load_known()
        if before == after:
            return
        _save_known(after)
    # The very first look records what is there without announcing it all.
    if before is None:
        return
    for line in describe_changes(before, after):
        for fn in list(_listeners):
            try:
                fn(line)
            except Exception:
                pass


def on_change(fn) -> None:
    """fn(sentence) is called when a new local AI app or model shows up."""
    _listeners.append(fn)


def start_watcher(interval: int = WATCH_SECONDS) -> None:
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True

    def loop():
        while True:
            try:
                detect(refresh=True)
            except Exception as e:
                print(f"[Local AI] Check failed: {e}")
            time.sleep(interval)

    threading.Thread(target=loop, name="local-ai-watcher", daemon=True).start()


# ---------------------------------------------------------------------------
# Using a model
# ---------------------------------------------------------------------------

def endpoint(model_id: str) -> tuple[str, str]:
    """(OpenAI base URL, model name) for a "<runtime>:<model>" id."""
    rid, _, name = (model_id or "").partition(":")
    for r in _runtimes(_settings()):
        if r["id"] == rid:
            return r["url"], name
    raise ValueError(f"Unknown local AI '{rid}'.")


def is_local(model_id: str) -> bool:
    rid = (model_id or "").partition(":")[0]
    return rid in RUNTIME_IDS or rid.startswith("custom")


def start_ollama(wait: float = 20.0) -> bool:
    """Start Ollama if it is installed and not answering. True once it answers."""
    s = _settings()
    base = _ollama_base(s)
    runtime = {"id": "ollama", "url": base + "/v1"}
    if _probe(runtime) is not None:
        return True
    exe = ollama_executable()
    if not exe:
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags)
    except OSError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.5)
        if _probe(runtime) is not None:
            return True
    return False


def ensure_ready(model_id: str) -> None:
    """Make sure the server behind a local model answers before calling it."""
    rid = (model_id or "").partition(":")[0]
    if rid == "ollama":
        if not start_ollama():
            raise RuntimeError("Ollama is not running and could not be started.")
        return
    base, _ = endpoint(model_id)
    if _probe({"id": rid, "url": base}) is None:
        note = next((r["start"] for r in RUNTIMES if r["id"] == rid), "Start it.")
        raise RuntimeError(f"That local AI is not running. {note}")


def pick_chat_model(preferred: str = "") -> str:
    """The local model Jarvis's chat should use: the one chosen in settings if
    it is still there, else the configured Ollama model, else any running
    local model. Empty when this PC has none."""
    available = models()
    ids = [m["id"] for m in available]
    if preferred in ids:
        return preferred
    s = _settings()
    ollama_default = "ollama:" + (s.get("ollama_model") or os.getenv("OLLAMA_MODEL") or "qwen2.5:3b")
    if ollama_default in ids:
        return ollama_default
    running = [m["id"] for m in available if m.get("running")]
    return (running or ids or [""])[0]
