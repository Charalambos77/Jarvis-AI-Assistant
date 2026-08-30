"""
Generic OAuth 2.0 Authorization Code flow for installed/desktop apps, using
the loopback-redirect method — the only one Google (and most modern OAuth2
providers) still supports; the old copy-paste "out-of-band" flow was fully
deprecated by Google in January 2023 and no longer works at all.

This is provider-agnostic on purpose: give it an authorize_url, token_url,
client_id/secret, and scopes, and it drives the entire browser-consent dance
automatically — opens the real browser, runs a temporary local server to
catch the redirect, exchanges the code for tokens. Onboarding a *new* OAuth
provider (Notion, Slack, GitHub, ...) is just adding its endpoints/scopes to
OAUTH_PROVIDERS below; no new flow code needed per service.

Two text fields (Client ID/Secret) can never "connect" anything on their
own — this module is what actually has to run for that to mean something.
"""
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import requests

CALLBACK_TIMEOUT_SECONDS = 180


class _CallbackResult:
    def __init__(self):
        self.code = None
        self.error = None
        self.event = threading.Event()


def _make_handler(expected_state: str, result: _CallbackResult):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            # Ignore stray requests (e.g. browsers probing /favicon.ico) that
            # don't carry an OAuth response — keep waiting for the real one.
            if "code" not in qs and "error" not in qs:
                self.send_response(404)
                self.end_headers()
                return

            state = qs.get("state", [None])[0]
            if state != expected_state:
                result.error = "state_mismatch"
            elif "error" in qs:
                result.error = qs["error"][0]
            elif "code" in qs:
                result.code = qs["code"][0]
            else:
                result.error = "no_code_returned"

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if result.error:
                self.wfile.write(b"<html><body><h2>Connection failed.</h2>You can close this tab and return to Jarvis.</body></html>")
            else:
                self.wfile.write(b"<html><body><h2>Connected.</h2>You can close this tab and return to Jarvis.</body></html>")
            result.event.set()

        def log_message(self, format, *args):
            pass  # silence default per-request stdout logging

    return Handler


def run_installed_app_flow(
    authorize_url: str,
    token_url: str,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    extra_authorize_params: dict | None = None,
) -> dict:
    """
    Drives a full OAuth2 Authorization Code + loopback-redirect flow:
    opens the user's real browser to `authorize_url`, waits (blocking, up to
    CALLBACK_TIMEOUT_SECONDS) for the provider to redirect back to a
    temporary local server, then exchanges the returned code for tokens.

    Returns {"status": "ok", "access_token", "refresh_token", "expires_in", "scope"}
    or {"status": "error", "error": "..."}.
    """
    state = secrets.token_urlsafe(16)
    result = _CallbackResult()
    handler = _make_handler(state, result)

    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",   # ask for a refresh_token (harmless if a provider ignores it)
        "prompt": "consent",        # force refresh_token issuance even on repeat connects
    }
    if extra_authorize_params:
        params.update(extra_authorize_params)

    full_authorize_url = f"{authorize_url}?{urllib.parse.urlencode(params)}"

    try:
        browser_opened = webbrowser.open(full_authorize_url)
        got_response = result.event.wait(timeout=CALLBACK_TIMEOUT_SECONDS)
    finally:
        server.shutdown()
        server.server_close()

    if not got_response:
        return {
            "status": "error",
            "error": "timed_out_waiting_for_browser_consent",
            "authorize_url": full_authorize_url,
            "browser_opened": browser_opened,
        }

    if result.error:
        return {"status": "error", "error": result.error}

    try:
        token_resp = requests.post(
            token_url,
            data={
                "code": result.code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        tokens = token_resp.json()
    except Exception as e:
        return {"status": "error", "error": f"token_exchange_failed: {e}"}

    if "access_token" not in tokens:
        return {"status": "error", "error": f"no access_token in token response: {tokens}"}

    return {
        "status": "ok",
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_in": tokens.get("expires_in"),
        "scope": tokens.get("scope"),
    }


def refresh_access_token(token_url: str, client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Exchanges a stored refresh_token for a fresh access_token."""
    try:
        resp = requests.post(
            token_url,
            data={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()
        if "access_token" not in tokens:
            return {"status": "error", "error": f"no access_token in refresh response: {tokens}"}
        return {"status": "ok", "access_token": tokens["access_token"], "expires_in": tokens.get("expires_in")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# Known OAuth2 providers — add a new service here (authorize_url, token_url,
# scopes) and the whole flow (browser consent, redirect capture, token
# exchange, refresh) works automatically with no other code changes needed.
# Google's endpoints are shared across all Google APIs; only the scopes
# differ per service.
OAUTH_PROVIDERS: dict[str, dict] = {
    "google_docs_api": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive.file"],
    },
    "google_drive_api": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # Same scopes as google_docs_api on purpose: Brain sometimes names this
        # capability "google_drive_api" even though the actual real handler
        # (google_docs_create_impl) calls the Docs API, which needs the
        # `documents` scope specifically — drive.file alone isn't enough and
        # would 403. Whichever name gets connected, the token must work.
        "scopes": ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive.file"],
    },
}


def get_oauth_provider(service_name: str) -> dict | None:
    key = (service_name or "").lower().replace("-", "_").replace(" ", "_")
    return OAUTH_PROVIDERS.get(key)
