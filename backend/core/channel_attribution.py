"""
Channel-dominance speaker attribution — using WHICH DEVICE captured a
sound to decide who said it, instead of asking a clustering model to
re-derive that from voice alone.

WHY THIS EXISTS
---------------
The recorder captures two physically separate streams:

  * **mic**      — the user talking into their own input device.
                   Definitionally the user.
  * **loopback**  — system audio playback, i.e. everyone on the far end
                   of the call. Definitionally NOT the user.

``utils/audio_utils.finalize_recording_streaming`` stream-merges those
two into one mono WAV, and ``core/diarization.py`` then hands that
single mono track to PyAnnote, which clusters speakers purely on voice
characteristics. That merge throws away perfect, free, physical ground
truth about who-is-who and then pays a neural model to guess it back.

The field symptom is the user's transcript turns containing the far
end's words — "the user repeating what they're saying". That was long
blamed on acoustic echo. It isn't: a real session measured
``Echo cancellation: not applied (erle_non_positive)`` — ERLE at or
below zero means the adaptive filter found no echo path to cancel at
all (the user was on a ``CORSAIR ST100`` headset; there is no acoustic
path from their headphones back into their boom mic). With no echo to
blame, what's left is diarization mis-attribution: PyAnnote put some of
the far end's speech in the same cluster as the user's.

WHAT THIS MODULE DOES
---------------------
1. At FINALIZE time — the only moment both raw streams still exist on
   disk, before ``recording_service`` deletes ``_recording_<id>.wav`` /
   ``_loopback_<id>.wav`` — compare the two streams frame by frame and
   emit a timeline of spans labelled ``mic`` / ``loopback`` / ``both`` /
   ``silence``, each with a confidence.
2. Persist that timeline as a sidecar next to the session WAV
   (``session_<ID>.channel_attribution.json``), because diarization
   runs LATER, at Process time, in a different process, long after the
   raw streams are gone.
3. At diarization time, map those spans back onto PyAnnote's turns so
   that spans the mic confidently owns are assigned to the user with
   certainty, and PyAnnote is left with the (much easier, and much
   less damaging when wrong) job of separating far-end speakers from
   each other.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
* It does not touch the merged WAV. The mixed audio is byte-identical
  with this feature on or off — this is additive ANALYSIS, run as its
  own read pass over its own file handles. Losing attribution is
  nothing; changing the user's only copy of a recording is everything
  (same contract as ``utils/aec.py``).
* It does not run during live capture. Every frame of work here happens
  in the finalize subprocess, at below-normal priority, after the
  recording has stopped.
* It never raises out to its callers. Every public entry point either
  returns a "stood down" result or is wrapped by its caller so a
  failure degrades to today's pure-voice diarization.

THE IDENTITY OF "THE USER"
--------------------------
Deliberately NOT a new concept. ``OWNER_SPEAKER_LABEL`` is
``core.live_transcriber.SPEAKER_YOU`` — the exact same string the live
transcript already uses to mean "this came from the user's own mic",
imported rather than redefined so there is one string in this codebase
that means "the user". Turns carrying that label flow through the
normal pipeline: ``recording_service._fingerprint_speakers`` computes
an ECAPA centroid for them and looks it up in the existing
known-speakers store (``services/speaker_profile_service.py``), which
is what puts the user's REAL name on their turns. No second identity
store, no new field on ``Speaker``, no parallel owner concept beside
``services/owner_service.py``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from math import gcd
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.signal import resample_poly

# The user's identity as a speaker. Same string the live transcript
# already publishes for mic-sourced segments — see the module docstring.
from core.live_transcriber import SPEAKER_YOU
from utils.logger import get_logger

logger = get_logger(__name__)

OWNER_SPEAKER_LABEL = SPEAKER_YOU
# What the transcript UI shows for that speaker until the known-speakers
# fingerprint match replaces it with the user's real name. "you" reads
# like a bug in a transcript; "You" reads like the live preview badge
# the user already knows.
OWNER_SPEAKER_DISPLAY_NAME = "You"

# Label a far-end fragment falls back to in the (practically
# impossible, but structurally guarded) case that an incoming
# diarization turn already carries the owner label. See
# `constrain_turns_to_owner` — the invariant "far-end words can never
# be handed to the user" must hold no matter what the caller passes in.
FAR_END_FALLBACK_LABEL = "SPEAKER_FAR"

# Sidecar naming follows the convention already in the recordings dir:
# session_<ID>.embeddings.json, session_<ID>.item_status.json, …
SIDECAR_SUFFIX = ".channel_attribution.json"
SCHEMA_VERSION = 1

# ── Span labels ──────────────────────────────────────────────────────
LABEL_SILENCE = "silence"
LABEL_MIC = "mic"
LABEL_LOOPBACK = "loopback"
LABEL_BOTH = "both"

_CODE_SILENCE = 0
_CODE_MIC = 1
_CODE_LOOPBACK = 2
_CODE_BOTH = 3
_CODE_TO_LABEL = (LABEL_SILENCE, LABEL_MIC, LABEL_LOOPBACK, LABEL_BOTH)

# ── Frame geometry ───────────────────────────────────────────────────
#
# 32 ms, non-overlapping.
#
#   * Long enough that the frame's mean-square energy is a stable
#     estimate of speech level rather than tracking waveform fine
#     structure: a 32 ms window spans 3-6 pitch periods of an adult
#     voice (80-300 Hz), so voiced/unvoiced alternation inside a
#     syllable averages out instead of making the level flap.
#   * Short enough that a span boundary is localized to ±32 ms, which
#     is an order of magnitude finer than the ~200-500 ms precision
#     diarization turn boundaries actually carry — so frame size is
#     never the limiting factor on where a turn is cut.
#   * 512 samples at the 16 kHz target rate: a power of two, and
#     16000/512 = 31.25 frames per second exactly, so the frame→time
#     mapping is exact in binary floating point at every frame index.
#
# core/vad.py uses 20 ms for the same class of measurement; we sit at
# the long end of the 20-50 ms speech-frame range because this measures
# a RATIO BETWEEN two channels, and a longer window buys a lower-
# variance ratio at a boundary cost we do not care about.
FRAME_MS = 32.0

# ── Activity + dominance thresholds ──────────────────────────────────
#
# EVERYTHING here is RELATIVE to each channel's own noise floor. Mic
# gain varies wildly — a headset boom mic on a preamp and a laptop
# array 60 cm away can differ by 30+ dB on the same speech — and
# loopback level depends on the meeting app's output volume. Any
# absolute "mic energy > X" rule would classify a hot mic as dominant
# for the entire meeting and a quiet one as never dominant. Normalizing
# each channel by its OWN floor makes the comparison scale-free, which
# is the only way one rule can work across devices.
#
# Floors are the 20th-percentile frame level of each channel over the
# whole recording — the same cheap "what does quiet sound like here"
# estimator core/vad.py uses per-buffer, applied here over the whole
# session because we have it all at once.

# A channel counts as ACTIVE in a frame when it is this many dB above
# its own floor. 8 dB ≈ 6.3× the power of the channel's quiet level —
# comfortably above room tone / line noise wobble, comfortably below
# any real speech, which sits 15-30 dB over its floor.
ACTIVITY_MARGIN_DB = 8.0

# Absolute guard so a channel that is genuinely, entirely silent
# (all-zero loopback because nothing was rendering to the endpoint,
# dither-only mic on a muted device) can never have its own numerical
# noise promoted to "speech" by the relative rule. -60 dBFS RMS is far
# below any speech that survived capture.
ABS_SILENCE_DBFS = -60.0

# How much louder (in dB above its own floor) one channel must be than
# the other before we call the frame that channel's. 6 dB is a factor
# of 4 in power — a margin, not a coin flip. Inside ±6 dB neither
# channel is convincingly louder, and the frame is labelled `both`
# (crosstalk / genuine simultaneous speech / speaker bleed) and is
# never used to override diarization.
DOMINANCE_MARGIN_DB = 6.0

# Per-frame confidence saturates at twice the dominance margin: 0.0 at
# a dead tie, 0.5 at the margin itself, 1.0 at 12 dB of separation.
CONFIDENCE_SATURATION_DB = 2.0 * DOMINANCE_MARGIN_DB

# ── Smoothing / hysteresis ───────────────────────────────────────────
#
# Raw per-frame decisions flap: a plosive, a breath, a 32 ms gap
# between words. Two mechanisms, applied in order:
#   1. A 5-frame (160 ms) mode filter, with the frame's own label given
#      a half-vote tie-break so a run only flips when the neighbourhood
#      genuinely disagrees with it. This kills single-frame speckle.
#   2. A minimum dwell time: any run shorter than MIN_SPAN_MS after
#      smoothing is absorbed into its neighbour rather than emitted.
#      This is the hysteresis — a new state has to hold for 192 ms
#      before it exists as a span at all.
SMOOTHING_FRAMES = 5
MIN_SPAN_MS = 192.0

# ── Trust gates (see `evaluate_trust`) ───────────────────────────────
#
# The point of these is that this feature must degrade to EXACTLY
# today's behaviour whenever the channel signal isn't trustworthy,
# rather than confidently mislabelling a transcript. A wrong speaker
# label asserted with certainty is worse than no attribution at all.

# Minimum confidence a span needs before it may override diarization.
MIN_OVERRIDE_CONFIDENCE = 0.6

# Fraction of SPEECH frames that may be `both` before we stand down
# entirely. On a headset the far end physically cannot enter the mic,
# so `both` only happens during genuine simultaneous talking —
# interruptions and backchannels, well under ~15% of speech in a real
# meeting. On speakers, the far end enters the mic every time the far
# end talks, so `both` climbs toward the far end's whole talk time
# (40-60%+). 0.35 sits in the gap between those two regimes: above it,
# we are almost certainly looking at a non-headset recording where
# dominance is blurred and attribution is least reliable — which is
# also exactly the case where genuine echo exists.
MAX_OVERLAP_FRACTION = 0.35

# Mean confidence across mic-labelled spans, below which the whole
# timeline is treated as too muddy to act on.
MIN_MEAN_CONFIDENCE = 0.5

# ── The non-headset (speaker bleed) detector ─────────────────────────
#
# Loud bleed is the one situation where dominance can be confidently
# WRONG rather than merely uncertain. If the mic picks the speakers up
# louder (relative to ITS floor) than the loopback tap is relative to
# ITS floor, far-end speech is labelled `mic` at high confidence and we
# would hand the far end's words to the user — the exact failure this
# feature exists to prevent. The `both` fraction does not always catch
# it: strong bleed doesn't blur dominance, it inverts it.
#
# So it is detected with TWO measurements that must BOTH fire, because
# either alone has a benign explanation:
#
#   bleed_correlation — Pearson correlation of the two channels' frame
#     energy envelopes (in dB), over frames where the far end is
#     actually playing. This is the direct physical test for "is the
#     user on a headset?": on a headset the mic cannot hear the far end
#     at all, so while the far end talks the mic sits flat at its noise
#     floor and carries no trace of the loopback envelope (~0). On
#     speakers every syllable arrives at the mic scaled by the room, so
#     the mic envelope becomes a copy of the loopback's (→1). Being a
#     log-domain envelope correlation, it is immune to the gain
#     difference between the two devices — the very thing that makes
#     raw level comparison useless here. Benign alone: quiet bleed at
#     -25 dB correlates almost perfectly and changes NOTHING, because
#     the far end is still far louder on the loopback.
#
#   contested_mic_fraction — the share of frames we would attribute to
#     the USER that happened while the far end was also active. This is
#     the measurement of actual damage: if bleed is what's producing
#     mic-dominant frames, virtually all of them land on top of far-end
#     speech. Benign alone: genuine double-talk (the user interrupting
#     on a headset) also lands there, but is uncorrelated with the
#     loopback envelope, so the first measurement stays near zero.
#
# Together they mean "the mic demonstrably hears the far end, AND that
# is where the user's supposed speech is coming from" — which is a
# speakerphone, and is where we stand down.
MAX_BLEED_CORRELATION = 0.5
MAX_CONTESTED_MIC_FRACTION = 0.25

# Far-end speech needed before the bleed correlation means anything.
# 32 frames ≈ 1 s at the default frame size.
MIN_BLEED_FRAMES = 32

# A recording with almost no detected speech gives the percentile-based
# floors nothing to work with. Below this much total speech we have no
# opinion.
MIN_SPEECH_SECONDS = 3.0

# Diarization fragments shorter than this are absorbed into their
# neighbour instead of being emitted. Prevents a turn from being
# shredded into sub-word confetti at span boundaries.
MIN_FRAGMENT_S = 0.20

_EPS = 1e-20


# ══ Frame analysis ═══════════════════════════════════════════════════


def _frame_mean_square(pcm: np.ndarray, frame_len: int) -> np.ndarray:
    """Per-frame mean square of a mono signal, zero-padding the tail."""
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n = len(pcm)
    if n == 0 or frame_len <= 0:
        return np.zeros(0, dtype=np.float64)
    n_frames = int(np.ceil(n / frame_len))
    pad = n_frames * frame_len - n
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.float32)])
    frames = pcm.reshape(n_frames, frame_len)
    return np.mean(np.square(frames, dtype=np.float64), axis=1)


class _FrameEnergyAccumulator:
    """Streaming counterpart to `_frame_mean_square`.

    Fed arbitrary-length blocks, emits one mean-square value per whole
    frame and carries the remainder across block boundaries. Exists so
    the finalize pass stays bounded-memory (the same reason the merge
    itself streams): a 3-hour recording produces ~340k float64 frame
    values (~2.7 MB) instead of a 170 MB resampled mic array.
    """

    def __init__(self, frame_len: int):
        self._frame_len = max(1, int(frame_len))
        self._carry = np.zeros(0, dtype=np.float32)
        self._out: List[np.ndarray] = []

    def push(self, block: np.ndarray) -> None:
        if block is None or len(block) == 0:
            return
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if len(self._carry):
            block = np.concatenate([self._carry, block])
        n_full = len(block) // self._frame_len
        if n_full:
            usable = block[: n_full * self._frame_len]
            self._out.append(
                np.mean(
                    np.square(
                        usable.reshape(n_full, self._frame_len),
                        dtype=np.float64,
                    ),
                    axis=1,
                )
            )
        self._carry = block[n_full * self._frame_len:].copy()

    def finish(self) -> np.ndarray:
        if len(self._carry):
            tail = np.zeros(self._frame_len, dtype=np.float32)
            tail[: len(self._carry)] = self._carry
            self._out.append(
                np.mean(np.square(tail, dtype=np.float64))[np.newaxis]
            )
            self._carry = np.zeros(0, dtype=np.float32)
        if not self._out:
            return np.zeros(0, dtype=np.float64)
        return np.concatenate(self._out)


def _to_db(mean_square: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(np.asarray(mean_square, dtype=np.float64) + _EPS)


def _active_mask(db: np.ndarray) -> np.ndarray:
    """Which frames of one channel carry real signal.

    Relative rule (own 20th-percentile floor + ACTIVITY_MARGIN_DB) with
    an absolute silence guard. The fallback mirrors core/vad.py: if the
    relative rule finds nothing but the channel is clearly not silent,
    the channel had no quiet frames to anchor a floor on (a continuous
    talker over the whole analysis window), so classify on the absolute
    guard alone rather than declaring an obviously loud channel silent.
    """
    if len(db) == 0:
        return np.zeros(0, dtype=bool)
    floor = float(np.percentile(db, 20))
    active = (db > floor + ACTIVITY_MARGIN_DB) & (db > ABS_SILENCE_DBFS)
    if not active.any() and float(np.max(db)) > ABS_SILENCE_DBFS:
        active = db > ABS_SILENCE_DBFS
    return active


def _channel_floor(db: np.ndarray) -> float:
    if len(db) == 0:
        return ABS_SILENCE_DBFS
    return float(np.percentile(db, 20))


def classify_frames(
    mic_mean_square: np.ndarray,
    loopback_mean_square: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Classify each frame as mic / loopback / both / silence.

    Both inputs are per-frame mean-square arrays on the SAME timeline
    and of the same length (the caller zero-pads loopback into the
    output timeline at its known offset).

    Returns ``(codes, confidence, dominance_db)``:
      * ``codes``        — int array of _CODE_* values.
      * ``confidence``   — 0..1 per frame. For mic/loopback frames it
        rises linearly with |dominance| and saturates at
        CONFIDENCE_SATURATION_DB. `both` frames are low by construction
        (|dominance| < DOMINANCE_MARGIN_DB ⇒ confidence < 0.5); silence
        frames are 1.0 (we are confident it is silence — but silence is
        never used to override anything).
      * ``dominance_db`` — mic level above its own floor minus loopback
        level above its own floor. Positive = mic-dominant.
    """
    mic_db = _to_db(mic_mean_square)
    lb_db = _to_db(loopback_mean_square)
    n = min(len(mic_db), len(lb_db))
    mic_db, lb_db = mic_db[:n], lb_db[:n]
    if n == 0:
        z = np.zeros(0, dtype=np.float64)
        return np.zeros(0, dtype=np.int8), z, z

    mic_active = _active_mask(mic_db)
    lb_active = _active_mask(lb_db)

    dominance = (
        (mic_db - _channel_floor(mic_db)) - (lb_db - _channel_floor(lb_db))
    )

    codes = np.full(n, _CODE_SILENCE, dtype=np.int8)
    only_mic = mic_active & ~lb_active
    only_lb = lb_active & ~mic_active
    contested = mic_active & lb_active

    codes[only_mic] = _CODE_MIC
    codes[only_lb] = _CODE_LOOPBACK
    codes[contested & (dominance >= DOMINANCE_MARGIN_DB)] = _CODE_MIC
    codes[contested & (dominance <= -DOMINANCE_MARGIN_DB)] = _CODE_LOOPBACK
    codes[contested & (np.abs(dominance) < DOMINANCE_MARGIN_DB)] = _CODE_BOTH

    confidence = np.clip(
        np.abs(dominance) / CONFIDENCE_SATURATION_DB, 0.0, 1.0)
    confidence[codes == _CODE_SILENCE] = 1.0
    return codes, confidence, dominance


def bleed_correlation(
    mic_mean_square: np.ndarray,
    loopback_mean_square: np.ndarray,
) -> float:
    """How much of the loopback's energy envelope shows up in the mic's,
    measured only while the far end is actually playing.

    ~0 for a headset (the mic never hears the far end), → 1 for
    speakers. See MAX_BLEED_CORRELATION for why this is the test that
    decides whether dominance can be trusted at all.

    Returns 0.0 — the benign value — when there isn't enough far-end
    speech, or either channel is flat, to measure anything. Those cases
    are caught by the other trust gates instead of being guessed at
    here.
    """
    mic_db = _to_db(mic_mean_square)
    lb_db = _to_db(loopback_mean_square)
    n = min(len(mic_db), len(lb_db))
    if n == 0:
        return 0.0
    mic_db, lb_db = mic_db[:n], lb_db[:n]
    lb_active = _active_mask(lb_db)
    if int(lb_active.sum()) < MIN_BLEED_FRAMES:
        return 0.0
    a = mic_db[lb_active]
    b = lb_db[lb_active]
    if float(np.std(a)) < 1e-6 or float(np.std(b)) < 1e-6:
        return 0.0
    with np.errstate(invalid="ignore"):
        r = float(np.corrcoef(a, b)[0, 1])
    return 0.0 if not np.isfinite(r) else r


def contested_mic_fraction(
    codes: np.ndarray, loopback_mean_square: np.ndarray,
) -> float:
    """Share of mic-attributed frames that happened while the far end
    was also active — the "how much damage would bleed be doing"
    half of the non-headset detector. See MAX_CONTESTED_MIC_FRACTION."""
    codes = np.asarray(codes)
    lb_active = _active_mask(_to_db(loopback_mean_square))
    n = min(len(codes), len(lb_active))
    if n == 0:
        return 0.0
    mic_frames = codes[:n] == _CODE_MIC
    total = int(mic_frames.sum())
    if total == 0:
        return 0.0
    return float(int((mic_frames & lb_active[:n]).sum()) / total)


def _mode_filter(codes: np.ndarray, width: int) -> np.ndarray:
    """Majority filter over a small odd window, with the frame's own
    label holding a half-vote tie-break so a stable run only flips when
    its neighbourhood genuinely outvotes it."""
    n = len(codes)
    if n == 0 or width <= 1:
        return codes
    width = width if width % 2 == 1 else width + 1
    kernel = np.ones(width, dtype=np.float64)
    scores = np.zeros((len(_CODE_TO_LABEL), n), dtype=np.float64)
    for c in range(len(_CODE_TO_LABEL)):
        member = (codes == c).astype(np.float64)
        scores[c] = np.convolve(member, kernel, mode="same") + 0.5 * member
    return np.argmax(scores, axis=0).astype(np.int8)


def _runs(codes: np.ndarray) -> List[List[int]]:
    """Run-length encode into [start_frame, end_frame_exclusive, code]."""
    out: List[List[int]] = []
    if len(codes) == 0:
        return out
    start = 0
    for i in range(1, len(codes)):
        if codes[i] != codes[start]:
            out.append([start, i, int(codes[start])])
            start = i
    out.append([start, len(codes), int(codes[start])])
    return out


def _absorb_short_runs(
    runs: List[List[int]], min_frames: int,
) -> List[List[int]]:
    """Minimum-dwell hysteresis: a run shorter than `min_frames` never
    becomes a span — it is absorbed into whichever neighbour is longer
    (previous on a tie, so behaviour is deterministic)."""
    if min_frames <= 1 or len(runs) <= 1:
        return runs
    work = [list(r) for r in runs]
    changed = True
    while changed and len(work) > 1:
        changed = False
        for i, (s, e, _c) in enumerate(work):
            if e - s >= min_frames:
                continue
            prev_len = (work[i - 1][1] - work[i - 1][0]) if i > 0 else -1
            next_len = (
                work[i + 1][1] - work[i + 1][0]
                if i + 1 < len(work) else -1
            )
            if prev_len < 0 and next_len < 0:
                break
            if prev_len >= next_len:
                work[i - 1][1] = e
            else:
                work[i + 1][0] = s
            work.pop(i)
            changed = True
            break
    # Coalesce neighbours that ended up with the same label.
    merged: List[List[int]] = []
    for r in work:
        if merged and merged[-1][2] == r[2]:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    return merged


def build_spans(
    codes: np.ndarray,
    confidence: np.ndarray,
    frame_seconds: float,
    smoothing_frames: int = SMOOTHING_FRAMES,
    min_span_ms: float = MIN_SPAN_MS,
) -> List[dict]:
    """Smooth per-frame labels and emit merged spans with a per-span
    confidence (the mean of its frames' confidences)."""
    if len(codes) == 0:
        return []
    smoothed = _mode_filter(np.asarray(codes), smoothing_frames)
    min_frames = max(1, int(round(min_span_ms / 1000.0 / frame_seconds)))
    runs = _absorb_short_runs(_runs(smoothed), min_frames)
    spans: List[dict] = []
    for start, end, code in runs:
        conf = float(np.mean(confidence[start:end])) if end > start else 0.0
        spans.append({
            "start": round(start * frame_seconds, 4),
            "end": round(end * frame_seconds, 4),
            "label": _CODE_TO_LABEL[code],
            "confidence": round(conf, 4),
        })
    return spans


# ══ Document assembly ════════════════════════════════════════════════


def _summarize(spans: Sequence[dict]) -> dict:
    per_label: Dict[str, float] = {
        LABEL_MIC: 0.0, LABEL_LOOPBACK: 0.0,
        LABEL_BOTH: 0.0, LABEL_SILENCE: 0.0,
    }
    conf_weighted: Dict[str, float] = {LABEL_MIC: 0.0, LABEL_LOOPBACK: 0.0}
    for s in spans:
        dur = max(0.0, float(s["end"]) - float(s["start"]))
        label = s["label"]
        per_label[label] = per_label.get(label, 0.0) + dur
        if label in conf_weighted:
            conf_weighted[label] += dur * float(s.get("confidence") or 0.0)

    speech = (per_label[LABEL_MIC] + per_label[LABEL_LOOPBACK]
              + per_label[LABEL_BOTH])
    total = speech + per_label[LABEL_SILENCE]

    def _frac(x: float, base: float) -> float:
        return round(x / base, 4) if base > 0 else 0.0

    mean_mic_conf = (
        conf_weighted[LABEL_MIC] / per_label[LABEL_MIC]
        if per_label[LABEL_MIC] > 0 else 0.0
    )
    mean_lb_conf = (
        conf_weighted[LABEL_LOOPBACK] / per_label[LABEL_LOOPBACK]
        if per_label[LABEL_LOOPBACK] > 0 else 0.0
    )
    # One number a consumer can read as "how clean was this recording":
    # the duration-weighted mean confidence over all attributed (non-
    # silence) time, with `both` time counted at its own (low) worth.
    attributed_conf = conf_weighted[LABEL_MIC] + conf_weighted[LABEL_LOOPBACK]
    overall = attributed_conf / speech if speech > 0 else 0.0

    return {
        "mic_seconds": round(per_label[LABEL_MIC], 3),
        "loopback_seconds": round(per_label[LABEL_LOOPBACK], 3),
        "both_seconds": round(per_label[LABEL_BOTH], 3),
        "silence_seconds": round(per_label[LABEL_SILENCE], 3),
        "speech_seconds": round(speech, 3),
        "mic_fraction": _frac(per_label[LABEL_MIC], total),
        "loopback_fraction": _frac(per_label[LABEL_LOOPBACK], total),
        "overlap_fraction": _frac(per_label[LABEL_BOTH], speech),
        "silence_fraction": _frac(per_label[LABEL_SILENCE], total),
        "mean_mic_confidence": round(mean_mic_conf, 4),
        "mean_loopback_confidence": round(mean_lb_conf, 4),
        "overall_confidence": round(overall, 4),
        "span_count": len(spans),
    }


def build_document(
    spans: Sequence[dict],
    *,
    samplerate: int,
    frame_ms: float,
    duration_s: float,
    loopback_present: bool,
    conference_room_mode: bool = False,
    alignment: str = "none",
    loopback_offset_s: Optional[float] = None,
    aec_applied: bool = False,
    session_id: Optional[str] = None,
    diagnostics: Optional[dict] = None,
) -> dict:
    """Assemble the sidecar document, including the trust verdict."""
    doc = {
        "version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(),
        "session_id": session_id,
        "sample_rate": int(samplerate),
        "frame_ms": float(frame_ms),
        "duration_s": round(float(duration_s), 3),
        "loopback_present": bool(loopback_present),
        "conference_room_mode": bool(conference_room_mode),
        # How mic and loopback were put on a common timeline before they
        # were compared. Only "wallclock" (the cross-correlation /
        # capture-anchor offset finalize already computes) is trusted —
        # see `evaluate_trust`.
        "alignment": alignment,
        "loopback_offset_s": (
            None if loopback_offset_s is None else round(
                float(loopback_offset_s), 4)
        ),
        # Whether the mic signal analysed here had already been through
        # offline AEC. Recorded so a future reader can tell an
        # AEC-cleaned dominance measurement from a raw one.
        "aec_applied": bool(aec_applied),
        "summary": _summarize(spans),
        "spans": list(spans),
    }
    diag = diagnostics or {}
    doc["summary"]["bleed_correlation"] = round(
        float(diag.get("bleed_correlation") or 0.0), 4)
    doc["summary"]["contested_mic_fraction"] = round(
        float(diag.get("contested_mic_fraction") or 0.0), 4)
    usable, reason = evaluate_trust(doc)
    doc["summary"]["usable"] = usable
    doc["summary"]["stand_down_reason"] = reason
    return doc


def stood_down_document(
    reason: str,
    *,
    samplerate: int = 16000,
    duration_s: float = 0.0,
    loopback_present: bool = False,
    conference_room_mode: bool = False,
    session_id: Optional[str] = None,
) -> dict:
    """A sidecar that records WHY no attribution was computed.

    Writing this is deliberate rather than writing nothing: "no sidecar"
    and "a sidecar that says conference-room mode" are different facts,
    and this codebase has been bitten before by an unreadable outcome
    rendering as a clean one (see `Session.aec_outcome`'s docstring on
    the "no decision came back" case). Consumers treat it exactly like
    a missing sidecar — `evaluate_trust` returns False — but a field
    pull can now tell the two apart.
    """
    doc = build_document(
        [],
        samplerate=samplerate,
        frame_ms=FRAME_MS,
        duration_s=duration_s,
        loopback_present=loopback_present,
        conference_room_mode=conference_room_mode,
        alignment="none",
        session_id=session_id,
    )
    doc["summary"]["usable"] = False
    doc["summary"]["stand_down_reason"] = reason
    return doc


def evaluate_trust(doc: Optional[dict]) -> Tuple[bool, Optional[str]]:
    """Decide whether a channel-attribution document may override
    diarization. Returns ``(usable, stand_down_reason)``.

    Re-evaluated at CONSUME time as well as at write time, so a sidecar
    written by an older build (or by a build with different thresholds)
    is judged by the rules currently in force rather than by a boolean
    frozen into the file.

    Ordered from "there is no signal at all" to "there is a signal but
    it isn't clean enough":

      no_sidecar            — nothing on disk. Every session recorded
                              before this feature shipped. Behaves
                              exactly as before: pure-voice diarization.
      unsupported_version   — a future schema we can't read.
      conference_room_mode  — the mic is capturing the whole ROOM and
                              system audio was never captured. "Mic
                              means the user" is simply false here.
      mic_only_recording    — no loopback track at all (real field case:
                              a session logged `lb=n/a`). There is no
                              second channel to compare against.
      no_wallclock_alignment— the two streams were only right-aligned by
                              length (the legacy heuristic used by the
                              recovery path). Comparing streams that may
                              be seconds apart produces garbage at
                              exactly the boundaries that matter.
      insufficient_speech   — too little detected speech for the
                              percentile floors to mean anything.
      overlap_dominant      — too much of the speech is `both`. This is
                              the speaker/speakerphone case: far-end
                              audio bleeds into the mic, dominance
                              blurs, and attribution is least reliable
                              precisely where genuine echo lives.
      mic_hears_far_end     — the mic's energy envelope tracks the
                              loopback's while the far end is playing,
                              i.e. the user is on SPEAKERS, not a
                              headset. The stronger form of the case
                              above: loud bleed doesn't blur dominance,
                              it INVERTS it, so far-end speech could be
                              labelled `mic` with high confidence. See
                              MAX_BLEED_CORRELATION.
      low_confidence        — mic spans exist but are too marginal.
      no_confident_user_spans — nothing clears MIN_OVERRIDE_CONFIDENCE,
                              so there is nothing to override with.
    """
    if not doc or not isinstance(doc, dict):
        return False, "no_sidecar"
    try:
        return _evaluate_trust(doc)
    except Exception as e:
        # A sidecar we can't even parse the fields of is exactly as
        # useless as no sidecar, and must be exactly as harmless.
        logger.warning(
            f"channel-attribution sidecar is malformed ({e}); falling "
            f"back to voice-only diarization")
        return False, "malformed_sidecar"


def _evaluate_trust(doc: dict) -> Tuple[bool, Optional[str]]:
    if int(doc.get("version") or 0) > SCHEMA_VERSION:
        return False, "unsupported_version"
    if doc.get("conference_room_mode"):
        return False, "conference_room_mode"
    if not doc.get("loopback_present"):
        return False, "mic_only_recording"
    if doc.get("alignment") != "wallclock":
        return False, "no_wallclock_alignment"

    summary = doc.get("summary") or {}
    if float(summary.get("speech_seconds") or 0.0) < MIN_SPEECH_SECONDS:
        return False, "insufficient_speech"
    if float(summary.get("overlap_fraction") or 0.0) > MAX_OVERLAP_FRACTION:
        return False, "overlap_dominant"
    if (float(summary.get("bleed_correlation") or 0.0) > MAX_BLEED_CORRELATION
            and float(summary.get("contested_mic_fraction") or 0.0)
            > MAX_CONTESTED_MIC_FRACTION):
        return False, "mic_hears_far_end"
    if float(summary.get("mean_mic_confidence") or 0.0) < MIN_MEAN_CONFIDENCE:
        return False, "low_confidence"
    if not confident_intervals(doc, LABEL_MIC):
        return False, "no_confident_user_spans"
    return True, None


def confident_intervals(
    doc: Optional[dict],
    label: str,
    min_confidence: float = MIN_OVERRIDE_CONFIDENCE,
) -> List[Tuple[float, float]]:
    """Merged (start, end) intervals for spans of `label` that clear
    `min_confidence`. Adjacent/overlapping intervals are coalesced so
    callers can walk them linearly."""
    out: List[List[float]] = []
    for span in (doc or {}).get("spans") or []:
        try:
            if span.get("label") != label:
                continue
            if float(span.get("confidence") or 0.0) < min_confidence:
                continue
            start = float(span["start"])
            end = float(span["end"])
        except (TypeError, ValueError, KeyError):
            continue
        if end <= start:
            continue
        if out and start <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


# ══ In-memory + streaming computation ════════════════════════════════


def compute_attribution(
    mic_pcm: np.ndarray,
    loopback_pcm: Optional[np.ndarray],
    samplerate: int,
    *,
    loopback_offset_s: Optional[float] = None,
    conference_room_mode: bool = False,
    aec_applied: bool = False,
    session_id: Optional[str] = None,
    frame_ms: float = FRAME_MS,
) -> dict:
    """Compute a full attribution document from two in-memory streams.

    `mic_pcm` defines the timeline (the merge writes exactly mic length).
    `loopback_pcm` is placed at `loopback_offset_s` into that timeline
    and zero-padded to fit — the SAME positioning the merge uses, so the
    comparison happens on aligned audio. Passing `loopback_offset_s=None`
    marks the alignment untrusted (see `evaluate_trust`).

    This is the reference implementation and the one the tests drive;
    `compute_attribution_from_files` is the bounded-memory equivalent
    used by the finalize path.
    """
    frame_len = max(1, int(round(samplerate * frame_ms / 1000.0)))
    mic_ms = _frame_mean_square(mic_pcm, frame_len)
    n_frames = len(mic_ms)
    duration_s = len(np.asarray(mic_pcm).reshape(-1)) / float(samplerate or 1)

    have_lb = loopback_pcm is not None and len(np.asarray(
        loopback_pcm).reshape(-1)) > 0
    if not have_lb:
        return stood_down_document(
            "mic_only_recording",
            samplerate=samplerate,
            duration_s=duration_s,
            loopback_present=False,
            conference_room_mode=conference_room_mode,
            session_id=session_id,
        )

    lb_ms_raw = _frame_mean_square(loopback_pcm, frame_len)
    offset_frames = int(round(
        (loopback_offset_s or 0.0) * samplerate / float(frame_len)))
    lb_ms = _place_on_timeline(lb_ms_raw, offset_frames, n_frames)

    codes, confidence, _dom = classify_frames(mic_ms, lb_ms)
    spans = build_spans(codes, confidence, frame_len / float(samplerate))
    return build_document(
        spans,
        samplerate=samplerate,
        frame_ms=frame_ms,
        duration_s=duration_s,
        loopback_present=True,
        conference_room_mode=conference_room_mode,
        alignment="wallclock" if loopback_offset_s is not None else "none",
        loopback_offset_s=loopback_offset_s,
        aec_applied=aec_applied,
        session_id=session_id,
        diagnostics={
            "bleed_correlation": bleed_correlation(mic_ms, lb_ms),
            "contested_mic_fraction": contested_mic_fraction(codes, lb_ms),
        },
    )


def _place_on_timeline(
    frames: np.ndarray, offset_frames: int, total_frames: int,
) -> np.ndarray:
    """Position a channel's per-frame values at `offset_frames` in a
    zero-filled timeline `total_frames` long.

    The offset is rounded to whole frames — a sub-frame (<32 ms)
    positioning error, an order of magnitude below the boundary
    precision anything downstream carries.
    """
    out = np.zeros(max(0, total_frames), dtype=np.float64)
    if total_frames <= 0 or len(frames) == 0:
        return out
    start = max(0, int(offset_frames))
    if start >= total_frames:
        return out
    n = min(len(frames), total_frames - start)
    out[start:start + n] = frames[:n]
    return out


def _resample_mono(block: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr:
        return np.asarray(block, dtype=np.float32)
    divisor = gcd(target_sr, orig_sr)
    up = target_sr // divisor
    down = orig_sr // divisor
    return resample_poly(
        np.asarray(block, dtype=np.float64), up, down,
    ).astype(np.float32)


def compute_attribution_from_files(
    mic_wav_path: str,
    mic_samplerate: int,
    loopback_16k_path: Optional[str],
    target_sr: int,
    loopback_offset_frames: Optional[int],
    *,
    conference_room_mode: bool = False,
    aec_applied: bool = False,
    session_id: Optional[str] = None,
    frame_ms: float = FRAME_MS,
    block_seconds: float = 10.0,
) -> dict:
    """Bounded-memory attribution over the two files finalize already
    has open, WITHOUT touching them.

    This is a second, independent read pass with its own file handles —
    deliberately not folded into the merge loop. The merged WAV must
    stay byte-identical whether this feature is on or off, and the
    cheapest way to guarantee that is for this code to never share a
    reader, a buffer, or a branch with the code that writes it. The
    cost is one extra streaming read of each input at below-normal
    priority in the finalize subprocess, after the recording has already
    stopped.

    `loopback_16k_path` must already be at `target_sr` (finalize's
    pass-1 resample output) and `loopback_offset_frames` is the same
    offset the mixing pass uses. `None` for either means there is no
    usable loopback and a stood-down document is returned.
    """
    import soundfile as sf

    frame_len = max(1, int(round(target_sr * frame_ms / 1000.0)))

    mic_acc = _FrameEnergyAccumulator(frame_len)
    mic_frames_in = 0
    with sf.SoundFile(str(mic_wav_path), mode="r") as reader:
        block_frames = max(int(mic_samplerate * block_seconds), 1024)
        while True:
            block = reader.read(block_frames, dtype="float32", always_2d=False)
            if block is None or len(block) == 0:
                break
            if block.ndim == 2:
                block = block.mean(axis=1)
            mic_frames_in += len(block)
            mic_acc.push(_resample_mono(block, mic_samplerate, target_sr))
    mic_ms = mic_acc.finish()
    duration_s = mic_frames_in / float(mic_samplerate or 1)

    if not loopback_16k_path or loopback_offset_frames is None:
        return stood_down_document(
            "mic_only_recording",
            samplerate=target_sr,
            duration_s=duration_s,
            loopback_present=False,
            conference_room_mode=conference_room_mode,
            session_id=session_id,
        )

    lb_acc = _FrameEnergyAccumulator(frame_len)
    with sf.SoundFile(str(loopback_16k_path), mode="r") as reader:
        block_frames = max(int(target_sr * block_seconds), 1024)
        while True:
            block = reader.read(block_frames, dtype="float32", always_2d=False)
            if block is None or len(block) == 0:
                break
            if block.ndim == 2:
                block = block.mean(axis=1)
            lb_acc.push(block)
    lb_ms = _place_on_timeline(
        lb_acc.finish(),
        int(round(loopback_offset_frames / float(frame_len))),
        len(mic_ms),
    )

    codes, confidence, _dom = classify_frames(mic_ms, lb_ms)
    spans = build_spans(codes, confidence, frame_len / float(target_sr))
    return build_document(
        spans,
        samplerate=target_sr,
        frame_ms=frame_ms,
        duration_s=duration_s,
        loopback_present=True,
        conference_room_mode=conference_room_mode,
        alignment="wallclock",
        loopback_offset_s=loopback_offset_frames / float(target_sr),
        aec_applied=aec_applied,
        session_id=session_id,
        diagnostics={
            "bleed_correlation": bleed_correlation(mic_ms, lb_ms),
            "contested_mic_fraction": contested_mic_fraction(codes, lb_ms),
        },
    )


# ══ Sidecar persistence ══════════════════════════════════════════════


def sidecar_path_for_audio(audio_path: str) -> Path:
    """`…/session_AB12CD34.wav` → `…/session_AB12CD34.channel_attribution.json`."""
    p = Path(audio_path)
    return p.with_name(p.stem + SIDECAR_SUFFIX)


def sidecar_path(recordings_dir, session_id: str) -> Path:
    return Path(recordings_dir) / f"session_{session_id}{SIDECAR_SUFFIX}"


def write_sidecar(path, doc: dict) -> bool:
    """Atomically write the sidecar (temp in the same dir + os.replace,
    the same pattern OwnerAliasStore / SpeakerProfileService use, so a
    crash mid-write never leaves half a JSON behind).

    Returns True on success. Never raises: a sidecar that didn't get
    written costs the attribution feature for that session and nothing
    else."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        logger.warning(
            f"Could not write channel-attribution sidecar {target.name}: {e} "
            f"— diarization for this session will fall back to voice-only "
            f"clustering")
        return False


def load_sidecar(path) -> Optional[dict]:
    """Read a sidecar. Returns None when it is missing, unreadable, or
    malformed — every one of which must degrade to today's pure-voice
    diarization rather than raise."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(
            f"channel-attribution sidecar {p.name} unreadable ({e}); "
            f"falling back to voice-only diarization")
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def load_sidecar_for_audio(audio_path: Optional[str]) -> Optional[dict]:
    if not audio_path:
        return None
    try:
        return load_sidecar(sidecar_path_for_audio(audio_path))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"channel-attribution sidecar lookup failed: {e}")
        return None


# ══ Applying the timeline to diarization turns ═══════════════════════


def constrain_turns_to_owner(
    turns: Sequence[dict],
    doc: Optional[dict],
    *,
    owner_label: str = OWNER_SPEAKER_LABEL,
    min_confidence: float = MIN_OVERRIDE_CONFIDENCE,
    min_fragment_s: float = MIN_FRAGMENT_S,
) -> Tuple[List[dict], dict]:
    """Rewrite diarization turns so the user's speech is decided by
    WHICH DEVICE CAPTURED IT, not by voice similarity.

    Mapping rule, in full:

      1. Take every `mic` span whose confidence clears
         `min_confidence`, coalesced into intervals. These are the only
         places attribution overrides diarization. `loopback` spans are
         NOT used to force a label — the far end may contain several
         people and separating them is exactly what PyAnnote is good
         at; we only need to stop it lending them the user's identity.
         `both` spans (crosstalk, overlap, speaker bleed) are left
         untouched on purpose: that is where the channel evidence is
         weakest, so PyAnnote's answer stands.
      2. CUT each diarization turn at those interval boundaries rather
         than voting the whole turn one way. A single PyAnnote turn
         routinely straddles a handover; forcing it whole would either
         hand the far end's tail to the user or throw away the user's
         head. Fragments inside a confident mic interval become
         `owner_label`; fragments outside keep PyAnnote's original
         speaker.
      3. Absorb fragments shorter than `min_fragment_s` into their
         neighbour so a turn is never shredded into sub-word confetti
         at a boundary.
      4. Structural guarantee: a fragment OUTSIDE a confident mic
         interval can never carry `owner_label`. PyAnnote emits
         `SPEAKER_xx` and can't collide with it, but the guard is
         explicit anyway — "far-end words must never be attributed to
         the user" is the whole point of this feature and it should not
         depend on a naming coincidence.

    Returns ``(turns, stats)``. When the document isn't trustworthy the
    input turns are returned UNCHANGED and ``stats["applied"]`` is
    False with the stand-down reason — that is the fallback to today's
    behaviour, and it is the common path (mic-only sessions, conference
    room mode, speakerphone recordings, and every session recorded
    before this shipped).
    """
    turns_in = [dict(t) for t in (turns or [])]
    usable, reason = evaluate_trust(doc)
    if not usable:
        return turns_in, {
            "applied": False,
            "reason": reason,
            "turns_in": len(turns_in),
            "turns_out": len(turns_in),
            "owner_seconds": 0.0,
        }

    intervals = confident_intervals(doc, LABEL_MIC, min_confidence)
    out: List[dict] = []
    owner_seconds = 0.0
    split_turns = 0

    for turn in turns_in:
        try:
            start = float(turn.get("start"))
            end = float(turn.get("end"))
        except (TypeError, ValueError):
            out.append(turn)
            continue
        speaker = turn.get("speaker") or turn.get("speaker_id") or "SPEAKER_00"
        if end <= start:
            out.append(turn)
            continue
        # Guard (4): an incoming label that already claims the owner
        # identity is not trusted outside a confident mic interval.
        far_label = (
            FAR_END_FALLBACK_LABEL if speaker == owner_label else speaker)

        pieces = _split_turn(start, end, intervals, owner_label, far_label)
        pieces = _absorb_short_fragments(
            pieces, min_fragment_s, start, end, owner_label, far_label)
        if len(pieces) > 1:
            split_turns += 1
        for a, b, label in pieces:
            if label == owner_label:
                owner_seconds += (b - a)
            new_turn = dict(turn)
            new_turn["start"] = a
            new_turn["end"] = b
            new_turn["speaker"] = label
            if "speaker_id" in new_turn:
                new_turn["speaker_id"] = label
            out.append(new_turn)

    out.sort(key=lambda t: (float(t["start"]), float(t["end"])))
    return out, {
        "applied": True,
        "reason": None,
        "turns_in": len(turns_in),
        "turns_out": len(out),
        "split_turns": split_turns,
        "owner_seconds": round(owner_seconds, 3),
        "owner_label": owner_label,
        "overall_confidence": (doc.get("summary") or {}).get(
            "overall_confidence"),
    }


def _split_turn(
    start: float,
    end: float,
    intervals: Sequence[Tuple[float, float]],
    owner_label: str,
    far_label: str,
) -> List[Tuple[float, float, str]]:
    pieces: List[Tuple[float, float, str]] = []
    cursor = start
    for a, b in intervals:
        if b <= start:
            continue
        if a >= end:
            break
        lo, hi = max(start, a), min(end, b)
        if hi <= lo:
            continue
        if lo > cursor:
            pieces.append((cursor, lo, far_label))
        pieces.append((lo, hi, owner_label))
        cursor = hi
    if cursor < end:
        pieces.append((cursor, end, far_label))
    if not pieces:
        pieces.append((start, end, far_label))
    return pieces


def _absorb_short_fragments(
    pieces: List[Tuple[float, float, str]],
    min_fragment_s: float,
    turn_start: float,
    turn_end: float,
    owner_label: str,
    far_label: str,
) -> List[Tuple[float, float, str]]:
    if len(pieces) <= 1:
        return pieces
    kept: List[List] = []
    carry_start: Optional[float] = None
    for a, b, label in pieces:
        if carry_start is not None:
            a = carry_start
            carry_start = None
        if (b - a) < min_fragment_s:
            if kept:
                kept[-1][1] = b
            else:
                carry_start = a
            continue
        kept.append([a, b, label])
    if carry_start is not None:
        if kept:
            kept[0][0] = carry_start
        else:
            # Every fragment was below the floor: emit one piece for the
            # whole turn, labelled by whichever side held more of it.
            owner_time = sum(
                b - a for a, b, lbl in pieces if lbl == owner_label)
            total = turn_end - turn_start
            label = owner_label if owner_time * 2 >= total else far_label
            return [(turn_start, turn_end, label)]
    merged: List[List] = []
    for piece in kept:
        if merged and merged[-1][2] == piece[2]:
            merged[-1][1] = piece[1]
        else:
            merged.append(list(piece))
    return [(m[0], m[1], m[2]) for m in merged]
