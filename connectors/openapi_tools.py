"""
Turn a connected API into tools, from its published description.

Connecting an API used to store its key and mark it "up", and that was all: no
agent could call it unless someone hand-wrote a handler in tool_executor.
YouTube, Supadata and Semantic Scholar sat connected and unusable that way.

Now connecting an API looks for a machine-readable description of it, in this
order:

  1. a spec URL the user gave in the Control room (kept in api_registry.json);
  2. a known spec for well-known services (KNOWN_SPECS);
  3. the usual places on the API's base URL (/openapi.json, /swagger.json, ...);
  4. a web search for the official spec, trying each link it returns.

Three formats are understood: OpenAPI 3, Swagger 2, and Google's API Discovery
documents (YouTube, Drive, Gmail...). Each operation becomes one tool with its
parameters as arguments, and one generic handler makes the real HTTP call with
the credentials the Control room already saved. The tools then go through the
same research, review and Control room checks as an MCP server's.

If no description can be found, the API stays connected with no tools, and the
Control room says so and asks for the spec's address.
"""
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests

FETCH_TIMEOUT = 20
CALL_TIMEOUT = 30
MAX_OPERATIONS = 100          # past this, the busiest APIs would flood the review
MAX_RESULT_CHARS = 20000
WELL_KNOWN_PATHS = ("/openapi.json", "/swagger.json", "/.well-known/openapi.json", "/v1/openapi.json",
                    "/api/openapi.json", "/v3/api-docs", "/swagger/v1/swagger.json")

# Specs for services Jarvis already knows by name. YouTube's is Google's
# Discovery document; if one of these stops answering, the other ways still run.
KNOWN_SPECS = {
    "youtube_api": "https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest",
    "youtube": "https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest",
    # Only addresses checked to answer with a spec belong here; anything else is
    # left to the base-URL paths and the web search.
}

# Headers an agent must never set itself: auth comes from the stored credentials.
_AUTH_PARAM_NAMES = {"authorization", "x-api-key", "api-key", "apikey", "key", "api_key", "access_token",
                     "x-goog-api-key"}


# Connection methods that need no key: a spec may declare one (Semantic Scholar's
# key only raises the rate limit), but the API still answers without it.
PUBLIC_METHODS = ("public_access", "http_request", "local_setup", "none")


class SpecNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Finding the spec
# ---------------------------------------------------------------------------

def _load_text(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml                     # optional: many specs are published as YAML
            data = yaml.safe_load(text)
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def is_spec(data) -> bool:
    return isinstance(data, dict) and (
        "openapi" in data or "swagger" in data or data.get("kind") == "discovery#restDescription")


def fetch_spec(url: str) -> dict | None:
    """The spec at `url`, or None if it isn't one."""
    if not str(url or "").startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers={"Accept": "application/json, application/yaml"})
        if resp.status_code != 200:
            return None
        data = _load_text(resp.text)
        return data if is_spec(data) else None
    except requests.RequestException:
        return None


def _search_for_spec(service: str) -> tuple[str, dict] | None:
    """Ask web search for the official spec and try each link it gives."""
    if not os.getenv("GEMINI_API_KEY"):
        return None
    try:
        from agents.tool_executor import web_search_impl
    except Exception:
        return None
    found = web_search_impl("__api_discovery__", "api_discovery",
                            f"official OpenAPI or Swagger specification JSON URL for the {service.replace('_', ' ')}")
    if found.get("status") != "ok":
        return None
    urls = [s.get("url") for s in found.get("sources") or [] if s.get("url")]
    urls += re.findall(r"https?://[^\s)\"'<>]+", found.get("summary") or "")
    for url in dict.fromkeys(u.rstrip(".,") for u in urls):
        spec = fetch_spec(url)
        if spec:
            return url, spec
    return None


def find_spec(service: str, spec_url: str | None = None, base_url: str | None = None) -> tuple[str, dict]:
    """(url, spec) for a service, trying every way in turn. Raises SpecNotFound."""
    tried = []
    for url in (spec_url, KNOWN_SPECS.get(service)):
        if url:
            tried.append(url)
            spec = fetch_spec(url)
            if spec:
                return url, spec
    if base_url:
        for path in WELL_KNOWN_PATHS:
            url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            tried.append(url)
            spec = fetch_spec(url)
            if spec:
                return url, spec
    searched = _search_for_spec(service)
    if searched:
        return searched
    raise SpecNotFound(
        f"No API description found for {service}"
        + (f" (tried {', '.join(tried[:4])}{'…' if len(tried) > 4 else ''})" if tried else "")
        + ". Paste the address of its OpenAPI/Swagger spec in the Control room.")


# ---------------------------------------------------------------------------
# Spec -> operations
# ---------------------------------------------------------------------------

def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text or "")
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _resolve(spec: dict, node, depth: int = 0):
    """Follow local $refs a few levels deep; enough for argument schemas."""
    if depth > 6:
        return {"type": "object"}
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
            target = spec
            for part in node["$ref"][2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(spec, target, depth + 1)
        return {k: _resolve(spec, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(spec, v, depth + 1) for v in node]
    return node


def _param_schema(param: dict) -> dict:
    schema = dict(param.get("schema") or {})
    for key in ("type", "enum", "items"):          # Swagger 2 puts these on the parameter itself
        if key in param and key not in schema:
            schema[key] = param[key]
    schema.setdefault("type", "string")
    if param.get("description"):
        schema["description"] = str(param["description"])[:300]
    return schema


def _base_url(spec: dict, spec_url: str) -> str:
    if spec.get("servers"):                          # OpenAPI 3
        url = str(spec["servers"][0].get("url") or "")
        for name, var in (spec["servers"][0].get("variables") or {}).items():
            url = url.replace("{" + name + "}", str(var.get("default", "")))
        return urljoin(spec_url, url)
    if spec.get("host"):                             # Swagger 2
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{spec['host']}{spec.get('basePath', '')}"
    parsed = urlparse(spec_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _auth(spec: dict) -> dict:
    """How the API wants its key: {"type": "header"|"query"|"bearer"|"none", "name": ...}."""
    schemes = (spec.get("components") or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
    for scheme in schemes.values():
        kind = scheme.get("type")
        if kind == "apiKey" and scheme.get("in") in ("header", "query"):
            return {"type": scheme["in"], "name": scheme.get("name") or "X-API-Key"}
        if kind == "http" and scheme.get("scheme", "").lower() == "bearer":
            return {"type": "bearer", "name": "Authorization"}
        if kind in ("oauth2", "openIdConnect"):
            return {"type": "bearer", "name": "Authorization"}
    return {"type": "none", "name": ""}


def _openapi_operations(spec: dict, spec_url: str) -> list[dict]:
    base = _base_url(spec, spec_url)
    auth = _auth(spec)
    secret_names = _AUTH_PARAM_NAMES | {auth["name"].lower()}
    ops = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict) or op.get("deprecated"):
                continue
            op = _resolve(spec, op)
            properties, required, locations = {}, [], {}
            for param in list(_resolve(spec, shared)) + list(op.get("parameters") or []):
                if not isinstance(param, dict) or not param.get("name"):
                    continue
                where = param.get("in")
                if where == "body":                  # Swagger 2 request body
                    properties["body"] = _resolve(spec, param.get("schema") or {"type": "object"})
                    locations["body"] = "body"
                    if param.get("required"):
                        required.append("body")
                    continue
                if where not in ("path", "query", "header") or param["name"].lower() in secret_names:
                    continue
                key = _snake(param["name"]) or param["name"]
                properties[key] = _param_schema(param)
                locations[key] = f"{where}:{param['name']}"
                if param.get("required") or where == "path":
                    required.append(key)
            body = ((op.get("requestBody") or {}).get("content") or {}).get("application/json")
            if body:
                properties["body"] = body.get("schema") or {"type": "object"}
                locations["body"] = "body"
                if (op.get("requestBody") or {}).get("required"):
                    required.append("body")
            name = _snake(op.get("operationId") or f"{method}_{path}")[:60]
            ops.append({
                "name": name,
                "description": str(op.get("summary") or op.get("description") or f"{method.upper()} {path}")[:600],
                "input_schema": {"type": "object", "properties": properties, "required": sorted(set(required))},
                "annotations": {"readOnlyHint": True} if method == "get" else
                               ({"destructiveHint": True} if method == "delete" else {}),
                "call": {"method": method.upper(), "url": base.rstrip("/") + path, "params": locations, "auth": auth},
            })
    return ops


def _discovery_operations(spec: dict) -> list[dict]:
    base = spec.get("rootUrl", "") + spec.get("servicePath", "")
    auth = {"type": "query", "name": "key"}
    ops = []

    def walk(resources, prefix):
        for rname, resource in (resources or {}).items():
            for mname, method in (resource.get("methods") or {}).items():
                properties, required, locations = {}, [], {}
                for pname, param in (method.get("parameters") or {}).items():
                    if pname.lower() in _AUTH_PARAM_NAMES:
                        continue
                    key = _snake(pname)
                    properties[key] = _param_schema(param)
                    locations[key] = f"{param.get('location', 'query')}:{pname}"
                    if param.get("required"):
                        required.append(key)
                if method.get("request"):
                    ref = (method["request"] or {}).get("$ref")
                    schema = (spec.get("schemas") or {}).get(ref, {"type": "object"})
                    properties["body"] = {"type": "object", "description": schema.get("description", ref or "")[:300]}
                    locations["body"] = "body"
                    required.append("body")
                http = (method.get("httpMethod") or "GET").upper()
                ops.append({
                    "name": _snake(f"{prefix}{rname}_{mname}")[:60],
                    "description": str(method.get("description") or f"{http} {method.get('path')}")[:600],
                    "input_schema": {"type": "object", "properties": properties, "required": sorted(set(required))},
                    "annotations": {"readOnlyHint": True} if http == "GET" else
                                   ({"destructiveHint": True} if http == "DELETE" else {}),
                    "call": {"method": http, "url": base + str(method.get("path") or ""), "params": locations,
                             "auth": auth},
                })
            walk(resource.get("resources"), f"{prefix}{rname}_")

    walk(spec.get("resources"), "")
    return ops


def operations(spec: dict, spec_url: str) -> list[dict]:
    """Every operation in the spec as a tool: name, description, input_schema, annotations, call."""
    ops = _discovery_operations(spec) if spec.get("kind") == "discovery#restDescription" \
        else _openapi_operations(spec, spec_url)
    seen, unique = set(), []
    for op in ops:
        name, n = op["name"] or "call", 2
        while name in seen:
            name, n = f"{op['name']}_{n}", n + 1
        seen.add(name)
        unique.append({**op, "name": name})
    return unique[:MAX_OPERATIONS]


# ---------------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------------

def _credential(service: str, auth: dict) -> str | None:
    s = service.upper().replace("-", "_").replace(" ", "_")
    if auth.get("type") == "bearer":
        token = os.getenv(f"{s}_ACCESS_TOKEN") or os.getenv(f"{s}_TOKEN") or os.getenv(f"{s}_API_KEY")
        refresh = os.getenv(f"{s}_REFRESH_TOKEN")
        if refresh:
            try:
                from connectors.oauth_flow import get_oauth_provider, refresh_access_token
                provider = get_oauth_provider(service)
                if provider:
                    out = refresh_access_token(provider["token_url"], os.getenv(f"{s}_CLIENT_ID", ""),
                                               os.getenv(f"{s}_CLIENT_SECRET", ""), refresh)
                    if out.get("status") == "ok":
                        token = out["access_token"]
            except Exception as e:
                print(f"[API tools] Could not refresh {service}'s token: {e}")
        return token
    try:
        from connectors.api_connector import get_service_config
        env_name = (get_service_config(service) or {}).get("api_key_env")
    except Exception:
        env_name = None
    return (os.getenv(env_name) if env_name else None) or os.getenv(f"{s}_API_KEY")


def call_operation(service: str, call: dict, args: dict) -> dict:
    """Make the real HTTP call for one operation. Honest about every failure."""
    args = dict(args or {})
    url = call["url"]
    query, headers, body = {}, {"Accept": "application/json"}, None
    for key, where in (call.get("params") or {}).items():
        if key not in args:
            continue
        value = args.pop(key)
        if where == "body":
            body = value
            continue
        location, _, real = where.partition(":")
        if location == "path":
            url = url.replace("{" + real + "}", requests.utils.quote(str(value), safe=""))
        elif location == "header":
            headers[real] = str(value)
        else:
            query[real] = value
    if re.search(r"\{[^}]+\}", url):
        return {"status": "error", "error": f"Missing a path argument for {url}."}

    auth = dict(call.get("auth") or {})
    try:
        from connectors.api_connector import get_service_config
        method = (get_service_config(service) or {}).get("method_id")
    except Exception:
        method = None
    if method == "oauth":
        auth = {"type": "bearer", "name": "Authorization"}   # connected by signing in, not with a key
    if auth.get("type", "none") != "none":
        secret = _credential(service, auth)
        if not secret and method not in PUBLIC_METHODS:
            return {"status": "error", "error": f"{service} has no stored credential. Connect it in the Control room."}
    else:
        secret = None
    if secret:
        if auth["type"] == "query":
            query[auth["name"]] = secret
        elif auth["type"] == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        else:
            headers[auth["name"]] = secret

    try:
        resp = requests.request(call["method"], url, params=query, headers=headers,
                                json=body if body is not None else None, timeout=CALL_TIMEOUT)
    except requests.RequestException as e:
        return {"status": "error", "error": str(e)}
    try:
        data = resp.json()
        text = json.dumps(data, ensure_ascii=False)
        payload = data if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "… (cut)"
    except ValueError:
        payload = resp.text[:MAX_RESULT_CHARS]
    if resp.status_code >= 400:
        return {"status": "error", "http_status": resp.status_code, "error": payload}
    return {"status": "ok", "http_status": resp.status_code, "data": payload}
