"""
Live speaker splitting for the "them" (loopback) stream.

Field report 2026-08-10 (Zoom notetaker parity): dual-stream live
transcription already gives us "you" vs "them" for free (see the module
docstring on core/live_transcriber.py), but every other participant on
the call lands in the same undifferentiated "them" bucket. Zoom's
notetaker splits far-end audio into "Speaker 1" / "Speaker 2" / etc. in
real time; this module is the live (not post-stop) equivalent.

Design goals, in order:

  1. Testable without speechbrain/torch. The CI venv doesn't have them
     (AGENTS.md), so this module takes an injected `embed_fn` rather
     than importing core.speaker_embeddings directly. Production wiring
     (recording_service.py) supplies a real embed_fn backed by ECAPA;
     tests supply a fake that returns fixed vectors.
  2. Never invent a new speaker off a too-short utterance. ECAPA-style
     embeddings are unreliable under ~1s of audio (see
     core/speaker_embeddings.py's own MIN_TOTAL_SECONDS), and a live
     tracker that creates "Speaker 4" every time someone says "yeah"
     is worse than useless — the field-report precedent that shaped
     this rule (see MIN_UTTERANCE_SECONDS below) is that a wrong-but-
     stable label reads as normal conversation lag; a fresh label every
     few seconds reads as broken software.
  3. Bounded drift. A live meeting has no post-hoc correction pass, so
     we cap the number of distinct speakers this tracker will invent
     (MAX_LIVE_SPEAKERS) — past the cap, new voices get folded into
     whichever existing centroid is closest rather than growing
     unboundedly.

Not in scope here: WHERE embeddings come from, or how utterance
boundaries are found. Those are the caller's job — in production, each
call to `assign()` corresponds to one VAD-detected utterance chunk (or
one fixed window, in the fallback path) that live_transcriber.py already
carved out for Whisper. Reusing those same boundaries means no extra
segmentation work is needed here.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)

# Cosine-similarity cutoff for "this is the same voice as an existing
# live centroid". Tuned conservatively (same order as
# speaker_profile_service.DEFAULT_MATCH_THRESHOLD's 0.75) — a live
# tracker has no user confirmation step to correct a wrong merge, so we
# would rather split one real speaker into two labels occasionally than
# merge two different speakers into one.
SIMILARITY_THRESHOLD = 0.75

# Utterances shorter than this are too short to embed reliably. Below
# this we return the previous speaker unchanged rather than embedding at
# all — see design goal #2 above.
MIN_UTTERANCE_SECONDS = 1.0

# Distinct live speakers this tracker will invent before it starts
# folding new voices into the nearest existing centroid instead of
# creating "Speaker 11", "Speaker 12", ... Bounds worst-case drift on a
# long, noisy call (echoes, brief crosstalk, a dog barking near someone's
# mic) from fragmenting into an ever-growing speaker list.
MAX_LIVE_SPEAKERS = 10


class LiveSpeakerTracker:
    """Assigns a running "Speaker N" (or known display name) label to
    each utterance of loopback audio, using an injected embedding
    function so the class has zero hard ML dependencies of its own.

    Thread-safety: NOT locked internally. live_transcriber.py only ever
    calls `assign()` from its single worker thread (see that module's
    docstring on why there is exactly one worker) — the same invariant
    that keeps faster-whisper calls serialized also keeps this class's
    mutable centroid state safe without its own lock.
    """

    def __init__(
        self,
        embed_fn: Callable[[np.ndarray, int], Optional[np.ndarray]],
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        min_utterance_seconds: float = MIN_UTTERANCE_SECONDS,
        max_speakers: int = MAX_LIVE_SPEAKERS,
        profile_lookup: Optional[
            Callable[[np.ndarray], Optional[Tuple[str, float]]]
        ] = None,
    ):
        # embed_fn(pcm, samplerate) -> L2-normalized embedding, or
        # None/raises on failure (both treated the same: no embedding
        # this round). Production wiring points this at
        # core.speaker_embeddings.embed_utterance; tests point it at a
        # fixed lookup table.
        self._embed_fn = embed_fn
        self._threshold = similarity_threshold
        self._min_utterance_seconds = min_utterance_seconds
        self._max_speakers = max(1, max_speakers)
        # Optional: profile_lookup(embedding) -> (display_name, similarity)
        # or None. Wraps SpeakerProfileService.find_match() so a known
        # voice gets its real name immediately instead of a generic
        # "Speaker N". Checked before the live centroid list on every
        # call that reaches embedding, so a known voice is recognized
        # even on the very first utterance of a meeting.
        self._profile_lookup = profile_lookup

        self._centroids: List[np.ndarray] = []
        self._labels: List[str] = []
        # Running-mean sample counts, parallel to _centroids/_labels.
        self._counts: List[int] = []
        self._last_label: Optional[str] = None

    @property
    def speaker_count(self) -> int:
        """Number of distinct live speakers currently tracked."""
        return len(self._centroids)

    def reset(self) -> None:
        """Clear all state. Called at the start of every new recording
        so "Speaker 1" from last week's meeting doesn't silently carry
        over — a fresh meeting should not inherit generic-labeled
        centroids from a completely different set of people. Known
        SpeakerProfile matches are unaffected since profile_lookup is
        re-consulted fresh on every call regardless of this reset."""
        self._centroids = []
        self._labels = []
        self._counts = []
        self._last_label = None

    def assign(self, pcm: np.ndarray, samplerate: int) -> str:
        """Return a speaker label for one utterance of loopback audio.

        Never raises — any embedding failure degrades to reusing the
        previous label (or a fresh generic one if there isn't a
        previous label yet), so a flaky embed call can never break the
        live transcript.
        """
        duration_s = (len(pcm) / float(samplerate)) if (
            pcm is not None and samplerate) else 0.0

        if duration_s < self._min_utterance_seconds:
            # Too short to trust an embedding on — never even call
            # embed_fn. Stick with whoever was talking a moment ago
            # rather than risk inventing a new speaker off a one-word
            # backchannel ("yeah", "mm-hmm"). If this is the very first
            # utterance of the meeting and it's already this short,
            # hand back a placeholder WITHOUT creating a centroid, so
            # the first genuinely embeddable utterance still gets to
            # create "Speaker 1" from a trustworthy sample instead of
            # this unreliable one.
            return self._last_label or "Speaker 1"

        embedding = self._safe_embed(pcm, samplerate)
        if embedding is None:
            # embed_fn failed or returned a degenerate/near-zero vector
            # — fall back to the previous label, or mint the very first
            # generic one if this is the first utterance of the meeting.
            return self._last_label or self._new_label()

        if self._profile_lookup is not None:
            try:
                match = self._profile_lookup(embedding)
            except Exception as e:
                logger.debug(f"Live speaker profile lookup failed: {e}")
                match = None
            if match is not None:
                name, _similarity = match
                self._last_label = name
                return name

        label = self._match_or_create(embedding)
        self._last_label = label
        return label

    # ── internals ────────────────────────────────────────────────────

    def _safe_embed(
        self, pcm: np.ndarray, samplerate: int,
    ) -> Optional[np.ndarray]:
        try:
            raw = self._embed_fn(pcm, samplerate)
        except Exception as e:
            logger.debug(f"Live speaker embed_fn raised: {e}")
            return None
        if raw is None:
            return None
        emb = np.asarray(raw, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(emb))
        if norm < 1e-8:
            return None
        return emb / norm

    def _new_label(self) -> str:
        label = f"Speaker {len(self._centroids) + 1}"
        return label

    def _match_or_create(self, embedding: np.ndarray) -> str:
        best_idx = -1
        best_sim = -1.0
        for i, centroid in enumerate(self._centroids):
            sim = float(np.dot(embedding, centroid))
            if sim > best_sim:
                best_idx, best_sim = i, sim

        if best_idx >= 0 and best_sim >= self._threshold:
            self._update_centroid(best_idx, embedding)
            return self._labels[best_idx]

        if len(self._centroids) >= self._max_speakers:
            # At the cap — fold into the closest existing voice instead
            # of growing the list further, even though it's below
            # threshold. Bounds drift on a long call at the cost of
            # occasionally merging two distinct-but-similar voices,
            # which is the intended tradeoff (see module docstring
            # design goal #3).
            if best_idx >= 0:
                self._update_centroid(best_idx, embedding)
                return self._labels[best_idx]
            # No centroids exist yet but max_speakers was configured to
            # 0-ish — degenerate config, fall back to a generic label
            # without tracking it (nothing to compare future turns to).
            return "Speaker 1"

        label = self._new_label()
        self._centroids.append(embedding)
        self._labels.append(label)
        self._counts.append(1)
        return label

    def _update_centroid(self, idx: int, embedding: np.ndarray) -> None:
        """Running-mean update of centroid `idx` with a new sample,
        re-normalized so cosine comparisons stay meaningful."""
        n = self._counts[idx]
        updated = (self._centroids[idx] * n + embedding) / (n + 1)
        norm = float(np.linalg.norm(updated))
        if norm > 1e-8:
            self._centroids[idx] = updated / norm
        self._counts[idx] = n + 1
