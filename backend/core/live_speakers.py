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
  2. Never invent a new speaker off a too-short or merely-ambiguous
     utterance. ECAPA-style embeddings are unreliable under ~1s of
     audio (see core/speaker_embeddings.py's own MIN_TOTAL_SECONDS),
     and a live tracker that creates "Speaker 4" every time someone
     says "yeah" is worse than useless — a wrong-but-stable label reads
     as normal conversation lag; a fresh label every few seconds reads
     as broken software. Field report 2026-08-11 is the case that
     proved it: one continuous speaker on a two-person call came out as
     SPEAKER 1/2/3/4/5/6/7/9. See the long "THE ASYMMETRY THIS MODULE
     NOW ENCODES" note above the threshold constants below — matching
     and CREATING are separate decisions with separate bars now.
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

# ── Thresholds ───────────────────────────────────────────────────────
#
# Field report 2026-08-11 (live speaker over-splitting), the reason
# every number below moved:
#
#   A real 2-person call on v2.23.0 produced SPEAKER 1, 2, 3, 4, 5, 6,
#   7 and 9 for ONE continuous speaker — a brand-new identity every few
#   seconds. Before v2.21.0 the same call was labelled a flat "them",
#   which was less informative but never WRONG. Trading a vague label
#   for a confidently incorrect one is a regression, not a trade-off.
#
#   Root cause: the old single SIMILARITY_THRESHOLD of 0.75 did double
#   duty. It decided both "is this the same voice?" AND, by failing,
#   "should a new speaker be born?". On the 0.6-8s VAD-sized clips this
#   tracker actually sees, ECAPA-style embeddings are noisy enough that
#   the SAME voice routinely scores under 0.75 — and every one of those
#   dips minted a new speaker. MIN_UTTERANCE_SECONDS only ever gated
#   whether we REUSED the previous label; it never gated creation.
#
# THE ASYMMETRY THIS MODULE NOW ENCODES — the design principle behind
# every constant here:
#
#   * Merging two people into one label is MILDLY wrong. The transcript
#     still reads as a conversation; a human skims it and moves on.
#   * Splitting one person into nine labels is BADLY wrong. It makes the
#     transcript unusable — no one can follow who said what.
#   * A confidently WRONG NAME ("Caleb Johnson" on a female speaker) is
#     the WORST of all. It doesn't just look broken, it actively
#     misleads, and it poisons trust in the whole transcript.
#
#   Therefore: bias hard toward MERGING and toward FEWER identities.
#   When in doubt, reuse an existing label. Never mint an identity, and
#   never attach a real name, off a marginal signal. The post-stop
#   pyannote pass sees the whole recording and remains the authority;
#   this live path is a preview and must be conservative.

# Cosine-similarity cutoff for "this is the same voice as an existing
# live centroid" — i.e. the MERGE bar.
#
# Deliberately LOW (0.55, was 0.75). ECAPA cosine on clean multi-second
# audio puts the same speaker at 0.75-0.95, but that range collapses on
# the short, noisy, single-sentence clips the live path works with —
# same-speaker scores of 0.55-0.70 are routine there, and different
# speakers still land around 0.20-0.50. 0.55 sits in the gap: it soaks
# up same-voice jitter (the over-splitting bug) while still sitting
# above where genuinely different voices score. Per the asymmetry
# above, an occasional over-merge is the cheap failure; over-splitting
# is the expensive one.
MATCH_THRESHOLD = 0.55

# The CREATE bar, and deliberately a SEPARATE, much stricter gate than
# the merge bar. A new speaker is only born when the clip is *clearly*
# nobody we've heard so far — best similarity to EVERY existing centroid
# below this. Between NEW_SPEAKER_THRESHOLD and MATCH_THRESHOLD is the
# "ambiguous" band: those clips are handed to the best-matching existing
# speaker rather than minting an identity on a coin flip. That band is
# exactly where the field report's phantom speakers 2-9 came from.
NEW_SPEAKER_THRESHOLD = 0.40

# Minimum clip length before a new identity may be created at all, and
# before ANY known-profile name may be attached. Both gates must pass;
# duration alone is never enough and similarity alone is never enough.
#
# 2.5s is well above MIN_UTTERANCE_SECONDS on purpose: 1s is the floor
# for "embedding is worth computing", but "confident enough to assert a
# NEW PERSON exists" (or to assert someone's actual NAME) needs a lot
# more evidence than that.
MIN_NEW_SPEAKER_SECONDS = 2.5

# Utterances shorter than this are too short to embed reliably. Below
# this we return the previous speaker unchanged rather than embedding at
# all — see design goal #2 above.
MIN_UTTERANCE_SECONDS = 1.0

# Stickiness margin. When several centroids score within this much of
# the best score, prefer whoever was ALREADY talking. Consecutive
# utterances from one person are the overwhelmingly common case in a
# real call; rapid alternation is not. Without this, a run of
# near-ties ping-pongs the label between two centroids mid-sentence,
# which reads as broken even when no new speakers are created.
STICKY_MARGIN = 0.05

# Cosine cutoff for attaching a KNOWN PROFILE'S REAL NAME in the live
# path. Much stricter than SpeakerProfileService.DEFAULT_MATCH_THRESHOLD
# (0.75), which governs the post-stop path where the pipeline has the
# whole recording to average over and the user gets a confirm/undo step.
#
# Field report 2026-08-11: at 0.75 the live transcript labelled a FEMALE
# speaker "CALEB JOHNSON" — a male colleague whose voiceprint happened
# to be saved. A wrong Speaker NUMBER is recoverable; a wrong NAME is
# actively misleading and undermines trust in everything else on screen.
# Below this bar we fall back to "Speaker N" and never guess a name.
PROFILE_NAME_THRESHOLD = 0.88

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
        match_threshold: float = MATCH_THRESHOLD,
        new_speaker_threshold: float = NEW_SPEAKER_THRESHOLD,
        min_utterance_seconds: float = MIN_UTTERANCE_SECONDS,
        min_new_speaker_seconds: float = MIN_NEW_SPEAKER_SECONDS,
        sticky_margin: float = STICKY_MARGIN,
        profile_name_threshold: float = PROFILE_NAME_THRESHOLD,
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
        # Every threshold is injectable so this is retunable from the
        # call site (and from a test) without editing the module — the
        # field report 2026-08-11 numbers are defaults, not gospel.
        self._match_threshold = match_threshold
        self._new_speaker_threshold = new_speaker_threshold
        self._min_utterance_seconds = min_utterance_seconds
        self._min_new_speaker_seconds = min_new_speaker_seconds
        self._sticky_margin = max(0.0, sticky_margin)
        self._profile_name_threshold = profile_name_threshold
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

        # ── Known-profile naming: two gates, both required ────────────
        #
        # Field report 2026-08-11: a female speaker was labelled "CALEB
        # JOHNSON" live. A wrong name is worse than a wrong number — it
        # reads as a confident assertion about a real person. So a name
        # is only attached when the clip is long enough to be worth
        # trusting AND the similarity clears a bar well above the
        # post-stop one. Anything short of that falls through to the
        # generic "Speaker N" path below rather than guessing.
        #
        # Post-stop diarization (core/diarization.py + the speaker
        # profile confirm flow) sees the WHOLE recording and remains the
        # authority for names; this live path is a preview that has one
        # noisy clip to go on, so it must be conservative.
        if (self._profile_lookup is not None
                and duration_s >= self._min_new_speaker_seconds):
            try:
                match = self._profile_lookup(embedding)
            except Exception as e:
                logger.debug(f"Live speaker profile lookup failed: {e}")
                match = None
            if match is not None:
                name, similarity = match
                try:
                    sim = float(similarity)
                except (TypeError, ValueError):
                    # A lookup that can't tell us how confident it is
                    # gets treated as not confident enough — see the
                    # asymmetry note at the top of this module.
                    sim = -1.0
                if sim >= self._profile_name_threshold:
                    self._last_label = name
                    return name
                logger.debug(
                    f"Live profile match '{name}' at {sim:.3f} is below the "
                    f"live naming bar ({self._profile_name_threshold}); "
                    f"falling back to a generic Speaker label.")

        label = self._match_or_create(embedding, duration_s)
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

    def _match_or_create(
        self, embedding: np.ndarray, duration_s: float,
    ) -> str:
        """Pick a label for one embedded utterance.

        Field report 2026-08-11: the decision order here is the whole
        fix. Creating a speaker is now a strictly harder thing to do
        than matching one — it needs its OWN threshold (much lower, i.e.
        "clearly nobody we know") AND its own duration floor. Every
        other outcome funnels into "assign the best existing match",
        because per the asymmetry at the top of this module, an
        over-merge is a survivable transcript and an over-split is not.
        """
        sims = [float(np.dot(embedding, c)) for c in self._centroids]

        best_idx = -1
        best_sim = -1.0
        for i, sim in enumerate(sims):
            if sim > best_sim:
                best_idx, best_sim = i, sim

        # Stickiness: among candidates within STICKY_MARGIN of the best
        # score, prefer whoever was already talking. Real calls are runs
        # of consecutive turns, not per-sentence alternation, so a
        # near-tie should resolve toward continuity rather than flipping
        # the label mid-thought. A clearly better different-speaker
        # match (outside the margin) still wins, so this can't pin the
        # transcript to one label forever.
        if self._last_label is not None and best_idx >= 0:
            for i, sim in enumerate(sims):
                if (self._labels[i] == self._last_label
                        and sim >= best_sim - self._sticky_margin):
                    best_idx, best_sim = i, sim
                    break

        if best_idx >= 0 and best_sim >= self._match_threshold:
            # Same voice as someone we're already tracking. The common,
            # boring, correct case — and now MUCH easier to reach than
            # it was at 0.75.
            self._update_centroid(best_idx, embedding)
            return self._labels[best_idx]

        at_cap = len(self._centroids) >= self._max_speakers
        # "Clearly different" means below the (much lower) create bar
        # against EVERY existing centroid. best_idx < 0 means there are
        # no centroids at all yet, which is trivially "clearly
        # different" — the first real speaker of the meeting.
        clearly_different = (best_idx < 0) or (
            best_sim < self._new_speaker_threshold)
        long_enough = duration_s >= self._min_new_speaker_seconds

        if not at_cap and clearly_different and long_enough:
            label = self._new_label()
            self._centroids.append(embedding)
            self._labels.append(label)
            self._counts.append(1)
            logger.debug(
                f"Live speaker created: {label} (best existing similarity "
                f"{best_sim:.3f} < {self._new_speaker_threshold}, "
                f"{duration_s:.1f}s clip)")
            return label

        if best_idx >= 0:
            # Everything that ISN'T a confident create folds into the
            # closest existing voice:
            #   * ambiguous similarity (between the two thresholds) —
            #     the exact band that produced speakers 2-9 in the field
            #     report;
            #   * a clip too short to justify a new identity;
            #   * at the max-speaker cap.
            #
            # Deliberately does NOT update the centroid. This sample was
            # good enough to LABEL from but not good enough to LEARN
            # from — folding it into the running mean would drag that
            # centroid toward a voice we're not confident belongs to it,
            # and a contaminated centroid then mis-sorts every later
            # utterance. Labels stay cheap to hand out; centroids stay
            # expensive to change.
            return self._labels[best_idx]

        # No centroids exist and we weren't allowed to create one (clip
        # too short, or a degenerate max_speakers<1 config). Hand back a
        # placeholder WITHOUT tracking it, so the first clip that IS
        # long enough still gets to create "Speaker 1" from a
        # trustworthy sample.
        return "Speaker 1"

    def _update_centroid(self, idx: int, embedding: np.ndarray) -> None:
        """Running-mean update of centroid `idx` with a new sample,
        re-normalized so cosine comparisons stay meaningful."""
        n = self._counts[idx]
        updated = (self._centroids[idx] * n + embedding) / (n + 1)
        norm = float(np.linalg.norm(updated))
        if norm > 1e-8:
            self._centroids[idx] = updated / norm
        self._counts[idx] = n + 1
