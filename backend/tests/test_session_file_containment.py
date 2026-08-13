"""Path containment for the audio/screenshot serving endpoints (security
review item 3).

Session JSON files are synced between machines through cloud storage, so
`audio_path` and each `screenshots[]` entry inside them are not fully
trusted local input. Both /sessions/{id}/audio and
/sessions/{id}/screenshots/{index} must refuse to serve a path that
resolves outside every configured recordings/archive root (SessionService's
scan_roots()) — returning 404, not 403, so a tampered JSON can't be used to
probe for the existence of arbitrary files on disk.

These tests call the endpoint coroutines directly (no TestClient — httpx
isn't in the CI venv, see AGENTS.md) and drive them through
`server.svc.session_svc`, matching the pattern in
test_session_archive_settings.py / test_session_discovery.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402
from services.session_service import SessionService  # noqa: E402


def _write_session(root, sid, **fields):
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"session_{sid}.json"
    payload = {"session_id": sid, "display_name": f"Meeting {sid}"}
    payload.update(fields)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _wire(monkeypatch, session_svc):
    """Point server.svc at a real SessionService without triggering
    load_settings()'s real Settings.from_env() (settings must be non-None
    so load_settings() is a no-op)."""
    import server
    monkeypatch.setattr(server.svc, "settings", SimpleNamespace())
    monkeypatch.setattr(server.svc, "session_svc", session_svc)
    return server


# ── audio ────────────────────────────────────────────────────────────

def test_audio_outside_every_root_returns_404(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    outside = tmp_path / "not-a-configured-root"
    outside.mkdir()
    evil_file = outside / "anything.wav"
    evil_file.write_bytes(b"\x00\x00")

    _write_session(recordings, "EVIL01", audio_path=str(evil_file))
    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_session_audio("EVIL01"))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_audio_under_primary_root_is_served(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    audio = recordings / "session_OK01.wav"
    audio.write_bytes(b"\x00\x00")
    _write_session(recordings, "OK01", audio_path=str(audio))

    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    resp = asyncio.run(server.get_session_audio("OK01"))
    assert resp.path == str(audio.resolve())


def test_audio_under_archive_root_is_served(tmp_path, monkeypatch):
    """Regression guard: a legitimate multi-root / archive setup (a
    non-primary root) must keep working, not just the primary dir."""
    local = tmp_path / "local"
    archive = tmp_path / "synced-archive"
    local.mkdir()
    archive.mkdir()
    audio = archive / "session_ARCH01.wav"
    audio.write_bytes(b"\x00\x00")
    _write_session(archive, "ARCH01", audio_path=str(audio))

    import_app()
    server = _wire(
        monkeypatch, SessionService(str(local), extra_dirs=[str(archive)]))

    resp = asyncio.run(server.get_session_audio("ARCH01"))
    assert resp.path == str(audio.resolve())


def test_audio_missing_path_returns_404(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    _write_session(recordings, "NOAUDIO01")  # no audio_path key at all

    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_session_audio("NOAUDIO01"))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


# ── screenshots ──────────────────────────────────────────────────────

def test_screenshot_outside_every_root_returns_404(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    outside = tmp_path / "not-a-configured-root"
    outside.mkdir()
    evil_shot = outside / "secret.png"
    evil_shot.write_bytes(b"\x89PNG")

    _write_session(recordings, "EVILSHOT01", screenshots=[str(evil_shot)])
    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_session_screenshot("EVILSHOT01", 0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_screenshot_under_primary_root_is_served(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    shot = recordings / "session_SHOTOK01_0.png"
    shot.write_bytes(b"\x89PNG")
    _write_session(recordings, "SHOTOK01", screenshots=[str(shot)])

    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    resp = asyncio.run(server.get_session_screenshot("SHOTOK01", 0))
    assert resp.path == str(shot.resolve())


def test_screenshot_under_archive_root_is_served(tmp_path, monkeypatch):
    """Same multi-root regression guard as audio, for screenshots."""
    local = tmp_path / "local"
    archive = tmp_path / "synced-archive"
    local.mkdir()
    archive.mkdir()
    shot = archive / "session_ARCHSHOT01_0.png"
    shot.write_bytes(b"\x89PNG")
    _write_session(archive, "ARCHSHOT01", screenshots=[str(shot)])

    import_app()
    server = _wire(
        monkeypatch, SessionService(str(local), extra_dirs=[str(archive)]))

    resp = asyncio.run(server.get_session_screenshot("ARCHSHOT01", 0))
    assert resp.path == str(shot.resolve())


def test_screenshot_index_out_of_range_returns_404_before_path_check(tmp_path, monkeypatch):
    recordings = tmp_path / "recordings"
    _write_session(recordings, "NOSHOT01", screenshots=[])

    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_session_screenshot("NOSHOT01", 0))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404


def test_traversal_style_path_outside_root_is_rejected(tmp_path, monkeypatch):
    """A `../` path that resolves outside every root must 404, not just
    a path that's superficially in a different directory."""
    recordings = tmp_path / "recordings" / "nested"
    sibling = tmp_path / "recordings-sibling"
    sibling.mkdir(parents=True)
    evil = sibling / "creds.wav"
    evil.write_bytes(b"\x00")

    traversal_path = str(recordings / ".." / ".." / "recordings-sibling" / "creds.wav")
    _write_session(recordings, "TRAV01", audio_path=traversal_path)

    import_app()
    server = _wire(monkeypatch, SessionService(str(recordings)))

    from fastapi import HTTPException
    try:
        asyncio.run(server.get_session_audio("TRAV01"))
        assert False, "expected HTTPException"
    except HTTPException as e:
        assert e.status_code == 404
