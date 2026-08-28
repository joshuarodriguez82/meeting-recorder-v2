"""The backend's actual port must be discoverable from outside the app.

`pick_free_port()` prefers 17645 but falls back to an OS-assigned
ephemeral port whenever something else already holds it — and that
number used to be written nowhere. The Tauri IPC `get_backend_port`
serves only the webview, so every external client (this MCP server, a
script, any AI tool driving the OpenAPI surface) assumed 17645 and
silently pointed at a dead port. The symptom is "the app isn't running"
while the app is plainly running, which sends people to restart things
that were never broken.

The app now writes the live port to `<data_root>/backend-port`, beside
`extension-token`. This resolves it, with a precedence that keeps every
existing escape hatch working:

    MEETING_RECORDER_URL  >  MEETING_RECORDER_PORT  >  port file  >  17645

An explicit override still wins — someone tunnelling or testing against
a stand-in must not be overruled by a file. The file only replaces the
guess.
"""

from __future__ import annotations

import pytest

from meeting_recorder_mcp import discovery


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MEETING_RECORDER_URL", "MEETING_RECORDER_PORT",
                "MEETING_RECORDER_HOST", "MEETING_RECORDER_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    yield


def _data_dir(monkeypatch, tmp_path, port_text=None):
    if port_text is not None:
        (tmp_path / "backend-port").write_text(port_text, encoding="utf-8")
    monkeypatch.setenv("MEETING_RECORDER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_the_written_port_is_used(monkeypatch, tmp_path):
    _data_dir(monkeypatch, tmp_path, "51234")
    assert discovery.resolve_base_url() == "http://127.0.0.1:51234"


def test_falls_back_to_the_pinned_port_when_no_file(monkeypatch, tmp_path):
    _data_dir(monkeypatch, tmp_path)
    assert discovery.resolve_base_url() == "http://127.0.0.1:17645"


def test_explicit_port_env_beats_the_file(monkeypatch, tmp_path):
    """A tunnel or a stand-in backend must not be overruled by whatever
    the last local app run happened to write."""
    _data_dir(monkeypatch, tmp_path, "51234")
    monkeypatch.setenv("MEETING_RECORDER_PORT", "19000")
    assert discovery.resolve_base_url() == "http://127.0.0.1:19000"


def test_explicit_url_env_beats_everything(monkeypatch, tmp_path):
    _data_dir(monkeypatch, tmp_path, "51234")
    monkeypatch.setenv("MEETING_RECORDER_URL", "http://box.local:8080")
    assert discovery.resolve_base_url() == "http://box.local:8080"


@pytest.mark.parametrize("junk", ["", "   ", "not-a-port", "0", "70000", "-1"])
def test_an_unusable_port_file_degrades_to_the_default(
        monkeypatch, tmp_path, junk):
    """A truncated or garbage file must not make the tools unavailable.
    Same posture as the token path: fall back, let the connection error
    do the explaining."""
    _data_dir(monkeypatch, tmp_path, junk)
    assert discovery.resolve_base_url() == "http://127.0.0.1:17645"


def test_whitespace_around_the_port_is_tolerated(monkeypatch, tmp_path):
    _data_dir(monkeypatch, tmp_path, "  51234\n")
    assert discovery.resolve_base_url() == "http://127.0.0.1:51234"
