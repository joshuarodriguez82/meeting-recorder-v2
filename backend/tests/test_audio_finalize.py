"""
finalize_recording_streaming is the single point every recording — live
stop AND crash recovery — funnels through on its way to disk. A bug here
is a lost meeting, so these tests pin its observable contract:
output format, duration math, loopback alignment, and every degraded-
input fallback the docstring promises.
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from utils.audio_utils import (
    TARGET_SAMPLE_RATE,
    finalize_recording_streaming,
    mix_stereo_to_mono,
    resample_to_16k,
)
from conftest import rms, write_silence_wav, write_sine_wav


def test_mic_only_merge_produces_16k_mono_pcm16(recordings_dir: Path):
    mic = write_sine_wav(recordings_dir / "_recording_a.wav",
                         duration_s=3.0, samplerate=48000)
    out = recordings_dir / "session_a.wav"

    duration_s, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=None,
        output_wav_path=str(out))

    assert loopback_mixed is False
    info = sf.info(str(out))
    assert info.samplerate == TARGET_SAMPLE_RATE
    assert info.channels == 1
    assert info.subtype == "PCM_16"
    assert duration_s == pytest.approx(3.0, abs=0.05)
    assert info.frames / info.samplerate == pytest.approx(3.0, abs=0.05)


def test_stereo_mic_is_downmixed(recordings_dir: Path):
    mic = write_sine_wav(recordings_dir / "_recording_st.wav",
                         duration_s=1.0, samplerate=44100, channels=2)
    out = recordings_dir / "session_st.wav"

    duration_s, _ = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=None,
        output_wav_path=str(out))

    assert sf.info(str(out)).channels == 1
    assert duration_s == pytest.approx(1.0, abs=0.05)


def test_missing_mic_raises_runtime_error(recordings_dir: Path):
    with pytest.raises(RuntimeError, match="not found"):
        finalize_recording_streaming(
            mic_wav_path=str(recordings_dir / "nope.wav"),
            loopback_wav_path=None,
            output_wav_path=str(recordings_dir / "out.wav"))


def test_empty_mic_raises_runtime_error(recordings_dir: Path):
    mic = recordings_dir / "_recording_empty.wav"
    sf.write(str(mic), np.zeros(0, dtype=np.float32), 48000, subtype="FLOAT")
    with pytest.raises(RuntimeError, match="empty"):
        finalize_recording_streaming(
            mic_wav_path=str(mic), loopback_wav_path=None,
            output_wav_path=str(recordings_dir / "out.wav"))


def test_header_only_loopback_falls_back_to_mic_only(recordings_dir: Path):
    """A <1KB loopback file (WASAPI opened the writer, no frames ever
    arrived) must not reach libsndfile — it has segfaulted the process
    on such files. The contract is a clean mic-only merge."""
    mic = write_sine_wav(recordings_dir / "_recording_b.wav",
                         duration_s=2.0, samplerate=48000)
    stub_loopback = recordings_dir / "_loopback_b.wav"
    stub_loopback.write_bytes(b"RIFF" + b"\x00" * 40)  # 44-byte header shell
    out = recordings_dir / "session_b.wav"

    duration_s, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(stub_loopback),
        output_wav_path=str(out))

    assert loopback_mixed is False
    assert duration_s == pytest.approx(2.0, abs=0.05)


def test_wallclock_offset_places_loopback_audio_at_offset(recordings_dir: Path):
    """Silent 10s mic + 2s loopback tone at a 5s wallclock anchor →
    energy must sit in [5s, 7s] of the merged file and nowhere before."""
    mic = write_silence_wav(recordings_dir / "_recording_c.wav",
                            duration_s=10.0, samplerate=48000)
    loopback = write_sine_wav(recordings_dir / "_loopback_c.wav",
                              duration_s=2.0, samplerate=44100)
    out = recordings_dir / "session_c.wav"

    _, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out), loopback_start_offset_s=5.0)

    assert loopback_mixed is True
    merged, sr = sf.read(str(out), dtype="float32")
    assert rms(merged[0:int(4.5 * sr)]) < 0.01          # pre-offset: silence
    assert rms(merged[int(5.2 * sr):int(6.8 * sr)]) > 0.1  # tone window
    assert rms(merged[int(7.5 * sr):]) < 0.01           # post-tone: silence


def test_no_offset_falls_back_to_right_alignment(recordings_dir: Path):
    """Legacy/recovery path: with no clock anchor, loopback right-aligns
    against mic length — tone lands in the FINAL 2s of a 10s mic."""
    mic = write_silence_wav(recordings_dir / "_recording_d.wav",
                            duration_s=10.0, samplerate=48000)
    loopback = write_sine_wav(recordings_dir / "_loopback_d.wav",
                              duration_s=2.0, samplerate=44100)
    out = recordings_dir / "session_d.wav"

    _, loopback_mixed = finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(out), loopback_start_offset_s=None)

    assert loopback_mixed is True
    merged, sr = sf.read(str(out), dtype="float32")
    assert rms(merged[0:int(7.5 * sr)]) < 0.01
    assert rms(merged[int(8.2 * sr):int(9.8 * sr)]) > 0.1


def test_finalize_cleans_up_its_resample_temp(recordings_dir: Path):
    """The pass-1 _lb16k_*.tmp.wav scratch file must never outlive the
    merge — leftovers are exactly what recovery has to garbage-collect."""
    mic = write_sine_wav(recordings_dir / "_recording_e.wav",
                         duration_s=1.0, samplerate=48000)
    loopback = write_sine_wav(recordings_dir / "_loopback_e.wav",
                              duration_s=1.0, samplerate=44100)
    finalize_recording_streaming(
        mic_wav_path=str(mic), loopback_wav_path=str(loopback),
        output_wav_path=str(recordings_dir / "session_e.wav"),
        loopback_start_offset_s=0.0)

    assert list(recordings_dir.glob("_lb16k_*.tmp.wav")) == []


def test_resample_to_16k_length_and_passthrough():
    one_second_48k = np.zeros(48000, dtype=np.float32)
    assert len(resample_to_16k(one_second_48k, 48000)) == 16000
    already_16k = np.zeros(16000, dtype=np.float32)
    assert len(resample_to_16k(already_16k, 16000)) == 16000


def test_mix_stereo_to_mono_shapes():
    stereo = np.ones((100, 2), dtype=np.float32)
    assert mix_stereo_to_mono(stereo).shape == (100,)
    mono = np.ones(100, dtype=np.float32)
    assert mix_stereo_to_mono(mono).shape == (100,)
