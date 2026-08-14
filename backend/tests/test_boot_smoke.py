"""
Boot smoke test — end-to-end wiring, not just the parts.

Field report 2026-08-13 (v2.26.0): the backend answered /settings with
200 but 500'd every /clients/config with

    AttributeError: 'NoneType' object has no attribute 'get_all'

Device lists were empty and recording was impossible. 495 backend tests
passed on that build, because nothing in the suite constructed the real
`Services` object and drove `load_settings()` end to end — every test
exercised a service in isolation, never the wiring that assembles all
of them and hands the result to the FastAPI routes.

This module closes that gap:

  1. `test_load_settings_populates_every_service` constructs a real
     `Services()` and asserts every attribute `load_settings()` is
     supposed to populate is non-None. The attribute list is derived
     from the method's own AST (see `_unconditionally_assigned_attrs`
     below) rather than hand-picked, so a newly added service that
     load_settings() forgets to assign gets caught automatically.

  2. `test_boot_smoke_endpoints_do_not_5xx` drives the real FastAPI app
     through TestClient after that same wiring and asserts a
     representative set of endpoints answer without a 500 — the actual
     symptom of the outage (200 from /settings, 500 from
     /clients/config) would have been caught here.

  3. `test_partial_init_failure_is_retried_not_permanent` is the
     regression test for the actual fix: a simulated exception partway
     through load_settings() must leave `_services_ready` False (not
     get masked by `self.settings` already being set) so the NEXT call
     retries initialisation instead of leaving the backend half-built
     forever.

Isolation: every path load_settings() can write to (RECORDINGS_DIR,
USER_DATA_DIR, config.env) is redirected under `tmp_path` so this suite
never touches a real user's data directory. See `isolated_server`.

Constraints (CI venv has ONLY numpy/scipy/soundfile/pytest/fastapi):
this module additionally needs `httpx` for FastAPI's TestClient — see
requirements note in pr-checks.yml.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from _app_import import import_app

SERVER_PY = Path(__file__).resolve().parents[1] / "server.py"


def _load_settings_guarded_body() -> list[ast.stmt]:
    """The statement list that actually runs on a (re)initialising call
    — the body of load_settings()'s top-level `if self.settings is None
    or not self._services_ready:` guard. Statements *nested* inside a
    further If (the optional summarizer / live_summarizer blocks, which
    depend on whether an LLM is configured) are deliberately not
    descended into: those two attributes are legitimately allowed to
    stay None with no LLM key configured, so they must not be swept
    into the "always non-None" assertion below."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "load_settings"
    )
    guard = next(n for n in fn.body if isinstance(n, ast.If))
    return guard.body


def _unconditionally_assigned_attrs() -> list[str]:
    """Every `self.<name> = ...` assigned directly (not inside a nested
    If/Try) in load_settings()'s guarded body — i.e. every attribute
    the method promises to populate on every successful call. This is
    the enumeration the smoke test asserts against, so a service added
    to Services.__init__ / load_settings() without wiring it up here
    would be caught the moment this list is regenerated against a
    build where it's still None."""
    names: list[str] = []
    for stmt in _load_settings_guarded_body():
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.append(target.attr)
    return names


# Computed once at collection time so both tests below and any human
# reading a failure see the exact same enumeration.
SERVICE_ATTRS = _unconditionally_assigned_attrs()

# Sanity on the derivation itself: guards against the AST walk silently
# finding nothing (e.g. load_settings() gets refactored to not use a
# top-level `if` guard at all) and the smoke test below passing
# vacuously with an empty assertion list.
assert "_services_ready" in SERVICE_ATTRS
assert "session_svc" in SERVICE_ATTRS
assert "qa_svc" in SERVICE_ATTRS
assert len(SERVICE_ATTRS) >= 20, (
    f"only found {len(SERVICE_ATTRS)} unconditional self.X assigns in "
    f"load_settings() — the AST walk in this test may have broken"
)


@pytest.fixture
def isolated_server(tmp_path, monkeypatch):
    """Import the real server module and redirect every path
    load_settings() can write to under tmp_path, so this suite never
    touches a real user's recordings / config / speaker-profile
    directory. Returns the imported server module."""
    app = import_app()
    import server as server_module
    import config.settings as config_settings_module

    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    user_data_dir = tmp_path / "userdata"
    user_data_dir.mkdir()

    monkeypatch.setenv("RECORDINGS_DIR", str(recordings_dir))
    # USER_DATA_DIR is bound into server.py's own namespace at import
    # time (`from config.settings import Settings, USER_DATA_DIR`), so
    # setting the env var alone would not affect it — load_settings()
    # references the module-global name directly (session-index db
    # path, SpeakerProfileService, AutoRecordBlocklistService).
    monkeypatch.setattr(server_module, "USER_DATA_DIR", user_data_dir)
    # Settings.from_env() reads config.env via _resolve_env_path(),
    # which walks real per-OS candidate locations (~/.config/..., etc)
    # independently of RECORDINGS_DIR. Point it at a file that can't
    # exist so this test can never pick up a real developer's saved
    # settings (API keys included).
    monkeypatch.setattr(
        config_settings_module, "_resolve_env_path",
        lambda: tmp_path / "config.env")
    # The sidecar fails closed on auth by design (see
    # test_auth_fail_closed.py) — without a real MEETING_RECORDER_TOKEN
    # every non-exempt request 401s before ever reaching the handler,
    # which would hide exactly the 500s this test exists to catch.
    monkeypatch.setattr(server_module, "_AUTH_DISABLED", True)

    return server_module


def test_load_settings_populates_every_service(isolated_server):
    """The actual invariant that broke in v2.26.0: after a successful
    load_settings() call, every service it is responsible for building
    must be usable — not just `settings` itself."""
    services = isolated_server.Services()

    assert services._services_ready is False  # freshly constructed

    result = services.load_settings()

    assert result is not None
    assert result is services.settings
    assert services._services_ready is True

    missing = [
        attr for attr in SERVICE_ATTRS
        if getattr(services, attr) is None
    ]
    assert not missing, (
        f"load_settings() left these attributes None: {missing}. "
        f"This is the exact v2.26.0 failure mode — /settings would "
        f"answer 200 while every endpoint touching one of these "
        f"services 500s."
    )


def test_boot_smoke_endpoints_do_not_5xx(isolated_server):
    """Drive the real app through the wiring above and confirm a
    representative set of endpoints never 500. /clients/config is the
    endpoint that actually broke in the field report (200 from
    /settings masked it); the rest cover the other services
    load_settings() builds.

    A 200 or a clean 4xx is acceptable — only a 5xx (or an exception
    escaping the request entirely) is a failure. Endpoints hit here
    deliberately need no request body / auth beyond what the fixture
    already disabled, and no ML/audio hardware.
    """
    from fastapi.testclient import TestClient

    services = isolated_server.Services()
    services.load_settings()
    # server.py's route handlers reference the module-level singleton
    # `svc` directly (not a FastAPI dependency), so the test has to
    # swap that singleton for our isolated instance — otherwise the
    # startup event would build (or reuse) a `svc` pointed at whatever
    # a previous test left behind.
    services_singleton_backup = isolated_server.svc
    isolated_server.svc = services
    try:
        with TestClient(isolated_server.app) as client:
            for path in (
                "/health",
                "/settings",
                "/clients/config",
                "/audio/devices",
                "/recording/status",
                "/sessions",
                "/diagnostics",
                "/calendar/available",
            ):
                resp = client.get(path)
                assert resp.status_code < 500, (
                    f"GET {path} returned {resp.status_code}: "
                    f"{resp.text[:500]}"
                )
    finally:
        isolated_server.svc = services_singleton_backup


def test_partial_init_failure_is_retried_not_permanent(isolated_server, monkeypatch):
    """Locks in the actual fix behind v2.26.2: a real UnboundLocalError
    (or any exception) partway through load_settings() must leave
    `_services_ready` False, so the guard's `not self._services_ready`
    half re-runs full initialisation on the next call — rather than the
    old `self.settings is None` guard, which stayed permanently False
    (skipping re-init forever) because `self.settings` is assigned on
    load_settings()'s very first line, before anything can fail.
    """
    services = isolated_server.Services()

    real_template_service = isolated_server.TemplateService
    call_count = {"n": 0}

    def flaky_template_service(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated partial init failure")
        return real_template_service(*args, **kwargs)

    monkeypatch.setattr(isolated_server, "TemplateService", flaky_template_service)

    with pytest.raises(RuntimeError, match="simulated partial init failure"):
        services.load_settings()

    # This is the exact v2.26.0 symptom shape: settings + everything
    # constructed BEFORE the failure point are set, everything after
    # stays None — and the guard must not mistake that for "done".
    assert services.settings is not None
    assert services.session_svc is not None       # built before the failure
    assert services.client_cfg_svc is not None     # built before the failure
    assert services.template_svc is None           # never got built
    assert services.qa_svc is None                 # everything after, too
    assert services._services_ready is False, (
        "_services_ready must be False after a partial failure — if "
        "this is True, a half-built Services object is being reported "
        "as ready, which is the exact v2.26.0 bug."
    )

    # Retry: the SAME instance, no workaround, must fully initialise —
    # this is the behavior the old `self.settings is None` guard broke.
    services.load_settings()

    assert services._services_ready is True
    assert services.template_svc is not None
    assert services.qa_svc is not None
    missing = [a for a in SERVICE_ATTRS if getattr(services, a) is None]
    assert not missing, f"retry left these still None: {missing}"
