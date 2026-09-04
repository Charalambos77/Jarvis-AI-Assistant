"""A changed HTML file must never be served from the webview's cache.

A fix that is genuinely in the file but invisible in the running app sends the
bug hunt to entirely the wrong place. Pages are marked no-store; API responses
are left alone.
"""
import sys

import jarvis

app = jarvis.app.test_client()


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


for route in ("/plan.html", "/command_center.html", "/execution.html", "/command-center", "/"):
    r = app.get(route)
    cc = r.headers.get("Cache-Control", "")
    check(f"{route} is served ({r.status_code})", r.status_code == 200)
    check(f"{route} is marked no-store", "no-store" in cc)
    check(f"{route} must revalidate", "must-revalidate" in cc)

# No validators, so the browser has nothing to revalidate with and cannot be
# sent back to a cached copy by a 304.
r = app.get("/plan.html")
check("no ETag is handed out for a page", r.headers.get("ETag") is None)
check("no Last-Modified is handed out for a page", r.headers.get("Last-Modified") is None)

# Every load returns the file as it is on disk, in full.
again = app.get("/plan.html")
check("a repeat load returns the full page, not an empty 304",
      again.status_code == 200 and len(again.data) > 1000)
check("the page served matches the file on disk",
      again.data == open("plan.html", "rb").read())

# API responses keep their normal caching behaviour.
r = app.get("/plans")
check("API responses are left alone", "no-store" not in r.headers.get("Cache-Control", ""))
check("API still returns JSON", r.mimetype == "application/json")

print("\nAll checks passed.")
