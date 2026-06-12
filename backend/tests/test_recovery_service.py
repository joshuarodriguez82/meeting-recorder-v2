"""
recovery_service is the last line between a crash and a lost meeting.
These tests pin the four startup outcomes — recovered, leftover-temp
cleanup, empty-skip, merge-failure — and the single most important
invariant in the module: when a merge fails, the temp WAVs are LEFT ON
DISK for manual recovery, never deleted.
"""

from pathlib import Path

import soundfile as sf

from services.recovery_service import recover_orphans, scan_orphans
from services.session_service import SessionService
from conftest import write_sine_wav


def _statuses(results):
    return {r["session_id"]: r["status"] for r in results}


def test_orphan_temps_become_a_recoverable_session(recordings_dir: Path):
    write_sine_wav(recordings_dir / "_recording_abc123.wav",
                   duration_s=2.0, samplerate=48000)
    write_sine_wav(recordings_dir / "_loopback_abc123.wav",
                   duration_s=2.0, samplerate=44100)
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(str(recordings_dir), svc)

    assert _statuses(results) == {"abc123": "recovered"}
    final_wav = recordings_dir / "session_abc123.wav"
    assert final_wav.exists()
    assert sf.info(str(final_wav)).frames > 0
    stub = svc.load("abc123")
    assert stub is not None
    assert stub["display_name"] == "Recovered Session abc123"
    assert stub["audio_path"] == str(final_wav)
    # Temps gone only AFTER both wav and json landed.
    assert not (recordings_dir / "_recording_abc123.wav").exists()
    assert not (recordings_dir / "_loopback_abc123.wav").exists()


def test_already_finalized_session_only_loses_its_stray_temps(recordings_dir: Path):
    """Crash AFTER finalize but before temp cleanup: the real session is
    whole, so recovery must only sweep the strays — never re-merge."""
    write_sine_wav(recordings_dir / "_recording_done1.wav",
                   duration_s=1.0, samplerate=48000)
    real_wav = write_sine_wav(recordings_dir / "session_done1.wav",
                              duration_s=5.0, samplerate=16000)
    (recordings_dir / "session_done1.json").write_text("{}")
    frames_before = sf.info(str(real_wav)).frames

    results = recover_orphans(str(recordings_dir), SessionService(str(recordings_dir)))

    assert _statuses(results) == {"done1": "cleaned_leftover_temps"}
    assert not (recordings_dir / "_recording_done1.wav").exists()
    assert sf.info(str(real_wav)).frames == frames_before  # untouched


def test_sub_1kb_mic_orphan_is_skipped_and_removed(recordings_dir: Path):
    (recordings_dir / "_recording_tiny1.wav").write_bytes(b"RIFF" + b"\x00" * 40)

    results = recover_orphans(str(recordings_dir), SessionService(str(recordings_dir)))

    assert _statuses(results) == {"tiny1": "empty_skipped"}
    assert not (recordings_dir / "_recording_tiny1.wav").exists()
    assert not (recordings_dir / "session_tiny1.wav").exists()


def test_failed_merge_preserves_temps_for_manual_recovery(recordings_dir: Path):
    """THE invariant: garbage mic data (>1KB so it passes the size gate,
    but not a WAV) fails the merge — and the temp files MUST survive."""
    corrupt_mic = recordings_dir / "_recording_bad1.wav"
    corrupt_mic.write_bytes(b"not a wav at all" * 256)  # 4 KB of junk

    results = recover_orphans(str(recordings_dir), SessionService(str(recordings_dir)))

    assert list(_statuses(results)) == ["bad1"]
    assert _statuses(results)["bad1"].startswith("merge_failed")
    assert corrupt_mic.exists(), "temp deleted after failed merge — data loss"
    assert not (recordings_dir / "session_bad1.json").exists()


def test_scan_classifies_finalized_vs_unfinalized(recordings_dir: Path):
    write_sine_wav(recordings_dir / "_recording_x1.wav", 1.0, 48000)
    write_sine_wav(recordings_dir / "_recording_x2.wav", 1.0, 48000)
    write_sine_wav(recordings_dir / "session_x2.wav", 1.0, 16000)
    (recordings_dir / "session_x2.json").write_text("{}")

    by_id = {o["session_id"]: o for o in scan_orphans(str(recordings_dir))}

    assert by_id["x1"]["already_finalized"] is False
    assert by_id["x2"]["already_finalized"] is True


def test_recovery_is_a_noop_on_clean_and_missing_dirs(recordings_dir: Path, tmp_path: Path):
    assert recover_orphans(str(recordings_dir), SessionService(str(recordings_dir))) == []
    assert recover_orphans(str(tmp_path / "never_existed"),
                           SessionService(str(recordings_dir))) == []
