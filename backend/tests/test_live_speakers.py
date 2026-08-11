"""
core/live_speakers.py — LiveSpeakerTracker.

Field report 2026-08-10 (Zoom notetaker parity): splits the live
"them" bucket into distinct Speaker N labels. Tested entirely with a
fake embed_fn (fixed vectors keyed by a marker sample in the fake pcm)
— speechbrain/torch are not importable in the CI venv (AGENTS.md), and
LiveSpeakerTracker is designed to need only an injected callable, never
a real ML import.

Field report 2026-08-11 (live speaker OVER-splitting) rewrote the
threshold expectations in this file. On a real 2-person call, one
continuous speaker was labelled SPEAKER 1/2/3/4/5/6/7/9 — a new
identity every few seconds — and a female speaker was labelled with a
male colleague's saved name. The tests below now pin the corrected
behavior:

  * matching is EASY (0.55) so the same voice merges through
    embedding jitter on short clips;
  * creating is HARD — a separate, much lower "clearly different"
    threshold (0.40) AND a 2.5s duration floor, both required;
  * anything ambiguous or short goes to the best existing match,
    never to a new identity;
  * a real NAME needs 0.88 similarity AND 2.5s, because a confidently
    wrong name is the worst failure this feature has.

The `_clip` default duration is 3.0s (above MIN_NEW_SPEAKER_SECONDS)
so tests that aren't ABOUT the duration gate read cleanly; tests that
exercise the gate pass an explicit short duration.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.live_speakers import (
    MATCH_THRESHOLD,
    MIN_NEW_SPEAKER_SECONDS,
    NEW_SPEAKER_THRESHOLD,
    PROFILE_NAME_THRESHOLD,
    LiveSpeakerTracker,
)

SR = 16000


def _clip(marker: float, duration_s: float = 3.0) -> np.ndarray:
    """A fake "utterance" — every sample equal to `marker`, so a fake
    embed_fn can identify which voice it's supposed to represent purely
    from the array contents, without any real signal processing.

    Default is 3.0s: long enough to clear MIN_NEW_SPEAKER_SECONDS, so a
    test only has to think about duration when duration is the point.
    """
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


def _unit(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float32)
    return a / float(np.linalg.norm(a))


VEC_A = np.array([1.0, 0.0, 0.0], dtype=np.float32)
VEC_B = np.array([0.0, 1.0, 0.0], dtype=np.float32)
VEC_A_NEAR = np.array([0.98, 0.2, 0.0], dtype=np.float32)  # cos-sim > 0.9 to A


def test_threshold_constants_encode_the_merge_bias():
    """The whole 2026-08-11 fix is that these are SEPARATE numbers and
    that creating is strictly harder than matching. Pin it so a future
    'simplification' back to one threshold fails loudly here."""
    assert NEW_SPEAKER_THRESHOLD < MATCH_THRESHOLD
    # The old single threshold was 0.75; matching must now be far more
    # forgiving than that or short-clip jitter re-splits voices.
    assert MATCH_THRESHOLD <= 0.6
    # Naming must be far STRICTER than matching, and stricter than the
    # post-stop profile default (0.75).
    assert PROFILE_NAME_THRESHOLD >= 0.85
    # A new identity needs materially more audio than the bare
    # embed-at-all floor of 1.0s.
    assert MIN_NEW_SPEAKER_SECONDS >= 2.0


# ── Fix 1: over-splitting ────────────────────────────────────────────


def test_one_jittery_voice_over_many_short_clips_stays_one_speaker():
    """REGRESSION TEST for field report 2026-08-11.

    The reported bug: a single continuous speaker on a 2-person call
    came out as SPEAKER 1, 2, 3, 4, 5, 6, 7 and 9. The mechanism was
    embedding jitter on short VAD-sized clips repeatedly dipping under
    the old 0.75 threshold, and every dip minting a new identity.

    Here: 12 clips of ONE voice, each a slightly rotated version of the
    same underlying vector (cosine to the base vector runs ~0.71-0.995,
    i.e. squarely in the range that used to create new speakers). The
    tracker must end with exactly ONE label.
    """
    rng = np.random.default_rng(20260811)
    base = _unit([1.0, 0.0, 0.0])
    vectors = {}
    for i in range(12):
        jitter = rng.normal(0.0, 0.35, size=3).astype(np.float32)
        jitter[0] = abs(jitter[0]) * 0.05   # keep it the SAME voice
        vectors[float(i)] = _unit(base + jitter)

    # Sanity: this jitter really does push below the OLD 0.75 bar at
    # least once — otherwise the test wouldn't reproduce the bug.
    assert any(float(np.dot(v, base)) < 0.75 for v in vectors.values())

    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn(vectors))
    labels = [tracker.assign(_clip(float(i)), SR) for i in range(12)]

    assert tracker.speaker_count == 1, (
        f"one voice split into {tracker.speaker_count} identities: "
        f"{sorted(set(labels))}")
    assert set(labels) == {"Speaker 1"}


def test_same_voice_twice_gets_same_label():
    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_A_NEAR}))
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(2.0), SR)
    assert l1 == l2 == "Speaker 1"
    assert tracker.speaker_count == 1


def test_clearly_different_voice_still_gets_new_label():
    """Biasing toward merging must not collapse everyone into one
    label — an orthogonal voice (cosine 0.0, well under the 0.40
    "clearly different" bar) on a long-enough clip still earns its own
    identity."""
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(2.0), SR)
    assert l1 != l2
    assert tracker.speaker_count == 2


def test_ambiguous_similarity_goes_to_best_match_not_a_new_speaker():
    """The band BETWEEN the two thresholds is where the field report's
    phantom speakers came from. A clip that's neither a confident match
    nor clearly different must be handed to the closest existing
    speaker — long clip or not."""
    # cos(A, ambiguous) ≈ 0.47: below MATCH_THRESHOLD (0.55) but above
    # NEW_SPEAKER_THRESHOLD (0.40) — squarely ambiguous.
    ambiguous = _unit([0.47, 0.883, 0.0])
    sim = float(np.dot(ambiguous, VEC_A))
    assert NEW_SPEAKER_THRESHOLD < sim < MATCH_THRESHOLD

    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: ambiguous}))
    l1 = tracker.assign(_clip(1.0), SR)
    # Deliberately a LONG clip: duration is not the reason this must
    # not create a speaker — the ambiguity is.
    l2 = tracker.assign(_clip(2.0, duration_s=8.0), SR)
    assert l2 == l1
    assert tracker.speaker_count == 1


def test_short_clip_never_creates_a_new_speaker_even_when_clearly_different():
    """Both gates are required. This clip is unmistakably a different
    voice (orthogonal, similarity 0.0) but it's under
    MIN_NEW_SPEAKER_SECONDS, so it folds into the best existing match
    instead of minting an identity."""
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    l1 = tracker.assign(_clip(1.0), SR)
    l2 = tracker.assign(_clip(2.0, duration_s=1.8), SR)  # > 1.0s, < 2.5s
    assert l2 == l1
    assert tracker.speaker_count == 1
    # Same voice, now on a long enough clip — NOW it may create.
    l3 = tracker.assign(_clip(2.0, duration_s=3.0), SR)
    assert l3 != l1
    assert tracker.speaker_count == 2


def test_sub_one_second_utterance_reuses_previous_speaker():
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: VEC_B}))
    l1 = tracker.assign(_clip(1.0), SR)
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
    # Still no centroid off a 1.5s clip — embeddable, but not enough
    # evidence to assert a NEW PERSON exists.
    label2 = tracker.assign(_clip(1.0, duration_s=1.5), SR)
    assert label2 == "Speaker 1"
    assert tracker.speaker_count == 0
    # A real (long-enough) utterance finally creates Speaker 1 fresh.
    label3 = tracker.assign(_clip(1.0, duration_s=3.0), SR)
    assert label3 == "Speaker 1"
    assert tracker.speaker_count == 1


def test_previous_speaker_wins_a_near_tie():
    """Stickiness: consecutive turns from one person are the common
    case, so a near-tie between two centroids resolves toward whoever
    was already talking rather than flipping the label."""
    v_a = _unit([1.0, 0.0, 0.0])
    v_b = _unit([0.0, 1.0, 0.0])
    # Sits almost exactly between A and B, tipped microscopically
    # toward B — without stickiness this lands on Speaker 2.
    tie = _unit([0.70, 0.72, 0.0])
    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: v_a, 2.0: v_b, 3.0: tie}))
    tracker.assign(_clip(2.0), SR)          # Speaker 1 = voice B
    a_label = tracker.assign(_clip(1.0), SR)  # Speaker 2 = voice A
    assert tracker.speaker_count == 2
    tied_label = tracker.assign(_clip(3.0), SR)
    assert tied_label == a_label  # stayed with whoever was just talking
    assert tracker.speaker_count == 2


def test_centroid_updates_move_toward_repeated_samples():
    # Start centroid at A, then repeatedly show a vector that's close to
    # A but not identical — the running mean should track toward it
    # rather than staying frozen at the very first sample.
    drift_vec = np.array([0.6, 0.8, 0.0], dtype=np.float32)  # cos(A, drift) = 0.6
    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A, 2.0: drift_vec}),
        match_threshold=0.5,
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


def test_at_the_cap_everything_folds_into_best_match():
    """Once at the cap, even a clearly-different long clip must be
    assigned, never appended — the bound on drift is unconditional."""
    vectors = {}
    for i in range(4):
        v = np.zeros(4, dtype=np.float32)
        v[i] = 1.0
        vectors[float(i)] = v
    tracker = LiveSpeakerTracker(embed_fn=_make_embed_fn(vectors), max_speakers=2)
    tracker.assign(_clip(0.0), SR)
    tracker.assign(_clip(1.0), SR)
    assert tracker.speaker_count == 2
    label = tracker.assign(_clip(2.0, duration_s=8.0), SR)
    assert label in ("Speaker 1", "Speaker 2")
    assert tracker.speaker_count == 2


# ── Fix 2: wrong names from profile matching ─────────────────────────


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


def test_profile_match_below_the_live_naming_bar_yields_speaker_n():
    """Field report 2026-08-11: a FEMALE speaker was labelled "CALEB
    JOHNSON" — a male colleague with a saved voiceprint. 0.80 clears
    SpeakerProfileService's post-stop default of 0.75 but must NOT be
    enough to assert a real person's name in the live preview."""
    def _profile_lookup(embedding):
        return "Caleb Johnson", 0.80  # above 0.75, below 0.88

    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A}),
        profile_lookup=_profile_lookup,
    )
    label = tracker.assign(_clip(1.0), SR)
    assert label == "Speaker 1"
    assert "Caleb" not in label
    # It fell through to the ordinary live-centroid path, so the voice
    # is now tracked generically.
    assert tracker.speaker_count == 1


def test_short_clip_never_yields_a_name_even_at_perfect_similarity():
    """A 1-second clip must never produce a person's name, no matter
    how confident the profile store claims to be."""
    calls = []

    def _profile_lookup(embedding):
        calls.append(1)
        return "Caleb Johnson", 0.99

    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A}),
        profile_lookup=_profile_lookup,
    )
    label = tracker.assign(_clip(1.0, duration_s=1.2), SR)
    assert label == "Speaker 1"
    # The lookup isn't even consulted on a clip this short.
    assert calls == []
    # And no identity was created off it either.
    assert tracker.speaker_count == 0


def test_profile_lookup_without_a_usable_similarity_does_not_name():
    """A lookup that can't say how confident it is is treated as not
    confident enough — never as a licence to print a name."""
    def _profile_lookup(embedding):
        return "Caleb Johnson", None  # type: ignore[return-value]

    tracker = LiveSpeakerTracker(
        embed_fn=_make_embed_fn({1.0: VEC_A}),
        profile_lookup=_profile_lookup,
    )
    assert tracker.assign(_clip(1.0), SR) == "Speaker 1"


# ── Degradation / lifecycle ──────────────────────────────────────────


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
