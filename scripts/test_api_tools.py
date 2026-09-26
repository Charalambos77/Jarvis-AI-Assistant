"""A connected API becomes tools, read from its published description.

  * OpenAPI 3, Swagger 2 and Google Discovery documents each turn into one tool
    per operation, with the right arguments, address and way of sending the key;
  * connecting an API finds its spec (here at /openapi.json on its base URL),
    and its tools go through the same review and Control room as an MCP server's;
  * approved tools make real HTTP calls with the stored key, which an agent can
    never set itself;
  * an API with no spec stays connected with no tools, and agents are told why.

Runs a small local HTTP server as the API; no model is called.
"""
import asyncio, json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("GEMINI_API_KEY", None)

from connectors import tool_catalog, api_connector, openapi_tools, mcp_client
from agents import tool_onboarding, tool_executor
import control_room


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


tmp = tempfile.mkdtemp(prefix="jarvis_api_tools_")
tool_catalog.CATALOG_PATH = os.path.join(tmp, "tool_catalog.json")
control_room.SPENDING_PATH = os.path.join(tmp, "spending.json")
api_connector.REGISTRY_PATH = os.path.join(tmp, "api_registry.json")
mcp_client.REGISTRY_PATH = os.path.join(tmp, "mcp_registry.json")
with open(mcp_client.REGISTRY_PATH, "w", encoding="utf-8") as f:
    json.dump({}, f)
control_room.POLL_SECONDS = 0.01
tool_onboarding.RESEARCH_IN_BACKGROUND = False
notes = []
tool_onboarding.set_notifier(notes.append)
control_room.set_notifier(notes.append)

# ---- the API: a tiny shop -------------------------------------------------------------------
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Tiny Shop"},
    "servers": [{"url": "/v1"}],
    "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Shop-Key"}},
                   "schemas": {"Order": {"type": "object", "properties": {"product_id": {"type": "string"},
                                                                          "amount": {"type": "number"}}}}},
    "paths": {
        "/products": {
            "get": {"operationId": "listProducts", "summary": "List products",
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}},
                                   {"name": "X-Shop-Key", "in": "header", "schema": {"type": "string"}}]},
        },
        "/products/{productId}": {
            "parameters": [{"name": "productId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getProduct", "summary": "Get one product"},
            "put": {"operationId": "updateProduct", "summary": "Update a product's name",
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}}},
            "delete": {"operationId": "deleteProduct", "summary": "Remove a product"},
        },
        "/orders": {
            "post": {"operationId": "createOrder", "summary": "Place an order",
                     "requestBody": {"required": True, "content": {"application/json": {
                         "schema": {"$ref": "#/components/schemas/Order"}}}}},
        },
        "/old": {"get": {"operationId": "oldThing", "deprecated": True}},
    },
}
seen_requests = []


class Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null") if length else None
        seen_requests.append({"method": method, "path": self.path, "key": self.headers.get("X-Shop-Key"),
                              "body": body})
        if self.path == "/openapi.json":
            return self._send(200, SPEC)
        if not self.path.startswith("/v1/"):
            return self._send(404, {"error": "not found"})
        if self.headers.get("X-Shop-Key") != "secret-123":
            return self._send(401, {"error": "bad key"})
        if method == "GET" and self.path.startswith("/v1/products?"):
            return self._send(200, {"products": ["kettle", "lamp"], "query": self.path.split("?", 1)[1]})
        if method == "GET" and self.path.startswith("/v1/products/"):
            return self._send(200, {"id": self.path.rsplit("/", 1)[1]})
        if method == "POST" and self.path == "/v1/orders":
            return self._send(201, {"order": "o1", "got": body})
        return self._send(200, {"ok": True})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"

# ---- 1. OpenAPI 3 -> tools ------------------------------------------------------------------
ops = {o["name"]: o for o in openapi_tools.operations(SPEC, BASE + "/openapi.json")}
check("every live operation becomes a tool, the deprecated one doesn't",
      set(ops) == {"list_products", "get_product", "update_product", "delete_product", "create_order"})
check("the address comes from the spec's server", ops["get_product"]["call"]["url"] == BASE + "/v1/products/{productId}")
check("a path argument is required and knows where it goes",
      ops["get_product"]["input_schema"]["required"] == ["product_id"]
      and ops["get_product"]["call"]["params"]["product_id"] == "path:productId")
check("the key is never an argument the agent can set",
      "x_shop_key" not in ops["list_products"]["input_schema"]["properties"])
check("the key goes in the header the spec names",
      ops["list_products"]["call"]["auth"] == {"type": "header", "name": "X-Shop-Key"})
check("a request body's $ref is followed",
      "amount" in ops["create_order"]["input_schema"]["properties"]["body"]["properties"])
risks = {n: tool_catalog.classify_risk(n, o["description"], o["annotations"]) for n, o in ops.items()}
check("reads are read, placing an order costs money, removing is destructive, updating is a write",
      risks == {"list_products": "read", "get_product": "read", "create_order": "costs_money",
                "delete_product": "destructive", "update_product": "write"})

# ---- 2. Swagger 2 and Google Discovery -> tools --------------------------------------------
swagger = {"swagger": "2.0", "host": "api.example.com", "basePath": "/v2", "schemes": ["https"],
           "securityDefinitions": {"k": {"type": "apiKey", "in": "query", "name": "api_key"}},
           "paths": {"/pets": {"post": {"operationId": "addPet", "parameters": [
               {"name": "pet", "in": "body", "required": True, "schema": {"type": "object"}}]}}}}
pet = openapi_tools.operations(swagger, "https://api.example.com/swagger.json")[0]
check("Swagger 2: address from host and basePath", pet["call"]["url"] == "https://api.example.com/v2/pets")
check("Swagger 2: a body parameter becomes the body argument",
      pet["call"]["params"] == {"body": "body"} and pet["input_schema"]["required"] == ["body"])
check("Swagger 2: a query key", pet["call"]["auth"] == {"type": "query", "name": "api_key"})

discovery = {"kind": "discovery#restDescription", "rootUrl": "https://www.googleapis.com/",
             "servicePath": "youtube/v3/", "resources": {"videos": {"methods": {
                 "list": {"httpMethod": "GET", "path": "videos", "description": "List videos",
                          "parameters": {"part": {"type": "string", "required": True, "location": "query"},
                                         "key": {"type": "string", "location": "query"}}},
                 "delete": {"httpMethod": "DELETE", "path": "videos", "description": "Delete a video"}},
                 "resources": {"ratings": {"methods": {"get": {"httpMethod": "GET", "path": "videos/rating"}}}}}}}
dops = {o["name"]: o for o in openapi_tools.operations(discovery, "https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest")}
check("Discovery: one tool per method, nested resources too",
      set(dops) == {"videos_list", "videos_delete", "videos_ratings_get"})
check("Discovery: the address and the key", dops["videos_list"]["call"]["url"] == "https://www.googleapis.com/youtube/v3/videos"
      and dops["videos_list"]["call"]["auth"] == {"type": "query", "name": "key"})
check("Discovery: 'key' is not an argument, 'part' is required",
      list(dops["videos_list"]["input_schema"]["properties"]) == ["part"]
      and dops["videos_list"]["input_schema"]["required"] == ["part"])
check("Discovery: DELETE is marked destructive", dops["videos_delete"]["annotations"] == {"destructiveHint": True})
check("Discovery: listing subscriptions is a read, not a purchase",
      tool_catalog.classify_risk("subscriptions_list", "", {"readOnlyHint": True}) == "read")
check("Discovery: adding a subscription still counts as money, to be safe",
      tool_catalog.classify_risk("subscriptions_insert", "", {}) == "costs_money")

# ---- 3. an API with no spec -----------------------------------------------------------------
api_connector.register_service("nospec", {"status": "up", "base_url": BASE + "/nothing-here"})
entry = tool_onboarding.observe_api("nospec")
check("no spec: the API is recorded with no tools", entry["state"] == "no_spec" and entry["tools"] == {})
check("no spec: the error says what to do", "Control room" in entry["spec_error"])
check("no spec: the user is told once", sum("nospec" in n and "no API description" in n for n in notes) == 1)
tool_onboarding.observe_api("nospec")
check("no spec: looking again doesn't tell them again", sum("nospec" in n for n in notes) == 1)
_, _, unavailable = tool_executor.get_tools_for_execution_agent(["nospec"], "x")
check("an agent that asks for it is told there is no API description",
      any("no API description" in u for u in unavailable))

# ---- 4. connecting the shop: spec found, reviewed, risky ones held --------------------------
api_connector.register_service("shop", {"status": "up", "base_url": BASE, "api_key_env": "SHOP_API_KEY"})
os.environ["SHOP_API_KEY"] = "secret-123"
looked = tool_onboarding.discover_connected_apis()
check("startup discovery looks at the shop and retries the one with no spec", sorted(looked) == ["nospec", "shop"])
entry = tool_catalog.get("shop")
check("the spec was found on the base URL", entry["spec_url"] == BASE + "/openapi.json" and entry["spec_title"] == "Tiny Shop")
check("each tool keeps how to call it", entry["tools"]["get_product"]["call"]["method"] == "GET")
check("agents call them api_shop_…", entry["tools"]["list_products"]["fn_name"] == "api_shop_list_products")
check("the review lists the read and write tools",
      {t["name"] for t in tool_onboarding.pending()[0]["tools"]} == {"list_products", "get_product", "update_product"})
check("the money and destructive tools wait in the Control room",
      sorted(t["tool"] for t in control_room.held_tools()) == ["create_order", "delete_product"])
got = {d["name"] for d in tool_executor.get_tools_for_execution_agent(["shop"], "x")[0]}
check("nothing is bound before the review", not any(n.startswith("api_shop") for n in got))

tool_catalog.update(lambda c: c["shop"]["review"].update(deadline=0))
check("the unanswered review approves itself", tool_onboarding.sweep() == ["shop"])
check("a built-in service is never looked at", "google_search" not in tool_onboarding.discover_connected_apis())

# ---- 5. who gets what, and real calls -------------------------------------------------------
decls, handlers, _ = tool_executor.get_tools_for_execution_agent([], "x")
names = {d["name"] for d in decls}
check("every agent gets the read tools", {"api_shop_list_products", "api_shop_get_product"} <= names)
check("but not the write tool", "api_shop_update_product" not in names)
check("the shop resolves by name", tool_executor._resolve_tool_key("shop", allow_llm=False) == "api:shop")
check("and loosely", tool_executor._resolve_tool_key("shop_api", allow_llm=False) == "api:shop")
decls, handlers, _ = tool_executor.get_tools_for_execution_agent(["shop_api"], "x")
names = {d["name"] for d in decls}
check("an agent whose plan names the shop gets the write tool", "api_shop_update_product" in names)
check("the declaration says it is an API", any(d["description"].startswith("[API:shop]") for d in decls))

seen_requests.clear()
res = tool_executor.run_tool(handlers, "x", "a1", "api_shop_list_products", {"limit": 2})
check("a read tool really calls the API", res["status"] == "ok" and res["data"]["products"] == ["kettle", "lamp"])
check("with the query argument and the stored key in its header",
      res["data"]["query"] == "limit=2" and seen_requests[-1]["key"] == "secret-123")
res = tool_executor.run_tool(handlers, "x", "a1", "api_shop_get_product", {"product_id": "a b"})
check("a path argument is put in the address, escaped", res["data"]["id"] == "a%20b")
res = tool_executor.run_tool(handlers, "x", "a1", "api_shop_get_product", {})
check("a missing path argument is refused before any request", res["status"] == "error" and "path" in res["error"])
os.environ["SHOP_API_KEY"] = "wrong"
res = tool_executor.run_tool(handlers, "x", "a1", "api_shop_list_products", {})
check("an API's refusal comes back as an error with its status", res["status"] == "error" and res["http_status"] == 401)
os.environ["SHOP_API_KEY"] = "secret-123"

# ---- 6. the money tool goes through the Control room ---------------------------------------
control_room.decide_tool("shop", "create_order", "always_ask")
decls, handlers, _ = tool_executor.get_tools_for_execution_agent(["shop"], "x")
check("once allowed as always ask, the plan's agent gets it", "api_shop_create_order" in {d["name"] for d in decls})


async def held_order():
    task = asyncio.create_task(control_room.check_call("api_shop_create_order", {"body": {"amount": 12}},
                                                       agent_id="a1", role="Buyer"))
    while not control_room.pending_calls():
        await asyncio.sleep(0.005)
    item = control_room.pending_calls()[0]
    control_room.decide_call(item["request_id"], "allow")
    return item, await task


item, verdict = asyncio.run(held_order())
check("a call to place an order is held, showing the amount", item["amount"] == 12.0)
check("allowed, it goes ahead", verdict is None)
res = tool_executor.run_tool(handlers, "x", "a1", "api_shop_create_order", {"body": {"product_id": "p1", "amount": 12}})
check("and the order really reaches the API with its body",
      res["http_status"] == 201 and res["data"]["got"] == {"product_id": "p1", "amount": 12})

# ---- 7. the Brain hears about it; disconnecting takes it away -------------------------------
note = tool_onboarding.connected_services_note()
check("the Brain is told about the shop and its tools", "- shop:" in note and "list_products (reads)" in note)
reg = api_connector.load_registry()
reg["shop"]["status"] = "unknown"
api_connector.save_registry()
names = {d["name"] for d in tool_executor.get_tools_for_execution_agent(["shop"], "x")[0]}
check("a disconnected API's tools reach no agent", not any(n.startswith("api_shop") for n in names))
check("and the Brain stops hearing about it", "- shop:" not in tool_onboarding.connected_services_note())

# ---- 8. a spec that moved keeps decisions for unchanged tools, a new address starts over --------
reg["shop"]["status"] = "up"
api_connector.save_registry()
before = tool_catalog.get("shop")["tools"]["list_products"]["status"]
tool_onboarding.observe_api("shop")
check("reading the same spec again changes nothing",
      tool_catalog.get("shop")["tools"]["list_products"]["status"] == before == "enabled")
SPEC["servers"] = [{"url": "/v2"}]
tool_onboarding.observe_api("shop", BASE + "/openapi.json")
check("tools pointed at a new address go back to review",
      tool_catalog.get("shop")["tools"]["list_products"]["status"] == "pending")

server.shutdown()
print("\nAll API tool checks passed.")
