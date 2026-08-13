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

# Make `utils` importable when this script is invoked directly (not as
# `python -m backend.scripts.measure_aec`) — mirrors the sys.path setup
# in scripts/finalize_audio.py.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The NLMS filter, ERLE metric, and alignment helpers live in
# utils/aec.py so the production offline-AEC path (finalize_recording_
# streaming, gated by Settings.echo_cancellation_enabled) reuses the
# exact same math this dev tool validates — no second implementation
# to keep in sync. See utils/aec.py's module docstring for the full
# design rationale.
from utils.aec import (  # noqa: E402
    ERLE_WINDOW_S,
    FILTER_TAPS,
    NLMS_REGULARIZER,
    NLMS_STEP,
    align_by_xcorr as _align_by_xcorr_sr,
    compute_erle as _compute_erle_sr,
    energy as _energy,
    nlms_filter,
    shift as _shift,
)

TARGET_SR = 16000


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
    """Thin wrapper over utils.aec.align_by_xcorr keeping this script's
    original samples-based signature (dev tool / self-test callers)."""
    return _align_by_xcorr_sr(
        near, far, TARGET_SR, max_lag_s=max_lag_samples / TARGET_SR)


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


def compute_erle(near: np.ndarray, residual: np.ndarray,
                 win_s: float = ERLE_WINDOW_S) -> Tuple[float, np.ndarray]:
    """Thin wrapper over utils.aec.compute_erle at this script's fixed
    TARGET_SR (16 kHz)."""
    return _compute_erle_sr(near, residual, TARGET_SR, win_s=win_s)


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
