"""
Portal push: bindings, the ingest POST, and the failure semantics.

Mapped to the integration spec's acceptance criteria, restricted to
what is provable without the live portal:

  1/2. The register is sent VERBATIM ({"register": <file contents>},
       nothing renamed/filtered) — idempotency is the portal's side of
       the contract, and sending identical bytes is ours.
  3.   Network failure raises PortalTransient (the worker's retry
       class); the worker requeues on it and on nothing else.
  4.   A 403 marks the binding broken, raises PortalBindingBroken, and
       the enqueue filter then refuses the scope — no retry loop.
  5.   A project with no binding never pushes anything.
  6.   The edit token appears in no bindings file, no log line and no
       exception text — including a hostile response body that echoes
       the token back.
  7.   The register-write hook never raises into the register path.

The portal itself is a stub: `urlopen` is monkeypatched at the service
module, so every wire byte is inspected without a socket.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from services import portal_push_service as pps  # noqa: E402
from services.export_worker import PortalPushWorker  # noqa: E402
from services.portal_push_service import (  # noqa: E402
    PortalBindingBroken, PortalPermanent, PortalPushService, PortalTransient,
)

TOKEN = "tok-EXAMPLE-0123456789abcdef"  # noqa: S105  # nosec B105 - synthetic fixture


class FakeSecrets:
    """In-memory keychain, so tests never touch the OS one."""

    def __init__(self):
        self.store = {}

    def set_secret(self, name, value):
        if value:
            self.store[name] = value
        else:
            self.store.pop(name, None)
        return True

    def get_secret(self, name):
        return self.store.get(name)


@pytest.fixture
def fake_secrets(monkeypatch):
    fk = FakeSecrets()
    monkeypatch.setattr(pps.secrets, "set_secret", fk.set_secret)
    monkeypatch.setattr(pps.secrets, "get_secret", fk.get_secret)
    return fk


def _svc(tmp_path, url="https://portal.example"):
    return PortalPushService(tmp_path, get_portal_url=lambda: url)


def _bound(tmp_path, fake_secrets, register=None):
    svc = _svc(tmp_path)
    svc.bind("acme", "genesys migration",
             customer_id="cust-1", opportunity_name="Genesys Migration",
             parent_name="Acme", edit_token=TOKEN)
    if register is None:
        register = {
            "client": "acme", "project": "genesys migration",
            "generated_at": "2026-08-21T00:00:00Z",
            "session_count": 2,
            "action_items": [
                {"id": "a1", "text": "Send SOW",
                 "owner": "SPEAKER_02, Mark",
                 "occurrences": [{"session": "s1"}, {"session": "s2"}]},
            ],
            "decisions": [], "requirements": [], "open_questions": [],
        }
    path = svc.register_path("acme", "genesys migration")
    path.write_text(json.dumps(register), encoding="utf-8")
    return svc, register


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_http(monkeypatch, handler):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return handler(req)

    monkeypatch.setattr(pps.urllib.request, "urlopen", fake_urlopen)
    return calls


# ── criteria 1/2: verbatim, under one key ────────────────────────────


def test_the_register_is_sent_verbatim(tmp_path, fake_secrets, monkeypatch):
    """The portal owns normalisation. Occurrences stay nested, the
    comma-salad owner string stays a string, nothing is filtered — the
    wire body is exactly {"register": <the file's parsed contents>}."""
    svc, register = _bound(tmp_path, fake_secrets)
    calls = _patch_http(monkeypatch, lambda req: FakeResponse(
        200, json.dumps({"ingested": True, "added": 1, "updated": 0,
                         "items": 1, "sessions": 2})))

    out = svc.push("acme", "genesys migration")

    assert out["added"] == 1
    sent = json.loads(calls[0].data.decode("utf-8"))
    assert sent == {"register": register}
    assert calls[0].get_full_url().endswith(
        "/customers/cust-1/engagement/ingest")
    assert calls[0].get_header("X-edit-token") == TOKEN


def test_success_is_recorded_on_the_binding(tmp_path, fake_secrets, monkeypatch):
    svc, _ = _bound(tmp_path, fake_secrets)
    _patch_http(monkeypatch, lambda req: FakeResponse(
        200, json.dumps({"added": 12, "updated": 3, "items": 275})))
    svc.push("acme", "genesys migration")
    b = svc.binding_for("acme", "genesys migration")
    assert "added=12" in b["last_push_result"]
    assert b["broken"] is False


# ── criterion 3: only transient failures retry ───────────────────────


def test_network_failure_is_transient(tmp_path, fake_secrets, monkeypatch):
    svc, _ = _bound(tmp_path, fake_secrets)

    def boom(req):
        raise OSError("connection refused")

    _patch_http(monkeypatch, boom)
    with pytest.raises(PortalTransient):
        svc.push("acme", "genesys migration")


def test_503_partial_write_is_the_retry_status(tmp_path, fake_secrets, monkeypatch):
    svc, _ = _bound(tmp_path, fake_secrets)
    _patch_http(monkeypatch, lambda req: FakeResponse(
        503, json.dumps({"persisted": 260, "failed": 15})))
    with pytest.raises(PortalTransient):
        svc.push("acme", "genesys migration")
    # NOT broken — 503 is explicitly "try again", not "re-bind".
    assert svc.binding_for("acme", "genesys migration")["broken"] is False


def test_400_and_422_never_retry(tmp_path, fake_secrets, monkeypatch):
    svc, _ = _bound(tmp_path, fake_secrets)
    for status in (400, 422):
        _patch_http(monkeypatch, lambda req, s=status: FakeResponse(
            s, json.dumps({"error": "no client in register"})))
        with pytest.raises(PortalPermanent):
            svc.push("acme", "genesys migration")


def test_the_worker_requeues_transient_and_only_transient(monkeypatch):
    """The worker retries PortalTransient on the (5s, 30s, 120s)
    schedule and drops everything else. Timers are collapsed so the
    test observes the requeue without sleeping."""
    import services.export_worker as ew

    fired = []

    class InstantTimer:
        def __init__(self, delay, fn, args=()):
            fired.append(delay)
            self.fn, self.args = fn, args
            self.daemon = True

        def start(self):
            self.fn(*self.args)

    monkeypatch.setattr(ew.threading, "Timer", InstantTimer)

    attempts = []
    done = threading.Event()

    def do_push(client, project):
        attempts.append(1)
        if len(attempts) <= 3:
            raise PortalTransient("portal unreachable")
        done.set()

    w = PortalPushWorker(do_push)
    w.enqueue("acme", "genesys migration")
    assert done.wait(10), "worker never exhausted the retry schedule"
    assert len(attempts) == 4          # initial + 3 retries
    assert fired == [5.0, 30.0, 120.0]

    # Broken bindings are dropped, not retried.
    attempts.clear()
    fired.clear()
    settled = threading.Event()

    def do_push_broken(client, project):
        attempts.append(1)
        settled.set()
        raise PortalBindingBroken("403")

    w2 = PortalPushWorker(do_push_broken)
    w2.enqueue("acme", "x")
    assert settled.wait(10)
    w2._q.join()
    assert len(attempts) == 1
    assert fired == []


# ── the portal's connection block binds paste-once ───────────────────
#
# The portal hands the SA ONE JSON block (portal / api / opportunity /
# customerId / editToken). The user pastes it; nobody dissects it into
# form fields. The push target is the block's `api` URL — the API
# Gateway host — NOT the portal website and NOT a Settings value.


SYNTH_CONNECTION = {
    "portal": "https://portal.example",
    "api": "https://abc123.execute-api.example.com/prod",
    "opportunity": "Genesys Migration",
    "customerId": "00000000-0000-4000-8000-000000000001",
    "editToken": TOKEN,
}


def test_the_connection_block_parses_exactly_as_the_portal_hands_it_over():
    parsed = pps.parse_connection(json.dumps(SYNTH_CONNECTION))
    assert parsed == {
        "api_base": "https://abc123.execute-api.example.com/prod",
        "portal_url": "https://portal.example",
        "opportunity_name": "Genesys Migration",
        "customer_id": "00000000-0000-4000-8000-000000000001",
        "edit_token": TOKEN,
    }
    # Pasted with the code fence it often arrives in.
    fenced = "```json\n" + json.dumps(SYNTH_CONNECTION) + "\n```"
    assert pps.parse_connection(fenced)["api_base"] == parsed["api_base"]


def test_connection_parse_failures_name_the_key_and_never_echo_the_token():
    with pytest.raises(ValueError) as exc:
        pps.parse_connection(json.dumps({"editToken": TOKEN}))
    assert TOKEN not in str(exc.value)
    assert "api" in str(exc.value) or "customerId" in str(exc.value)
    with pytest.raises(ValueError):
        pps.parse_connection("this is not json {")
    with pytest.raises(ValueError):
        pps.parse_connection(json.dumps(
            dict(SYNTH_CONNECTION, api="ftp://abc.example/prod")))


def test_push_targets_the_connection_api_url_not_a_settings_value(
        tmp_path, fake_secrets, monkeypatch):
    """Settings has NO portal URL — the binding carries its own api base
    from the pasted block, and the push must reach exactly
    {api}/customers/{customerId}/engagement/ingest."""
    svc = PortalPushService(tmp_path, get_portal_url=lambda: "")
    svc.bind("acme", "genesys migration",
             customer_id=SYNTH_CONNECTION["customerId"],
             opportunity_name="Genesys Migration", parent_name="",
             edit_token=TOKEN,
             api_base=SYNTH_CONNECTION["api"],
             portal_url=SYNTH_CONNECTION["portal"])
    path = svc.register_path("acme", "genesys migration")
    path.write_text(json.dumps({"client": "acme", "action_items": []}),
                    encoding="utf-8")
    calls = _patch_http(monkeypatch, lambda req: FakeResponse(
        200, json.dumps({"added": 1, "updated": 0, "items": 1})))

    svc.push("acme", "genesys migration")

    assert calls[0].get_full_url() == (
        "https://abc123.execute-api.example.com/prod/customers/"
        "00000000-0000-4000-8000-000000000001/engagement/ingest")
    b = svc.binding_for("acme", "genesys migration")
    assert b["apiBase"] == SYNTH_CONNECTION["api"]
    # The block's website URL is display metadata, never the target.
    assert b["portalUrl"] == "https://portal.example"
    assert TOKEN not in (tmp_path / pps.BINDINGS_FILENAME).read_text(
        encoding="utf-8")


# ── criterion 4: 403 breaks the binding and stops the line ───────────


def test_403_marks_broken_and_future_pushes_stop(tmp_path, fake_secrets,
                                                 monkeypatch):
    svc, _ = _bound(tmp_path, fake_secrets)
    _patch_http(monkeypatch, lambda req: FakeResponse(403, "forbidden"))

    with pytest.raises(PortalBindingBroken):
        svc.push("acme", "genesys migration")

    b = svc.binding_for("acme", "genesys migration")
    assert b["broken"] is True
    assert "re-bind" in b["broken_reason"].lower() or "403" in b["broken_reason"]
    # The enqueue filter now refuses the scope — no retry loop.
    assert svc.should_push("acme", "genesys migration") is False
    # Re-binding is the recovery path and clears the flag.
    svc.bind("acme", "genesys migration", customer_id="cust-1",
             opportunity_name="Genesys Migration", parent_name="Acme",
             edit_token="tok-NEW")  # nosec B106 - synthetic fixture
    assert svc.should_push("acme", "genesys migration") is True


# ── criterion 5: no binding, no push ─────────────────────────────────


def test_unbound_and_rollup_scopes_never_push(tmp_path, fake_secrets):
    svc = _svc(tmp_path)
    assert svc.should_push("acme", "genesys migration") is False
    svc.bind("acme", "genesys migration", customer_id="c",
             opportunity_name="", parent_name="", edit_token=TOKEN)
    # The client-level rollup (project "") is the union of the project
    # registers; pushing it double-files every item.
    assert svc.should_push("acme", "") is False
    with pytest.raises(ValueError):
        svc.bind("acme", "", customer_id="c", opportunity_name="",
                 parent_name="", edit_token=TOKEN)


# ── criterion 6: the token exists nowhere it can leak ────────────────


def test_the_token_never_reaches_disk_logs_or_errors(tmp_path, fake_secrets,
                                                     monkeypatch, caplog):
    svc, _ = _bound(tmp_path, fake_secrets)

    # A hostile portal that echoes the token back in its error body —
    # the one path that could launder it into an exception message.
    _patch_http(monkeypatch, lambda req: FakeResponse(
        422, f"bad register (token was {TOKEN})"))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PortalPermanent) as exc:
            svc.push("acme", "genesys migration")

    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text
    bindings_blob = (tmp_path / pps.BINDINGS_FILENAME).read_text(
        encoding="utf-8")
    assert TOKEN not in bindings_blob
    b = svc.binding_for("acme", "genesys migration")
    assert TOKEN not in json.dumps(b)


# ── criterion 7: the hot path cannot be hurt ─────────────────────────


def test_the_register_write_hook_never_raises(tmp_path):
    """EngagementService fires the callback after a successful cache
    write. A callback that explodes must be swallowed — the register
    path is on the user's request path."""
    from services.engagement_service import EngagementService

    calls = []

    def exploding(client_key, project_key):
        calls.append((client_key, project_key))
        raise RuntimeError("worker is on fire")

    sessions = SimpleNamespace(
        recordings_dir=tmp_path,
        list_sessions=lambda: [],
    )
    svc = EngagementService(sessions, on_register_written=exploding)
    register = svc.build_register("acme", "genesys migration")  # must not raise
    assert register is not None
    assert calls == [("acme", "genesys migration")]


def test_the_rollup_register_does_not_fire_the_hook(tmp_path):
    from services.engagement_service import EngagementService

    calls = []
    sessions = SimpleNamespace(recordings_dir=tmp_path,
                               list_sessions=lambda: [])
    svc = EngagementService(
        sessions, on_register_written=lambda c, p: calls.append((c, p)))
    svc.build_register("acme")            # client-level rollup
    svc.build_register("acme", "poc")     # per-project
    assert calls == [("acme", "poc")]
