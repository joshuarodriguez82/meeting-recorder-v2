"""
Truncated-WAV-header recovery.

When the app is killed mid-recording (crash, installer launch, power
loss), the streaming capture WAV is left with a header whose declared
length was never finalized — it reports a fraction of the audio that is
physically on disk. Before this fix, recovery trusted that header,
merged the short length, and then DELETED the temp that still held the
full recording. Field repro: session 191D826D, 2026-06-30 — 20+ minutes
captured, 1 minute survived.

These tests pin:
  1. repair_truncated_wav_header restores the real length in place and
     is a no-op on cleanly-closed files.
  2. wav_byte_implied_duration reports the byte-derived length, ignoring
     a lying header.
  3. recovery repairs a truncated orphan and produces the FULL merge.
  4. the truncation tripwire keeps the temps (and drops the short merge)
     if a merge still comes out far shorter than the source bytes imply.
"""

import struct
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from services.recovery_service import recover_orphans
from services.session_service import SessionService
from utils.audio_utils import (
    _wav_layout,
    repair_truncated_wav_header,
    wav_byte_implied_duration,
)
from conftest import write_sine_wav


def _statuses(results):
    return {r["session_id"]: r["status"] for r in results}


def _write_pcm16_wav(path: Path, duration_s: float, samplerate: int) -> Path:
    t = np.arange(int(duration_s * samplerate)) / samplerate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), tone, samplerate, subtype="PCM_16")
    return path


def _lie_about_length(path: Path, keep_frames: int) -> None:
    """Rewrite a WAV's data + RIFF size fields to claim only
    `keep_frames`, leaving the full PCM on disk — exactly what a
    mid-write kill leaves behind."""
    layout = _wav_layout(str(path))
    assert layout is not None
    ba = layout["block_align"]
    fake_data = keep_frames * ba
    with open(path, "r+b") as f:
        f.seek(layout["data_offset"] - 4)
        f.write(struct.pack("<I", fake_data))
        f.seek(4)
        f.write(struct.pack("<I", (layout["data_offset"] - 8) + fake_data))


def test_repair_restores_truncated_header(tmp_path: Path):
    p = _write_pcm16_wav(tmp_path / "mic.wav", duration_s=4.0, samplerate=48000)
    full_frames = sf.info(str(p)).frames

    _lie_about_length(p, keep_frames=48000 // 5)  # claim 0.2 s of 4 s
    assert sf.info(str(p)).frames < full_frames // 2  # header now lies

    rep = repair_truncated_wav_header(str(p))
    assert rep["repaired"] is True
    # Full audio readable again — within one frame of the original.
    assert sf.info(str(p)).frames >= full_frames - 1


def test_repair_is_noop_on_clean_wav(tmp_path: Path):
    p = _write_pcm16_wav(tmp_path / "clean.wav", duration_s=2.0, samplerate=48000)
    frames = sf.info(str(p)).frames

    rep = repair_truncated_wav_header(str(p))
    assert rep["repaired"] is False
    assert rep["reason"] == "header_ok"
    assert sf.info(str(p)).frames == frames  # untouched


def test_repair_tolerates_non_wav(tmp_path: Path):
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav at all" * 256)
    rep = repair_truncated_wav_header(str(junk))
    assert rep["repaired"] is False


def test_byte_implied_duration_ignores_lying_header(tmp_path: Path):
    p = _write_pcm16_wav(tmp_path / "x.wav", duration_s=4.0, samplerate=48000)
    _lie_about_length(p, keep_frames=48000)  # header claims 1 s

    assert sf.info(str(p)).frames <= 48000 + 1              # header lies: ~1 s
    assert wav_byte_implied_duration(str(p)) == pytest.approx(4.0, abs=0.1)


def test_recovery_repairs_truncated_orphan_and_keeps_full_audio(
    recordings_dir: Path,
):
    mic = _write_pcm16_wav(
        recordings_dir / "_recording_trunc1.wav", duration_s=6.0,
        samplerate=48000)
    _lie_about_length(mic, keep_frames=48000)  # header claims 1 s of 6 s
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"trunc1": "recovered"}
    final = recordings_dir / "session_trunc1.wav"
    assert final.exists()
    merged_s = sf.info(str(final)).frames / sf.info(str(final)).samplerate
    # The whole ~6 s recovered, not the 1 s the broken header advertised.
    assert merged_s > 5.0
    assert not (recordings_dir / "_recording_trunc1.wav").exists()


def test_tripwire_keeps_temps_when_merge_truncates(
    recordings_dir: Path, monkeypatch,
):
    """Belt-and-suspenders: if a merge still comes out far shorter than
    the mic bytes imply (a header we couldn't repair), recovery must
    KEEP the temps and DROP the short merge — never replace the only
    full copy with a fragment."""
    write_sine_wav(recordings_dir / "_recording_g1.wav",
                   duration_s=30.0, samplerate=48000)

    import services.recovery_service as rs

    def fake_finalize(mic_wav_path, loopback_wav_path, output_wav_path,
                      target_sr=16000, **kwargs):
        # Simulate a truncated merge: write ~1 s and report 1 s.
        sf.write(output_wav_path,
                 np.zeros(target_sr, dtype=np.float32),
                 target_sr, subtype="PCM_16")
        return 1.0, False

    monkeypatch.setattr(rs, "finalize_recording_streaming", fake_finalize)

    results = recover_orphans(
        str(recordings_dir), SessionService(str(recordings_dir)),
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"g1": "kept_source_duration_mismatch"}
    # The source temp survives for manual recovery ...
    assert (recordings_dir / "_recording_g1.wav").exists()
    # ... and the truncated merge + any stub JSON are gone.
    assert not (recordings_dir / "session_g1.wav").exists()
    assert not (recordings_dir / "session_g1.json").exists()
