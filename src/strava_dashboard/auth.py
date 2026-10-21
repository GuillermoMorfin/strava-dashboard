"""Strava OAuth 2.0: authorization, token exchange, refresh, and storage.

Flow (run once with `strava-dash auth`):
  1. Open the Strava authorize page in the browser.
  2. Strava redirects to http://localhost:<port>/callback?code=...&state=...&scope=...
  3. A one-shot local HTTP server captures the code, checks `state` and scopes.
  4. The code is exchanged for access + refresh tokens, saved to a 0600 JSON file.

Access tokens expire after ~6 hours; `get_valid_tokens` refreshes them on demand
and persists the (possibly rotated) refresh token.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
# activity:read_all includes private activities; profile:read_all is needed for HR zones.
SCOPES = ("read", "activity:read_all", "profile:read_all")
REQUIRED_SCOPES = frozenset({"activity:read_all"})


class AuthError(RuntimeError):
    """Raised when authorization fails or tokens are unavailable."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int
    athlete_id: int | None = None

    def is_expired(self, now: float | None = None, leeway: int = 60) -> bool:
        now = time.time() if now is None else now
        return now >= self.expires_at - leeway

    @classmethod
    def from_response(cls, data: dict, athlete_id: int | None = None) -> Tokens:
        athlete = data.get("athlete") or {}
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
            athlete_id=athlete.get("id", athlete_id),
        )


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(http: httpx.Client, payload: dict) -> dict:
    response = http.post(TOKEN_URL, data=payload)
    if response.status_code != 200:
        raise AuthError(f"Token request failed ({response.status_code}): {response.text}")
    return response.json()


def exchange_code(http: httpx.Client, client_id: str, client_secret: str, code: str) -> Tokens:
    data = _post_token(
        http,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    return Tokens.from_response(data)


def refresh_tokens(
    http: httpx.Client, client_id: str, client_secret: str, tokens: Tokens
) -> Tokens:
    data = _post_token(
        http,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        },
    )
    return Tokens.from_response(data, athlete_id=tokens.athlete_id)


class TokenStore:
    """Persists tokens as JSON with owner-only permissions."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Tokens | None:
        if not self.path.exists():
            return None
        return Tokens(**json.loads(self.path.read_text()))

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(tokens), f, indent=2)
        os.replace(tmp, self.path)


def get_valid_tokens(
    store: TokenStore,
    http: httpx.Client,
    client_id: str,
    client_secret: str,
    now: float | None = None,
) -> Tokens:
    tokens = store.load()
    if tokens is None:
        raise AuthError("Not authorized yet. Run `strava-dash auth` first.")
    if tokens.is_expired(now=now):
        tokens = refresh_tokens(http, client_id, client_secret, tokens)
        store.save(tokens)
    return tokens


@dataclass
class CallbackResult:
    code: str | None = None
    state: str | None = None
    scope: str | None = None
    error: str | None = None


def parse_callback(path: str) -> CallbackResult:
    query = parse_qs(urlparse(path).query)

    def first(key: str) -> str | None:
        return query.get(key, [None])[0]

    return CallbackResult(
        code=first("code"), state=first("state"), scope=first("scope"), error=first("error")
    )


def validate_callback(result: CallbackResult, expected_state: str) -> str:
    """Return the auth code, or raise AuthError if the callback is unusable."""
    if result.error:
        raise AuthError(f"Strava returned an error: {result.error}")
    if not result.state or not secrets.compare_digest(result.state, expected_state):
        raise AuthError("OAuth state mismatch; possible CSRF. Try `strava-dash auth` again.")
    if not result.code:
        raise AuthError("No authorization code in callback.")
    granted = set((result.scope or "").split(","))
    missing = REQUIRED_SCOPES - granted
    if missing:
        raise AuthError(
            f"Missing required scope(s): {', '.join(sorted(missing))}. "
            "Re-run auth and leave 'View data about your private activities' checked."
        )
    return result.code


class CallbackServer:
    """One-shot server for http://localhost:<port>/callback.

    Binding happens in the constructor so a busy port fails before the user is
    sent to Strava.
    """

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.captured: list[CallbackResult] = []
        self._server = HTTPServer((host, port), self._handler_class())
        self._server.timeout = 0.5

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def _handler_class(self):
        captured = self.captured

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib naming)
                if urlparse(self.path).path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                captured.append(parse_callback(self.path))
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h2>Strava authorization received.</h2><p>You can close this tab.</p>"
                )

            def log_message(self, *args):  # keep the terminal quiet
                pass

        return Handler

    def wait(self, timeout: float = 300) -> CallbackResult:
        deadline = time.monotonic() + timeout
        try:
            while not self.captured and time.monotonic() < deadline:
                self._server.handle_request()
        finally:
            self._server.server_close()
        if not self.captured:
            raise AuthError(f"Timed out after {timeout:.0f}s waiting for the Strava redirect.")
        return self.captured[0]


def authorize_interactive(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    port: int,
    store: TokenStore,
    open_browser: bool = True,
) -> Tokens:
    try:
        server = CallbackServer(port)
    except OSError as exc:
        raise AuthError(
            f"Can't listen on port {port} ({exc}). Set STRAVA_REDIRECT_PORT to a free port."
        ) from exc
    state = secrets.token_urlsafe(24)
    url = build_authorize_url(client_id, redirect_uri, state)
    print("Opening Strava to authorize this app. If the browser doesn't open, visit:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)
    code = validate_callback(server.wait(), state)
    with httpx.Client(timeout=30) as http:
        tokens = exchange_code(http, client_id, client_secret, code)
    store.save(tokens)
    return tokens
