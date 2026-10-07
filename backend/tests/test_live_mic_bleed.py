"""
Live transcript: once the mic is shown to be hearing the call, mic
chunks that land on far-end playback are not shown as "You".

Synthetic audio stands in for the field case (core/live_mic_bleed):
a far end talking in bursts on the loopback, and a mic that either
hears none of it (headset) or every burst (laptop speakers, user
muted or quiet).
"""

from __future__ import annotations

import queue

import numpy as np
import pytest

from core.live_mic_bleed import MIN_MIC_SECONDS, MicBleedGate
from core.live_transcriber import LiveTranscriber

SR = 16000


def _loopback(pattern, seconds_each=5.0, amp=0.1, seed=3):
    """Loopback audio: for each True in `pattern`, `seconds_each` of
    far-end speech; for each False, the same length of near-silence."""
    rng = np.random.default_rng(seed)
    parts = []
    for on in pattern:
        n = int(SR * seconds_each)
        noise = rng.standard_normal(n).astype(np.float32)
        parts.append(noise * (amp if on else 1e-4))
    return np.concatenate(parts)


def _feed(gate, audio, block=1024):
    for i in range(0, len(audio), block):
        gate.push_loopback(audio[i:i + block])


PATTERN = [True, False] * 12          # 120 s: 5 s talk, 5 s quiet
TALK = [(i * 10.0, i * 10.0 + 5.0) for i in range(12)]
QUIET = [(i * 10.0 + 5.0, i * 10.0 + 10.0) for i in range(12)]


def test_a_headset_mic_is_never_judged_to_hear_the_call():
    gate = MicBleedGate(SR)
    _feed(gate, _loopback(PATTERN))
    verdicts = [gate.mic_chunk(a + 0.5, b - 0.5) for a, b in QUIET]
    assert not any(v.drop for v in verdicts)
    assert not gate.hears_call


def test_speakers_while_muted_drop_the_far_ends_copy():
    gate = MicBleedGate(SR)
    _feed(gate, _loopback(PATTERN))
    verdicts = [gate.mic_chunk(a, b) for a, b in TALK]
    # Nothing is judged on the first few chunks…
    seen = 0.0
    for (a, b), v in zip(TALK, verdicts):
        if seen + (b - a) < MIN_MIC_SECONDS:
            assert not v.drop
        seen += b - a
    # …then every copy of the far end is dropped.
    assert gate.hears_call
    assert all(v.drop for v in verdicts[-5:])


def test_the_user_speaking_into_a_quiet_moment_still_shows():
    gate = MicBleedGate(SR)
    _feed(gate, _loopback(PATTERN))
    for a, b in TALK:
        gate.mic_chunk(a, b)
    assert gate.hears_call
    a, b = QUIET[-1]
    assert gate.mic_chunk(a + 0.5, b - 0.5).drop is False


def test_ordinary_interruptions_on_a_headset_do_not_trip_it():
    """Headset double-talk: a fifth of the user's speech overlaps the far
    end. Field calls on headsets measured 0.000."""
    gate = MicBleedGate(SR)
    _feed(gate, _loopback(PATTERN))
    for i, (a, b) in enumerate(QUIET):
        gate.mic_chunk(a + 0.5, b - 0.5)
        if i % 4 == 0:
            ta, tb = TALK[i]
            gate.mic_chunk(ta + 1.0, ta + 2.0)
    assert not gate.hears_call


def test_without_system_audio_nothing_is_dropped():
    gate = MicBleedGate(SR)
    verdicts = [gate.mic_chunk(a, b) for a, b in TALK]
    assert not any(v.drop for v in verdicts) and not gate.hears_call


# ── through the live transcriber ─────────────────────────────────────

class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class _Model:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **opts):
        self.calls += 1
        return iter([_Seg(0.0, len(audio) / SR,
                          f"far end sentence number {self.calls} here")]), None


class _Engine:
    def __init__(self):
        self._model = _Model()


def test_the_live_transcript_stops_showing_the_far_end_as_you():
    engine = _Engine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    q = lt.subscribe()
    lt._running = True                     # feed without the worker thread
    lt.push_loopback(_loopback(PATTERN))
    mic_audio = np.zeros(int(SR * 5.0), dtype=np.float32)
    for a, _b in TALK:
        lt._transcribe_window(lt._mic, mic_audio, a)
    shown = []
    while True:
        try:
            shown.append(q.get_nowait())
        except queue.Empty:
            break
    you = [e for e in shown if e.get("speaker") == "you"]
    # The first chunks show while the evidence builds; the rest never
    # reach Whisper, let alone the screen.
    assert 0 < len(you) < len(TALK)
    assert engine._model.calls == len(you)


@pytest.mark.parametrize("label", ["room"])
def test_conference_room_mode_is_left_alone(label):
    """In room mode the mic is labelled "room", not "you": the gate is
    about not crediting the far end to the user, so it stays out."""
    engine = _Engine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    lt._mic.label = label
    lt._running = True
    lt.push_loopback(_loopback(PATTERN))
    mic_audio = np.zeros(int(SR * 5.0), dtype=np.float32)
    for a, _b in TALK:
        lt._transcribe_window(lt._mic, mic_audio, a)
    assert engine._model.calls == len(TALK)
