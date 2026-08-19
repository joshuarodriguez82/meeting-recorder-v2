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

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

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

    results = recover_orphans(
        str(recordings_dir),
        SessionService(str(recordings_dir)),
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"done1": "cleaned_leftover_temps"}
    assert not (recordings_dir / "_recording_done1.wav").exists()
    assert sf.info(str(real_wav)).frames == frames_before  # untouched


def test_sub_1kb_mic_orphan_is_skipped_and_removed(recordings_dir: Path):
    (recordings_dir / "_recording_tiny1.wav").write_bytes(b"RIFF" + b"\x00" * 40)

    results = recover_orphans(
        str(recordings_dir),
        SessionService(str(recordings_dir)),
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"tiny1": "empty_skipped"}
    assert not (recordings_dir / "_recording_tiny1.wav").exists()
    assert not (recordings_dir / "session_tiny1.wav").exists()


def test_failed_merge_preserves_temps_for_manual_recovery(recordings_dir: Path):
    """THE invariant: garbage mic data (>1KB so it passes the size gate,
    but not a WAV) fails the merge — and the temp files MUST survive."""
    corrupt_mic = recordings_dir / "_recording_bad1.wav"
    corrupt_mic.write_bytes(b"not a wav at all" * 256)  # 4 KB of junk

    results = recover_orphans(
        str(recordings_dir),
        SessionService(str(recordings_dir)),
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert list(_statuses(results)) == ["bad1"]
    assert _statuses(results)["bad1"].startswith("merge_failed")
    assert corrupt_mic.exists(), "temp deleted after failed merge — data loss"
    assert not (recordings_dir / "session_bad1.json").exists()


def test_scan_classifies_finalized_vs_unfinalized(recordings_dir: Path):
    write_sine_wav(recordings_dir / "_recording_x1.wav", 1.0, 48000)
    write_sine_wav(recordings_dir / "_recording_x2.wav", 1.0, 48000)
    write_sine_wav(recordings_dir / "session_x2.wav", 1.0, 16000)
    (recordings_dir / "session_x2.json").write_text("{}")

    by_id = {o["session_id"]: o for o in scan_orphans(
        str(recordings_dir),
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )}

    assert by_id["x1"]["already_finalized"] is False
    assert by_id["x2"]["already_finalized"] is True


def test_recovery_finds_orphans_in_local_capture_dir(recordings_dir: Path, tmp_path: Path):
    """v2.10.5 moved active-capture temps off the (possibly cloud-synced)
    recordings_dir into %TEMP%\\meeting_recorder_capture\\. Recovery
    must scan that location too — without it, a backend crash during
    recording leaves temps in the capture dir and they NEVER get
    merged into a session. The 2026-06-15 8B88C1C3 segfault made this
    invisible-recovery hole visible."""
    capture = tmp_path / "fake_capture"
    capture.mkdir()
    # Temps live in the capture dir (v2.10.5+ layout).
    write_sine_wav(capture / "_recording_capdir1.wav",
                   duration_s=2.0, samplerate=48000)
    write_sine_wav(capture / "_loopback_capdir1.wav",
                   duration_s=2.0, samplerate=44100)
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc, capture_dir=str(capture))

    assert _statuses(results) == {"capdir1": "recovered"}
    # Final WAV + JSON land in recordings_dir as before.
    assert (recordings_dir / "session_capdir1.wav").exists()
    assert svc.load("capdir1") is not None
    # Temps deleted from the CAPTURE dir, not recordings_dir.
    assert not (capture / "_recording_capdir1.wav").exists()
    assert not (capture / "_loopback_capdir1.wav").exists()


def test_recovery_preserves_user_metadata_from_stub_json(recordings_dir: Path, tmp_path: Path):
    """v2.11.1 writes a session JSON stub on start_recording so a
    mid-recording / mid-finalize crash doesn't lose the session row.
    When recovery later picks up the orphan temps, the stub's user-
    visible fields (display_name, client, project, notes) must be
    preserved — without this, every crashed-then-recovered session
    would be renamed to "Recovered Session <id>" and lose any
    pre-stop labelling."""
    capture = tmp_path / "fake_capture"
    capture.mkdir()
    write_sine_wav(capture / "_recording_stub1.wav",
                   duration_s=2.0, samplerate=48000)

    # Pre-existing stub (what v2.11.1 start_recording would have written).
    import json as _j
    (recordings_dir / "session_stub1.json").write_text(_j.dumps({
        "session_id": "stub1",
        "display_name": "Important Customer Call",
        "client": "Umbrella",
        "project": "Phase 2 Discovery",
        "notes": "User picked this name before the crash",
        "started_at": "2026-06-15T10:00:00",
        "ended_at": None,
        "audio_path": str(recordings_dir / "session_stub1.wav"),
        "speakers": {},
        "segments": [],
        "screenshots": [],
        "attendees": [],
    }))
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc, capture_dir=str(capture))

    assert _statuses(results) == {"stub1": "recovered"}
    loaded = svc.load("stub1")
    assert loaded is not None
    # User-visible fields preserved from stub.
    assert loaded["display_name"] == "Important Customer Call"
    assert loaded["client"] == "Umbrella"
    assert loaded["project"] == "Phase 2 Discovery"
    assert loaded["notes"] == "User picked this name before the crash"
    # ended_at + audio_path overwritten by recovery.
    assert loaded["ended_at"] is not None
    assert loaded["audio_path"] == str(recordings_dir / "session_stub1.wav")


def test_session_json_with_utf8_bom_loads(recordings_dir: Path):
    """v2.11.1: session JSONs written by external tools (PowerShell's
    Set-Content -Encoding UTF8) include a UTF-8 BOM. The previous
    json.load(open(path, encoding='utf-8')) raised JSONDecodeError on
    the BOM byte and the session silently disappeared from the list.
    SessionService.load now uses utf-8-sig — BOM is tolerated."""
    json_path = recordings_dir / "session_bomtest.json"
    # Hand-write a JSON file with a leading BOM.
    body = '{"session_id":"bomtest","display_name":"BOM Test","audio_path":"x.wav","speakers":{},"segments":[],"screenshots":[],"client":"","project":"","notes":"","attendees":[]}'
    json_path.write_bytes(b'\xef\xbb\xbf' + body.encode('utf-8'))

    svc = SessionService(str(recordings_dir))
    loaded = svc.load("bomtest")
    assert loaded is not None
    assert loaded["session_id"] == "bomtest"
    assert loaded["display_name"] == "BOM Test"


def test_recovery_is_a_noop_on_clean_and_missing_dirs(recordings_dir: Path, tmp_path: Path):
    iso = str(tmp_path / "_unused_capture_isolation")
    assert recover_orphans(
        str(recordings_dir),
        SessionService(str(recordings_dir)),
        capture_dir=iso,
    ) == []
    assert recover_orphans(
        str(tmp_path / "never_existed"),
        SessionService(str(recordings_dir)),
        capture_dir=iso,
    ) == []


# ── crash-mid-finalize recovery (field repro 2026-08-14) ──────────────
#
# Session.finalize_status is written to disk as "finalizing" BEFORE the
# (possibly minutes-long) finalize subprocess is spawned, specifically
# so a crash mid-finalize is visible instead of silently absent. But
# nothing used to clear that marker again on restart — a session could
# report itself as "still finalizing" forever. These tests pin that a
# backend restart during finalize is always resolved by the startup
# recovery pass, never left stuck.

def _write_stub_json(recordings_dir: Path, session_id: str, **overrides) -> None:
    import json as _j
    base = {
        "session_id": session_id,
        "display_name": f"Session {session_id}",
        "started_at": "2026-08-14T10:00:00",
        "ended_at": "2026-08-14T10:30:00",
        "audio_path": str(recordings_dir / f"session_{session_id}.wav"),
        "speakers": {}, "segments": [], "screenshots": [], "attendees": [],
        "client": "", "project": "", "notes": "",
        "finalize_status": "finalizing",
        "finalize_started_at": "2026-08-14T10:51:10",
        "finalize_error": None,
    }
    base.update(overrides)
    (recordings_dir / f"session_{session_id}.json").write_text(_j.dumps(base))


def test_orphan_recovery_clears_stuck_finalizing_marker(recordings_dir: Path):
    """The common case: backend crashes mid-finalize, mic/loopback
    temps survive, restart merges them into a real session. The
    "finalizing" marker left over from before the crash must not
    survive into the recovered session — the audio is right there."""
    write_sine_wav(recordings_dir / "_recording_fin1.wav",
                   duration_s=2.0, samplerate=48000)
    _write_stub_json(recordings_dir, "fin1")
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"fin1": "recovered"}
    loaded = svc.load_full("fin1")
    assert loaded.finalize_status is None
    assert loaded.finalize_started_at is None
    assert loaded.finalize_error is None


def test_already_finalized_crash_clears_stuck_finalizing_marker(recordings_dir: Path):
    """Crash happened AFTER finalize wrote the WAV but BEFORE the
    completed state (finalize_status=None) was persisted — the on-disk
    JSON still says "finalizing" even though the audio is whole. The
    already-finalized / cleaned-leftover-temps branch must still clear
    the stale marker, not just sweep the strays."""
    write_sine_wav(recordings_dir / "_recording_fin2.wav", 1.0, 48000)
    write_sine_wav(recordings_dir / "session_fin2.wav", 5.0, 16000)
    _write_stub_json(recordings_dir, "fin2")
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {"fin2": "cleaned_leftover_temps"}
    loaded = svc.load_full("fin2")
    assert loaded.finalize_status is None


def test_failed_orphan_merge_marks_finalize_failed_instead_of_stuck(recordings_dir: Path):
    """Merge itself fails (corrupt mic temp) — the raw audio genuinely
    can't be recovered. The session must come out of recovery reporting
    "failed" with a reason, never left claiming "finalizing" forever."""
    corrupt_mic = recordings_dir / "_recording_fin3.wav"
    corrupt_mic.write_bytes(b"not a wav at all" * 256)
    _write_stub_json(recordings_dir, "fin3")
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert list(_statuses(results).values())[0].startswith("merge_failed")
    loaded = svc.load_full("fin3")
    assert loaded.finalize_status == "failed"
    assert loaded.finalize_error is not None
    assert corrupt_mic.exists()  # still preserved for manual recovery


def test_stuck_finalizing_sweep_clears_marker_when_audio_already_complete(
    recordings_dir: Path,
):
    """Narrowest race: finalize succeeded, the subprocess's temp files
    were already deleted, but the backend died before the completed
    state (finalize_status=None) reached disk. No orphan temp survives
    for scan_orphans() to find at all — the sweep at the end of
    recover_orphans() must still find this session by its
    finalize_status alone and see that the audio is right there."""
    write_sine_wav(recordings_dir / "session_fin4.wav", 5.0, 16000)
    _write_stub_json(recordings_dir, "fin4")
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {
        "fin4": "finalize_marker_cleared_audio_present"}
    loaded = svc.load_full("fin4")
    assert loaded.finalize_status is None


def test_stuck_finalizing_sweep_marks_failed_when_nothing_recoverable(
    recordings_dir: Path,
):
    """Worst case for this narrow race: no orphan temp, no completed
    WAV either — genuinely nothing to recover from. Must resolve to
    "failed" with an explanatory reason, never sit as "finalizing"
    forever with no process left running to ever clear it."""
    _write_stub_json(recordings_dir, "fin5")
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert _statuses(results) == {
        "fin5": "finalize_marked_failed_no_recovery"}
    loaded = svc.load_full("fin5")
    assert loaded.finalize_status == "failed"
    assert loaded.finalize_error is not None


def test_recovery_leaves_non_finalizing_sessions_untouched(recordings_dir: Path):
    """A session with no finalize history at all (finalize_status is
    None, the common case for every session that predates this
    feature, or that never had a finalize in flight) must not be
    touched by the sweep — it isn't stuck, there's nothing to resolve."""
    _write_stub_json(recordings_dir, "fin6", finalize_status=None,
                     finalize_started_at=None)
    svc = SessionService(str(recordings_dir))

    results = recover_orphans(
        str(recordings_dir), svc,
        capture_dir=str(recordings_dir / "_unused_capture_isolation"),
    )

    assert results == []
    loaded = svc.load_full("fin6")
    assert loaded.finalize_status is None
