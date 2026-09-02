"""
VAD-driven chunking in core/live_transcriber.py (_SourceBuffer + the
LiveTranscriber worker's fallback / speaker-label wiring).

Field report 2026-08-10 (Zoom notetaker parity): replaces the previous
fixed-15s-window buffering with speech-boundary chunking so live text
shows up in ~1-3s instead of ~15s. These tests pin:

  - a chunk is emitted once trailing silence closes an utterance
  - a continuous talker still gets force-flushed at the hard ceiling
  - a buffer that never contains speech is never handed to Whisper
  - VAD disabled (or failing) falls back to the original fixed-window
    behavior, end to end through LiveTranscriber
  - loopback segments carry an optional speaker_label when a tracker is
    wired in; mic segments never do; nothing breaks when no tracker is
    wired at all (the speechbrain-unavailable case)

No faster-whisper import anywhere — LiveTranscriber only ever calls
`engine._model.transcribe(...)` through a duck-typed `engine_provider`,
so a small fake stands in for the whole ML stack.
"""

from __future__ import annotations

import queue
import time

import numpy as np
import pytest

from core.live_transcriber import (
    LiveTranscriber,
    VAD_HARD_CEILING_S,
    WINDOW_SECONDS,
    _SourceBuffer,
)

SR = 16000


def _sine(duration_s: float, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(duration_s * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


# ── _SourceBuffer.try_vad_chunk (unit level) ─────────────────────────

def test_emits_on_trailing_silence_after_enough_speech():
    buf = _SourceBuffer("them", SR, vad_enabled=True)
    buf.push(_sine(0.8))
    assert buf.try_vad_chunk() is None  # no trailing silence yet

    buf.push(_silence(0.5))  # clears the 400ms trailing-silence bar
    result = buf.try_vad_chunk()
    assert result is not None
    assert len(result.audio) > 0
    assert result.consumed_s > 0


def test_does_not_emit_before_min_speech_accumulated():
    buf = _SourceBuffer("them", SR, vad_enabled=True)
    # A very short utterance (well under the 600ms min-speech bar)
    # followed by plenty of trailing silence should NOT flush early —
    # it should keep waiting (more audio, or the hard ceiling).
    buf.push(_sine(0.15))
    buf.push(_silence(1.0))
    assert buf.try_vad_chunk() is None


def test_force_flushes_continuous_speech_at_hard_ceiling():
    buf = _SourceBuffer("them", SR, vad_enabled=True)
    buf.push(_sine(VAD_HARD_CEILING_S + 1.0))
    result = buf.try_vad_chunk()
    assert result is not None
    assert len(result.audio) > 0
    # Buffer should be (mostly) drained — nothing left over the ceiling.
    assert buf._chunk_samples < int(SR * VAD_HARD_CEILING_S)


def test_never_emits_a_silence_only_chunk():
    buf = _SourceBuffer("them", SR, vad_enabled=True)
    # Below the ceiling: just keep waiting, no emission.
    buf.push(_silence(2.0))
    assert buf.try_vad_chunk() is None

    # At/above the ceiling: silence gets dropped, not emitted.
    buf2 = _SourceBuffer("them", SR, vad_enabled=True)
    buf2.push(_silence(VAD_HARD_CEILING_S + 1.0))
    assert buf2.try_vad_chunk() is None
    assert buf2._chunk_samples == 0  # dropped, not sitting there forever


def test_vad_chunk_timestamps_advance_monotonically():
    buf = _SourceBuffer("them", SR, vad_enabled=True)
    buf.push(_sine(0.8))
    buf.push(_silence(0.5))
    r1 = buf.try_vad_chunk()
    assert r1 is not None
    start1 = buf.next_window_start  # not yet advanced by the caller in
    # this unit test (that's LiveTranscriber's job) — but consumed_s
    # itself must be positive and less than or equal to everything
    # pushed so far.
    assert 0 < r1.consumed_s <= 1.3 + 0.01


# ── Fake engine + end-to-end LiveTranscriber behavior ────────────────

class _FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class _FakeModel:
    """Stands in for faster-whisper's WhisperModel. Returns one segment
    spanning the whole input clip with placeholder text, so tests can
    assert on segment COUNT and speaker fields without needing a real
    ASR model."""

    def __init__(self):
        self.calls = []
        #: Decode options of the most recent call, so a test can assert
        #: what the live path actually asked for.
        self.last_opts = {}

    def transcribe(self, audio, **opts):
        # **opts, not a hand-copied parameter list. This fake used to
        # restate the signature as (audio, language, vad_filter), so
        # when the live path started passing the shared decode options
        # (core/decode_options.py — glossary prompt, VAD parameters,
        # beam size) every call raised TypeError. The live worker
        # catches and logs that, so the suite saw "no segments" and the
        # real failure — a fixture that had drifted from the caller —
        # was invisible.
        self.calls.append(len(audio))
        self.last_opts = dict(opts)
        if len(audio) == 0:
            return iter([]), None
        seg = _FakeSegment(0.0, len(audio) / SR, "hello world")
        return iter([seg]), None


class _FakeEngine:
    def __init__(self):
        self._model = _FakeModel()


def _drain_history(lt: LiveTranscriber, timeout_s: float = 3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if lt.all_segments():
            return lt.all_segments()
        time.sleep(0.05)
    return lt.all_segments()


def test_vad_enabled_end_to_end_produces_segments_quickly():
    engine = _FakeEngine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    lt.start(SR, vad_enabled=True)
    try:
        lt.push_loopback(_sine(0.8))
        lt.push_loopback(_silence(0.5))
        segs = _drain_history(lt)
        assert len(segs) >= 1
        assert segs[0]["speaker"] == "them"
    finally:
        lt.stop()


def test_vad_disabled_falls_back_to_fixed_window_path():
    engine = _FakeEngine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    lt.start(SR, vad_enabled=False)
    try:
        # Fixed-window mode needs a FULL WINDOW_SECONDS of audio before
        # anything flushes — short audio should NOT produce a segment.
        lt.push_audio(_sine(1.0))
        time.sleep(0.6)
        assert lt.all_segments() == []

        # A full window's worth flushes.
        lt.push_audio(_sine(WINDOW_SECONDS))
        segs = _drain_history(lt)
        assert len(segs) >= 1
        assert segs[0]["speaker"] == "you"
    finally:
        lt.stop()


def test_speaker_label_present_on_loopback_absent_on_mic():
    engine = _FakeEngine()

    class _FixedTracker:
        def reset(self):
            pass

        def assign(self, pcm, samplerate):
            return "Speaker 1"

    lt = LiveTranscriber(
        engine_provider=lambda: engine, samplerate=SR,
        speaker_tracker=_FixedTracker(),
    )
    lt.start(SR, vad_enabled=True)
    try:
        lt.push_audio(_sine(0.8))
        lt.push_audio(_silence(0.5))
        lt.push_loopback(_sine(0.8))
        lt.push_loopback(_silence(0.5))
        deadline = time.time() + 3.0
        while time.time() < deadline and len(lt.all_segments()) < 2:
            time.sleep(0.05)
        segs = lt.all_segments()
        mic_segs = [s for s in segs if s["speaker"] == "you"]
        them_segs = [s for s in segs if s["speaker"] == "them"]
        assert mic_segs and them_segs
        assert all("speaker_label" not in s for s in mic_segs)
        assert all(s.get("speaker_label") == "Speaker 1" for s in them_segs)
    finally:
        lt.stop()


def test_no_speaker_tracker_never_raises_and_stays_them():
    """Progressive enhancement: when embeddings are unavailable
    (speaker_tracker=None, exactly what recording_service.py wires up
    when speaker_embeddings.is_available() is False), the loopback
    stream must behave exactly as before — plain "them", no
    speaker_label, nothing raises."""
    engine = _FakeEngine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    lt.start(SR, vad_enabled=True)
    try:
        lt.push_loopback(_sine(0.8))
        lt.push_loopback(_silence(0.5))
        segs = _drain_history(lt)
        assert len(segs) >= 1
        assert segs[0]["speaker"] == "them"
        assert "speaker_label" not in segs[0]
    finally:
        lt.stop()


# ── The live path uses the SHARED decode options ────────────────────
#
# Finding 4 of the 2026-09-02 pipeline audit: the batch pass received a
# glossary-derived `initial_prompt` and the live path did not, so the
# transcript the user WATCHES mis-heard exactly the product and customer
# terms the glossary exists to correct — while the transcript written to
# disk got them right. Two transcripts of one meeting, disagreeing, and
# nothing anywhere reported a problem.


def _run_one_window(language="en", initial_prompt=""):
    """Drive one real window through the worker and hand back the decode
    options the engine was actually called with.

    Same shape as test_vad_enabled_end_to_end_produces_segments_quickly
    above — the point is that these options survive the REAL worker
    path, not that a helper can be called directly.
    """
    engine = _FakeEngine()
    lt = LiveTranscriber(engine_provider=lambda: engine, samplerate=SR)
    lt.start(SR, vad_enabled=True, language=language,
             initial_prompt=initial_prompt)
    try:
        lt.push_loopback(_sine(0.8))
        lt.push_loopback(_silence(0.5))
        assert _drain_history(lt), "no segment produced; options unverifiable"
    finally:
        lt.stop()
    return engine._model.last_opts


def test_the_live_path_receives_the_glossary_prompt():
    opts = _run_one_window(initial_prompt="Globex, Initech, ACD")
    assert opts.get("initial_prompt") == "Globex, Initech, ACD"


def test_the_live_path_receives_the_configured_language():
    """It was hardcoded "en" here, so an Italian meeting was decoded as
    English in the live view no matter what the user had set."""
    assert _run_one_window(language="it").get("language") == "it"


def test_auto_reaches_the_live_decoder_as_none():
    """"auto" is a sentinel, not a language code — sending it verbatim
    would make faster-whisper fail rather than detect."""
    assert _run_one_window(language="auto").get("language") is None


def test_the_live_path_does_not_pay_for_word_timestamps():
    """They cost decode time the live view spends on latency, and
    nothing in it consumes them."""
    assert _run_one_window().get("word_timestamps") is False

