"""
Every parameterless POST answers an empty body without falling over.

WHY THIS EXISTS
---------------
The GET sweep (test_every_get_route_answers.py) covers every read-only
route. POSTs were left to per-feature suites, and an audit on 2026-10-01
counted 65 POST routes with no test calling them at all — the same gap
that let ``POST /audio/probe-mic`` answer 500 on every request for three
releases with nothing noticing.

WHAT IS CHECKED
---------------
One call per route with ``{}`` as the body, in the same isolated,
unconfigured environment the GET sweep uses. Anything under 500 passes:
422 (FastAPI rejected the body), 400/403/409 (the handler ran and
declined) and 200 (an action that needs no body) are all a handler
behaving. A 5xx, or an exception escaping, is a handler falling over.

Routes with path parameters are excluded for the same reason as in the
GET sweep: an invented ID proves nothing.

THE EXEMPTION LIST IS THE POINT
-------------------------------
A POST that cannot be called here — because it spends money, launches
something, or writes outside the test sandbox — is named below with the
concrete reason. Every other POST, including any added later, is called.
"""

from __future__ import annotations

from test_boot_smoke import isolated_server  # noqa: F401  (fixture)

EXEMPT: dict[str, str] = {
    "/recording/start":
        "opens real audio devices and starts a capture",
    "/integrations/mcp/install":
        "runs pip to install a package into a Python environment",
    "/extension/install":
        "writes the extension into the real user config directory, "
        "outside the test sandbox",
    "/diagnostics/export":
        "writes a zip into the real user config directory",
    "/system/open-folder":
        "launches the OS file manager",
    "/models/load":
        "starts loading the speech models on a background thread",
    "/briefing/signin":
        "launches a browser window for an interactive sign-in",
}

REQUEST_TIMEOUT_S = 20.0


def _parameterless_posts(app) -> list:
    paths = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "POST" in methods and "{" not in path:
            paths.add(path)
    return sorted(paths)


def test_the_sweep_actually_covers_something(isolated_server):  # noqa: F811
    """There were 42 parameterless POSTs when this was written."""
    paths = _parameterless_posts(isolated_server.app)
    assert len(paths) >= 35, (
        f"only {len(paths)} parameterless POST route(s) found — the "
        f"enumeration is probably broken, not the app")


def test_every_exemption_still_names_a_real_route(isolated_server):  # noqa: F811
    paths = set(_parameterless_posts(isolated_server.app))
    stale = sorted(p for p in EXEMPT if p not in paths)
    assert not stale, (
        f"these exemptions name routes that no longer exist: {stale}")


def test_every_parameterless_post_answers_without_a_500(isolated_server):  # noqa: F811
    from fastapi.testclient import TestClient

    services = isolated_server.Services()
    services.load_settings()
    backup = isolated_server.svc
    isolated_server.svc = services
    failures = []
    checked = 0
    try:
        with TestClient(isolated_server.app) as client:
            for path in _parameterless_posts(isolated_server.app):
                if path in EXEMPT:
                    continue
                checked += 1
                try:
                    resp = client.post(path, json={}, timeout=REQUEST_TIMEOUT_S)
                except Exception as e:  # noqa: BLE001
                    failures.append(
                        f"POST {path} raised {type(e).__name__}: {e}")
                    continue
                if resp.status_code >= 500:
                    failures.append(
                        f"POST {path} -> {resp.status_code}: "
                        f"{resp.text[:200]}")
    finally:
        isolated_server.svc = backup
    assert checked >= 30, f"only {checked} POST route(s) were called"
    assert not failures, (
        f"{len(failures)} of {checked} POST route(s) failed with an empty "
        f"body. A 4xx is fine — a handler that ran and declined. These "
        f"fell over:\n  " + "\n  ".join(failures))
