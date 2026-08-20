"""
PortAudio must never be entered by two threads at once.

FIELD INCIDENT 2026-08-20 — an unrecoverable boot loop.

`PyAudio()` calls `Pa_Initialize()`; `.terminate()` calls
`Pa_Terminate()`. Both mutate one process-global library state in C and
must not run concurrently. faulthandler caught exactly that:

    Thread A  _prewarm_audio → list_output_devices → PyAudio.terminate()
    Thread B  auto-record starting a recording → PyAudio.__init__()

    Windows fatal exception: access violation   (0xC0000005)
    ...and 0xC0000374 on other spawns — heap corruption, same fault.

It was fatal rather than intermittent because the user had a meeting
already IN PROGRESS: `AutoRecordService` fired a recording start
milliseconds after boot, exactly when the audio pre-warm runs. Backend
died, supervisor respawned, meeting still in progress, died again —
sixteen cycles in four minutes with the app unusable and Settings
unreachable (Settings is served BY the backend).

There is no Python exception to assert on: a C-level access violation
kills the interpreter outright, so a test cannot "catch" the bug by
reproducing it. What CAN be pinned is the invariant that prevents it —
**at most one thread inside PortAudio at any instant** — by driving the
real locking helpers against a fake that reports overlap.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests._app_import import _stub_optional_modules

# sounddevice is a real hardware dependency and is not in the minimal
# test env. Stub it BEFORE importing audio_capture, which imports it at
# module scope. The PortAudio path under test is pyaudiowpatch, which
# the fixture replaces wholesale, so nothing here needs real audio.
_stub_optional_modules()

from core import audio_capture  # noqa: E402


class OverlapDetectingPyAudio:
    """Stand-in for pyaudiowpatch that records concurrent entry.

    Each __init__/terminate holds a short, real sleep so genuinely
    concurrent callers overlap in wall-clock time rather than merely
    interleaving between bytecodes — without it, the GIL alone could
    hide a missing lock and the test would pass vacuously.
    """

    def __init__(self, tracker):
        self._tracker = tracker
        tracker.enter("init")
        time.sleep(0.01)
        tracker.exit("init")

    def terminate(self):
        self._tracker.enter("terminate")
        time.sleep(0.01)
        self._tracker.exit("terminate")

    # Enough surface for list_output_devices to run.
    def get_host_api_count(self):
        return 1

    def get_host_api_info_by_index(self, i):
        return {"name": "Windows WASAPI", "index": 0, "deviceCount": 1}

    def get_device_info_by_host_api_device_index(self, api_idx, i):
        return {
            "index": 7, "name": "Speakers (loopback)", "isLoopbackDevice": True,
            "maxInputChannels": 2, "defaultSampleRate": 48000.0,
        }


class Tracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.inside = 0
        self.max_inside = 0
        self.overlaps = []

    def enter(self, what):
        with self._lock:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
            if self.inside > 1:
                self.overlaps.append(what)

    def exit(self, what):
        with self._lock:
            self.inside -= 1


@pytest.fixture
def fake_portaudio(monkeypatch):
    tracker = Tracker()

    class Module:
        paFloat32 = 1

        @staticmethod
        def PyAudio():  # noqa: N802 - mirrors the real API
            return OverlapDetectingPyAudio(tracker)

    monkeypatch.setattr(audio_capture, "pyaudio", Module)
    return tracker


def _hammer(fn, threads=8, iterations=6):
    errors = []

    def run():
        try:
            for _ in range(iterations):
                fn()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=run) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert not errors, errors
    assert not any(t.is_alive() for t in ts), "a worker deadlocked"


def test_concurrent_sessions_never_overlap(fake_portaudio):
    """THE regression. Eight threads doing init→use→terminate; if the
    lock is missing or released between use and terminate, two land
    inside PortAudio together — which on Windows is the access
    violation."""
    def use():
        with audio_capture.portaudio_session() as p:
            p.get_host_api_count()

    _hammer(use)
    assert fake_portaudio.max_inside == 1, (
        f"{fake_portaudio.max_inside} threads were inside PortAudio at once "
        f"(overlaps: {fake_portaudio.overlaps[:5]})")


def test_the_exact_field_crash_pairing_is_serialised(fake_portaudio):
    """Thread A enumerating devices while Thread B constructs a handle
    for a recording — the precise pairing faulthandler captured."""
    stop = threading.Event()
    errors = []

    def enumerator():          # _prewarm_audio → list_output_devices
        try:
            while not stop.is_set():
                audio_capture._output_cache = None  # defeat the 60s cache
                audio_capture.list_output_devices()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def recorder():            # auto-record → _start_loopback_windows
        try:
            for _ in range(15):
                pa = audio_capture.portaudio_new()
                audio_capture.portaudio_terminate(pa)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    # Several of each: with one thread apiece the two can miss each
    # other by luck, and a test that only sometimes reproduces the bug
    # it is named for is worse than no test — it reads as coverage.
    # Verified to FAIL when the lock is removed.
    threads = ([threading.Thread(target=enumerator) for _ in range(3)]
               + [threading.Thread(target=recorder) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads[3:]:      # recorders finish on their own
        t.join(timeout=30)
    stop.set()
    for t in threads[:3]:
        t.join(timeout=30)
    a = b = None

    assert not errors, errors
    assert fake_portaudio.max_inside == 1, (
        "device enumeration and recording start overlapped inside "
        "PortAudio — this is the 0xC0000005 crash")


def test_terminate_runs_even_when_the_body_raises(fake_portaudio):
    """A throw mid-enumeration must still release PortAudio. Leaking an
    initialised library would strand the refcount and make the NEXT
    caller's terminate the one that corrupts."""
    with pytest.raises(RuntimeError):
        with audio_capture.portaudio_session() as p:
            p.get_host_api_count()
            raise RuntimeError("boom")
    assert fake_portaudio.inside == 0


def test_terminate_never_raises(fake_portaudio):
    """Teardown runs on stop and error paths where a throw would mask
    the original failure."""
    class Exploding:
        def terminate(self):
            raise OSError("device went away")

    audio_capture.portaudio_terminate(Exploding())   # must not raise
    audio_capture.portaudio_terminate(None)          # nor on None


def test_the_lock_is_reentrant(fake_portaudio):
    """The capture path can enumerate while already holding the lock. A
    plain Lock would turn that into a deadlock — trading a crash for a
    hang is not a fix."""
    done = threading.Event()

    def nested():
        with audio_capture.portaudio_session() as outer:
            outer.get_host_api_count()
            with audio_capture.portaudio_session() as inner:
                inner.get_host_api_count()
        done.set()

    t = threading.Thread(target=nested)
    t.start()
    t.join(timeout=10)
    assert done.is_set(), "nested acquisition deadlocked"
