"""
Native macOS system audio — no BlackHole, any output device.

Field report 2026-09-23: on a Mac the only way to record the other
participants was BlackHole behind a Multi-Output Device, and connecting
Bluetooth headphones switched output straight to them — BlackHole got
silence and the other side went unrecorded, silently. Windows records
any output device directly; macOS 13+ can too, through ScreenCaptureKit.

ScreenCaptureKit itself only exists on a Mac. What is tested here is
everything around it: the decoding of its buffers, when the option is
offered, how a missing permission is reported, and the real capture path
(queue → WAV → live transcript) fed by a stand-in for the Apple side.
"""

from __future__ import annotations

import sys
import time
import types

import numpy as np
import pytest
import soundfile as sf

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from core import audio_capture as ac  # noqa: E402
from core import mac_system_audio as msa  # noqa: E402
from core.capture_health import (  # noqa: E402
    REASON_PERMISSION,
    SYSTEM_AUDIO_UNAVAILABLE,
    classify_open_error,
    live_capture_issue,
    missing_system_audio_warning,
)


# ── Decoding ScreenCaptureKit buffers ───────────────────────────────

def test_planar_stereo_is_downmixed_per_frame():
    """ScreenCaptureKit's layout: all of channel 0, then all of channel 1."""
    left = np.array([1.0, 1.0, 1.0], dtype=np.float32)
    right = np.array([0.0, 0.5, -1.0], dtype=np.float32)
    raw = np.concatenate([left, right]).tobytes()
    mono = msa.decode_float_pcm(raw, frames=3, channels=2,
                                non_interleaved=True)
    assert mono.tolist() == [0.5, 0.75, 0.0]


def test_interleaved_stereo_is_downmixed_per_frame():
    raw = np.array([1.0, 0.0, 1.0, 0.5, 1.0, -1.0],
                   dtype=np.float32).tobytes()
    mono = msa.decode_float_pcm(raw, frames=3, channels=2,
                                non_interleaved=False)
    assert mono.tolist() == [0.5, 0.75, 0.0]


def test_the_layout_flag_matters():
    """Reading planar data as interleaved mixes neighbouring samples of
    ONE channel — audible garbage. The two readings must differ."""
    raw = np.arange(6, dtype=np.float32).tobytes()
    planar = msa.decode_float_pcm(raw, 3, 2, True)
    inter = msa.decode_float_pcm(raw, 3, 2, False)
    assert planar.tolist() != inter.tolist()


def test_a_buffer_that_does_not_match_its_shape_is_not_guessed_at():
    raw = np.ones(5, dtype=np.float32).tobytes()
    assert len(msa.decode_float_pcm(raw, frames=3, channels=2,
                                    non_interleaved=True)) == 5


def test_mono_passes_through():
    raw = np.array([0.1, 0.2], dtype=np.float32).tobytes()
    out = msa.decode_float_pcm(raw, 2, 1, True)
    assert out.tolist() == pytest.approx([0.1, 0.2])


# ── When it is offered ──────────────────────────────────────────────

@pytest.fixture
def fake_bindings(monkeypatch):
    for name in ("ScreenCaptureKit", "CoreMedia"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def test_offered_on_macos_13_and_later(fake_bindings):
    assert msa.is_supported("darwin", "13.0")
    assert msa.is_supported("darwin", "15.4.1")


def test_not_offered_on_macos_12(fake_bindings):
    """The app supports macOS 12; ScreenCaptureKit audio starts at 13.
    BlackHole remains the path there."""
    assert not msa.is_supported("darwin", "12.7.4")


def test_not_offered_off_macos(fake_bindings):
    assert not msa.is_supported("win32", "13.0")
    assert not msa.is_supported("linux", "13.0")


def test_not_offered_without_the_bindings(monkeypatch):
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", None)
    assert not msa.is_supported("darwin", "14.0")


def test_an_unreadable_version_is_not_offered(fake_bindings):
    assert not msa.is_supported("darwin", "")
    assert not msa.is_supported("darwin", "garbage")


def test_listed_first_on_a_mac_beside_blackhole(monkeypatch):
    monkeypatch.setattr(ac, "IS_WINDOWS", False)
    monkeypatch.setattr(ac, "_output_cache", None)
    monkeypatch.setattr(msa, "is_supported", lambda *a, **k: True)
    monkeypatch.setattr(ac.sd, "query_devices", lambda *a, **k: [
        {"name": "MacBook Pro Microphone", "max_input_channels": 1,
         "default_samplerate": 48000.0},
        {"name": "BlackHole 2ch", "max_input_channels": 2,
         "default_samplerate": 48000.0},
    ], raising=False)
    names = [d["name"] for d in ac.list_output_devices()]
    assert names == [msa.SYSTEM_AUDIO_NAME, "BlackHole 2ch"]
    assert ac.list_output_devices()[0]["index"] == msa.SYSTEM_AUDIO_INDEX


def test_the_index_cannot_collide_with_a_real_device():
    assert msa.SYSTEM_AUDIO_INDEX < 0


# ── Permission ──────────────────────────────────────────────────────

class _NSError:
    def __init__(self, code, text):
        self._code, self._text = code, text

    def code(self):
        return self._code

    def __str__(self):
        return self._text


def test_a_declined_permission_says_where_to_turn_it_on():
    msg = msa.describe_start_error(_NSError(-3801, "The user declined TCCs"))
    assert "Screen & System Audio Recording" in msg


def test_another_failure_shows_what_macos_said():
    msg = msa.describe_start_error(_NSError(-3802, "Failed to start stream"))
    assert "Failed to start stream" in msg
    assert "Privacy" not in msg


def test_the_live_warning_carries_the_permission_fix():
    assert classify_open_error(msa.PERMISSION_MESSAGE) == REASON_PERMISSION
    issue = live_capture_issue(
        elapsed_s=1.0, mic_silent_for_s=0.1, system_configured=True,
        system_open_error=msa.PERMISSION_MESSAGE, system_silent_for_s=None,
        platform="darwin", grace_s=5.0, dead_after_s=45.0)
    assert issue.code == SYSTEM_AUDIO_UNAVAILABLE
    assert "Screen & System Audio Recording" in issue.message
    assert "Sound settings" not in issue.message


def test_the_session_warning_names_the_permission():
    w = missing_system_audio_warning(
        system_configured=True, system_samples=0,
        system_open_error=msa.PERMISSION_MESSAGE)
    assert "permission" in w


# ── The real capture path, Apple side stood in ──────────────────────

class _FakeScreenCapture:
    """Stands in for ScreenCaptureAudio: delivers mono blocks the way
    its ScreenCaptureKit handler does."""
    fail_with = None
    instances = []

    def __init__(self, on_block):
        self.on_block = on_block
        self.stopped = False
        _FakeScreenCapture.instances.append(self)

    def start(self):
        if _FakeScreenCapture.fail_with:
            raise RuntimeError(_FakeScreenCapture.fail_with)

    def stop(self):
        self.stopped = True


@pytest.fixture
def native(monkeypatch):
    _FakeScreenCapture.fail_with = None
    _FakeScreenCapture.instances = []
    monkeypatch.setattr(msa, "ScreenCaptureAudio", _FakeScreenCapture)
    monkeypatch.setattr(ac, "_ACTIVE_CAPTURES", set())
    return _FakeScreenCapture


def test_system_audio_reaches_the_wav_and_the_live_transcript(native,
                                                              tmp_path):
    wav = tmp_path / "_loopback_S.wav"
    teed = []
    cap = ac.AudioCapture(mic_device_index=None,
                          output_device_index=msa.SYSTEM_AUDIO_INDEX,
                          on_chunk=lambda c: None,
                          loopback_wav_path=str(wav),
                          on_loopback_chunk=teed.append)
    cap.start()
    src = native.instances[0]
    block = np.full(4800, 0.25, dtype=np.float32)
    for _ in range(5):
        src.on_block(block)
    time.sleep(0.8)
    cap.stop()

    assert src.stopped
    assert cap.loopback_error is None
    data, sr = sf.read(str(wav), dtype="float32")
    assert sr == msa.SAMPLE_RATE
    assert len(data) == 5 * 4800
    assert sum(len(b) for b in teed) == 5 * 4800
    stats = cap.get_capture_stats()
    assert stats["loopback_samples"] == 5 * 4800
    assert stats["loopback_sr"] == msa.SAMPLE_RATE


def test_a_refused_start_records_mic_only_and_says_why(native, tmp_path):
    native.fail_with = msa.PERMISSION_MESSAGE
    cap = ac.AudioCapture(mic_device_index=None,
                          output_device_index=msa.SYSTEM_AUDIO_INDEX,
                          on_chunk=lambda c: None,
                          loopback_wav_path=str(tmp_path / "lb.wav"))
    cap.start()
    cap.stop()
    assert cap.loopback_error == msa.PERMISSION_MESSAGE
    assert classify_open_error(cap.loopback_error) == REASON_PERMISSION


def test_blackhole_devices_still_take_the_blackhole_path(native,
                                                         monkeypatch):
    called = []
    monkeypatch.setattr(ac, "IS_WINDOWS", False)
    monkeypatch.setattr(ac.AudioCapture, "_start_loopback_macos",
                        lambda self: called.append("blackhole"))
    cap = ac.AudioCapture(mic_device_index=None, output_device_index=4,
                          on_chunk=lambda c: None)
    cap.start()
    cap.stop()
    assert called == ["blackhole"] and native.instances == []
