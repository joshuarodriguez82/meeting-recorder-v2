"""
GET /extension/info and POST /extension/install -- the Settings
"Chrome Extension" card's data source and its "Install / Update
extension files" button (AGENTS.md build items #3/#4).

Exercises the endpoint FUNCTIONS directly (not over HTTP), same
pattern test_extension_calendar_import_endpoint.py uses, with
server.svc's extension_calendar_svc and the extension_bundle_service
names server.py imported replaced by lightweight spies/monkeypatches.
No LLM, no filesystem.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("dotenv", MagicMock())

from _app_import import import_app  # noqa: E402

import_app()
import server  # noqa: E402


def _wire_status(monkeypatch, status=None):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(
        server.svc, "extension_calendar_svc",
        SimpleNamespace(capture_status=lambda: status or {
            "updated_at": None, "event_count": 0, "future_event_count": 0,
            "last_seen_version": None, "last_seen_version_at": None,
        }))


# ── GET /extension/info ──────────────────────────────────────────────

def test_info_reports_bundled_and_last_seen_versions(monkeypatch):
    _wire_status(monkeypatch, status={
        "updated_at": None, "event_count": 0, "future_event_count": 0,
        "last_seen_version": "1.1.0",
        "last_seen_version_at": "2026-08-13T09:00:00",
    })
    monkeypatch.setattr(server, "bundled_extension_version", lambda: "1.2.0")
    monkeypatch.setattr(
        server, "extension_export_dir", lambda: Path("/fake/chrome-extension"))

    result = asyncio.run(server.extension_info())

    assert result["bundled_version"] == "1.2.0"
    assert result["last_seen_version"] == "1.1.0"
    assert result["last_seen_at"] == "2026-08-13T09:00:00"
    assert result["status"] == "update_available"
    assert result["install_path"] == str(Path("/fake/chrome-extension"))


def test_info_equal_versions_is_up_to_date(monkeypatch):
    _wire_status(monkeypatch, status={
        "updated_at": None, "event_count": 0, "future_event_count": 0,
        "last_seen_version": "1.2.0",
        "last_seen_version_at": "2026-08-14T09:00:00",
    })
    monkeypatch.setattr(server, "bundled_extension_version", lambda: "1.2.0")
    monkeypatch.setattr(server, "extension_export_dir", lambda: Path("/fake"))

    result = asyncio.run(server.extension_info())
    assert result["status"] == "up_to_date"


def test_info_never_posted_is_its_own_distinct_state(monkeypatch):
    _wire_status(monkeypatch)  # default status: never posted
    monkeypatch.setattr(server, "bundled_extension_version", lambda: "1.2.0")
    monkeypatch.setattr(server, "extension_export_dir", lambda: Path("/fake"))

    result = asyncio.run(server.extension_info())

    assert result["last_seen_version"] is None
    assert result["last_seen_at"] is None
    assert result["status"] == "never_posted"


def test_info_degrades_clearly_when_no_bundle_present(monkeypatch):
    """A dev checkout without the zip-bundle build must not 500 --
    bundled_version reads back None and the endpoint still returns
    200 with an honest status."""
    _wire_status(monkeypatch)
    monkeypatch.setattr(server, "bundled_extension_version", lambda: None)
    monkeypatch.setattr(server, "extension_export_dir", lambda: Path("/fake"))

    result = asyncio.run(server.extension_info())

    assert result["bundled_version"] is None
    assert result["status"] == "unknown"


def test_info_survives_extension_calendar_svc_missing(monkeypatch):
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server.svc, "extension_calendar_svc", None)
    monkeypatch.setattr(server, "bundled_extension_version", lambda: "1.2.0")
    monkeypatch.setattr(server, "extension_export_dir", lambda: Path("/fake"))

    result = asyncio.run(server.extension_info())
    assert result["status"] == "never_posted"


# ── POST /extension/install ─────────────────────────────────────────

def test_install_returns_written_files_and_stable_path(monkeypatch):
    monkeypatch.setattr(
        server, "export_extension_files",
        lambda: ["background.js", "manifest.json"])
    monkeypatch.setattr(
        server, "extension_export_dir", lambda: Path("/fake/chrome-extension"))

    result = asyncio.run(server.install_extension_files())

    assert result["ok"] is True
    assert result["files"] == ["background.js", "manifest.json"]
    assert result["file_count"] == 2
    assert result["path"] == str(Path("/fake/chrome-extension"))


def test_install_404s_clearly_when_no_bundle_present(monkeypatch):
    def _raise():
        raise FileNotFoundError("no bundled chrome-extension/ found")
    monkeypatch.setattr(server, "export_extension_files", _raise)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(server.install_extension_files())
    assert getattr(exc_info.value, "status_code", None) == 404


def test_install_500s_with_detail_on_unexpected_failure(monkeypatch):
    def _raise():
        raise OSError("disk full")
    monkeypatch.setattr(server, "export_extension_files", _raise)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(server.install_extension_files())
    assert getattr(exc_info.value, "status_code", None) == 500
