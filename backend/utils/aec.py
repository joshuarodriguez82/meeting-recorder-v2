"""
Shared offline acoustic-echo-cancellation (AEC) core.

Pure-numpy NLMS adaptive filter + cross-correlation alignment + ERLE
metrics. Used by two callers:

  - backend/scripts/measure_aec.py — the dev-only offline AEC validator/
    CLI that this module's math was originally written for (see its
    docstring: it exists to decide "whether shipping a real-time AEC
    integration is worth the cross-platform packaging cost").
  - backend/utils/audio_utils.py's ``finalize_recording_streaming`` —
    the production offline-AEC-before-mix step, gated by
    ``Settings.echo_cancellation_enabled`` (default False).

WHY OFFLINE, NOT REALTIME
--------------------------
The user records with an external mic + speakers (by choice, not a
headset). Unmuting picks up the far-end caller's audio coming back out
of the speakers, so the caller's speech gets captured a second time on
the mic channel and mis-attributed as the user's own speech (double
transcription, degraded diarization). A realtime AEC integration would
fix this too, but at real packaging + audio-thread-risk cost across
three platforms for a feature whose benefit is unproven. Since both the
mic and loopback (system-audio) WAVs already exist, unmodified, at
finalize time — and finalize already runs in an isolated subprocess
with no realtime constraint — doing AEC there is strictly safer and
cheaper. See backend/scripts/finalize_audio.py and
services/recording_service.py's ``_run_finalize_subprocess`` docstring
for the subprocess-isolation design this slots into.

SAFETY CONTRACT
----------------
This user has lost recordings before and is highly sensitive to it.
``decide_offline_aec`` is the single gate between "adaptive filter
output" and "gets mixed into the user's only copy of a recording" —
every branch in it defaults to REJECT (fall back to the original,
unmodified mic signal) unless the cleaned result passes ALL of:

  1. finite (no NaN/Inf) at every stage,
  2. same length as the input near-end signal,
  3. ERLE (echo return loss enhancement) strictly greater than 0 dB —
     ERLE <= 0 means the filter achieved nothing (or made it worse),
  4. ERLE not implausibly high (> 40 dB) — a real echo path mixed with
     genuine near-end speech essentially never gets cancelled to
     1/10000th of its original energy; a result that clean means the
     filter almost certainly ate real speech along with the echo, not
     that the echo path was unusually cooperative.

Nothing in this module ever raises out to its caller — every public
entry point catches its own exceptions and reports a rejection instead,
so "losing echo cancellation" can never become "losing the recording."
"""

from __future__ import annotations

from math import gcd
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from scipy.signal import resample_poly

FILTER_TAPS = 1024              # 64 ms tail at 16 kHz
NLMS_STEP = 0.5
NLMS_REGULARIZER = 1e-6
ERLE_WINDOW_S = 1.0

# ── Production-path sanity-check thresholds (decide_offline_aec) ────
# See the module docstring's SAFETY CONTRACT section for the full
# justification; summarized at point of use below.
MIN_ACCEPTABLE_ERLE_DB = 0.0
MAX_PLAUSIBLE_ERLE_DB = 40.0
# Coarse cross-stream start alignment (loopback_start_offset_s / the
# right-alignment heuristic) is handled by the caller before this
# module ever sees the signals. What's searched here is only the
# residual ACOUSTIC delay — speaker → air → mic — which is physically
# bounded to roughly 10-80 ms for a desktop/laptop setup. ±150 ms
# leaves headroom without letting the cross-correlation lock onto a
# spurious far-away lag on a noisy/quiet recording.
RESIDUAL_DELAY_SEARCH_S = 0.15


def nlms_filter(
    near: np.ndarray,
    far: np.ndarray,
    taps: int = FILTER_TAPS,
    mu: float = NLMS_STEP,
    eps: float = NLMS_REGULARIZER,
) -> np.ndarray:
    """Run a normalised-LMS adaptive filter. Returns the residual
    (echo-cancelled near-end signal).

    Both inputs must be the same length, mono float32, time-aligned.
    """
    assert len(near) == len(far), "near and far must be equal length"
    n = len(near)
    h = np.zeros(taps, dtype=np.float32)
    out = np.zeros(n, dtype=np.float32)
    far = far.astype(np.float32, copy=False)
    near = near.astype(np.float32, copy=False)
    # Pre-pad far so we can index x_ref[i-taps+1 : i+1] safely
    pad = np.zeros(taps - 1, dtype=np.float32)
    far_padded = np.concatenate([pad, far])
    for i in range(n):
        # Windowed reference, newest sample last → flip for convolution-style dot
        x = far_padded[i:i + taps][::-1]
        y_hat = float(np.dot(h, x))
        e = float(near[i]) - y_hat
        out[i] = e
        norm = float(np.dot(x, x)) + eps
        h += (mu * e / norm) * x
    return out


def energy(x: np.ndarray) -> float:
    return float(np.mean(x.astype(np.float64) ** 2)) + 1e-20


def compute_erle(
    near: np.ndarray, residual: np.ndarray, sr: int, win_s: float = ERLE_WINDOW_S,
) -> Tuple[float, np.ndarray]:
    """Return (overall_ERLE_dB, per_window_ERLE_dB).

    ERLE = 10 log10(E[near^2] / E[residual^2])."""
    win = max(int(win_s * sr), 1)
    n = min(len(near), len(residual))
    n_win = n // win
    per = np.zeros(n_win, dtype=np.float64)
    for i in range(n_win):
        s = i * win
        per[i] = 10.0 * np.log10(energy(near[s:s + win]) /
                                 energy(residual[s:s + win]))
    overall = 10.0 * np.log10(energy(near[:n]) / energy(residual[:n]))
    return float(overall), per


def align_by_xcorr(
    near: np.ndarray, far: np.ndarray, sr: int, max_lag_s: float = 1.0,
) -> int:
    """Estimate the delay: how many samples after `far` is played does
    it appear in `near`. Caps the search at ±max_lag_s.

    A positive return value means the echo lags the reference by N
    samples, so the filter sees the echo most efficiently if `far` is
    *delayed* by N samples before being fed in (puts the echo arrival
    at tap 0 of the adaptive filter, keeps the required tap count down
    to the tail length).
    """
    max_lag_samples = max(int(max_lag_s * sr), 1)
    n = min(len(near), len(far), sr * 30)  # 30s correlation cap
    if n < sr // 4:
        return 0
    a = near[:n] - near[:n].mean()
    b = far[:n] - far[:n].mean()
    nfft = 1 << (int(np.log2(2 * n - 1)) + 1)
    A = np.fft.rfft(a, nfft)
    B = np.fft.rfft(b, nfft)
    xc = np.fft.irfft(A * np.conj(B), nfft)
    pos = xc[:max_lag_samples + 1]
    neg = xc[-max_lag_samples:]
    full = np.concatenate([neg, pos])
    lag = int(np.argmax(np.abs(full))) - max_lag_samples
    return lag


def shift(x: np.ndarray, n: int) -> np.ndarray:
    """Shift x by n samples; positive n = delay (pad front), negative = advance."""
    if n == 0:
        return x
    if n > 0:
        return np.concatenate([np.zeros(n, dtype=x.dtype), x])[:len(x)]
    n = -n
    return np.concatenate([x[n:], np.zeros(n, dtype=x.dtype)])


def _empty_decision(reason: str) -> dict:
    return {
        "accepted": False,
        "reason": reason,
        "erle_db": None,
        "residual_delay_ms": None,
        "near_energy": None,
        "residual_energy": None,
        "cleaned": None,
    }


def decide_offline_aec(
    near: np.ndarray,
    far_coarse_aligned: np.ndarray,
    sr: int,
    residual_delay_search_s: float = RESIDUAL_DELAY_SEARCH_S,
) -> dict:
    """Run NLMS AEC on `near` using `far_coarse_aligned` as the
    reference, then decide whether the result is safe to use.

    `far_coarse_aligned` must already be the same length as `near` and
    positioned on the same timeline — e.g. built by placing the
    loopback signal at its known wallclock offset into a zero buffer
    the length of `near`. This function only searches for the SMALL
    residual acoustic delay on top of that (see RESIDUAL_DELAY_SEARCH_S),
    not the large cross-stream start-time offset, which the caller is
    responsible for.

    Returns a dict:
      {
        "accepted": bool,
        "reason": str,                  # machine-readable decision code
        "erle_db": float | None,        # overall ERLE if computed
        "residual_delay_ms": float | None,
        "near_energy": float | None,
        "residual_energy": float | None,
        "cleaned": np.ndarray | None,   # same length as `near`; only
                                         # set when accepted
      }

    Never raises. See the module docstring's SAFETY CONTRACT for the
    thresholds and their justification.
    """
    try:
        near = np.asarray(near)
        far_coarse_aligned = np.asarray(far_coarse_aligned)

        if len(near) != len(far_coarse_aligned):
            return _empty_decision("length_mismatch")
        if len(near) == 0:
            return _empty_decision("empty_input")
        if not np.all(np.isfinite(near)):
            return _empty_decision("near_non_finite")
        if not np.all(np.isfinite(far_coarse_aligned)):
            return _empty_decision("far_non_finite")

        far_std = float(np.std(far_coarse_aligned))
        if far_std < 1e-6:
            # Silence / all-zero reference: nothing to cancel. Running
            # NLMS here would burn a full pass for zero possible
            # benefit — reject up front rather than let a degenerate
            # near-zero-norm reference produce numerically meaningless
            # filter taps.
            return _empty_decision("reference_silent")

        residual_lag = align_by_xcorr(
            near, far_coarse_aligned, sr, max_lag_s=residual_delay_search_s)
        far_final = shift(far_coarse_aligned, residual_lag)

        residual = nlms_filter(
            near.astype(np.float32, copy=False),
            far_final.astype(np.float32, copy=False),
        )

        if len(residual) != len(near):
            return _empty_decision("output_length_mismatch")
        if not np.all(np.isfinite(residual)):
            return _empty_decision("residual_non_finite")

        overall_erle, _ = compute_erle(near, residual, sr)
        near_e = energy(near)
        res_e = energy(residual)

        if not np.isfinite(overall_erle):
            return {
                "accepted": False, "reason": "erle_non_finite",
                "erle_db": None, "residual_delay_ms": residual_lag / sr * 1000.0,
                "near_energy": near_e, "residual_energy": res_e, "cleaned": None,
            }
        if overall_erle <= MIN_ACCEPTABLE_ERLE_DB:
            reason = "erle_non_positive"
            accepted = False
        elif overall_erle > MAX_PLAUSIBLE_ERLE_DB:
            reason = "erle_implausible"
            accepted = False
        else:
            reason = "ok"
            accepted = True

        return {
            "accepted": accepted,
            "reason": reason,
            "erle_db": float(overall_erle),
            "residual_delay_ms": residual_lag / sr * 1000.0,
            "near_energy": near_e,
            "residual_energy": res_e,
            "cleaned": residual if accepted else None,
        }
    except Exception as e:
        return _empty_decision(f"exception:{e!r}")


def _load_mono_resampled(path: str, target_sr: int) -> np.ndarray:
    """Read a WAV in full, downmix to mono, resample to target_sr,
    return float32."""
    import soundfile as sf
    data, sr = sf.read(path, always_2d=True, dtype="float32")
    mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    mono = mono.astype(np.float32, copy=False)
    if sr != target_sr:
        g = gcd(sr, target_sr)
        up, down = target_sr // g, sr // g
        mono = resample_poly(mono, up, down).astype(np.float32, copy=False)
    return mono


def apply_offline_aec_to_mic_file(
    mic_wav_path: str,
    loopback_16k_path: str,
    target_sr: int,
    coarse_offset_frames: int,
    expected_frames: int,
    out_path: str,
) -> dict:
    """File-level wrapper around ``decide_offline_aec`` for the
    production finalize path.

    Loads the mic WAV (resampled to target_sr) and the loopback
    reference (already at target_sr — produced by finalize's own
    pass-1 resample), coarse-aligns the reference onto the mic's
    timeline at `coarse_offset_frames` (the SAME offset the mixing
    step uses — the wallclock anchor or the right-alignment
    heuristic), and runs ``decide_offline_aec``.

    On acceptance, writes the cleaned mic to `out_path` as a mono
    float32 WAV at target_sr, forced to EXACTLY `expected_frames`
    samples (truncated or zero-padded) — this guarantees the merge
    step downstream produces the identical duration the non-AEC path
    would have, regardless of any rounding difference between this
    function's whole-file resample and the streaming per-block
    resample the merge itself uses.

    Never raises. Returns the same decision dict as
    ``decide_offline_aec``, with `out_path` added (only set when
    accepted and the write succeeded) and `cleaned` cleared to None
    after writing so callers don't hold a second copy of a potentially
    large array around just for logging.
    """
    try:
        import soundfile as sf

        near = _load_mono_resampled(mic_wav_path, target_sr)
        far_ref = _load_mono_resampled(loopback_16k_path, target_sr)

        far_coarse = np.zeros(len(near), dtype=np.float32)
        start = max(0, coarse_offset_frames)
        if start < len(near) and len(far_ref) > 0:
            n_copy = min(len(far_ref), len(near) - start)
            if n_copy > 0:
                far_coarse[start:start + n_copy] = far_ref[:n_copy]

        decision = decide_offline_aec(near, far_coarse, target_sr)
        if not decision["accepted"]:
            return decision

        cleaned = decision["cleaned"]
        if len(cleaned) > expected_frames:
            cleaned = cleaned[:expected_frames]
        elif len(cleaned) < expected_frames:
            padded = np.zeros(expected_frames, dtype=np.float32)
            padded[:len(cleaned)] = cleaned
            cleaned = padded

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, cleaned, target_sr, subtype="FLOAT")

        decision = dict(decision)
        decision["out_path"] = out_path
        decision["cleaned"] = None
        return decision
    except Exception as e:
        d = _empty_decision(f"exception:{e!r}")
        return d
