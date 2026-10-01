"""
A device connected after launch must appear without restarting the app.

Field report 2026-09-23 (macOS): a Bluetooth headset connected after the
app started never appeared as a microphone until the whole app was
restarted. PortAudio snapshots the device list when it is initialised,
and sounddevice initialises it once, at import; a 60 s cache sat on top.

The re-scan re-initialises PortAudio, which is undefined behaviour with a
stream open — so the part these tests pin hardest is WHEN it refuses.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from core import audio_capture as ac  # noqa: E402


@pytest.fixture
def rescans(monkeypatch):
    calls = []
    monkeypatch.setattr(ac, "_reinitialize_portaudio",
                        lambda: calls.append(1))
    monkeypatch.setattr(ac, "_ACTIVE_CAPTURES", set())
    return calls


def test_a_rescan_reinitialises_and_drops_the_cache(rescans):
    ac._input_cache = (1e18, [{"index": 0, "name": "stale"}])
    ac._output_cache = (1e18, [])
    assert ac.refresh_devices() is True
    assert rescans == [1]
    assert ac._input_cache is None and ac._output_cache is None


def test_no_rescan_while_a_capture_is_open(rescans):
    """Terminating PortAudio under an open stream is undefined — the
    recording is worth more than a fresher device list."""
    cap = object()
    ac._register_capture(cap)
    assert ac.refresh_devices() is False
    assert rescans == []
    ac._unregister_capture(cap)
    assert ac.refresh_devices() is True


def test_a_failed_rescan_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(ac, "_ACTIVE_CAPTURES", set())

    def boom():
        raise RuntimeError("PortAudio said no")
    monkeypatch.setattr(ac, "_reinitialize_portaudio", boom)
    assert ac.refresh_devices() is False


def _capture():
    return ac.AudioCapture(mic_device_index=None, output_device_index=None,
                           on_chunk=lambda c: None)


def test_a_capture_registers_before_opening_anything(rescans):
    cap = _capture()
    cap.start()          # no devices: opens nothing, still registered
    assert id(cap) in ac._ACTIVE_CAPTURES
    cap.stop()
    assert id(cap) not in ac._ACTIVE_CAPTURES


def test_a_capture_whose_close_hung_stays_registered(rescans, monkeypatch):
    """An abandoned stream may still be live: never re-initialise under
    it. A restart is better than a crash."""
    cap = _capture()
    cap.start()

    def hang():
        import time
        time.sleep(5)
    monkeypatch.setattr(cap, "_close_all_streams", hang)
    cap.stop()
    assert id(cap) in ac._ACTIVE_CAPTURES
    assert ac.refresh_devices() is False


def test_a_capture_that_failed_to_start_unregisters(rescans, monkeypatch):
    cap = ac.AudioCapture(mic_device_index=3, output_device_index=None,
                          on_chunk=lambda c: None)

    def no_device(*a, **k):
        raise RuntimeError("no such device")
    monkeypatch.setattr(ac.sd, "query_devices", no_device, raising=False)
    with pytest.raises(Exception):
        cap.start()
    assert id(cap) not in ac._ACTIVE_CAPTURES


def test_the_endpoint_rescans_only_when_asked(rescans):
    from tests._app_import import import_app
    import server
    import_app()
    body = asyncio.run(server.get_audio_devices())
    assert body["refreshed"] is False and rescans == []
    body = asyncio.run(server.get_audio_devices(refresh=True))
    assert body["refreshed"] is True and rescans == [1]
