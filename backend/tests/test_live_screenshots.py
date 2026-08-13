"""
Crash-resilience for screenshots, and the endpoint that serves them
while a recording is still in progress.

Field reports of the backend dying mid-recording showed a silent-data-
loss gap: a screenshot's PNG landed on disk the moment it was captured,
but nothing linked it to the session until stop_recording() ran — so a
crash between "screenshot taken" and "recording stopped cleanly"
orphaned it. Two things close that gap:

  - attach_screenshot() (POST /recording/screenshot) now mirrors the
    updated session to disk immediately, best-effort, in addition to
    the existing stop/process save.
  - GET /recording/screenshots/{index} serves straight off the
    in-memory active session (recording_svc.current_session) instead
    of the session file, so the Record view's live thumbnail strip
    doesn't depend on that disk mirror ever landing — same containment
    guard (_resolve_within_scan_roots) as the historical
    /sessions/{id}/screenshots/{index} endpoint.

Uses the same headless-import + direct-coroutine-call pattern as
test_session_file_containment.py / test_capture_confidence.py (httpx
isn't in the CI venv, see AGENTS.md; RecordingService(settings=None)
with state poked directly, same as test_capture_confidence.py).
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app, _stub_optional_modules  # noqa: E402

_stub_optional_modules()

from services.session_service import SessionService  # noqa: E402
from services import recording_service as rs  # noqa: E402
from models.session import Session  # noqa: E402


def _wire(monkeypatch, session_svc, recording_svc):
    """Point server.svc at fakes without triggering load_settings()'s
    real Settings.from_env() — same pattern as
    test_session_file_containment.py's _wire()."""
    import server
    monkeypatch.setattr(server.svc, "settings", SimpleNamespace())
    monkeypatch.setattr(server.svc, "session_svc", session_svc)
    monkeypatch.setattr(server.svc, "recording_svc", recording_svc)
    return server


def _armed_recording_svc(session: Session) -> "rs.RecordingService":
    """A RecordingService with just enough state poked in to exercise
    add_screenshot() / current_session / is_recording without touching
    real audio devices, models, or Settings — mirrors
    test_capture_confidence.py's _arm_recording()."""
    svc = rs.RecordingService(settings=None)
    svc._recording = True
    svc._session = session
    return svc


# ── persistence on attach (the crash-resilience gap) ────────────────

def test_attach_screenshot_persists_without_stop_recording(tmp_path, monkeypatch):
    """The whole point: a screenshot attached mid-recording must be
    found on disk even though stop_recording() is never called in this
    test — that's the mid-recording-crash scenario."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")

    session = Session("LIVE01")
    session_svc = SessionService(str(recordings))
    recording_svc = _armed_recording_svc(session)

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    from server import ScreenshotRequest
    result = asyncio.run(
        server.attach_screenshot(ScreenshotRequest(path=str(shot))))
    assert result == {"ok": True, "count": 1}

    on_disk = session_svc.load("LIVE01")
    assert on_disk is not None, "session JSON should exist without stop_recording()"
    assert on_disk["screenshots"] == [str(shot)]


def test_attach_screenshot_second_capture_also_persists(tmp_path, monkeypatch):
    """Not just the first screenshot — the mirror keeps up across
    multiple captures in the same recording."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    shot1 = tmp_path / "shot1.png"
    shot1.write_bytes(b"\x89PNG")
    shot2 = tmp_path / "shot2.png"
    shot2.write_bytes(b"\x89PNG")

    session = Session("LIVE02")
    session_svc = SessionService(str(recordings))
    recording_svc = _armed_recording_svc(session)

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    from server import ScreenshotRequest
    asyncio.run(server.attach_screenshot(ScreenshotRequest(path=str(shot1))))
    result = asyncio.run(
        server.attach_screenshot(ScreenshotRequest(path=str(shot2))))
    assert result == {"ok": True, "count": 2}

    on_disk = session_svc.load("LIVE02")
    assert on_disk["screenshots"] == [str(shot1), str(shot2)]


def test_attach_screenshot_save_failure_is_swallowed(tmp_path, monkeypatch):
    """Best-effort: a broken disk mirror must not raise into the
    request, fail the attach, or stop the in-memory list from growing —
    the in-memory list stays authoritative for the running recording."""
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")

    session = Session("LIVE03")
    recording_svc = _armed_recording_svc(session)

    broken_session_svc = MagicMock()
    broken_session_svc.save.side_effect = OSError("disk full")

    import_app()
    server = _wire(monkeypatch, broken_session_svc, recording_svc)

    from server import ScreenshotRequest
    result = asyncio.run(
        server.attach_screenshot(ScreenshotRequest(path=str(shot))))
    assert result == {"ok": True, "count": 1}
    assert session.screenshots == [str(shot)]
    broken_session_svc.save.assert_called_once()


# ── GET /recording/screenshots/{index} (the live endpoint) ──────────

def test_live_screenshot_no_active_recording_404s(monkeypatch):
    import_app()
    server = _wire(
        monkeypatch, MagicMock(),
        SimpleNamespace(is_recording=False, current_session=None))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_active_recording_screenshot(0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_live_screenshot_bad_index_404s(monkeypatch):
    session = Session("LIVE04")  # screenshots starts empty
    recording_svc = _armed_recording_svc(session)

    import_app()
    server = _wire(monkeypatch, MagicMock(), recording_svc)

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_active_recording_screenshot(0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_live_screenshot_missing_file_404s(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    missing = recordings / "gone.png"  # never actually written

    session = Session("LIVE05")
    session.screenshots = [str(missing)]
    recording_svc = _armed_recording_svc(session)
    session_svc = SessionService(str(recordings))

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_active_recording_screenshot(0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_live_screenshot_outside_scan_roots_404s_not_500(tmp_path, monkeypatch):
    """Same containment guard as the historical endpoint: even though
    this is served from the trusted in-memory session object, not a
    JSON round-tripped through disk, the path is still resolved through
    _resolve_within_scan_roots() rather than served directly — do not
    weaken that check for this endpoint."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    outside = tmp_path / "not-a-configured-root"
    outside.mkdir()
    evil = outside / "secret.png"
    evil.write_bytes(b"\x89PNG")

    session = Session("LIVE06")
    session.screenshots = [str(evil)]
    recording_svc = _armed_recording_svc(session)
    session_svc = SessionService(str(recordings))

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_active_recording_screenshot(0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_live_screenshot_under_primary_root_is_served(tmp_path, monkeypatch):
    """The happy path: recording_service.screenshot_dir() writes under
    recordings_dir/screenshots/session_<id>, which sits under the
    primary scan root, so a legitimate capture is served."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    shot = recordings / "screenshots" / "session_LIVE07" / "shot_0.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG")

    session = Session("LIVE07")
    session.screenshots = [str(shot)]
    recording_svc = _armed_recording_svc(session)
    session_svc = SessionService(str(recordings))

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    resp = asyncio.run(server.get_active_recording_screenshot(0))
    assert resp.path == str(shot.resolve())


def test_live_screenshot_serves_without_any_disk_persistence(tmp_path, monkeypatch):
    """The point of reading from recording_svc.current_session rather
    than session_svc.load(): this must work even when the session JSON
    has never been written to disk at all — the exact gap that made the
    thumbnail strip 404 before this endpoint existed."""
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    shot = recordings / "screenshots" / "session_LIVE08" / "shot_0.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG")

    session = Session("LIVE08")
    session.screenshots = [str(shot)]
    recording_svc = _armed_recording_svc(session)
    session_svc = SessionService(str(recordings))

    assert session_svc.load("LIVE08") is None  # no JSON on disk at all

    import_app()
    server = _wire(monkeypatch, session_svc, recording_svc)

    resp = asyncio.run(server.get_active_recording_screenshot(0))
    assert resp.path == str(shot.resolve())
