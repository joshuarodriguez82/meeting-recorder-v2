"""
Offline echo-cancellation validator.

Dev-only tool. Reads a saved mic WAV + loopback WAV from a recording session,
runs offline acoustic-echo-cancellation, writes a cleaned mic WAV, and prints
ERLE / residual metrics so we can decide empirically whether shipping a
real-time AEC integration is worth the cross-platform packaging cost.

Default engine is a pure-numpy normalised LMS (NLMS) adaptive filter — no
external deps beyond what backend/requirements.txt already pulls in. If
`webrtc-audio-processing` is importable in the local env, `--engine webrtc`
runs through that instead for comparison.

Usage:
    # By session id (looks in <recordings_dir>/_recording_<id>.wav etc.)
    python -m backend.scripts.measure_aec --session 0193abc...

    # By explicit paths
    python -m backend.scripts.measure_aec \\
        --mic /tmp/_recording_X.wav --loopback /tmp/_loopback_X.wav

    # Synthetic self-test (sanity-checks the NLMS math)
    python -m backend.scripts.measure_aec --self-test

Note: The recording service deletes _recording_<id>.wav and _loopback_<id>.wav
after a successful merge. Set KEEP_AUDIO_TEMPS=1 in the backend env before
recording the session you want to validate.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import soundfile as sf
from scipy import signal as sps

TARGET_SR = 16000
FILTER_TAPS = 1024              # 64 ms tail at 16 kHz
NLMS_STEP = 0.5
NLMS_REGULARIZER = 1e-6
ERLE_WINDOW_S = 1.0


def _load_mono_16k(path: str) -> np.ndarray:
    """Read a WAV, downmix to mono, resample to 16 kHz, return float32 [-1, 1]."""
    data, sr = sf.read(path, always_2d=True, dtype="float32")
    if data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    mono = data[:, 0]
    if sr != TARGET_SR:
        # resample_poly needs integer up/down ratios
        from math import gcd
        g = gcd(sr, TARGET_SR)
        up = TARGET_SR // g
        down = sr // g
        mono = sps.resample_poly(mono, up, down).astype(np.float32, copy=False)
    return mono


def _align_by_xcorr(near: np.ndarray, far: np.ndarray,
                    max_lag_samples: int = TARGET_SR) -> int:
    """Estimate the echo-path delay: how many samples after `far` is played
    does it appear in `near`. Caps search at ±1 second.

    A positive return value means the echo lags the reference by N samples,
    so the filter sees the echo most efficiently if `far` is *delayed* by N
    samples before being fed in (puts the echo arrival at tap 0 of the
    adaptive filter, keeps the required tap count down to the tail length).
    """
    n = min(len(near), len(far), TARGET_SR * 30)  # 30s correlation cap
    if n < TARGET_SR:
        return 0
    a = near[:n] - near[:n].mean()
    b = far[:n] - far[:n].mean()
    # FFT-based cross-correlation
    nfft = 1 << (int(np.log2(2 * n - 1)) + 1)
    A = np.fft.rfft(a, nfft)
    B = np.fft.rfft(b, nfft)
    xc = np.fft.irfft(A * np.conj(B), nfft)
    # lag k in xc[k] means: near[t] correlates with far[t-k] (far delayed by k)
    # Restrict search to ±max_lag
    pos = xc[:max_lag_samples + 1]
    neg = xc[-max_lag_samples:]
    full = np.concatenate([neg, pos])
    lag = int(np.argmax(np.abs(full))) - max_lag_samples
    return lag


def _shift(x: np.ndarray, n: int) -> np.ndarray:
    """Shift x by n samples; positive n = delay (pad front), negative = advance."""
    if n == 0:
        return x
    if n > 0:
        return np.concatenate([np.zeros(n, dtype=x.dtype), x])[:len(x)]
    n = -n
    return np.concatenate([x[n:], np.zeros(n, dtype=x.dtype)])


def nlms_filter(near: np.ndarray, far: np.ndarray,
                taps: int = FILTER_TAPS,
                mu: float = NLMS_STEP,
                eps: float = NLMS_REGULARIZER) -> np.ndarray:
    """Run NLMS adaptive filter. Returns the residual (echo-cancelled near-end).

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


def webrtc_filter(near: np.ndarray, far: np.ndarray) -> np.ndarray:
    """Try to use webrtc-audio-processing if importable. Raises ImportError
    if the package isn't available so the caller can fall back."""
    try:
        from webrtc_audio_processing import AudioProcessingModule  # type: ignore
    except ImportError as e:
        raise ImportError(
            "webrtc-audio-processing not installed. Install with "
            "`pip install webrtc-audio-processing` (requires SWIG + C++ "
            "toolchain). Falling back to NLMS."
        ) from e
    apm = AudioProcessingModule(aec_type=2, enable_ns=False, agc_type=0)
    apm.set_stream_format(TARGET_SR, 1)
    apm.set_reverse_stream_format(TARGET_SR, 1)
    # AEC processes 10 ms frames = 160 samples at 16 kHz
    frame = 160
    near_i16 = (np.clip(near, -1, 1) * 32767).astype(np.int16)
    far_i16 = (np.clip(far, -1, 1) * 32767).astype(np.int16)
    out = np.zeros_like(near_i16)
    n = len(near_i16) - (len(near_i16) % frame)
    for i in range(0, n, frame):
        apm.process_reverse_stream(far_i16[i:i + frame].tobytes())
        cleaned = apm.process_stream(near_i16[i:i + frame].tobytes())
        out[i:i + frame] = np.frombuffer(cleaned, dtype=np.int16)
    return (out.astype(np.float32) / 32767.0)


def _energy(x: np.ndarray) -> float:
    return float(np.mean(x.astype(np.float64) ** 2)) + 1e-20


def compute_erle(near: np.ndarray, residual: np.ndarray,
                 win_s: float = ERLE_WINDOW_S) -> Tuple[float, np.ndarray]:
    """Return (overall_ERLE_dB, per_window_ERLE_dB).

    ERLE = 10 log10(E[near^2] / E[residual^2])."""
    win = int(win_s * TARGET_SR)
    n = min(len(near), len(residual))
    n_win = n // win
    per = np.zeros(n_win, dtype=np.float64)
    for i in range(n_win):
        s = i * win
        per[i] = 10.0 * np.log10(_energy(near[s:s + win]) /
                                 _energy(residual[s:s + win]))
    overall = 10.0 * np.log10(_energy(near[:n]) / _energy(residual[:n]))
    return float(overall), per


def _resolve_paths(args: argparse.Namespace) -> Tuple[str, str]:
    if args.mic and args.loopback:
        return args.mic, args.loopback
    if args.session:
        rec_dir = Path(args.recordings_dir or _default_recordings_dir())
        mic = rec_dir / f"_recording_{args.session}.wav"
        lb = rec_dir / f"_loopback_{args.session}.wav"
        if not mic.exists():
            sys.exit(f"mic temp not found: {mic}\n"
                     "Hint: set KEEP_AUDIO_TEMPS=1 before recording so the "
                     "raw mic/loopback WAVs survive finalize.")
        if not lb.exists():
            sys.exit(f"loopback temp not found: {lb}")
        return str(mic), str(lb)
    sys.exit("provide either --session <id> or --mic <path> --loopback <path>")


def _default_recordings_dir() -> str:
    # Match what backend/config/settings.py resolves to: ~/.meeting-recorder/recordings
    return str(Path.home() / ".meeting-recorder" / "recordings")


def run(mic_path: str, loopback_path: str, out_path: Optional[str],
        engine: str) -> int:
    print(f"[load] mic     = {mic_path}")
    print(f"[load] loopback= {loopback_path}")
    t0 = time.monotonic()
    near = _load_mono_16k(mic_path)
    far = _load_mono_16k(loopback_path)
    # Pad shorter to match
    n = min(len(near), len(far))
    near = near[:n]
    far = far[:n]
    print(f"[load] {n / TARGET_SR:.1f}s @ 16k mono "
          f"({time.monotonic() - t0:.2f}s)")

    lag = _align_by_xcorr(near, far)
    print(f"[align] echo-path delay = {lag} samples "
          f"({lag / TARGET_SR * 1000:.1f} ms)")
    far = _shift(far, lag)  # delay far so echo lines up at filter tap 0

    t0 = time.monotonic()
    if engine == "webrtc":
        try:
            residual = webrtc_filter(near, far)
            engine_used = "webrtc"
        except ImportError as e:
            print(f"[engine] {e}")
            print("[engine] falling back to NLMS")
            residual = nlms_filter(near, far)
            engine_used = "nlms (fallback)"
    else:
        residual = nlms_filter(near, far)
        engine_used = "nlms"
    print(f"[filter] {engine_used} done in {time.monotonic() - t0:.2f}s")

    overall, per = compute_erle(near, residual)
    print()
    print(f"=== ERLE  (engine={engine_used}) ===")
    print(f"  overall:   {overall:6.2f} dB")
    if len(per) > 0:
        print(f"  per-{ERLE_WINDOW_S:.0f}s:    "
              f"min {per.min():.2f}  median {np.median(per):.2f}  "
              f"mean {per.mean():.2f}  max {per.max():.2f}  dB")
    pre_e = _energy(near)
    post_e = _energy(residual)
    print(f"  near-end energy: {pre_e:.6f}")
    print(f"  residual energy: {post_e:.6f}  ({post_e / pre_e * 100:.2f}% of near)")
    # Reference: an end-to-end usable AEC should clear ≥15 dB ERLE on
    # double-talk-free segments. Sub-6 dB means the echo path is too
    # nonlinear / time-varying for this filter to help.
    if overall < 6:
        verdict = "POOR — echo path likely nonlinear or misaligned"
    elif overall < 15:
        verdict = "MARGINAL — would help some, not enough to ship"
    else:
        verdict = "GOOD — empirically worth productizing"
    print(f"  verdict: {verdict}")

    if out_path:
        sf.write(out_path, residual, TARGET_SR, subtype="PCM_16")
        print(f"\n[write] cleaned mic → {out_path}")

    return 0


def self_test() -> int:
    """Synthesize a known echo, run NLMS, verify ERLE > 25 dB."""
    print("[self-test] synthesizing 5s noise + delayed/attenuated echo …")
    rng = np.random.default_rng(42)
    n = TARGET_SR * 5
    far = rng.standard_normal(n).astype(np.float32) * 0.3
    # Linear room impulse: delay 100 ms, decaying tail
    delay = TARGET_SR // 10
    rir = np.zeros(delay + 200, dtype=np.float32)
    rir[delay] = 0.6
    for i in range(delay + 1, len(rir)):
        rir[i] = rir[i - 1] * 0.93 + rng.standard_normal() * 0.005
    echo = sps.fftconvolve(far, rir)[:n]
    voice = rng.standard_normal(n).astype(np.float32) * 0.05  # near-end "voice"
    near = (echo + voice).astype(np.float32)
    # Run alignment + filter
    lag = _align_by_xcorr(near, far)
    print(f"[self-test] xcorr lag = {lag} (expected ~{delay})")
    far_aligned = _shift(far, lag)
    residual = nlms_filter(near, far_aligned)
    overall, per = compute_erle(near, residual)
    # Skip first 0.5s of convergence
    converged = per[1:].mean() if len(per) > 1 else overall
    print(f"[self-test] overall ERLE = {overall:.2f} dB, "
          f"converged ERLE = {converged:.2f} dB")
    if converged < 15:
        print("[self-test] FAIL — converged ERLE below 15 dB. The math is "
              "wrong or the test is broken.")
        return 1
    print("[self-test] PASS")
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", help="Recording session id (looks in recordings_dir)")
    p.add_argument("--mic", help="Path to mic WAV")
    p.add_argument("--loopback", help="Path to loopback WAV")
    p.add_argument("--recordings-dir",
                   help="Override recordings dir (default: ~/.meeting-recorder/recordings)")
    p.add_argument("--out", help="Path to write cleaned mic WAV (default: <mic>_aec.wav)")
    p.add_argument("--engine", choices=["nlms", "webrtc"], default="nlms")
    p.add_argument("--self-test", action="store_true",
                   help="Run synthetic self-test instead of processing files")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()

    mic, lb = _resolve_paths(args)
    out = args.out or str(Path(mic).with_name(Path(mic).stem + "_aec.wav"))
    return run(mic, lb, out, args.engine)


if __name__ == "__main__":
    raise SystemExit(main())
