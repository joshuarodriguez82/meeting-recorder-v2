"""
core/live_speakers.py — LiveSpeakerTracker.

Field report 2026-08-10 (Zoom notetaker parity): splits the live
"them" bucket into distinct Speaker N labels. Tested entirely with a
fake embed_fn (fixed vectors keyed by a marker sample in the fake pcm)
— speechbrain/torch are not importable in the CI venv (AGENTS.md), and
LiveSpeakerTracker is designed to need only an injected callable, never
a real ML import.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.live_speakers import LiveSpeakerTracker

SR = 16000


def _clip(marker: float, duration_s: float = 1.5) -> np.ndarray:
    """A fake "utterance" — every sample equal to `marker`, so a fake
    embed_fn can identify which voice it's supposed to represent purely
    from the array contents, without any real signal processing."""
    return np.full(int(duration_s * SR), marker, dtype=np.float32)


def _make_embed_fn(vectors: dict):
    """vectors: {marker: embedding}. Returns None for unknown markers,
    mimicking a real encoder's degenerate-input path."""

    def _embed(pcm, samplerate):
        if len(pcm) == 0:
            return None
        marker = round(float(pcm[0]), 3)
        return vectors.get(marker)

    return _embed


VEC_A = np.array([1.0, 0.0, 0.0], dtype=np.float32)
VEC_B = np.array([0.0, 1.0, 0.0], dtype=np.float32)
VEC_A_NEAR = np.array([0.98, 0.2, 0.0], dtype=np.float32)  # cos-sim > 0.9 to A


def test_same_voice_twice_gets_same_label():
    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_A_NEAR}))
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(2.0), SR)
    assert l1 == l2 == "Speaker 1"
    assert tracker.speaker_count == 1


def test_clearly_different_voice_gets_new_label():
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(2.0), SR)
    assert l1 != l2
    assert tracker.speaker_count == 2


def test_sub_one_second_utterance_reuses_previous_speaker():
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    l1 = tracker.assign(_clip(1.0, duration_s=1.5), SR)
    # A short (400ms) clip that LOOKS like voice B by marker, but is
    # under the 1.0s reliability floor — must NOT create a new speaker,
    # must NOT even consult the embedding.
    l2 = tracker.assign(_clip(2.0, duration_s=0.4), SR)
    assert l2 == l1
    assert tracker.speaker_count == 1


def test_first_utterance_too_short_returns_placeholder_without_creating_centroid():
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A}))
    label = tracker.assign(_clip(1.0, duration_s=0.2), SR)
    assert label == "Speaker 1"
    assert tracker.speaker_count == 0  # no centroid created yet
    # A real (long-enough) first utterance still creates Speaker 1 fresh.
    label2 = tracker.assign(_clip(1.0, duration_s=1.5), SR)
    assert label2 == "Speaker 1"
    assert tracker.speaker_count == 1


def test_centroid_updates_move_toward_repeated_samples():
    # Start centroid at A, then repeatedly show a vector that's close to
    # A but not identical — the running mean should track toward it
    # rather than staying frozen at the very first sample.
    drift_vec = np.array([0.6, 0.8, 0.0], dtype=np.float32)  # cos(A, drift) = 0.6
    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: drift_vec}),
        similarity_threshold=0.5,
    )
    tracker.assign(_clip(1.0), SR)
    centroid_before = tracker._centroids[0].copy()
    for _ in range(5):
        tracker.assign(_clip(2.0), SR)
    centroid_after = tracker._centroids[0]
    # Similarity to the drift vector should have increased as the
    # centroid absorbed repeated samples of it.
    sim_before = float(np.dot(centroid_before, drift_vec))
    sim_after = float(np.dot(centroid_after, drift_vec))
    assert sim_after > sim_before
    assert tracker.speaker_count == 1  # never split into a new speaker


def test_speaker_cap_is_respected():
    # 12 totally distinct orthogonal-ish vectors, cap at 3.
    vectors = {}
    for i in range(12):
        v = np.zeros(12, dtype=np.float32)
        v[i] = 1.0
        vectors[float(i)] = v
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn(vectors), max_speakers=3)
    for i in range(12):
        tracker.assign(_clip(float(i)), SR)
    assert tracker.speaker_count == 3


def test_known_profile_match_returns_real_name_immediately():
    def _profile_lookup(embedding):
        if float(np.dot(embedding, VEC_A)) > 0.9:
            return "Maria Chen", 0.91
        return None

    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A}),
        profile_lookup=_profile_lookup,
    )
    label = tracker.assign(_clip(1.0), SR)
    assert label == "Maria Chen"
    # A known-profile match should not pollute the generic centroid list.
    assert tracker.speaker_count == 0


def test_embed_failure_degrades_to_previous_label_without_raising():
    def _flaky_embed(pcm, samplerate):
        raise RuntimeError("boom")

    tracker = LiveSpeakerTracker(embed_fn=_flaky_embed)
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(1.0), SR)
    assert l1 == "Speaker 1"
    assert l2 == l1  # stayed on the placeholder, no crash
    assert tracker.speaker_count == 0


def test_reset_clears_state_but_keeps_config():
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    tracker.assign(_clip(1.0), SR)
    tracker.assign(_clip(2.0), SR)
    assert tracker.speaker_count == 2
    tracker.reset()
    assert tracker.speaker_count == 0
    # Fresh meeting starts back at "Speaker 1".
    label = tracker.assign(_clip(1.0), SR)
    assert label == "Speaker 1"
