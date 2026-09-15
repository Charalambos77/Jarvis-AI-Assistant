"""
Website inspector — lets an agent look at a real website instead of reading
about it.

Web search only returns what other people wrote about a site. A brief about
competitors' fonts, colours, imagery and emotional journey needs the site
itself, and until this existed no agent could open a page: `web_scraper` was
quietly resolved to web search, so agents believed they could read pages and
got summaries instead.

One call opens the page in headless Chromium (Playwright), reads the live HTML
and computed CSS, takes screenshots at mobile, tablet and desktop widths, and
has Gemini describe what the screenshots show. It runs on this machine: no
account and no key beyond the Gemini key Jarvis already uses.
"""
import concurrent.futures
import glob
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, width, height). Height is only the first screen; screenshots cover the page.
VIEWPORTS = [
    ("mobile", 375, 812),
    ("tablet", 768, 1024),
    ("desktop", 1440, 900),
]
MAX_SCREENSHOT_HEIGHT = 6000      # long pages are cut here, and the result says so
NAVIGATION_TIMEOUT_MS = 30000
SETTLE_TIMEOUT_MS = 8000          # extra wait for the network to go quiet, if it ever does
LAZY_SETTLE_TIMEOUT_MS = 3000     # wait for what scrolling set loading
CAPTURE_TIMEOUT_S = 180

# Scrolls to the capture limit a screen at a time, then back to the top.
SCROLL_JS = r"""
async (limit) => {
  const pause = () => new Promise(resolve => setTimeout(resolve, 150));
  const step = Math.max(300, Math.floor(window.innerHeight * 0.8));
  const bottom = Math.min(document.documentElement.scrollHeight, limit);
  for (let y = 0; y < bottom; y += step) {
    window.scrollTo(0, y);
    await pause();
  }
  window.scrollTo(0, bottom);   // the steps can stop short of the last screen
  await pause();
  window.scrollTo(0, 0);
  await pause();
}
"""
MAX_MEASURED_CHARS = 30000        # measured data handed to the vision model
MAX_FOCUS_CHARS = 300

# Each inspection runs its own browser and agents run in parallel. Two browsers
# at once is plenty for one machine; the rest wait their turn.
_BROWSER_SLOTS = threading.BoundedSemaphore(2)

PLAYWRIGHT_MISSING = (
    "Website inspection needs the Playwright package, which isn't installed. "
    "Install it with: venv\\Scripts\\python -m pip install playwright==1.62.0"
)
BROWSER_MISSING = (
    "Website inspection needs a Chromium-based browser and found none: not Playwright's own, "
    "not Edge, not Chrome. Install one with: venv\\Scripts\\python -m playwright install chromium"
)


class InspectorUnavailable(Exception):
    """The inspector can't run on this machine; the message says how to fix it."""


# ---------------------------------------------------------------------------
# Address safety
# ---------------------------------------------------------------------------

def check_public_url(url: str, resolve=socket.getaddrinfo) -> str | None:
    """Why this address may not be inspected, or None when it is a public website.

    Agents pick URLs from pages they read, so a page could steer one at this
    computer or the local network (a router admin page, a local dev server).
    Only http(s) addresses that resolve to public IPs are allowed.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return "Only http:// and https:// web addresses can be inspected."
    host = parsed.hostname
    if not host:
        return "That is not a complete web address."
    try:
        infos = resolve(host, None)
    except Exception:
        return f"Could not find the website '{host}'."
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return f"Could not work out where '{host}' points."
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return f"'{host}' points inside this computer or local network; only public websites can be inspected."
    return None


def _guard_route(url_guard, blocked: list, blocked_requests: list):
    """A route handler that refuses every request the guard rejects, not only page loads.

    Only navigations used to be checked, so an image, script, fetch or redirect a page
    aimed at this computer or the local network still went out from the browser.
    Refused navigations go in `blocked`, everything else refused in `blocked_requests`.
    The guard judges an address by where it points, so each origin is checked once,
    not once per request.
    """
    verdicts = {}

    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in verdicts:
            verdicts[origin] = url_guard(request.url)
        problem = verdicts[origin]
        if problem:
            (blocked if request.is_navigation_request() else blocked_requests).append(problem)
            route.abort("blockedbyclient")
            return
        route.continue_()
    return handle


def _guard_websocket(url_guard, blocked_requests: list):
    """The same check for WebSockets, which request routing never sees."""
    def handle(ws):
        # ws:// and wss:// point wherever http:// and https:// would.
        problem = url_guard(re.sub(r"^ws", "http", ws.url, count=1))
        if problem:
            # Left unconnected, the socket never reaches the address. Don't close it:
            # closing from inside this handler hangs the page load.
            blocked_requests.append(problem)
            return
        ws.connect_to_server()
    return handle


# ---------------------------------------------------------------------------
# What is read from the live page
# ---------------------------------------------------------------------------

# Everything measured at desktop width: the design system as the browser actually
# renders it, not as the stylesheet claims.
DESKTOP_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0;
  };
  const tally = (map, key) => { if (key) map[key] = (map[key] || 0) + 1; };
  const top = (map, n) => Object.entries(map).sort((a, b) => b[1] - a[1]).slice(0, n).map(([value, count]) => ({ value, count }));
  const textOf = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const firstFont = family => (family || '').split(',')[0].replace(/["']/g, '').trim();

  const fonts = {}, textColors = {}, backgrounds = {}, effects = {};
  let animated = 0, transitioned = 0;
  const all = document.body ? Array.from(document.body.querySelectorAll('*')).slice(0, 4000) : [];
  for (const el of all) {
    if (!visible(el)) continue;
    const s = getComputedStyle(el);
    const ownText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
    if (ownText) { tally(fonts, firstFont(s.fontFamily)); tally(textColors, s.color); }
    if (s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)') tally(backgrounds, s.backgroundColor);
    if (s.backgroundImage && s.backgroundImage.includes('gradient')) tally(effects, 'gradient background');
    if (s.filter && s.filter !== 'none') tally(effects, 'filter: ' + s.filter);
    if (s.backdropFilter && s.backdropFilter !== 'none') tally(effects, 'backdrop-filter: ' + s.backdropFilter);
    if (s.mixBlendMode && s.mixBlendMode !== 'normal') tally(effects, 'mix-blend-mode: ' + s.mixBlendMode);
    if (s.boxShadow && s.boxShadow !== 'none') tally(effects, 'box-shadow');
    if (s.animationName && s.animationName !== 'none') animated++;
    if ((s.transitionDuration || '').split(',').some(d => parseFloat(d) > 0)) transitioned++;
  }

  const typeOf = selector => {
    const el = Array.from(document.querySelectorAll(selector)).find(visible);
    if (!el) return null;
    const s = getComputedStyle(el);
    return { font: s.fontFamily, size: s.fontSize, weight: s.fontWeight, line_height: s.lineHeight,
             letter_spacing: s.letterSpacing, transform: s.textTransform, color: s.color, sample: textOf(el).slice(0, 120) };
  };

  const ctas = Array.from(document.querySelectorAll('a, button, [role=button], input[type=submit]'))
    .filter(visible)
    .map(el => {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return { text: (textOf(el) || el.value || el.getAttribute('aria-label') || '').slice(0, 60),
               background: s.backgroundColor, color: s.color, border_radius: s.borderRadius, padding: s.padding,
               font_weight: s.fontWeight, top_px: Math.round(r.top + window.scrollY), href: (el.getAttribute('href') || '').slice(0, 120),
               area: Math.round(r.width * r.height) };
    })
    .filter(c => c.text && c.background !== 'rgba(0, 0, 0, 0)')
    .sort((a, b) => b.area - a.area)
    .slice(0, 12)
    .map(({ area, ...rest }) => rest);

  const images = Array.from(document.images).filter(visible);
  const meta = name => ((document.querySelector(`meta[name="${name}"]`) || {}).content || '');
  const html = document.body ? document.body.innerHTML.toLowerCase() : '';

  return {
    title: document.title,
    description: meta('description').slice(0, 300),
    language: document.documentElement.lang || '',
    has_viewport_meta: !!document.querySelector('meta[name="viewport"]'),
    page_height_px: document.documentElement.scrollHeight,
    fonts_used_for_text: top(fonts, 8),
    web_fonts_loaded: Array.from(new Set(Array.from(document.fonts || []).filter(f => f.status === 'loaded').map(f => f.family.replace(/["']/g, '')))).slice(0, 15),
    type_scale: { h1: typeOf('h1'), h2: typeOf('h2'), h3: typeOf('h3'), body: typeOf('p'),
                  button: typeOf('button, [role=button], a[class*=btn], a[class*=button]') },
    text_colours: top(textColors, 10),
    background_colours: top(backgrounds, 10),
    visual_effects: top(effects, 10),
    motion: { elements_with_css_animation: animated, elements_with_transitions: transitioned,
              running_animations: document.getAnimations ? document.getAnimations().length : null },
    calls_to_action: ctas,
    navigation_links: Array.from(document.querySelectorAll('nav a, header a')).filter(visible).map(textOf).filter(Boolean).slice(0, 25),
    headings: Array.from(document.querySelectorAll('h1, h2, h3')).filter(visible).map(h => ({ level: h.tagName.toLowerCase(), text: textOf(h).slice(0, 140) })).slice(0, 40),
    images: { count: images.length,
              wide_images: images.filter(i => i.getBoundingClientRect().width >= 400).length,
              missing_alt: images.filter(i => !i.getAttribute('alt')).length,
              inline_svgs: document.querySelectorAll('svg').length,
              videos: document.querySelectorAll('video').length,
              samples: images.slice(0, 8).map(i => ({ alt: (i.getAttribute('alt') || '').slice(0, 80), src: (i.currentSrc || i.src || '').slice(0, 160), width_px: Math.round(i.getBoundingClientRect().width) })) },
    forms: { count: document.forms.length, fields: document.querySelectorAll('input:not([type=hidden]), select, textarea').length },
    social_proof_mentions: ['testimonial', 'review', 'case stud', 'case-stud', 'client', 'award', 'trusted by'].filter(k => html.includes(k)),
    accessibility: { skip_link: !!document.querySelector('a[href^="#main"], a[href^="#content"]'),
                     landmarks: ['header', 'nav', 'main', 'footer'].filter(t => document.querySelector(t)) },
  };
}
"""

# Measured at every width, to see how the layout responds.
LAYOUT_JS = r"""
() => {
  const visible = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const classText = el => typeof el.className === 'string' ? el.className : ((el.className && el.className.baseVal) || '');
  const h1 = Array.from(document.querySelectorAll('h1')).find(visible);
  return {
    viewport_width: window.innerWidth,
    page_height: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0),
    horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    visible_navigation_links: Array.from(document.querySelectorAll('nav a, header a')).filter(visible).length,
    menu_button_visible: Array.from(document.querySelectorAll('button, [role=button], a')).filter(visible)
      .some(el => /menu|hamburger|burger|toggle/i.test([el.getAttribute('aria-label') || '', classText(el), el.id || ''].join(' '))),
    body_font_size: document.body ? getComputedStyle(document.body).fontSize : null,
    h1_font_size: h1 ? getComputedStyle(h1).fontSize : null,
  };
}
"""

_RGB_RE = re.compile(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)")


def _as_hex(value):
    """rgb()/rgba() inside any string → #rrggbb, which is how people talk about colour."""
    if isinstance(value, str):
        def swap(m):
            r, g, b = (int(m.group(i)) for i in (1, 2, 3))
            out = f"#{r:02x}{g:02x}{b:02x}"
            alpha = m.group(4)
            if alpha is not None:
                a = float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)
                if a < 1:
                    out += f" at {round(a * 100)}% opacity"
            return out
        return _RGB_RE.sub(swap, value)
    if isinstance(value, list):
        return [_as_hex(v) for v in value]
    if isinstance(value, dict):
        return {k: _as_hex(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _installed_playwright_browsers() -> list[str]:
    """Chromium builds other Playwright installs left on this machine, newest first.

    Headless-shell builds come first: they are what Playwright itself uses for
    headless runs, and they start faster than the full browser.
    """
    root = os.getenv("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(os.getenv("LOCALAPPDATA", ""), "ms-playwright")

    def revision(exe_path):
        match = re.search(r"-(\d+)[\\/]", exe_path)
        return int(match.group(1)) if match else 0

    shells = glob.glob(os.path.join(root, "chromium_headless_shell-*", "chrome-headless-shell-*", "chrome-headless-shell.exe"))
    full = glob.glob(os.path.join(root, "chromium-*", "chrome-win*", "chrome.exe"))
    return sorted(shells, key=revision, reverse=True) + sorted(full, key=revision, reverse=True)


def _launch_browser(p, PlaywrightError):
    """(browser, label). Never downloads anything.

    Each Playwright release wants one exact Chromium build, and pip installs the
    package without it. Rather than fetch another ~150 MB, use a browser that is
    already here: Playwright's own build if present, then any Playwright Chromium
    another install left behind, then Edge (built into Windows), then Chrome.
    """
    try:
        return p.chromium.launch(headless=True), "playwright chromium"
    except PlaywrightError as e:
        if "Executable doesn't exist" not in str(e):
            raise
    for exe_path in _installed_playwright_browsers():
        try:
            label = os.path.basename(os.path.dirname(os.path.dirname(exe_path)))
            return p.chromium.launch(headless=True, executable_path=exe_path), label
        except PlaywrightError:
            continue
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(headless=True, channel=channel), channel
        except PlaywrightError:
            continue
    raise InspectorUnavailable(BROWSER_MISSING)


class _CaptureCancelled(Exception):
    """The caller stopped waiting, so the capture gives up at its next step."""


def _capture_sync(url: str, out_dir: str, url_guard, cancelled: threading.Event | None = None) -> dict:
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        raise InspectorUnavailable(PLAYWRIGHT_MISSING)

    if cancelled is None:
        cancelled = threading.Event()
    deadline = time.monotonic() + CAPTURE_TIMEOUT_S
    # Wait for a slot in short steps, so an inspection the caller has already given
    # up on doesn't take the next slot that frees.
    while not _BROWSER_SLOTS.acquire(timeout=1):
        if cancelled.is_set():
            raise _CaptureCancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("Too many website inspections are running at once; try again shortly.")
    try:
        os.makedirs(out_dir, exist_ok=True)
        shots, layouts, measured, final_url, blocked = [], {}, None, url, []
        blocked_requests = []
        if url_guard:
            # Made once, so each address is looked up once across all three widths.
            guard_requests = _guard_route(url_guard, blocked, blocked_requests)
            guard_sockets = _guard_websocket(url_guard, blocked_requests)
        with sync_playwright() as p:
            browser, browser_label = _launch_browser(p, PlaywrightError)
            try:
                for name, width, height in VIEWPORTS:
                    # A step already running can't be interrupted, but no new one starts.
                    if cancelled.is_set():
                        raise _CaptureCancelled()
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, device_scale_factor=1,
                        is_mobile=name == "mobile", has_touch=name != "desktop", locale="en-US",
                        # A service worker's requests skip routing, and so the guard.
                        # A one-off capture never needs one.
                        service_workers="block",
                    )
                    try:
                        if url_guard:
                            context.route("**/*", guard_requests)
                            context.route_web_socket(lambda _: True, guard_sockets)
                        page = context.new_page()
                        page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                        except PlaywrightError:
                            if blocked:
                                raise InspectorUnavailable(blocked[0])
                            raise
                        try:
                            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                        except PlaywrightError:
                            pass    # busy sites never go quiet; what has loaded is enough
                        page.wait_for_timeout(800)   # let entrance animations land
                        # Content that only loads as it scrolls into view (logo walls,
                        # lazy images) was blank in the screenshots and missing from the
                        # measurements, so walk down the page once before either.
                        try:
                            page.evaluate(SCROLL_JS, MAX_SCREENSHOT_HEIGHT)
                            page.wait_for_load_state("networkidle", timeout=LAZY_SETTLE_TIMEOUT_MS)
                        except PlaywrightError:
                            pass
                        final_url = page.url

                        layout = page.evaluate(LAYOUT_JS)
                        layouts[name] = layout
                        page_height = int(layout.get("page_height") or height)
                        shot_height = max(height, min(page_height, MAX_SCREENSHOT_HEIGHT))
                        path = os.path.join(out_dir, f"{name}.jpg")
                        page.screenshot(path=path, type="jpeg", quality=70, full_page=True,
                                        clip={"x": 0, "y": 0, "width": width, "height": shot_height})
                        shots.append({"view": name, "width": width, "path": path,
                                      "cut_off": page_height > MAX_SCREENSHOT_HEIGHT})
                        if name == "desktop":
                            measured = page.evaluate(DESKTOP_JS)
                    finally:
                        context.close()
            finally:
                browser.close()
        return {"final_url": final_url, "browser": browser_label, "screenshots": shots,
                "layouts": _as_hex(layouts), "measured": _as_hex(measured),
                "blocked_requests": list(dict.fromkeys(blocked_requests))[:20]}
    finally:
        _BROWSER_SLOTS.release()


def capture_site(url: str, out_dir: str, *, url_guard) -> dict:
    """Open the page at each width, measure it and screenshot it.

    Runs on its own thread: Playwright's sync API refuses to run on a thread
    with an asyncio loop, and research agents call their tools from inside one.
    Pass `url_guard=check_public_url` for anything an agent asked for.
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="website-inspector")
    cancelled = threading.Event()
    future = pool.submit(_capture_sync, url, out_dir, url_guard, cancelled)
    try:
        return future.result(timeout=CAPTURE_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        # The thread can't be killed, and it used to run on to the end: holding a browser
        # slot and writing into the folder the caller had already deleted. Tell it to stop
        # at its next step, and remove whatever it wrote once it has.
        cancelled.set()
        future.add_done_callback(lambda _: shutil.rmtree(out_dir, ignore_errors=True))
        raise
    finally:
        pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Visual analysis
# ---------------------------------------------------------------------------

def analyse_screenshots(url: str, capture: dict, focus: str | None = None):
    """(analysis, error): what a vision model sees in the screenshots, read with the measurements."""
    if not GEMINI_API_KEY:
        return None, "No GEMINI_API_KEY is set, so the screenshots were captured but not analysed."

    measured = json.dumps({"desktop": capture.get("measured"), "by_width": capture.get("layouts")},
                          ensure_ascii=False, default=str)
    if len(measured) > MAX_MEASURED_CHARS:
        measured = measured[:MAX_MEASURED_CHARS] + " …"
    focus = (focus or "").strip()[:MAX_FOCUS_CHARS]

    prompt = f"""You are a senior UI/UX analyst studying a real website: {url}

Screenshots of the page follow at mobile, tablet and desktop widths, each from the top of the page down (very long pages are cut off). After them comes data measured from the live page's HTML and computed CSS. Trust the data for exact values — font names, sizes, colours, counts — and use the screenshots for everything visual.
{f"Pay particular attention to: {focus}" if focus else ""}

Describe only what you can see or what the data shows. If a cookie banner, pop-up, loading state or blank area hides part of the page, say so instead of guessing.

Return a JSON object with these keys, each a few specific sentences:
{{
  "typography": "fonts, pairings, sizes and weights, and the impression they give",
  "colour_palette": "main colours with hex values, and how they are used",
  "imagery": "types of images (photography, illustration, 3D, stock or custom), their style and quality",
  "filters_and_effects": "overlays, filters, gradients, shadows, blur, and where they appear",
  "layout_and_whitespace": "grid, density, use of empty space, visual hierarchy",
  "navigation": "menu structure and how it adapts across widths",
  "calls_to_action": "main CTAs: wording, placement, colour and emphasis",
  "social_proof": "testimonials, logos, case studies, awards — and how they are presented",
  "emotional_journey": "what a visitor is meant to feel as they scroll, and the moments designed to build trust or urgency",
  "responsiveness": "how the layout changes from desktop to tablet to mobile, and anything that breaks",
  "accessibility": "visible accessibility strengths and problems, such as contrast and text size",
  "standout_ideas": ["specific things worth borrowing"]
}}"""

    contents = [prompt]
    try:
        for shot in capture.get("screenshots", []):
            with open(shot["path"], "rb") as f:
                contents.append(f"{shot['view'].upper()} screenshot ({shot['width']}px wide):")
                contents.append(types.Part.from_bytes(data=f.read(), mime_type="image/jpeg"))
        contents.append("MEASURED FROM THE LIVE PAGE:\n" + measured)

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        text = (response.text or "").strip()
        if not text:
            return None, "The vision model returned nothing for these screenshots."
        try:
            return json.loads(text), None
        except json.JSONDecodeError:
            return {"summary": text}, None
    except Exception as e:
        return None, f"The screenshots were captured but could not be analysed: {e}"


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def _inspection_dir(project_name: str, url: str) -> str:
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{parsed.hostname or 'site'}{parsed.path or ''}".lower()).strip("-")[:60]
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    return os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Website Inspections", f"{slug or 'site'}_{stamp}")


def inspect_website_impl(project_name: str, agent_id: str, url: str, focus: str | None = None) -> dict:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    problem = check_public_url(url)
    if problem:
        return {"status": "error", "action": "inspect_website", "url": url, "error": problem}

    out_dir = _inspection_dir(project_name, url)
    try:
        capture = capture_site(url, out_dir, url_guard=check_public_url)
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        if isinstance(e, InspectorUnavailable):
            message = str(e)
        elif isinstance(e, concurrent.futures.TimeoutError):
            message = f"Inspecting {url} took longer than {CAPTURE_TIMEOUT_S} seconds and was stopped."
        else:
            first_line = (str(e).strip().splitlines() or [type(e).__name__])[0]
            message = f"Could not open {url}: {first_line}"
        return {"status": "error", "action": "inspect_website", "url": url, "error": message}

    analysis, analysis_error = analyse_screenshots(url, capture, focus)

    result = {
        "status": "ok",
        "action": "inspect_website",
        "url": url,
        "final_url": capture["final_url"],
        "browser": capture["browser"],
        "screenshots": [
            {"view": s["view"], "width_px": s["width"], "path": os.path.relpath(s["path"], BASE_DIR),
             "cut_off_at_px": MAX_SCREENSHOT_HEIGHT if s["cut_off"] else None}
            for s in capture["screenshots"]
        ],
        "measured": capture["measured"],
        "responsive_layout": capture["layouts"],
        "visual_analysis": analysis,
    }
    if analysis_error:
        result["analysis_error"] = analysis_error
    if capture.get("blocked_requests"):
        # Parts of the page that were refused, so a missing image isn't read as the design.
        result["blocked_requests"] = capture["blocked_requests"]

    # Keep the measurements beside the screenshots. Otherwise only the agent ever
    # sees them, and a later claim like "the headings are Lato" can't be checked.
    measurements_path = os.path.join(out_dir, "inspection.json")
    try:
        with open(measurements_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        result["measurements_path"] = os.path.relpath(measurements_path, BASE_DIR)
    except OSError as e:
        print(f"[Inspector] Could not save measurements for {url}: {e}")
    return result
