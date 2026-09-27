"""
Where to get a service's API key: the page that creates the key, not the docs.

The Plugging Gate used to link whatever the research wrote as `doc_url` (the
service's documentation, by the prompt's own wording) and fell back to a fixed
list that pointed at home pages and help pages. Either way the user landed on
reading material and had to hunt for the key.

key_page(service) answers in this order:

  1. KNOWN_KEY_PAGES: a list of providers with the page that creates the key
     and the page that signs you up, checked against each provider's own docs.
  2. A page Jarvis has already checked for this service (data/key_pages.json).
  3. Candidate pages, each fetched and checked: the key link the research gave,
     the usual key paths on that site (/account/api-keys, /settings/tokens...),
     then what a web search suggests. A page counts only if its address or its
     text says it creates keys (or it is a sign-in that returns to such a page).
     Documentation, reference, blog and pricing pages are rejected.

When nothing can be confirmed, the answer says so, so the page can tell the user
the link is Jarvis's best guess rather than presenting it as the key page.
"""
import json
import os
import re
import threading
import time
from urllib.parse import urljoin, urlparse, unquote

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(BASE_DIR, "data", "key_pages.json")
FETCH_TIMEOUT = 8
CACHE_DAYS = 30
_LOCK = threading.Lock()

# kind: "api_key"      the page creates an API key or token;
#       "oauth_client" the page creates OAuth client credentials (Google Docs/Drive);
#       "none"         no key is needed at all.
# match: words in the service name that pick this provider, most specific first
# (YouTube and Maps before plain Google).
# verified: False for the deep links that come from the provider's docs or common
# use but could not be opened from here (they still beat a docs page). The key
# page is always the one a signed-in user creates keys on; signing in first is
# expected.
KNOWN_KEY_PAGES = [
    {"match": ["youtube"], "kind": "api_key", "key_url": "https://console.cloud.google.com/apis/credentials", "signup_url": "https://console.cloud.google.com/apis/library/youtube.googleapis.com", "verified": True, "note": "Pick or create a Google Cloud project, turn on the YouTube Data API (the sign-up link), then Create credentials > API key."},
    {"match": ["google maps", "maps", "places", "geocoding"], "kind": "api_key", "key_url": "https://console.cloud.google.com/google/maps-apis/credentials", "signup_url": "https://console.cloud.google.com/google/maps-apis/start", "verified": True, "note": "Google asks you to pick a project and set up billing before the key is created."},
    {"match": ["custom search", "programmable search"], "kind": "api_key", "key_url": "https://console.cloud.google.com/apis/credentials", "signup_url": "https://programmablesearchengine.google.com/controlpanel/create", "verified": True, "note": "Also needs a search engine ID from the sign-up link. Google has closed this API to new customers."},
    {"match": ["gemini", "google ai", "ai studio", "google search", "web search", "google genai"], "kind": "api_key", "key_url": "https://aistudio.google.com/app/apikey", "signup_url": "https://accounts.google.com/signup", "verified": True, "note": "Jarvis's web search runs on this Gemini key too."},
    {"match": ["google docs", "google drive", "google sheets", "gmail", "google calendar", "google slides", "google workspace"], "kind": "oauth_client", "key_url": "https://console.cloud.google.com/auth/clients/create", "signup_url": "https://console.cloud.google.com/auth/overview", "verified": False, "note": "Set up the consent screen first (the sign-up link), then create a Desktop app client."},
    {"match": ["openai", "chatgpt", "dall e", "whisper"], "kind": "api_key", "key_url": "https://platform.openai.com/api-keys", "signup_url": "https://platform.openai.com/signup", "verified": True, "note": ""},
    {"match": ["anthropic", "claude"], "kind": "api_key", "key_url": "https://platform.claude.com/settings/keys", "signup_url": "https://platform.claude.com/", "verified": True, "note": ""},
    {"match": ["github"], "kind": "api_key", "key_url": "https://github.com/settings/personal-access-tokens/new", "signup_url": "https://github.com/signup", "verified": True, "note": ""},
    {"match": ["stripe"], "kind": "api_key", "key_url": "https://dashboard.stripe.com/apikeys", "signup_url": "https://dashboard.stripe.com/register", "verified": True, "note": "Test-mode keys are at dashboard.stripe.com/test/apikeys."},
    {"match": ["paypal"], "kind": "api_key", "key_url": "https://developer.paypal.com/dashboard/applications/sandbox", "signup_url": "https://www.paypal.com/bizsignup/", "verified": True, "note": "Create an app to get its client ID and secret; switch to Live for real payments."},
    {"match": ["supadata"], "kind": "api_key", "key_url": "https://dash.supadata.ai", "signup_url": "https://dash.supadata.ai", "verified": False, "note": "The key is on the Supadata dashboard after you sign in."},
    {"match": ["notion"], "kind": "api_key", "key_url": "https://www.notion.so/profile/integrations", "signup_url": "https://www.notion.so/signup", "verified": True, "note": "Create a new internal integration and copy its secret."},
    {"match": ["slack"], "kind": "api_key", "key_url": "https://api.slack.com/apps?new_app=1", "signup_url": "https://slack.com/get-started#/createnew", "verified": True, "note": "The token appears under OAuth & Permissions after you install the app."},
    {"match": ["discord"], "kind": "api_key", "key_url": "https://discord.com/developers/applications", "signup_url": "https://discord.com/register", "verified": True, "note": "New Application, then Bot, then Reset Token."},
    {"match": ["hugging face", "huggingface", "hf"], "kind": "api_key", "key_url": "https://huggingface.co/settings/tokens/new", "signup_url": "https://huggingface.co/join", "verified": True, "note": ""},
    {"match": ["replicate"], "kind": "api_key", "key_url": "https://replicate.com/account/api-tokens", "signup_url": "https://replicate.com/signin", "verified": True, "note": ""},
    {"match": ["cohere"], "kind": "api_key", "key_url": "https://dashboard.cohere.com/api-keys", "signup_url": "https://dashboard.cohere.com/welcome/register", "verified": True, "note": ""},
    {"match": ["elevenlabs", "eleven labs"], "kind": "api_key", "key_url": "https://elevenlabs.io/app/settings/api-keys", "signup_url": "https://elevenlabs.io/app/sign-up", "verified": True, "note": ""},
    {"match": ["semantic scholar"], "kind": "api_key", "key_url": "https://www.semanticscholar.org/product/api#api-key-form", "signup_url": "https://www.semanticscholar.org/product/api#api-key-form", "verified": True, "note": "The key is optional: Semantic Scholar works without one, more slowly. You ask for one with a form."},
    {"match": ["serpapi", "serp"], "kind": "api_key", "key_url": "https://serpapi.com/manage-api-key", "signup_url": "https://serpapi.com/users/sign_up", "verified": True, "note": ""},
    {"match": ["mistral"], "kind": "api_key", "key_url": "https://console.mistral.ai/api-keys", "signup_url": "https://console.mistral.ai/", "verified": False, "note": ""},
    {"match": ["groq"], "kind": "api_key", "key_url": "https://console.groq.com/keys", "signup_url": "https://console.groq.com/login", "verified": True, "note": ""},
    {"match": ["perplexity"], "kind": "api_key", "key_url": "https://www.perplexity.ai/account/api/keys", "signup_url": "https://www.perplexity.ai/account/api", "verified": False, "note": ""},
    {"match": ["deepseek"], "kind": "api_key", "key_url": "https://platform.deepseek.com/api_keys", "signup_url": "https://platform.deepseek.com/sign_up", "verified": True, "note": ""},
    {"match": ["openrouter"], "kind": "api_key", "key_url": "https://openrouter.ai/settings/keys", "signup_url": "https://openrouter.ai/sign-up", "verified": True, "note": ""},
    {"match": ["twilio"], "kind": "api_key", "key_url": "https://console.twilio.com/us1/account/keys-credentials/api-keys", "signup_url": "https://www.twilio.com/try-twilio", "verified": True, "note": ""},
    {"match": ["sendgrid"], "kind": "api_key", "key_url": "https://app.sendgrid.com/settings/api_keys", "signup_url": "https://signup.sendgrid.com/", "verified": True, "note": ""},
    {"match": ["airtable"], "kind": "api_key", "key_url": "https://airtable.com/create/tokens/new", "signup_url": "https://airtable.com/signup", "verified": True, "note": ""},
    {"match": ["pexels"], "kind": "api_key", "key_url": "https://www.pexels.com/api/new/", "signup_url": "https://www.pexels.com/join/", "verified": False, "note": ""},
    {"match": ["unsplash"], "kind": "api_key", "key_url": "https://unsplash.com/oauth/applications/new", "signup_url": "https://unsplash.com/join", "verified": True, "note": "Create an application; its Access Key is the key."},
    {"match": ["newsapi", "news api"], "kind": "api_key", "key_url": "https://newsapi.org/register", "signup_url": "https://newsapi.org/register", "verified": True, "note": "The key is shown as soon as you register; later it is at newsapi.org/account."},
    {"match": ["openweathermap", "openweather", "weather"], "kind": "api_key", "key_url": "https://home.openweathermap.org/api_keys", "signup_url": "https://home.openweathermap.org/users/sign_up", "verified": True, "note": ""},
    {"match": ["tavily"], "kind": "api_key", "key_url": "https://app.tavily.com/home", "signup_url": "https://app.tavily.com/", "verified": True, "note": ""},
    {"match": ["exa"], "kind": "api_key", "key_url": "https://dashboard.exa.ai/api-keys", "signup_url": "https://dashboard.exa.ai/", "verified": True, "note": ""},
    {"match": ["firecrawl"], "kind": "api_key", "key_url": "https://www.firecrawl.dev/app/api-keys", "signup_url": "https://www.firecrawl.dev/signin/signup", "verified": True, "note": ""},
    {"match": ["apify"], "kind": "api_key", "key_url": "https://console.apify.com/settings/integrations", "signup_url": "https://console.apify.com/sign-up", "verified": True, "note": ""},
    {"match": ["assemblyai", "assembly ai"], "kind": "api_key", "key_url": "https://www.assemblyai.com/dashboard/api-keys", "signup_url": "https://www.assemblyai.com/dashboard/signup", "verified": False, "note": ""},
    {"match": ["deepgram"], "kind": "api_key", "key_url": "https://console.deepgram.com/", "signup_url": "https://console.deepgram.com/signup", "verified": False, "note": "Open your project, then API Keys, then Create a New API Key."},
    {"match": ["stability", "stable diffusion"], "kind": "api_key", "key_url": "https://platform.stability.ai/account/keys", "signup_url": "https://platform.stability.ai/", "verified": True, "note": ""},
    {"match": ["pinecone"], "kind": "api_key", "key_url": "https://app.pinecone.io/organizations/-/projects/-/keys", "signup_url": "https://app.pinecone.io/?sessionType=signup", "verified": False, "note": ""},
    {"match": ["spotify"], "kind": "api_key", "key_url": "https://developer.spotify.com/dashboard/create", "signup_url": "https://www.spotify.com/signup", "verified": True, "note": "Create an app to get its client ID and secret."},
    {"match": ["reddit"], "kind": "api_key", "key_url": "https://www.reddit.com/prefs/apps", "signup_url": "https://www.reddit.com/register/", "verified": True, "note": "Use 'create another app' at the bottom. Reddit may ask you to apply for API access first."},
    {"match": ["twitter", "x com", "x"], "kind": "api_key", "key_url": "https://console.x.com/", "signup_url": "https://console.x.com/", "verified": False, "note": "Create an app in the X developer console to get its keys."},
    {"match": ["telegram"], "kind": "api_key", "key_url": "https://t.me/BotFather", "signup_url": "https://telegram.org/", "verified": True, "note": "Message @BotFather and send /newbot to get the bot token."},
    {"match": ["shopify"], "kind": "api_key", "key_url": "https://dev.shopify.com/dashboard", "signup_url": "https://accounts.shopify.com/signup", "verified": False, "note": ""},
    {"match": ["hubspot"], "kind": "api_key", "key_url": "https://app.hubspot.com/l/private-apps", "signup_url": "https://app.hubspot.com/signup-hubspot/crm", "verified": False, "note": ""},
    {"match": ["trello"], "kind": "api_key", "key_url": "https://trello.com/power-ups/admin", "signup_url": "https://trello.com/signup", "verified": True, "note": "Create a Power-Up, then use its API key tab to make a key and token."},
    {"match": ["vercel"], "kind": "api_key", "key_url": "https://vercel.com/account/settings/tokens", "signup_url": "https://vercel.com/signup", "verified": True, "note": ""},
    {"match": ["cloudflare"], "kind": "api_key", "key_url": "https://dash.cloudflare.com/profile/api-tokens", "signup_url": "https://dash.cloudflare.com/sign-up", "verified": True, "note": ""},
    {"match": ["aws", "amazon web services", "s3"], "kind": "api_key", "key_url": "https://console.aws.amazon.com/iam/home#/security_credentials", "signup_url": "https://portal.aws.amazon.com/billing/signup", "verified": True, "note": "For an IAM user, open IAM > Users > the user > Security credentials > Create access key."},
    {"match": ["arxiv"], "kind": "none", "key_url": None, "signup_url": None, "verified": True, "note": "arXiv's API is public."},
]

LABELS = {
    "api_key": "Create your API key",
    "oauth_client": "Create your OAuth client",
    "signup": "Sign up first",
    "none": "No key needed",
    "unconfirmed": "Find your API key",
}

# What an address looks like when the page makes keys, signs you up, or only explains.
_KEY_PATH = re.compile(
    r"(api[-_]?keys?|apikeys?|/keys?(/|$|\?|#)|/tokens?(/|$|\?|#)|access[-_]?tokens?|access[-_]?keys?|"
    r"credentials|personal[-_]access|/integrations(/|$)|/developers?/(apps|applications|keys|portal)|"
    r"/security/(credentials|keys)|/app/apikey|/settings/(api|tokens|keys|developer))", re.I)
_SIGNUP_PATH = re.compile(r"(sign[-_]?up|register|/join(/|$)|get[-_]?started|create[-_]?account)", re.I)
_LOGIN_PATH = re.compile(r"(log[-_]?in|sign[-_]?in|/auth(/|$)|/session|accounts\.google\.com)", re.I)
_INFO_PATH = re.compile(r"(/docs?(/|$)|documentation|/reference|/guides?(/|$)|/blog|/pricing|/help(/|$)|"
                        r"/support|/faq|/learn|/tutorials?|/changelog|/about)", re.I)
_KEY_TEXT = re.compile(r"(create (a |an |new |your )?(secret |api |access )?(key|token)|generate (a |an |new )?"
                       r"(api |access )?(key|token)|new (api |secret |access )?key|api keys|access tokens|"
                       r"personal access token|revoke key)", re.I)
_SIGNUP_TEXT = re.compile(r"(create (an |your )?account|sign up|get started for free|register)", re.I)

# Paths tried on a provider's site when the research only knew its address.
GUESS_PATHS = ("/account/api-keys", "/settings/api-keys", "/dashboard/api-keys", "/api-keys", "/settings/keys",
               "/account/keys", "/settings/tokens", "/dashboard/keys", "/keys", "/developer")


def _clean(service: str) -> str:
    # No camelCase split: "GitHub" must stay one word, not "git hub".
    text = re.sub(r"[^a-z0-9]+", " ", (service or "").lower())
    return " " + re.sub(r"\b(api|apis|mcp|service|server|connector)\b", " ", text).strip() + " "


def known(service: str) -> dict | None:
    """The curated entry for a service, or None."""
    name = _clean(service)
    for entry in KNOWN_KEY_PAGES:
        if any(f" {word} " in name or name.strip() == word for word in entry["match"]):
            return entry
    return None


def _answer(service, kind, url, source, confirmed, signup_url=None, note="") -> dict:
    return {"service": service, "kind": kind, "url": url, "label": LABELS.get(kind if confirmed or kind == "none"
                                                                             else "unconfirmed", "Open"),
            "confirmed": confirmed, "source": source, "signup_url": signup_url, "note": note}


# ---------------------------------------------------------------------------
# Checking a page
# ---------------------------------------------------------------------------

def classify_url(url: str) -> str | None:
    """From the address alone: "api_key", "signup", "login", "info" or None."""
    parsed = urlparse(url or "")
    path = unquote(parsed.path + ("?" + parsed.query if parsed.query else ""))
    on_docs_site = re.match(r"(docs|help|support|learn)\.", parsed.netloc or "")
    if (_INFO_PATH.search(parsed.path) or on_docs_site) and not _KEY_PATH.search(parsed.path):
        return "info"
    if _KEY_PATH.search(path):
        return "api_key"
    if _SIGNUP_PATH.search(path):
        return "signup"
    if _LOGIN_PATH.search(parsed.netloc + parsed.path):
        return "login"
    return None


def check_page(url: str, fetch=None) -> dict:
    """Fetch a page and say what it is: {"kind", "url", "reason"}.

    kind is "api_key" (creates keys), "signup", "info" (explains, rejected),
    "missing" (404 or unreachable) or None (can't tell).
    A sign-in page that returns to a key page counts as the key page: that is
    where the user lands after signing in.
    """
    fetch = fetch or (lambda u: requests.get(u, timeout=FETCH_TIMEOUT, allow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0 Jarvis key-page check"}))
    try:
        resp = fetch(url)
    except requests.RequestException as e:
        return {"kind": "missing", "url": url, "reason": f"could not open it ({type(e).__name__})"}
    final = getattr(resp, "url", url) or url
    if resp.status_code == 404 or resp.status_code >= 500:
        return {"kind": "missing", "url": url, "reason": f"HTTP {resp.status_code}"}
    asked, landed = classify_url(url), classify_url(final)
    text = (resp.text or "")[:200000]
    if asked == "info":
        return {"kind": "info", "url": url, "reason": "a documentation or information page"}
    if asked == "api_key" and (landed in ("api_key", "login", None) or _KEY_TEXT.search(text)):
        # Key pages sit behind a sign-in; landing on one with the key page to come back to is right.
        return {"kind": "api_key", "url": url, "reason": "creates keys" if landed == "api_key" else "sign in, then keys"}
    if _KEY_TEXT.search(text) and landed != "info":
        return {"kind": "api_key", "url": final if landed == "api_key" else url, "reason": "the page talks about creating keys"}
    if asked == "signup" or landed == "signup" or (_SIGNUP_TEXT.search(text) and landed != "info"):
        return {"kind": "signup", "url": url, "reason": "sign-up page"}
    if landed == "info":
        return {"kind": "info", "url": url, "reason": "sends you to documentation"}
    return {"kind": None, "url": url, "reason": "nothing on it says it creates keys"}


# ---------------------------------------------------------------------------
# Cache of pages already checked
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_cache(service: str, answer: dict) -> None:
    with _LOCK:
        cache = _load_cache()
        cache[service] = {**answer, "checked_at": time.time()}
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
        os.replace(tmp, CACHE_PATH)


# ---------------------------------------------------------------------------
# Finding the page
# ---------------------------------------------------------------------------

def _search_candidates(service: str) -> list[str]:
    if not os.getenv("GEMINI_API_KEY"):
        return []
    try:
        from agents.tool_executor import web_search_impl
    except Exception:
        return []
    name = service.replace("_", " ")
    found = web_search_impl("__key_pages__", "key_pages",
                            f"exact URL of the page where a developer creates a new API key for {name} "
                            "(the dashboard page, not the documentation)")
    if found.get("status") != "ok":
        return []
    urls = [s.get("url") for s in found.get("sources") or [] if s.get("url")]
    urls += re.findall(r"https?://[^\s)\"'<>\]]+", found.get("summary") or "")
    return [u.rstrip(".,") for u in urls]


def _guesses(hints: list[str]) -> list[str]:
    out = []
    for hint in hints:
        parsed = urlparse(hint or "")
        if not parsed.netloc:
            continue
        root = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        hosts = [root]
        bare = re.sub(r"^(www|docs|developer|developers|api)\.", "", parsed.netloc)
        for sub in ("app", "dashboard", "console", "platform"):
            hosts.append(f"https://{sub}.{bare}")
        out += [urljoin(host, path) for host in dict.fromkeys(hosts) for path in GUESS_PATHS]
    return out


def find_key_page(service: str, hints=(), fetch=None, search=None, max_checks: int = 12) -> dict:
    """Check candidate pages until one creates keys; a sign-up page is kept as a fallback."""
    hints = [h for h in (hints or ()) if isinstance(h, str) and h.startswith(("http://", "https://"))]
    search = search or _search_candidates
    signup, first_hint = None, hints[0] if hints else None
    checked = 0
    for stage in ("hints", "guesses", "search"):
        candidates = hints if stage == "hints" else (_guesses(hints) if stage == "guesses" else search(service))
        for url in dict.fromkeys(candidates):
            if checked >= max_checks:
                break
            # Guessed paths are only worth a request when the address itself looks right.
            if stage == "guesses" and classify_url(url) != "api_key":
                continue
            checked += 1
            result = check_page(url, fetch=fetch)
            if result["kind"] == "api_key":
                return _answer(service, "api_key", result["url"], "checked", True, signup_url=signup,
                               note=f"Checked: {result['reason']}.")
            if result["kind"] == "signup" and not signup:
                signup = result["url"]
    if signup:
        return _answer(service, "signup", signup, "checked", True,
                       note="Jarvis found the sign-up page but not the key page; the key is usually under your "
                            "account or dashboard once you have signed up.")
    return _answer(service, "unconfirmed", _not_docs(first_hint), "hint", False,
                   note="Jarvis could not confirm a page that creates the key. Sign in there and look for "
                        "API keys in your account or dashboard.")


def _not_docs(url: str | None) -> str | None:
    """A docs link turned into the service's own site, which has the sign-in and sign-up buttons."""
    if not url or classify_url(url) != "info":
        return url
    host = re.sub(r"^(docs|developer|developers|api|help|support)\.", "", urlparse(url).netloc)
    return f"https://{host}/" if host else None


def key_page(service: str, hints=(), fetch=None, search=None, use_cache: bool = True) -> dict:
    """Where to send the user to get this service's key. Never raises."""
    entry = known(service)
    if entry:
        answer = _answer(service, entry["kind"], entry.get("key_url"), "known", True,
                         signup_url=entry.get("signup_url"), note=entry.get("note", ""))
        answer["verified"] = entry.get("verified", True)
        return answer
    if use_cache:
        cached = _load_cache().get(service)
        # A confirmed page is kept for a month; a failed search is tried again the next day.
        days = CACHE_DAYS if (cached or {}).get("confirmed") else 1
        if cached and time.time() - float(cached.get("checked_at") or 0) < days * 86400:
            return {k: v for k, v in cached.items() if k != "checked_at"}
    try:
        answer = find_key_page(service, hints, fetch=fetch, search=search)
    except Exception as e:
        print(f"[Key pages] Could not look up {service}: {e}")
        first = next((h for h in hints or () if isinstance(h, str) and h.startswith("http")), None)
        return _answer(service, "unconfirmed", _not_docs(first), "hint", False, note="Jarvis could not check any page.")
    _save_cache(service, answer)
    return answer
