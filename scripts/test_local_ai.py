"""Jarvis finds local AI apps on his own.

  * A running Ollama and a running LM Studio are found by their ports, and
    their models show up with ids "ollama:<name>" and "lmstudio:<name>".
  * Something else answering on a local AI port is not mistaken for one.
  * Ollama installed but closed: its downloaded models are read from its
    models folder and still offered, since Jarvis can start it.
  * LM Studio installed but closed: listed with how to start it, no models.
  * A new app or model is announced once; the first look only records.
  * The IDE's model menu and the chat's local model use what was found, and
    calling an LM Studio model goes to LM Studio's address.

No real servers: small HTTP servers on free ports stand in.
"""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["JARVIS_PORT"] = "5999"

from connectors import local_ai as la


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


def serve(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            if body is None:
                self.send_response(404); self.end_headers(); return
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


tmp = tempfile.mkdtemp(prefix="jarvis_localai_")
home = os.path.join(tmp, "home")
os.makedirs(home)
os.environ["HOME"] = home
os.environ["USERPROFILE"] = home
os.environ["LOCALAPPDATA"] = os.path.join(home, "AppData", "Local")
os.environ["PATH"] = os.path.join(tmp, "nobin")
la.SETTINGS_PATH = os.path.join(tmp, "settings.json")
la.KNOWN_PATH = os.path.join(tmp, "known.json")

ollama_models = {"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.2:latest"}]}
ollama_srv, ollama_url = serve({"/api/tags": ollama_models})
lm_models = {"data": [{"id": "qwen2.5-7b-instruct"}, {"id": "text-embedding-nomic-embed-text-v1.5"}]}
lm_srv, lm_url = serve({"/v1/models": lm_models})
other_srv, other_url = serve({"/v1/models": b"<html>hello</html>"})
closed_port = "http://127.0.0.1:9"


def set_ports(ollama=closed_port, lm=closed_port, jan=closed_port):
    ports = {"ollama": ollama, "lmstudio": lm, "jan": jan}
    for r in la.RUNTIMES:
        r["url"] = ports.get(r["id"], closed_port) + "/v1"
    with open(la.SETTINGS_PATH, "w") as f:
        json.dump({"ollama_url": ollama, "ollama_model": "qwen2.5:3b"}, f)


said = []
la.on_change(said.append)

# --- nothing at all -------------------------------------------------------
set_ports()
r = la.detect(refresh=True)
check("nothing installed: no apps, no models", r["runtimes"] == [] and r["models"] == [])
check("first look announces nothing", said == [])

# --- Ollama and LM Studio running ------------------------------------------
set_ports(ollama=ollama_url, lm=lm_url, jan=other_url)
r = la.detect(refresh=True)
ids = [m["id"] for m in r["models"]]
check("Ollama's models found", "ollama:qwen2.5:3b" in ids and "ollama:llama3.2:latest" in ids)
check("LM Studio's chat model found", "lmstudio:qwen2.5-7b-instruct" in ids)
check("LM Studio's embedding model left out", not any("embed" in i for i in ids))
check("an unrelated program on Jan's port is not taken for Jan",
      "jan" not in [x["id"] for x in r["runtimes"]])
check("both apps announced", any("Ollama" in s for s in said) and any("LM Studio" in s for s in said))

n = len(said)
la.detect(refresh=True)
check("nothing new: no second announcement", len(said) == n)

lm_models["data"].append({"id": "mistral-7b-instruct"})
la.detect(refresh=True)
check("a new LM Studio model is announced",
      len(said) == n + 1 and "mistral-7b-instruct" in said[-1])

check("endpoint of an LM Studio model is LM Studio's address",
      la.endpoint("lmstudio:qwen2.5-7b-instruct") == (lm_url + "/v1", "qwen2.5-7b-instruct"))
check("endpoint keeps colons in Ollama names",
      la.endpoint("ollama:qwen2.5:3b") == (ollama_url + "/v1", "qwen2.5:3b"))

# --- the chat's pick --------------------------------------------------------
check("chat uses the model chosen in settings",
      la.pick_chat_model("lmstudio:mistral-7b-instruct") == "lmstudio:mistral-7b-instruct")
check("chat falls back to the configured Ollama model", la.pick_chat_model("gone:x") == "ollama:qwen2.5:3b")
set_ports(lm=lm_url)
la.detect(refresh=True)
check("no Ollama: chat uses LM Studio", la.pick_chat_model("") == "lmstudio:qwen2.5-7b-instruct")

# --- installed but closed ---------------------------------------------------
man = os.path.join(home, ".ollama", "models", "manifests", "registry.ollama.ai")
for ns, model, tag in [("library", "qwen2.5", "3b"), ("library", "gemma3", "4b"), ("someone", "coder", "q4")]:
    os.makedirs(os.path.join(man, ns, model), exist_ok=True)
    open(os.path.join(man, ns, model, tag), "w").write("{}")
check("Ollama's downloaded models read from disk",
      la.ollama_models_on_disk() == ["gemma3:4b", "qwen2.5:3b", "someone/coder:q4"])

ollama_exe = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Ollama", "ollama.exe")
os.makedirs(os.path.dirname(ollama_exe))
open(ollama_exe, "w").close()
lms_dir = os.path.join(home, ".lmstudio")
os.makedirs(lms_dir)
set_ports()
r = la.detect(refresh=True)
apps = {x["id"]: x for x in r["runtimes"]}
check("closed Ollama is listed as installed", apps.get("ollama", {}).get("running") is False)
check("closed Ollama's models are still offered",
      "ollama:gemma3:4b" in [m["id"] for m in r["models"]])
check("closed LM Studio is listed with how to start it",
      "lmstudio" in apps and "start" in apps["lmstudio"]["note"].lower())
check("closed LM Studio offers no model it can't reach",
      not any(m["id"].startswith("lmstudio:") for m in r["models"]))

# --- the IDE's model menu ---------------------------------------------------
import ide
ide.SETTINGS_PATH = la.SETTINGS_PATH
set_ports(ollama=ollama_url, lm=lm_url)
eng = ide.engines(refresh=True)
mids = [m["id"] for m in eng["models"]]
check("IDE menu lists Ollama and LM Studio models",
      "ollama:qwen2.5:3b" in mids and "lmstudio:qwen2.5-7b-instruct" in mids)
check("Jarvis engine is available with only local AI",
      {e["id"]: e for e in eng["engines"]}["jarvis"]["available"])

calls = []


class FakeCompletions:
    def create(self, **kw):
        calls.append(kw)
        if "response_format" in kw:
            raise Exception("'response_format.type' must be 'json_schema'")
        msg = type("M", (), {"content": '{"ok": true}'})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


class FakeOpenAI:
    def __init__(self, base_url, api_key):
        calls.append({"base_url": base_url})
        self.chat = type("Chat", (), {"completions": FakeCompletions()})


import openai
openai.OpenAI = FakeOpenAI
out = ide.call_model("lmstudio:qwen2.5-7b-instruct", "sys", "hi")
check("an LM Studio model is called at LM Studio's address", calls[0]["base_url"] == lm_url + "/v1")
check("JSON mode refused: asked again without it", out == '{"ok": true}' and "response_format" not in calls[-1])

try:
    ide.call_model("jan:some-model", "sys", "hi")
    check("a closed app gives a clear error", False)
except ide.IDEError as e:
    check("a closed app gives a clear error", "not running" in str(e))

for s in (ollama_srv, lm_srv, other_srv):
    s.shutdown()
print("All local AI detection checks passed")
