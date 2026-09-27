"""The "get your API key" link opens the page that creates the key, not the docs.

  * Known providers answer from a checked list: the key page and the sign-up page.
  * Any other service gets candidate pages checked one by one: the research's
    key link, the usual key paths on its site, then a web search. Documentation,
    reference and pricing pages are rejected; a sign-in that returns to a key
    page counts, since that is where the user lands after signing in.
  * When nothing can be confirmed, the answer says so instead of passing off a
    docs page as the key page.
  * The Plugging Gate and the Control room use these answers.

No network: pages are served by a fake fetch.
"""
import os, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("GEMINI_API_KEY", None)

from connectors import key_pages as kp


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


kp.CACHE_PATH = os.path.join(tempfile.mkdtemp(prefix="jarvis_keypages_"), "key_pages.json")


class Resp:
    def __init__(self, status=200, text="", url=None):
        self.status_code, self.text, self.url = status, text, url


def site(pages):
    """A fake web: url -> Resp; anything else is a 404. Records what was asked."""
    asked = []

    def fetch(url):
        asked.append(url)
        page = pages.get(url)
        return page(url) if callable(page) else (page or Resp(404, "", url))
    return fetch, asked


# ---- 1. the checked list ------------------------------------------------------------------
check("every known entry has a kind and a way to match it",
      all(e.get("kind") in ("api_key", "oauth_client", "none") and e.get("match") for e in kp.KNOWN_KEY_PAGES))
check("every known key page is an https address, not a docs page",
      all(e["kind"] == "none" or (e["key_url"].startswith("https://") and kp.classify_url(e["key_url"]) != "info")
          for e in kp.KNOWN_KEY_PAGES))
openai = kp.key_page("openai_api")
check("OpenAI opens its key page", openai["url"] == "https://platform.openai.com/api-keys" and openai["confirmed"])
check("with the label that says what the page does", openai["label"] == "Create your API key")
check("and the sign-up page for people with no account", bool(openai["signup_url"]))
youtube = kp.key_page("youtube_api")
check("YouTube matches before plain Google", youtube["url"] and "console.cloud.google.com" in youtube["url"])
docs = kp.key_page("google_docs_api")
check("Google Docs needs an OAuth client, and says so", docs["kind"] == "oauth_client"
      and docs["label"] == "Create your OAuth client")
check("arXiv needs no key", kp.key_page("arxiv_api")["kind"] == "none")
check("names are matched loosely", kp.key_page("GitHub API")["url"] == kp.key_page("github")["url"])
check("a word inside another word doesn't match", kp.known("hubspotter_tool") is None)

# ---- 2. what an address says ---------------------------------------------------------------
check("a docs page is information", kp.classify_url("https://docs.example.com/docs/authentication") == "info")
check("an API reference is information", kp.classify_url("https://example.com/reference/intro") == "info")
check("a settings token page makes keys", kp.classify_url("https://example.com/settings/tokens") == "api_key")
check("a dashboard key page makes keys", kp.classify_url("https://app.example.com/dashboard/api-keys") == "api_key")
check("a sign-up page is a sign-up", kp.classify_url("https://example.com/signup") == "signup")
check("a home page says nothing", kp.classify_url("https://example.com/") is None)

# ---- 3. checking a page --------------------------------------------------------------------
fetch, _ = site({
    "https://app.example.com/account/api-keys": Resp(200, "<h1>Log in</h1>", "https://app.example.com/login?next=/account/api-keys"),
    "https://example.com/docs/auth": Resp(200, "To create an API key, open your dashboard."),
    "https://example.com/start": Resp(200, "<button>Create new secret key</button>", "https://example.com/start"),
    "https://example.com/team": Resp(200, "We are a company.", "https://example.com/team"),
})
check("a key page behind a sign-in counts as the key page",
      kp.check_page("https://app.example.com/account/api-keys", fetch)["kind"] == "api_key")
check("a docs page is rejected even when it talks about keys",
      kp.check_page("https://example.com/docs/auth", fetch)["kind"] == "info")
check("a page whose text creates keys counts", kp.check_page("https://example.com/start", fetch)["kind"] == "api_key")
check("a page that doesn't exist is missing", kp.check_page("https://example.com/nope", fetch)["kind"] == "missing")
check("a page that says nothing about keys doesn't count", kp.check_page("https://example.com/team", fetch)["kind"] is None)

# ---- 4. finding the page for a service nobody listed ---------------------------------------
fetch, asked = site({
    "https://docs.newsvc.io/getting-started": Resp(200, "Welcome. Read the guide.", "https://docs.newsvc.io/getting-started"),
    "https://app.newsvc.io/settings/api-keys": Resp(200, "Sign in", "https://app.newsvc.io/login?return=/settings/api-keys"),
})
found = kp.key_page("newsvc", hints=["https://docs.newsvc.io/getting-started"], fetch=fetch, search=lambda s: [])
check("the research's docs link is not used as the key page", found["url"] != "https://docs.newsvc.io/getting-started")
check("the usual key path on the service's app site is found and checked",
      found["url"] == "https://app.newsvc.io/settings/api-keys" and found["confirmed"] and found["source"] == "checked")
check("only addresses that look like key pages are tried", all(kp.classify_url(u) == "api_key" or u in
      ("https://docs.newsvc.io/getting-started",) for u in asked))

before = len(asked)
again = kp.key_page("newsvc", hints=["https://docs.newsvc.io/getting-started"], fetch=fetch, search=lambda s: [])
check("a page already checked is remembered, not fetched again", again["url"] == found["url"] and len(asked) == before)

fetch, _ = site({"https://searched.example/dev/keys": Resp(200, "Generate API key", "https://searched.example/dev/keys")})
searched = kp.key_page("otherthing", hints=[], fetch=fetch, search=lambda s: ["https://searched.example/dev/keys"])
check("a web search result is used once it checks out", searched["url"] == "https://searched.example/dev/keys"
      and searched["confirmed"])

fetch, _ = site({"https://join.example/signup": Resp(200, "Create an account", "https://join.example/signup")})
signup = kp.key_page("joinme", hints=["https://join.example/signup"], fetch=fetch, search=lambda s: [])
check("with only a sign-up page found, it sends you to sign up and says the key comes after",
      signup["kind"] == "signup" and signup["label"] == "Sign up first" and "sign" in signup["note"].lower())

fetch, _ = site({"https://docs.lost.example/intro": Resp(200, "Docs", "https://docs.lost.example/intro")})
lost = kp.key_page("lostsvc", hints=["https://docs.lost.example/intro"], fetch=fetch, search=lambda s: [])
check("when nothing checks out, the answer is marked unconfirmed", not lost["confirmed"]
      and lost["label"] == "Find your API key" and "could not confirm" in lost["note"])
check("and it still never opens the docs: it opens the service's own site instead",
      lost["url"] == "https://lost.example/")


def broken(url):
    raise RuntimeError("boom")


check("a lookup that blows up never breaks the gate",
      kp.key_page("crashy", hints=["https://x.example/a"], fetch=broken, search=lambda s: [], use_cache=False)["confirmed"] is False)

# ---- 5. the pages use it ---------------------------------------------------------------------
plan = open(os.path.join(ROOT, "plan.html"), encoding="utf-8").read()
check("the Plugging Gate no longer links the research's docs page first", "let url = r.doc_url" not in plan)
check("the old list of home and help pages is gone", "kafka.apache.org" not in plan and "arxiv.org/help/api" not in plan)
check("the gate shows the checked key page", "keyPageLinkHtml(r)" in plan and "fillKeyPageLinks(content)" in plan)
room = open(os.path.join(ROOT, "control_room.html"), encoding="utf-8").read()
check("the Control room's connect form shows it too", "showKeyPage(service)" in room and "/api/key-page" in room)
coord = open(os.path.join(ROOT, "multi_agent_coordinator.py"), encoding="utf-8").read()
check("the gate's services get a key page before the gate opens", "await attach_key_pages(tool_recs)" in coord)
for f in ("agents/brain.py", "agents/research_agent.py"):
    check(f"the research is asked for the key page, not only docs ({f})",
          '"key_url"' in open(os.path.join(ROOT, f), encoding="utf-8").read())

print("\nAll key page checks passed.")
