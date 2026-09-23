"""
Every parameterless GET answers. Not a representative sample of them.

WHY THIS EXISTS
---------------
``test_boot_smoke.py`` drives "a representative set of endpoints" — and
representative means hand-picked, which means the set only grows when
someone remembers to grow it. It lists eight. The app has 51 GET routes
that take no path parameters.

The cost of the gap, twice in one week:

  * ``POST /audio/probe-mic`` answered **500 on every request** from
    v2.81.0 to v2.82.2. Nothing called it.
  * ``GET /sessions/{id}`` 500'd on a locked file (2026-09-10), which
    only surfaced because a user hit it and sent a screenshot.

Both were one call away from being caught. Neither was called.

WHY *GET* SPECIFICALLY
----------------------
A GET is read-only by contract, so calling all of them is safe in a way
that calling all the POSTs is not — half of those start recordings,
write files or spend money at an API. This takes the category that can
be swept exhaustively and sweeps it, rather than sampling the category
and calling that coverage.

Routes with path parameters are excluded because inventing an ID gets a
404 that proves nothing about the handler. Those need a real fixture,
which is what the per-feature suites are for.

WHAT COUNTS AS A PASS
---------------------
Anything under 500. A 4xx is a handler that ran and declined —
unconfigured, not found, bad request — which is the correct answer for
most of these in a blank environment. A 5xx, or an exception escaping
the request, is the handler falling over, and that is what shipped.

THE EXEMPTION LIST IS THE POINT
-------------------------------
A route that cannot be swept has to be named here with a reason. That
turns "nobody thought about this endpoint" into a visible, reviewable
decision at the moment the endpoint is added — which is the step that
was missing when probe-mic shipped.
"""

from __future__ import annotations

import pytest

from test_boot_smoke import isolated_server  # noqa: F401  (fixture)


#: Parameterless GETs this sweep must not call, and why. Anything not
#: listed here IS called. Keep the reasons concrete — "flaky" is not a
#: reason, "needs a real recording in flight" is.
#:
#: Empty on purpose. The one streaming GET in the app
#: (/recording/transcript/stream) is swept rather than exempted: with
#: no recording in flight it completes immediately, and the per-request
#: timeout below turns a hang into a failure instead of a hung suite.
#: Exempting it would have traded real coverage for a hypothetical.
EXEMPT: dict[str, str] = {}

#: Per-request ceiling. An endpoint that blocks is a bug this sweep
#: should REPORT, not a reason for the suite to sit there until CI
#: kills it with no output.
REQUEST_TIMEOUT_S = 15.0


def _parameterless_gets(app) -> list:
    """Every GET route with no path parameters, sorted for a stable
    failure list."""
    paths = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "GET" in methods and "{" not in path:
            paths.add(path)
    return sorted(paths)


def test_the_sweep_actually_covers_something(isolated_server):  # noqa: F811
    """A sweep that silently enumerates nothing is worse than no sweep:
    it reports success for having checked zero routes. The app had 51
    when this was written; the floor guards against the enumeration
    quietly breaking."""
    paths = _parameterless_gets(isolated_server.app)
    assert len(paths) >= 40, (
        f"only {len(paths)} parameterless GET route(s) found — the "
        f"enumeration is probably broken, not the app")


def test_every_exemption_still_names_a_real_route(isolated_server):  # noqa: F811
    """An exemption for a route that no longer exists is a stale excuse
    that would silently cover a NEW route if one ever took that path."""
    paths = set(_parameterless_gets(isolated_server.app))
    stale = sorted(p for p in EXEMPT if p not in paths)
    assert not stale, (
        f"these exemptions name routes that no longer exist: {stale}")


def test_every_parameterless_get_answers_without_a_500(isolated_server):  # noqa: F811
    """The sweep. One call per route, every failure reported together.

    Collected rather than fail-fast on purpose: if three endpoints are
    broken, three is what you want to see, not the alphabetically
    first one three times in a row.
    """
    from fastapi.testclient import TestClient

    services = isolated_server.Services()
    services.load_settings()
    # Same singleton swap test_boot_smoke does — the handlers reference
    # server.svc directly rather than through a FastAPI dependency.
    backup = isolated_server.svc
    isolated_server.svc = services

    failures = []
    checked = 0
    try:
        with TestClient(isolated_server.app) as client:
            for path in _parameterless_gets(isolated_server.app):
                if path in EXEMPT:
                    continue
                checked += 1
                try:
                    resp = client.get(path, timeout=REQUEST_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001
                    failures.append(
                        f"GET {path} raised {type(e).__name__}: {e}")
                    continue
                if resp.status_code >= 500:
                    failures.append(
                        f"GET {path} -> {resp.status_code}: "
                        f"{resp.text[:200]}")
    finally:
        isolated_server.svc = backup

    assert not failures, (
        f"{len(failures)} of {checked} GET route(s) failed. A 4xx is "
        f"fine — a handler that ran and declined. These fell over:\n  "
        + "\n  ".join(failures))
