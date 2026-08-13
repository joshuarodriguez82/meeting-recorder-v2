"""Sidecar auth fails closed (security review item 4).

server.py binds to 127.0.0.1, which keeps remote hosts out but not other
local processes/webpages. Previously, an unset MEETING_RECORDER_TOKEN
silently disabled auth entirely (fail-open) — a dev convenience that also
meant any local bug that dropped the env var would open the API to any
localhost caller. Now an unset token fails CLOSED (401 on every non-exempt
request) unless MEETING_RECORDER_AUTH_DISABLED=1 is explicitly set.

`_AUTH_TOKEN` / `_AUTH_DISABLED` are computed once at module import from
the environment. Rather than fight import-order/caching to control the
real environment, these tests monkeypatch the module-level globals
directly and call the middleware coroutine (`require_backend_token`)
itself with a synthetic ASGI request — matching this suite's existing
pattern of monkeypatching `server.svc.*` rather than reaching for a real
HTTP client (httpx/TestClient isn't in the CI venv).
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402


def _make_request(path="/sessions", method="GET", headers=None, query_string=""):
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "headers": raw_headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 17645),
        "scheme": "http",
        "root_path": "",
    }
    return Request(scope)


async def _call_next_ok(request):
    return Response("ok", status_code=200)


def _server(monkeypatch, *, token: str, auth_disabled: bool):
    import_app()
    import server
    monkeypatch.setattr(server, "_AUTH_TOKEN", token)
    monkeypatch.setattr(server, "_AUTH_DISABLED", auth_disabled)
    return server


# ── fail closed: no token, no explicit disable ──────────────────────────

def test_rejected_when_no_token_and_disable_flag_absent(monkeypatch):
    server = _server(monkeypatch, token="", auth_disabled=False)
    req = _make_request("/sessions")
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 401


def test_rejected_even_with_a_bearer_token_presented_when_none_configured(monkeypatch):
    """No server-side token means nothing can ever match it — a request
    that happens to send *some* bearer token still must not slip through."""
    server = _server(monkeypatch, token="", auth_disabled=False)
    req = _make_request(
        "/sessions", headers={"authorization": "Bearer whatever-the-caller-sent"})
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 401


def test_health_stays_exempt_even_when_failing_closed(monkeypatch):
    server = _server(monkeypatch, token="", auth_disabled=False)
    req = _make_request("/health")
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 200


def test_options_preflight_still_passes_when_failing_closed(monkeypatch):
    server = _server(monkeypatch, token="", auth_disabled=False)
    req = _make_request("/sessions", method="OPTIONS")
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 200


# ── explicit opt-out: MEETING_RECORDER_AUTH_DISABLED=1 ──────────────────

def test_allowed_when_auth_explicitly_disabled(monkeypatch):
    server = _server(monkeypatch, token="", auth_disabled=True)
    req = _make_request("/sessions")
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 200


# ── normal authenticated operation still works ───────────────────────────

def test_allowed_with_correct_bearer_token(monkeypatch):
    server = _server(monkeypatch, token="secret-token-123", auth_disabled=False)
    req = _make_request(
        "/sessions", headers={"authorization": "Bearer secret-token-123"})
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 200


def test_allowed_with_correct_query_token(monkeypatch):
    server = _server(monkeypatch, token="secret-token-123", auth_disabled=False)
    req = _make_request(
        "/sessions/abc/audio", query_string="token=secret-token-123")
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 200


def test_rejected_with_wrong_bearer_token(monkeypatch):
    server = _server(monkeypatch, token="secret-token-123", auth_disabled=False)
    req = _make_request(
        "/sessions", headers={"authorization": "Bearer wrong-token"})
    resp = asyncio.run(server.require_backend_token(req, _call_next_ok))
    assert resp.status_code == 401
