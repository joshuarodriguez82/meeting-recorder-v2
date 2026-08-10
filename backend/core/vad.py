"""
Dependency-free energy + zero-crossing voice activity detector.

Field report 2026-08-10 (Zoom notetaker parity): the live transcript
took ~15s to show any text because live_transcriber.py buffered mic and
loopback audio into fixed 15-second windows before running Whisper —
users compared it unfavorably to Zoom's notetaker, which shows text
within a couple of seconds of someone finishing a sentence. Fixing that
means chunking on speech boundaries instead of a wall-clock timer, which
means detecting where speech starts and stops in the raw PCM.

We deliberately do NOT reach for webrtcvad, torch, or any ML VAD here.
Two reasons:

  1. The CI test venv only has numpy/scipy/soundfile/pytest/fastapi
     (see AGENTS.md) — a hard dependency on webrtcvad or a torch-based
     VAD would make this module untestable in CI, and worse, would add
     another native wheel to the Tauri bundle for every platform we
     ship (win/mac-arm/mac-intel/linux).
  2. A short-term-energy + zero-crossing-rate detector is the classic
     textbook VAD (Rabiner & Sambur, 1975) and is more than accurate
     enough for "did someone stop talking" — we're not trying to
     distinguish speech from music, just speech from silence/room tone,
     and Whisper's own internal `vad_filter=True` still runs as a
     second pass on whatever we hand it.

Algorithm, in short: split the signal into short frames (20ms default),
compute each frame's RMS energy and zero-crossing rate, mark a frame
"voiced" when its energy clears an adaptive threshold (a multiple of the
buffer's own noise floor, with an absolute floor so pure near-silence
input never trips it) AND it actually oscillates (zero-crossings > 0 —
filters out a constant/DC blip that has energy but isn't a real signal).
Adjacent voiced runs separated by a short gap get merged (people pause
mid-sentence without meaning to end their turn); very short surviving
runs get discarded (breath noise, mouse clicks bleeding into the mic).
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

# Frame size for the energy/ZCR analysis. 20ms is the standard speech-
# processing frame length — short enough to localize an utterance
# boundary to within ~20ms, long enough that RMS energy over the frame
# is a stable estimate rather than reacting to individual cycles.
FRAME_MS = 20.0

# A frame counts as "voiced" when its energy exceeds
# max(noise_floor * ENERGY_REL_MULT, ENERGY_ABS_FLOOR), where noise_floor
# is the 20th-percentile frame energy of the buffer being analyzed (a
# cheap per-call estimate of "what does quiet sound like right now" that
# adapts to mic gain / background noise without needing a calibration
# step). The absolute floor exists so a buffer that's genuinely all
# silence (noise_floor ~= 0) doesn't get an effective threshold of 0,
# which would call every bit of numerical noise "speech".
ENERGY_REL_MULT = 3.0
ENERGY_ABS_FLOOR = 0.015

# Voiced runs separated by a gap shorter than this get merged into one
# utterance — a natural mid-sentence pause shouldn't fragment a single
# turn into multiple chunks (and multiple Whisper calls).
MERGE_GAP_MS = 300.0

# Runs shorter than this after merging are discarded — too short to be
# a real utterance (breath noise, a click, a single very short "hm").
MIN_SPAN_MS = 250.0


def find_utterances(
    pcm: np.ndarray,
    samplerate: int,
    frame_ms: float = FRAME_MS,
    merge_gap_ms: float = MERGE_GAP_MS,
    min_span_ms: float = MIN_SPAN_MS,
    energy_rel_mult: float = ENERGY_REL_MULT,
    energy_abs_floor: float = ENERGY_ABS_FLOOR,
) -> List[Tuple[int, int]]:
    """Find speech spans in `pcm`, a mono float32-ish array at `samplerate`.

    Returns a list of (start_sample, end_sample) tuples, in ascending
    order, non-overlapping. Empty list for pure silence or an empty
    input array — never raises on a plain numpy array input.
    """
    if pcm is None or samplerate is None or samplerate <= 0:
        return []
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    n = len(pcm)
    if n == 0:
        return []

    frame_len = max(1, int(round(samplerate * frame_ms / 1000.0)))
    n_frames = int(np.ceil(n / frame_len))
    pad_len = n_frames * frame_len - n
    padded = (np.concatenate([pcm, np.zeros(pad_len, dtype=np.float32)])
              if pad_len else pcm)
    frames = padded.reshape(n_frames, frame_len)

    energies = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    if frame_len > 1:
        signs = np.sign(frames)
        crossings = np.sum(signs[:, 1:] != signs[:, :-1], axis=1)
        zcr = crossings / float(frame_len - 1)
    else:
        zcr = np.zeros(n_frames, dtype=np.float64)

    noise_floor = float(np.percentile(energies, 20)) if n_frames else 0.0
    threshold = max(noise_floor * energy_rel_mult, energy_abs_floor)
    energy_voiced = energies > threshold
    if not energy_voiced.any() and float(np.max(energies)) > energy_abs_floor:
        # The relative (noise-floor-multiple) threshold found nothing —
        # which happens when the WHOLE buffer is uniformly at speech
        # level with no quieter frames to anchor a noise floor on (a
        # continuous talker who never pauses within this analysis
        # window). The percentile-based noise_floor ends up close to
        # the signal's own level, so REL_MULT pushes the threshold
        # above everything. Since there's clearly real signal here
        # (above the absolute floor), fall back to classifying on the
        # absolute floor alone rather than reporting "no speech" over a
        # buffer that's obviously not silent.
        energy_voiced = energies > energy_abs_floor
    voiced = energy_voiced & (zcr > 0.0)

    # Collapse the voiced boolean vector into (start_frame, end_frame)
    # runs. Frame counts here are small (a few hundred at most for an
    # 8s hard-ceiling buffer at 20ms/frame), so a plain Python loop is
    # simpler to read than a vectorized run-length trick and costs
    # microseconds either way.
    spans_frames: List[Tuple[int, int]] = []
    in_run = False
    run_start = 0
    for i, v in enumerate(voiced):
        if v and not in_run:
            in_run = True
            run_start = i
        elif not v and in_run:
            in_run = False
            spans_frames.append((run_start, i))
    if in_run:
        spans_frames.append((run_start, n_frames))

    if not spans_frames:
        return []

    merge_gap_frames = max(0, int(round(merge_gap_ms / frame_ms)))
    merged: List[List[int]] = [list(spans_frames[0])]
    for s, e in spans_frames[1:]:
        if s - merged[-1][1] <= merge_gap_frames:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    min_span_samples = int(round(samplerate * min_span_ms / 1000.0))
    out: List[Tuple[int, int]] = []
    for s, e in merged:
        start_sample = s * frame_len
        end_sample = min(n, e * frame_len)
        if end_sample - start_sample >= min_span_samples:
            out.append((start_sample, end_sample))
    return out
