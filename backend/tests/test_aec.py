"""
Offline acoustic echo cancellation (utils/aec.py).

Covers the DSP core (decide_offline_aec / nlms_filter) with synthetic
fixtures, the file-level wrapper (apply_offline_aec_to_mic_file), and
the production integration point (finalize_recording_streaming's
echo_cancellation_enabled flag). This user has lost recordings before
and is highly sensitive to it — the overriding property under test
throughout is that AEC can only ever IMPROVE or NO-OP the recording,
never damage it: any pathological input, degenerate result, or
exception must fall back to the original, unmodified mic signal.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from scipy.signal import fftconvolve

from utils.aec import (
    MAX_PLAUSIBLE_ERLE_DB,
    MIN_ACCEPTABLE_ERLE_DB,
    apply_offline_aec_to_mic_file,
    compute_erle,
    decide_offline_aec,
    nlms_filter,
)
from utils.audio_utils import finalize_recording_streaming
from conftest import write_sine_wav, write_silence_wav

SR = 16000


def _synthetic_echo_fixture(
    seed: int = 42,
    duration_s: float = 6.0,
    delay_s: float = 0.05,
    far_amp: float = 0.3,
    voice_amp: float = 0.05,
):
    """Build (near, far, voice) — far-end reference, a fake echo of it
    convolved through a short synthetic room impulse response + delay,
    and a distinct near-end "speech" component the echo is added to.
    `near = echo(far) + voice`. Mirrors measure_aec.py's self-test
    fixture, parametrized so tests can vary the delay / amplitudes.
    """
    rng = np.random.default_rng(seed)
    n = int(SR * duration_s)
    far = rng.standard_normal(n).astype(np.float32) * far_amp
    delay = int(delay_s * SR)
    rir = np.zeros(delay + 200, dtype=np.float32)
    rir[delay] = 0.6
    for i in range(delay + 1, len(rir)):
        rir[i] = rir[i - 1] * 0.93 + rng.standard_normal() * 0.005
    echo = fftconvolve(far, rir)[:n].astype(np.float32)
    # Distinct seed so voice is statistically independent of far/echo.
    voice_rng = np.random.default_rng(seed + 1000)
    voice = voice_rng.standard_normal(n).astype(np.float32) * voice_amp
    near = (echo + voice).astype(np.float32)
    return near, far, voice


# ── Core DSP: decide_offline_aec ──────────────────────────────────────

def test_nlms_measurably_reduces_echo_positive_erle():
    """The headline claim: on a synthetic echo, NLMS must achieve a real,
    positive ERLE — not just "not broken"."""
    near, far, _voice = _synthetic_echo_fixture()
    decision = decide_offline_aec(near, far, SR)
    assert decision["accepted"] is True, decision["reason"]
    assert decision["erle_db"] is not None
    assert decision["erle_db"] > 10.0, (
        f"expected a strong positive ERLE on a clean synthetic echo, "
        f"got {decision['erle_db']:.2f} dB")


def test_near_end_speech_survives_cancellation():
    """The most important guard: the cleaned signal must still strongly
    correlate with the TRUE near-end speech component — i.e. the filter
    removed the echo without eating the user's own voice."""
    near, far, voice = _synthetic_echo_fixture()
    decision = decide_offline_aec(near, far, SR)
    assert decision["accepted"] is True, decision["reason"]
    cleaned = decision["cleaned"]
    assert cleaned is not None
    assert len(cleaned) == len(voice)

    corr = np.corrcoef(cleaned, voice)[0, 1]
    # 0.8 is a high bar in practice: this fixture's echo is ~4x the
    # voice's amplitude, so even a well-converged linear canceller
    # leaves some residual echo bleed relative to the quiet true voice.
    # An AEC that were eating real speech (rather than just failing to
    # fully remove echo) would show correlation near zero, not 0.8+.
    assert corr > 0.8, (
        f"cleaned signal only correlates {corr:.3f} with the true "
        f"near-end voice — AEC is eating real speech, not just echo")


@pytest.mark.parametrize("residual_delay_s", [0.01, 0.03, 0.08])
def test_residual_acoustic_delay_search_converges_across_range(residual_delay_s):
    """Speaker->air->mic delay is typically 10-80ms; the residual-delay
    search (on top of any coarse cross-stream offset, which is 0 here)
    must find it and still produce a usable, positive-ERLE result across
    that whole physically-plausible range."""
    near, far, _voice = _synthetic_echo_fixture(delay_s=residual_delay_s)
    decision = decide_offline_aec(near, far, SR)
    assert decision["accepted"] is True, (
        f"delay={residual_delay_s*1000:.0f}ms: {decision['reason']}")
    assert decision["erle_db"] > MIN_ACCEPTABLE_ERLE_DB


# ── Pathological references: must fall back, never crash ─────────────

def test_silent_reference_falls_back_without_crash():
    near, _far, _voice = _synthetic_echo_fixture()
    far_silent = np.zeros_like(near)
    decision = decide_offline_aec(near, far_silent, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["reason"] == "reference_silent"
    assert decision["cleaned"] is None


def test_all_zeros_both_signals_falls_back_without_crash():
    near = np.zeros(SR * 2, dtype=np.float32)
    far = np.zeros(SR * 2, dtype=np.float32)
    decision = decide_offline_aec(near, far, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["cleaned"] is None


def test_wrong_length_reference_falls_back_without_crash():
    near, far, _voice = _synthetic_echo_fixture()
    far_short = far[:len(far) // 2]
    decision = decide_offline_aec(near, far_short, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["reason"] == "length_mismatch"
    assert decision["cleaned"] is None


def test_nan_in_reference_falls_back_without_crash():
    near, far, _voice = _synthetic_echo_fixture()
    far_nan = far.copy()
    far_nan[1000] = np.nan
    decision = decide_offline_aec(near, far_nan, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["reason"] == "far_non_finite"
    assert decision["cleaned"] is None


def test_nan_in_near_falls_back_without_crash():
    near, far, _voice = _synthetic_echo_fixture()
    near_nan = near.copy()
    near_nan[500] = np.nan
    decision = decide_offline_aec(near_nan, far, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["reason"] == "near_non_finite"
    assert decision["cleaned"] is None


def test_inf_in_reference_falls_back_without_crash():
    near, far, _voice = _synthetic_echo_fixture()
    far_inf = far.copy()
    far_inf[2000] = np.inf
    decision = decide_offline_aec(near, far_inf, SR)  # must not raise
    assert decision["accepted"] is False
    assert decision["reason"] == "far_non_finite"


def test_empty_input_falls_back_without_crash():
    decision = decide_offline_aec(
        np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32), SR)
    assert decision["accepted"] is False
    assert decision["reason"] == "empty_input"


# ── Sanity check rejects a degenerate result ──────────────────────────

def test_uncorrelated_reference_yields_non_positive_erle_and_is_rejected():
    """far bears no relationship to near at all (pure noise reference
    against independent near-end speech, no echo present) — NLMS has
    nothing real to learn, so ERLE should land at/below 0 dB and the
    sanity check must reject it rather than use a filter that achieved
    nothing (or made things worse)."""
    rng = np.random.default_rng(3)
    n = SR * 4
    near = (rng.standard_normal(n).astype(np.float32) * 0.05)  # pure "voice", no echo
    far = np.random.default_rng(4).standard_normal(n).astype(np.float32) * 0.3
    decision = decide_offline_aec(near, far, SR)
    assert decision["erle_db"] is not None
    assert decision["erle_db"] <= MIN_ACCEPTABLE_ERLE_DB
    assert decision["accepted"] is False
    assert decision["reason"] == "erle_non_positive"
    assert decision["cleaned"] is None


def test_implausibly_clean_result_is_rejected_as_likely_eating_speech():
    """Degenerate case: near IS (a scaled copy of) far with no
    independent voice at all — NLMS can drive the residual to near-zero,
    producing an ERLE far beyond what a real echo-plus-speech mixture
    would ever show. The upper bound must reject this as implausible
    rather than accept a suspiciously-too-good result."""
    rng = np.random.default_rng(5)
    n = SR * 8  # long enough for NLMS to fully converge on this trivial case
    far = rng.standard_normal(n).astype(np.float32) * 0.3
    near = far * 0.5  # pure linear echo, zero independent near-end content
    decision = decide_offline_aec(near, far, SR)
    assert decision["erle_db"] is not None
    assert decision["erle_db"] > MAX_PLAUSIBLE_ERLE_DB
    assert decision["accepted"] is False
    assert decision["reason"] == "erle_implausible"
    assert decision["cleaned"] is None


# ── nlms_filter / compute_erle sanity (thin, catches gross regressions) ─

def test_nlms_filter_output_same_length_as_input():
    near, far, _voice = _synthetic_echo_fixture(duration_s=2.0)
    out = nlms_filter(near, far)
    assert len(out) == len(near)
    assert np.all(np.isfinite(out))


def test_compute_erle_matches_manual_formula():
    near = np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32)
    residual = np.array([0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    overall, _per = compute_erle(near, residual, sr=2, win_s=1.0)
    expected = 10.0 * np.log10(1.0 / 0.01)
    assert overall == pytest.approx(expected, abs=0.01)


# ── File-level wrapper: apply_offline_aec_to_mic_file ─────────────────

def test_file_wrapper_forces_exact_expected_frame_count(tmp_path: Path):
    """Regardless of what NLMS naturally produces, the file wrapper must
    force the written cleaned WAV to EXACTLY `expected_frames` — this is
    what guarantees finalize's output duration never diverges from the
    non-AEC path, even by a rounding sample."""
    near, far, _voice = _synthetic_echo_fixture(duration_s=3.0)
    mic_path = tmp_path / "mic.wav"
    lb_path = tmp_path / "lb16k.wav"
    sf.write(str(mic_path), near, SR, subtype="FLOAT")
    sf.write(str(lb_path), far, SR, subtype="FLOAT")
    out_path = tmp_path / "cleaned.wav"

    deliberately_mismatched = len(near) - 37
    decision = apply_offline_aec_to_mic_file(
        mic_wav_path=str(mic_path),
        loopback_16k_path=str(lb_path),
        target_sr=SR,
        coarse_offset_frames=0,
        expected_frames=deliberately_mismatched,
        out_path=str(out_path),
    )
    assert decision["accepted"] is True, decision["reason"]
    assert decision["out_path"] == str(out_path)
    assert decision["cleaned"] is None  # cleared after write
    assert sf.info(str(out_path)).frames == deliberately_mismatched


def test_file_wrapper_survives_missing_mic_file(tmp_path: Path):
    """A bad path must never raise out of the wrapper — it's the last
    line of defense before finalize's mixing pass."""
    lb_path = tmp_path / "lb16k.wav"
    sf.write(str(lb_path), np.zeros(SR, dtype=np.float32), SR, subtype="FLOAT")
    decision = apply_offline_aec_to_mic_file(
        mic_wav_path=str(tmp_path / "does_not_exist.wav"),
        loopback_16k_path=str(lb_path),
        target_sr=SR,
        coarse_offset_frames=0,
        expected_frames=SR,
        out_path=str(tmp_path / "out.wav"),
    )
    assert decision["accepted"] is False
    assert decision["reason"].startswith("exception:")
    assert not (tmp_path / "out.wav").exists()


def test_file_wrapper_survives_corrupt_loopback_file(tmp_path: Path):
    mic_path = tmp_path / "mic.wav"
    sf.write(str(mic_path), np.zeros(SR, dtype=np.float32), SR, subtype="FLOAT")
    bad_lb = tmp_path / "lb16k.wav"
    bad_lb.write_bytes(b"not a real wav file")
    decision = apply_offline_aec_to_mic_file(
        mic_wav_path=str(mic_path),
        loopback_16k_path=str(bad_lb),
        target_sr=SR,
        coarse_offset_frames=0,
        expected_frames=SR,
        out_path=str(tmp_path / "out.wav"),
    )
    assert decision["accepted"] is False
    assert not (tmp_path / "out.wav").exists()


# ── Production integration: finalize_recording_streaming ─────────────

def test_flag_off_never_invokes_aec_and_output_is_unaffected(
    recordings_dir: Path, monkeypatch,
):
    """With echo_cancellation_enabled left at its default (False), the
    merge must take EXACTLY the pre-existing code path: AEC is never
    even attempted. Proven by making the AEC entry point raise if
    called at all — a passing test means it wasn't."""
    import utils.audio_utils as audio_utils_mod

    def _must_not_be_called(*a, **kw):
        raise AssertionError(
            "apply_offline_aec_to_mic_file must not be called when "
            "echo_cancellation_enabled is False")

    monkeypatch.setattr(
        audio_utils_mod, "apply_offline_aec_to_mic_file", _must_not_be_called)

    mic = write_sine_wav(recordings_dir / "_recording_flag_off.wav",
                         duration_s=2.0, samplerate=48000)
    loopback = write_sine_wav(recordings_dir / "_loopback_flag_off.wav",
                              duration_s=2.0, samplerate=44100)
    out = recordings_dir / "session_flag_off.wav"

    duration_s, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out), loopback_start_offset_s=0.0,
        echo_cancellation_enabled=False,
    )
    assert loopback_mixed is True
    assert duration_s == pytest.approx(2.0, abs=0.05)


def test_flag_off_is_bit_identical_to_flag_omitted(recordings_dir: Path):
    """echo_cancellation_enabled defaults to False — passing it
    explicitly as False must produce byte-identical output to not
    passing it at all."""
    mic = write_sine_wav(recordings_dir / "_recording_bitid.wav",
                         duration_s=2.0, samplerate=48000)
    loopback = write_sine_wav(recordings_dir / "_loopback_bitid.wav",
                              duration_s=2.0, samplerate=44100)
    out_default = recordings_dir / "session_bitid_default.wav"
    out_explicit = recordings_dir / "session_bitid_explicit.wav"

    finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out_default), loopback_start_offset_s=0.0,
    )
    finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out_explicit), loopback_start_offset_s=0.0,
        echo_cancellation_enabled=False,
    )
    assert out_default.read_bytes() == out_explicit.read_bytes()


def test_echo_cancellation_enabled_end_to_end_preserves_duration(
    recordings_dir: Path,
):
    """Full finalize pass with a real echo relationship between mic and
    loopback and the flag ON: must not crash, and — whether or not the
    sanity check ultimately accepts the AEC result — must produce
    EXACTLY the same duration/frame count as the flag-OFF run on the
    same inputs (hard safety requirement: AEC must never change output
    duration)."""
    rng = np.random.default_rng(11)
    n = SR * 5
    far = rng.standard_normal(n).astype(np.float32) * 0.3
    delay = int(0.04 * SR)
    rir = np.zeros(delay + 150, dtype=np.float32)
    rir[delay] = 0.6
    for i in range(delay + 1, len(rir)):
        rir[i] = rir[i - 1] * 0.9 + rng.standard_normal() * 0.005
    echo = fftconvolve(far, rir)[:n].astype(np.float32)
    voice = np.random.default_rng(12).standard_normal(n).astype(np.float32) * 0.05
    mic_signal = (echo + voice).astype(np.float32)

    mic_path = recordings_dir / "_recording_e2e.wav"
    lb_path = recordings_dir / "_loopback_e2e.wav"
    sf.write(str(mic_path), mic_signal, SR, subtype="FLOAT")
    sf.write(str(lb_path), far, SR, subtype="FLOAT")

    out_off = recordings_dir / "session_e2e_off.wav"
    out_on = recordings_dir / "session_e2e_on.wav"

    duration_off, mixed_off = finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
        output_wav_path=str(out_off), target_sr=SR,
        loopback_start_offset_s=0.0, echo_cancellation_enabled=False,
    )
    duration_on, mixed_on = finalize_recording_streaming(
        mic_wav_path=str(mic_path), loopback_wav_path=str(lb_path),
        output_wav_path=str(out_on), target_sr=SR,
        loopback_start_offset_s=0.0, echo_cancellation_enabled=True,
    )

    assert mixed_off is True and mixed_on is True
    assert duration_on == pytest.approx(duration_off, abs=1e-9)
    assert sf.info(str(out_off)).frames == sf.info(str(out_on)).frames

    # No stray AEC scratch temp left behind either way.
    assert list(recordings_dir.glob("_aecmic_*.tmp.wav")) == []


def test_echo_cancellation_with_silent_loopback_falls_back_cleanly(
    recordings_dir: Path,
):
    """Loopback present but silent (nothing playing through the
    speakers) must not crash finalize and must not corrupt the mic —
    reference_silent rejection inside decide_offline_aec."""
    mic = write_sine_wav(recordings_dir / "_recording_silentlb.wav",
                         duration_s=2.0, samplerate=SR)
    loopback = write_silence_wav(recordings_dir / "_loopback_silentlb.wav",
                                 duration_s=2.0, samplerate=SR)
    out = recordings_dir / "session_silentlb.wav"

    duration_s, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out), target_sr=SR,
        loopback_start_offset_s=0.0, echo_cancellation_enabled=True,
    )
    assert duration_s == pytest.approx(2.0, abs=0.05)
    assert list(recordings_dir.glob("_aecmic_*.tmp.wav")) == []
