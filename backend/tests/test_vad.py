"""
core/vad.py — energy+zero-crossing VAD detector.

Field report 2026-08-10 (Zoom notetaker parity): these pin the boundary
behavior VAD-driven live-transcript chunking depends on — a bug here
either splits utterances that should stay together (more Whisper calls,
choppier live text) or merges/discards ones that shouldn't (lost
speech). All test audio is synthetic (sine-tone bursts against silence),
matching the existing suite's convention (see conftest.write_sine_wav).
"""

from __future__ import annotations

import numpy as np
import pytest

from core.vad import find_utterances

SR = 16000


def _sine(duration_s: float, amp: float = 0.3, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(duration_s * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(duration_s: float) -> np.ndarray:
    return np.zeros(int(duration_s * SR), dtype=np.float32)


def test_detects_single_utterance_in_silence_speech_silence():
    audio = np.concatenate([_silence(0.3), _sine(0.5), _silence(0.3)])
    spans = find_utterances(audio, SR)
    assert len(spans) == 1
    start, end = spans[0]
    # The detected span should sit roughly where the tone is (allow a
    # generous frame-quantization margin either side).
    assert abs(start - int(0.3 * SR)) < int(0.1 * SR)
    assert abs(end - int(0.8 * SR)) < int(0.1 * SR)


def test_merges_bursts_separated_by_150ms():
    audio = np.concatenate([
        _silence(0.2), _sine(0.4), _silence(0.15), _sine(0.4), _silence(0.2),
    ])
    spans = find_utterances(audio, SR)
    assert len(spans) == 1


def test_splits_bursts_separated_by_600ms():
    audio = np.concatenate([
        _silence(0.2), _sine(0.4), _silence(0.6), _sine(0.4), _silence(0.2),
    ])
    spans = find_utterances(audio, SR)
    assert len(spans) == 2


def test_ignores_a_100ms_blip():
    audio = np.concatenate([_silence(0.3), _sine(0.1), _silence(0.3)])
    assert find_utterances(audio, SR) == []


def test_returns_nothing_for_pure_silence():
    assert find_utterances(_silence(1.0), SR) == []


def test_returns_nothing_for_empty_input():
    assert find_utterances(np.zeros(0, dtype=np.float32), SR) == []


def test_continuous_speech_with_no_pauses_is_still_detected():
    """Regression guard: a buffer that's uniformly at speech level
    throughout (no quieter frames anywhere) must not be misread as
    silence just because the adaptive noise-floor estimate has nothing
    quiet to anchor on. See the fallback-to-absolute-floor branch in
    find_utterances."""
    audio = _sine(2.0)
    spans = find_utterances(audio, SR)
    assert len(spans) == 1
    start, end = spans[0]
    assert start == 0
    assert end == len(audio)


def test_two_speech_spans_do_not_overlap_and_are_ordered():
    audio = np.concatenate([
        _silence(0.2), _sine(0.4), _silence(0.6), _sine(0.4), _silence(0.2),
    ])
    spans = find_utterances(audio, SR)
    assert spans == sorted(spans)
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2


@pytest.mark.parametrize("samplerate", [8000, 16000, 48000])
def test_works_across_samplerates(samplerate):
    t = np.arange(int(0.5 * samplerate)) / samplerate
    tone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    audio = np.concatenate([
        np.zeros(int(0.3 * samplerate), dtype=np.float32), tone,
        np.zeros(int(0.3 * samplerate), dtype=np.float32),
    ])
    spans = find_utterances(audio, samplerate)
    assert len(spans) == 1
