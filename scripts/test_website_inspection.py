"""inspect_website: name resolution, address safety, a real browser capture, and the analysis step.

The capture runs a real headless Chromium against a page served from this
process, called from inside a running asyncio loop exactly as research agents
call their tools. The vision model is faked.
"""
import asyncio, http.server, json, os, shutil, sys, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agents import tool_executor as te
from agents import website_inspector as wi

PROJECT = "__website_inspection_test__"
PROJECT_DIR = os.path.join(wi.BASE_DIR, "Let Jarvis Handle It", PROJECT)


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


# ---- 1. names agents use reach the inspector, and nothing else is stolen ---------
for name in [
    "web_scraper",
    "web_scraping_tools",
    "Visual Analysis Tool",
    "website_screenshot",
    "UX/UI Analysis Platforms (e.g., Hotjar, FullStory, Crazy Egg)",
    "Cross-Browser Testing Platforms (e.g., BrowserStack, LambdaTest)",
    "Advanced Web Scraping / Data Extraction Tool (e.g., Python with Scrapy/BeautifulSoup, or commercial scraping platforms)",
]:
    check(f"'{name}' resolves to the website inspector",
          te._resolve_tool_key(name, allow_llm=False) == "inspect_website")

for name, expected in [
    ("web_search", "google_search"), ("google_search", "google_search"), ("internet_search", "google_search"),
    ("web_browsing", "google_search"), ("arxiv_search", "arxiv_api"), ("search_memory_patterns", "memory_patterns"),
]:
    check(f"'{name}' still resolves to {expected}", te._resolve_tool_key(name, allow_llm=False) == expected)
check("an SEO suite is not mistaken for the inspector",
      te._resolve_tool_key("SEO/SEM Analysis Suite (e.g., Ahrefs, SEMrush, Moz Pro)", allow_llm=False) != "inspect_website")

declarations, handlers, unavailable = te.get_tools_for_execution_agent(["web_scraper"], PROJECT)
check("an agent asking for web_scraper really gets inspect_website, with nothing reported missing",
      any(d.get("name") == "inspect_website" for d in declarations) and "inspect_website" in handlers and not unavailable)
buckets = te.classify_requested_tools(["web_scraper", "Visual Analysis Tool"])
check("the plugging gate never asks for credentials for it",
      not buckets["connectable"] and set(buckets["always_on"]) == {"web_scraper", "Visual Analysis Tool"})


# ---- 2. only public websites --------------------------------------------------------
def resolves_to(ip):
    return lambda host, port: [(None, None, None, None, (ip, 0))]


def unresolvable(host, port):
    raise OSError("no such host")


check("file:// is refused", wi.check_public_url("file:///C:/Windows/win.ini") is not None)
check("ftp:// is refused", wi.check_public_url("ftp://example.com", resolves_to("93.184.215.14")) is not None)
check("an address without a host is refused", wi.check_public_url("https://", resolves_to("93.184.215.14")) is not None)
for ip in ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "169.254.169.254", "::1", "0.0.0.0"]:
    check(f"a host resolving to {ip} is refused",
          wi.check_public_url("https://sneaky.example", resolves_to(ip)) is not None)
check("a public website is allowed", wi.check_public_url("https://example.com", resolves_to("93.184.215.14")) is None)
check("a site that doesn't resolve is refused with a reason",
      "Could not find" in (wi.check_public_url("https://nope.invalid", unresolvable) or ""))

refused = wi.inspect_website_impl(PROJECT, "agent", "http://127.0.0.1:9/")
check("the tool refuses this computer before opening a browser",
      refused["status"] == "error" and "local network" in refused["error"] and not os.path.exists(PROJECT_DIR))


# ---- 3. a real capture ----------------------------------------------------------------
PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
PAGE = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Custom software for ambitious teams.">
<title>Northwind Studio</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 0; color: #1f2937; }}
  header {{ display: flex; justify-content: space-between; padding: 16px 24px; background: #0b1020; }}
  nav a {{ color: #fff; margin-left: 16px; }}
  .menu {{ display: none; }}
  h1 {{ font-family: Georgia, serif; font-size: 48px; }}
  .hero {{ padding: 48px 24px; filter: saturate(1.2); }}
  .btn {{ background: #ff5500; color: #ffffff; padding: 12px 24px; border-radius: 8px; display: inline-block; }}
  @media (max-width: 600px) {{ nav {{ display: none; }} .menu {{ display: block; }} h1 {{ font-size: 32px; }} }}
</style></head>
<body>
  <header><strong style="color:#fff">Northwind</strong>
    <nav><a href="/work">Work</a><a href="/pricing">Pricing</a><a href="/contact">Contact</a></nav>
    <button class="menu" aria-label="Open menu">☰</button></header>
  <main class="hero">
    <h1>Software that grows your business</h1>
    <h2>Trusted by 40 clients</h2>
    <p>We design and build web apps, mobile apps and AI tools.</p>
    <a class="btn" href="/contact">Book a call</a>
    <img src="{PIXEL}" style="width:500px;max-width:100%;height:200px">
  </main>
  <div style="height:2400px"></div>
  <section id="late" style="min-height:120px"></section>
  <script>
    // Like a logo wall that only renders once scrolled into view.
    new IntersectionObserver((entries, observer) => {{
      if (entries.some(e => e.isIntersecting)) {{
        const h = document.createElement('h2');
        h.textContent = 'Loaded on scroll';
        document.getElementById('late').appendChild(h);
        observer.disconnect();
      }}
    }}).observe(document.getElementById('late'));
  </script>
</body></html>"""


class _Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
LOCAL_URL = f"http://127.0.0.1:{server.server_address[1]}/"


class _FakeModels:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def generate_content(self, model=None, contents=None, config=None, **kwargs):
        self.calls.append(contents)
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("Response", (), {"text": self.reply})()


def use_model(reply):
    models = _FakeModels(reply)
    wi.genai.Client = lambda *a, **k: type("Client", (), {"models": models})()
    return models


try:
    out_dir = os.path.join(PROJECT_DIR, "capture")

    async def from_inside_an_event_loop():
        # Research agents call tools synchronously from inside a running loop;
        # Playwright's sync API would refuse to run on that thread.
        return wi.capture_site(LOCAL_URL, out_dir, url_guard=None)

    capture = asyncio.run(from_inside_an_event_loop())
    m = capture["measured"]

    check("it captures from inside a running event loop", capture["final_url"] == LOCAL_URL)
    print(f"      (browser used: {capture['browser']})")
    check("it records which browser it used, without downloading one", bool(capture["browser"]))
    check("three screenshots, one per width, are written",
          [s["view"] for s in capture["screenshots"]] == ["mobile", "tablet", "desktop"]
          and all(os.path.getsize(s["path"]) > 1000 for s in capture["screenshots"]))
    check("each width really is that width",
          [capture["layouts"][v]["viewport_width"] for v in ("mobile", "tablet", "desktop")] == [375, 768, 1440])
    check("the page's own details are read", m["title"] == "Northwind Studio"
          and m["description"] == "Custom software for ambitious teams." and m["has_viewport_meta"] is True)
    check("heading and body fonts are measured",
          "Georgia" in m["type_scale"]["h1"]["font"] and m["type_scale"]["h1"]["size"] == "48px"
          and "Arial" in m["type_scale"]["body"]["font"])
    check("colours come back as hex", any(c["value"] == "#0b1020" for c in m["background_colours"]))
    check("the main call to action is found with its colour",
          any(c["text"] == "Book a call" and c["background"] == "#ff5500" and c["href"] == "/contact" for c in m["calls_to_action"]))
    check("navigation and headings are read",
          m["navigation_links"] == ["Work", "Pricing", "Contact"]
          and [h["text"] for h in m["headings"]][:2] == ["Software that grows your business", "Trusted by 40 clients"])
    check("content that only loads when scrolled into view is captured",
          "Loaded on scroll" in [h["text"] for h in m["headings"]])
    check("images and effects are counted",
          m["images"]["count"] == 1 and m["images"]["missing_alt"] == 1
          and any(e["value"].startswith("filter: saturate") for e in m["visual_effects"]))
    check("social proof wording is noticed", "trusted by" in m["social_proof_mentions"])
    check("responsive behaviour is measured: the menu collapses on mobile",
          capture["layouts"]["mobile"]["menu_button_visible"] is True
          and capture["layouts"]["mobile"]["visible_navigation_links"] == 0
          and capture["layouts"]["desktop"]["visible_navigation_links"] == 3
          and capture["layouts"]["mobile"]["h1_font_size"] == "32px"
          and capture["layouts"]["mobile"]["horizontal_overflow"] is False)

    # A redirect or link into this computer is refused even mid-capture.
    try:
        wi.capture_site(LOCAL_URL, os.path.join(PROJECT_DIR, "guarded"), url_guard=wi.check_public_url)
        guarded = None
    except wi.InspectorUnavailable as e:
        guarded = str(e)
    check("the browser itself refuses navigations the guard rejects", guarded and "local network" in guarded)

    # A capture past the time limit stops at its next step and cleans up after itself,
    # instead of running on with a browser slot and rewriting a folder already deleted.
    import time as _time
    real_limit = wi.CAPTURE_TIMEOUT_S
    wi.CAPTURE_TIMEOUT_S = 1
    slow_dir = os.path.join(PROJECT_DIR, "too_slow")
    try:
        wi.capture_site(LOCAL_URL, slow_dir, url_guard=None)
        timed_out = False
    except TimeoutError:
        timed_out = True
    finally:
        wi.CAPTURE_TIMEOUT_S = real_limit
    check("a capture past the time limit is reported as timed out", timed_out)
    freed = [wi._BROWSER_SLOTS.acquire(timeout=60) for _ in range(2)]
    for got in freed:
        if got:
            wi._BROWSER_SLOTS.release()
    for _ in range(50):
        if not os.path.exists(slow_dir):
            break
        _time.sleep(0.1)
    check("once it stops, both browser slots are free again and its folder is gone",
          all(freed) and not os.path.exists(slow_dir))

    # What a page loads itself is held to the same rule as the page. A second local
    # server stands in for an address inside the network; the guard refuses its port.
    from urllib.parse import urlparse

    class _Private(http.server.BaseHTTPRequestHandler):
        hits = []

        def do_GET(self):
            _Private.hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    private = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Private)
    threading.Thread(target=private.serve_forever, daemon=True).start()
    PRIVATE_PORT = private.server_address[1]
    PROBE_PAGE = f"""<!doctype html><html><head><title>Probe</title></head><body>
<h1>Probe</h1>
<img src="http://127.0.0.1:{PRIVATE_PORT}/image.png">
<img src="/to-private">
<script>
  fetch("http://127.0.0.1:{PRIVATE_PORT}/fetch").catch(() => {{}});
  try {{ new WebSocket("ws://127.0.0.1:{PRIVATE_PORT}/socket"); }} catch (e) {{}}
</script>
</body></html>"""

    class _Probe(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/to-private":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{PRIVATE_PORT}/redirected")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = PROBE_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    probe_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Probe)
    threading.Thread(target=probe_server.serve_forever, daemon=True).start()
    PROBE_URL = f"http://127.0.0.1:{probe_server.server_address[1]}/"

    def refuse_private(address):
        return "points inside this computer or local network" if urlparse(address).port == PRIVATE_PORT else None

    try:
        wi.capture_site(PROBE_URL, os.path.join(PROJECT_DIR, "probe_open"), url_guard=None)
        reached = set(_Private.hits)
        print(f"      (reached without a guard: {sorted(reached)})")
        check("without a guard the page really does reach the other address: image, fetch, socket and redirect",
              {"/image.png", "/fetch", "/socket", "/redirected"} <= reached)
        _Private.hits.clear()
        probe = wi.capture_site(PROBE_URL, os.path.join(PROJECT_DIR, "probe_guarded"), url_guard=refuse_private)
        check("with the guard, nothing the page loads reaches it", _Private.hits == [])
        check("the page itself is still captured", probe["measured"]["title"] == "Probe")
        check("what was refused is reported with the capture",
              any("local network" in b for b in probe["blocked_requests"]))
    finally:
        private.shutdown()
        probe_server.shutdown()

    # ---- 4. the whole tool, with a fake vision model --------------------------------
    real_guard = wi.check_public_url
    wi.check_public_url = lambda url, resolve=None: None     # let the tool reach the local page
    wi.GEMINI_API_KEY = "test-key"
    try:
        analysis = {"typography": "Georgia headings over Arial body text.", "standout_ideas": ["Orange CTA"]}
        models = use_model(json.dumps(analysis))
        result = te.run_tool(handlers, PROJECT, "competitor_analyst", "inspect_website",
                             {"url": LOCAL_URL, "focus": "the call to action"})
        check("the tool succeeds end to end", result["status"] == "ok")
        check("its result is plain JSON an agent can receive", json.loads(json.dumps(result)) == result)
        check("the analysis reaches the agent", result["visual_analysis"] == analysis and "analysis_error" not in result)
        check("screenshots are saved inside the project, by relative path",
              all(s["path"].startswith(os.path.join("Let Jarvis Handle It", PROJECT, "Website Inspections"))
                  and os.path.exists(os.path.join(wi.BASE_DIR, s["path"])) for s in result["screenshots"]))
        saved = os.path.join(wi.BASE_DIR, result.get("measurements_path", "missing"))
        check("the measurements are saved beside the screenshots",
              os.path.exists(saved) and "Georgia" in open(saved, encoding="utf-8").read())
        sent = models.calls[0]
        images = [c for c in sent if not isinstance(c, str)]
        check("the model is sent all three screenshots", len(images) == 3)
        check("the model is sent the measurements and the focus",
              any("#ff5500" in c for c in sent if isinstance(c, str))
              and "the call to action" in sent[0])

        use_model(RuntimeError("model unavailable"))
        result = wi.inspect_website_impl(PROJECT, "agent", LOCAL_URL)
        check("if analysis fails, the measurements and screenshots still come back",
              result["status"] == "ok" and result["visual_analysis"] is None
              and "could not be analysed" in result["analysis_error"] and result["measured"]["title"] == "Northwind Studio")

        wi.GEMINI_API_KEY = None
        result = wi.inspect_website_impl(PROJECT, "agent", LOCAL_URL)
        check("without a Gemini key it says the screenshots weren't analysed",
              result["status"] == "ok" and "not analysed" in result["analysis_error"])

        result = wi.inspect_website_impl(PROJECT, "agent", "http://127.0.0.1:1/")
        check("a page that won't load is an error with a reason, and leaves no folder behind",
              result["status"] == "error" and result["error"].startswith("Could not open")
              and len(os.listdir(os.path.join(PROJECT_DIR, "Website Inspections"))) == 3)
    finally:
        wi.check_public_url = real_guard
finally:
    server.shutdown()
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)

print("\nAll website inspection checks passed.")
