"""Port + token discovery, cross-platform."""

from __future__ import annotations

import pytest

from meeting_recorder_mcp import discovery
from meeting_recorder_mcp.discovery import (
    DEFAULT_PORT,
    TokenNotFound,
    resolve_base_url,
    resolve_location,
    resolve_token,
)


# ── base URL ────────────────────────────────────────────────────────

def test_base_url_defaults_to_the_pinned_port():
    assert resolve_base_url() == f"http://127.0.0.1:{DEFAULT_PORT}"


def test_port_env_override(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_PORT", "52111")
    assert resolve_base_url() == "http://127.0.0.1:52111"


def test_full_url_override_beats_port(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_PORT", "52111")
    monkeypatch.setenv("MEETING_RECORDER_URL", "http://127.0.0.1:9999/")
    assert resolve_base_url() == "http://127.0.0.1:9999"


def test_garbage_port_falls_back_rather_than_raising(monkeypatch):
    # Same posture as backend/server.py's __main__ block: warn and use
    # the default rather than refuse to start.
    monkeypatch.setenv("MEETING_RECORDER_PORT", "not-a-port")
    assert resolve_base_url() == f"http://127.0.0.1:{DEFAULT_PORT}"


# ── token ───────────────────────────────────────────────────────────

def test_token_from_env_wins(monkeypatch, tmp_path):
    (tmp_path / "extension-token").write_text("f" * 64)
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEETING_RECORDER_TOKEN", "e" * 64)
    token, source = resolve_token()
    assert token == "e" * 64
    assert source == "env:MEETING_RECORDER_TOKEN"


def test_token_read_from_extension_token_file(monkeypatch, tmp_path):
    # The real filename written by src-tauri/src/lib.rs::token_file_path.
    (tmp_path / "extension-token").write_text("b" * 64 + "\n")
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    token, source = resolve_token()
    assert token == "b" * 64
    assert source.endswith("extension-token")


def test_token_file_with_bom_is_read(monkeypatch, tmp_path):
    # PowerShell's Set-Content -Encoding UTF8 emits a BOM; the backend
    # explicitly defends against this elsewhere, so we do too.
    (tmp_path / "extension-token").write_text("c" * 64, encoding="utf-8-sig")
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    token, _ = resolve_token()
    assert token == "c" * 64


def test_missing_token_raises_with_the_paths_it_checked(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    with pytest.raises(TokenNotFound) as excinfo:
        resolve_token()
    searched = [str(p) for p in excinfo.value.searched]
    assert any(p.endswith("extension-token") for p in searched)


def test_empty_token_file_is_not_accepted(monkeypatch, tmp_path):
    (tmp_path / "extension-token").write_text("   \n")
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    with pytest.raises(TokenNotFound):
        resolve_token()


def test_non_hex_token_is_used_but_flagged(monkeypatch, tmp_path):
    (tmp_path / "extension-token").write_text("short-token")
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    loc = resolve_location()
    assert loc.token == "short-token"
    assert loc.token_looks_unusual is True


def test_hex64_token_is_not_flagged(monkeypatch, tmp_path):
    (tmp_path / "extension-token").write_text("0123456789abcdef" * 4)
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    assert resolve_location().token_looks_unusual is False


# ── per-platform data dirs ──────────────────────────────────────────

def test_windows_dirs(monkeypatch):
    monkeypatch.setattr(discovery, "_is_windows", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\\Users\\<you>\AppData\Local")
    monkeypatch.setenv("APPDATA", r"C:\\Users\\<you>\AppData\Roaming")
    dirs = [str(d) for d in discovery.candidate_data_dirs()]
    # LOCALAPPDATA first — that's what lib.rs::data_root_dir prefers,
    # and APPDATA is the OneDrive-redirected one the app avoids.
    assert dirs[0].endswith("MeetingRecorder")
    assert "Local" in dirs[0]
    assert any("Roaming" in d for d in dirs)


def test_macos_dirs(monkeypatch):
    monkeypatch.setattr(discovery, "_is_windows", lambda: False)
    monkeypatch.setattr(discovery, "_is_macos", lambda: True)
    dirs = [str(d) for d in discovery.candidate_data_dirs()]
    assert dirs[0].endswith("Library/Application Support/MeetingRecorder")


def test_linux_probes_both_data_and_config_dirs(monkeypatch):
    # The Rust shell writes extension-token under XDG_DATA_HOME while
    # the Python backend writes config.env under XDG_CONFIG_HOME.
    monkeypatch.setattr(discovery, "_is_windows", lambda: False)
    monkeypatch.setattr(discovery, "_is_macos", lambda: False)
    monkeypatch.setenv("XDG_DATA_HOME", "~/.local/share")
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/.config")
    dirs = [str(d) for d in discovery.candidate_data_dirs()]
    assert dirs[0] == "~/.local/share/MeetingRecorder"
    assert "~/.config/MeetingRecorder" in dirs


def test_data_dir_override_is_probed_first(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", "/somewhere/else")
    assert str(discovery.candidate_data_dirs()[0]) == "/somewhere/else"
