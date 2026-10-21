import json
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from strava_dashboard.auth import (
    TOKEN_URL,
    AuthError,
    CallbackResult,
    CallbackServer,
    Tokens,
    TokenStore,
    build_authorize_url,
    exchange_code,
    get_valid_tokens,
    parse_callback,
    validate_callback,
)


def token_payload(access="acc", refresh="ref", expires_at=2_000_000_000, athlete=True):
    data = {"access_token": access, "refresh_token": refresh, "expires_at": expires_at}
    if athlete:
        data["athlete"] = {"id": 42}
    return data


def mock_http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_authorize_url_has_required_params():
    url = build_authorize_url("123", "http://localhost:8765/callback", "xyz")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["123"]
    assert q["redirect_uri"] == ["http://localhost:8765/callback"]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["xyz"]
    assert "activity:read_all" in q["scope"][0].split(",")


def test_exchange_code_posts_form_and_parses_tokens():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = parse_qs(request.content.decode())
        return httpx.Response(200, json=token_payload())

    tokens = exchange_code(mock_http(handler), "123", "secret", "the-code")
    assert seen["url"] == TOKEN_URL
    assert seen["body"]["grant_type"] == ["authorization_code"]
    assert seen["body"]["code"] == ["the-code"]
    assert tokens == Tokens("acc", "ref", 2_000_000_000, 42)


def test_exchange_code_error_raises():
    http = mock_http(lambda r: httpx.Response(400, json={"message": "Bad Request"}))
    with pytest.raises(AuthError, match="400"):
        exchange_code(http, "123", "secret", "bad")


def test_token_store_roundtrip_with_private_permissions(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.load() is None
    tokens = Tokens("a", "r", 100, 7)
    store.save(tokens)
    assert store.load() == tokens
    mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
    assert mode == 0o600


def test_get_valid_tokens_returns_fresh_tokens_without_network(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.save(Tokens("a", "r", expires_at=10_000, athlete_id=1))

    def handler(request):
        raise AssertionError("should not refresh")

    tokens = get_valid_tokens(store, mock_http(handler), "id", "secret", now=1_000)
    assert tokens.access_token == "a"


def test_get_valid_tokens_refreshes_and_persists_rotated_token(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.save(Tokens("old", "old-refresh", expires_at=1_000, athlete_id=1))

    def handler(request):
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["refresh_token"]
        assert body["refresh_token"] == ["old-refresh"]
        # Refresh responses don't include the athlete.
        return httpx.Response(200, json=token_payload("new", "new-refresh", 99_999, athlete=False))

    tokens = get_valid_tokens(store, mock_http(handler), "id", "secret", now=1_000)
    assert tokens == Tokens("new", "new-refresh", 99_999, 1)
    assert json.loads((tmp_path / "t.json").read_text())["refresh_token"] == "new-refresh"


def test_get_valid_tokens_without_auth_raises(tmp_path):
    with pytest.raises(AuthError, match="strava-dash auth"):
        get_valid_tokens(TokenStore(tmp_path / "none.json"), mock_http(None), "id", "s")


def test_parse_callback():
    r = parse_callback("/callback?state=s&code=c&scope=read,activity:read_all")
    assert r == CallbackResult(code="c", state="s", scope="read,activity:read_all")


@pytest.mark.parametrize(
    "result, match",
    [
        (CallbackResult(error="access_denied"), "access_denied"),
        (CallbackResult(code="c", state="wrong", scope="activity:read_all"), "state"),
        (CallbackResult(state="s", scope="activity:read_all"), "No authorization code"),
        (CallbackResult(code="c", state="s", scope="read"), "activity:read_all"),
    ],
)
def test_validate_callback_rejects_bad_callbacks(result, match):
    with pytest.raises(AuthError, match=match):
        validate_callback(result, "s")


def test_validate_callback_ok():
    result = CallbackResult(code="c", state="s", scope="read,activity:read_all")
    assert validate_callback(result, "s") == "c"


def test_callback_server_captures_redirect_and_ignores_other_paths():
    import threading

    server = CallbackServer(port=0)  # bound immediately, so no startup race
    base = f"http://127.0.0.1:{server.port}"
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", server.wait(timeout=10)))
    t.start()
    assert httpx.get(f"{base}/favicon.ico").status_code == 404
    resp = httpx.get(f"{base}/callback?code=c&state=s&scope=activity:read_all")
    t.join(timeout=10)
    assert resp.status_code == 200
    assert out["r"].code == "c"


def test_callback_server_times_out():
    with pytest.raises(AuthError, match="Timed out"):
        CallbackServer(port=0).wait(timeout=0.2)
