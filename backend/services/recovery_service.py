"""
Startup crash-recovery for interrupted recordings.

If the backend was killed (OS crash, power loss, STATUS_ACCESS_VIOLATION,
force-quit) during stop_recording, temp files `_recording_<ID>.wav` and
`_loopback_<ID>.wav` are left behind with no `session_<ID>.wav` to back
them. Without recovery, users lose the session entirely.

On each backend startup we scan TWO locations for orphan temps:

  1. The user's ``recordings_dir`` — legacy location where v2.10.4 and
     earlier wrote streaming-capture temps.
  2. ``%TEMP%\\meeting_recorder_capture\\`` — current location (v2.10.5+)
     for the streaming-capture temps, moved off cloud-synced volumes
     because the filter driver was stalling the audio thread.

Each orphan gets merged with the same streaming path used by
stop_recording. A stub session JSON is written so the recording appears
in the Session Browser ready to be transcribed.

Also resolves sessions left with ``Session.finalize_status ==
"finalizing"`` by a backend that died mid-finalize (field repro
2026-08-14): that marker is written to disk BEFORE the finalize
subprocess is spawned specifically so a crash mid-finalize is visible
rather than silently absent, but nothing was clearing it again on
restart — a session could stay "finalizing" forever. ``recover_orphans``
now resolves every such session it encounters (via the orphan loop when
a temp survives, or the stuck-finalizing sweep at the end of the
function when it doesn't): audio present -> marker cleared, audio
genuinely unrecoverable -> marker flipped to "failed" with a reason.
"""

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from models.session import Session
from services.session_service import SessionService
from utils.audio_utils import (
    finalize_recording_streaming,
    wav_byte_implied_duration,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _local_capture_dir() -> Path:
    """Same dir name as ``services.recording_service._local_capture_dir``
    — kept in sync by convention. Duplicated here so this module has no
    circular dep on recording_service (which depends on Session +
    diarization + transcription engines, all heavy)."""
    return Path(tempfile.gettempdir()) / "meeting_recorder_capture"


def scan_orphans(
    recordings_dir: str,
    *,
    capture_dir: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return a list of orphan recordings as {session_id, mic, loopback,
    already_finalized}.

    Scans both ``recordings_dir`` (legacy) and the local capture temp
    dir (current, since v2.10.5). De-dupes on session_id — if the same
    session has temps in both locations (shouldn't happen but cheap to
    guard) the local-capture-dir copy wins because that's where active
    recordings write.

    ``capture_dir`` is the local-only capture temp dir. Defaults to the
    production location (``%TEMP%\\meeting_recorder_capture\\``). Tests
    pass a tmp-path to isolate from any stale files left there by real
    recordings on the dev box."""
    orphans_by_sid: Dict[str, Dict[str, str]] = {}

    def _add_from(scan_dir: Path) -> None:
        if not scan_dir.exists():
            return
        recs_path = Path(recordings_dir)
        for mic_temp in scan_dir.glob("_recording_*.wav"):
            sid = mic_temp.stem.replace("_recording_", "")
            final_wav = recs_path / f"session_{sid}.wav"
            final_json = recs_path / f"session_{sid}.json"
            lb_temp = scan_dir / f"_loopback_{sid}.wav"
            # "Already finalized" means the merged session WAV exists
            # AND a non-stub session JSON is present. A stub JSON
            # (audio_path set but the WAV file doesn't exist yet) means
            # finalize never completed — recovery still needs to run.
            audio_complete = final_wav.exists()
            json_complete = (
                final_json.exists() and audio_complete
            )
            orphans_by_sid[sid] = {
                "session_id": sid,
                "mic": str(mic_temp),
                "loopback": str(lb_temp) if lb_temp.exists() else "",
                "already_finalized": bool(audio_complete and json_complete),
            }

    _add_from(Path(recordings_dir))
    _add_from(Path(capture_dir) if capture_dir else _local_capture_dir())
    return list(orphans_by_sid.values())


def recover_orphans(
    recordings_dir: str,
    session_svc: SessionService,
    target_sr: int = 16000,
    *,
    capture_dir: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Merge orphan temp WAVs into real sessions and write a stub JSON so each
    appears in the Session Browser. Safe to call on every startup — it's a
    no-op when there are no orphans.

    Returns a list of `{session_id, audio_path, duration_s, status}` for
    every orphan encountered.
    """
    results: List[Dict[str, str]] = []
    recs_path = Path(recordings_dir)
    if not recs_path.exists():
        return results

    # Every session_id this pass touches — used below to find sessions
    # left marked "finalizing" that scan_orphans() didn't even see (no
    # temp file survived), so they don't get missed by the orphan loop.
    handled_sids: set = set()

    for orphan in scan_orphans(recordings_dir, capture_dir=capture_dir):
        sid = orphan["session_id"]
        handled_sids.add(sid)
        mic = orphan["mic"]
        lb = orphan["loopback"] or None

        if orphan.get("already_finalized"):
            # Session is whole; just purge leftover temps.
            _safe_unlink(mic)
            if lb:
                _safe_unlink(lb)
            _clear_stale_finalizing_marker(session_svc, sid)
            results.append({"session_id": sid, "status": "cleaned_leftover_temps"})
            continue

        # Skip truly empty mic files (recording never got any chunks)
        try:
            mic_size = Path(mic).stat().st_size
        except OSError:
            mic_size = 0
        if mic_size < 1024:
            logger.info(
                f"Orphan mic file {Path(mic).name} is empty — removing")
            _safe_unlink(mic)
            if lb:
                _safe_unlink(lb)
            _mark_finalize_failed_if_stuck(
                session_svc, sid,
                "The backend restarted before this recording captured any "
                "usable audio.")
            results.append({"session_id": sid, "status": "empty_skipped"})
            continue

        final_wav = str(recs_path / f"session_{sid}.wav")
        try:
            logger.info(
                f"Recovering orphan session {sid} "
                f"(mic={_fmt_size(mic_size)}"
                + (f", loopback={_fmt_size(Path(lb).stat().st_size)}"
                   if lb else "")
                + ")"
            )
            duration_s, _ = finalize_recording_streaming(
                mic_wav_path=mic,
                loopback_wav_path=lb,
                output_wav_path=final_wav,
                target_sr=target_sr,
            )
        except Exception as e:
            logger.error(
                f"Could not merge orphan session {sid}: {e} — temp files "
                f"left on disk for manual recovery"
            )
            _mark_finalize_failed_if_stuck(
                session_svc, sid,
                f"The backend restarted mid-finalize and automatic "
                f"recovery of the raw capture failed: {e}")
            results.append({"session_id": sid, "status": f"merge_failed: {e}"})
            continue

        # Truncation tripwire. The merged output should be about as long
        # as the mic temp's on-disk bytes imply. If it came out
        # dramatically shorter, the merge dropped audio (e.g. a header
        # we couldn't repair) — DELETE the short output and KEEP the
        # temps, rather than replacing the only full-length copy with a
        # fragment and then unlinking the source. This is the invariant
        # that would have saved session 191D826D: 20+ min of mic bytes,
        # a 1-min merge, source deleted.
        mic_implied_s = wav_byte_implied_duration(mic)
        if mic_implied_s > 5.0 and duration_s < mic_implied_s * 0.9 - 1.0:
            logger.error(
                f"Merged {sid}.wav is {duration_s:.1f}s but the mic temp's "
                f"bytes imply {mic_implied_s:.1f}s — refusing to discard the "
                f"source. KEEPING temp files for manual recovery; dropping "
                f"the truncated merge."
            )
            _safe_unlink(final_wav)
            _mark_finalize_failed_if_stuck(
                session_svc, sid,
                "The backend restarted mid-finalize and the recovered "
                "audio came out shorter than the raw capture implied — "
                "kept the original capture on disk for manual recovery "
                "instead of the truncated merge.")
            results.append({
                "session_id": sid,
                "status": "kept_source_duration_mismatch",
            })
            continue

        # Build a stub Session so SessionService.save writes JSON in the
        # exact on-disk format used by finalized sessions.
        try:
            mic_mtime = datetime.datetime.fromtimestamp(
                Path(mic).stat().st_mtime)
        except OSError:
            mic_mtime = datetime.datetime.now()
        ended_at = mic_mtime
        started_at = mic_mtime - datetime.timedelta(seconds=duration_s)

        # If a stub JSON already exists from v2.11.1's "JSON-first"
        # writes (set on start_recording / before finalize), preserve the
        # user-visible fields the user/UI may have already populated
        # (display_name, client, project, notes, attendees, template,
        # speaker renames). Without this, every crashed-then-recovered
        # session would be renamed back to "Recovered Session <id>" and
        # lose any pre-stop labelling the user did.
        session: Session = session_svc.load_full(sid) or Session(session_id=sid)
        if not getattr(session, "display_name", "") or session.display_name.strip() == "":
            session.display_name = f"Recovered Session {sid}"
        if not getattr(session, "started_at", None):
            session.started_at = started_at
        session.ended_at = ended_at
        session.audio_path = final_wav
        # CRASH-MID-FINALIZE RECOVERY: if the backend died while this
        # session's finalize_status was "finalizing" (or a prior
        # "failed" attempt), this fresh merge just produced a real,
        # complete WAV at final_wav — the in-progress/failed marker is
        # now stale and would otherwise leave the session reporting
        # itself as stuck finalizing (or failed) forever, even though
        # /sessions/{id}/process would work fine. Clear it.
        session.finalize_status = None
        session.finalize_started_at = None
        session.finalize_error = None
        try:
            session_svc.save(session)
        except Exception as e:
            logger.error(
                f"Merged {sid}.wav but couldn't write session JSON: {e}"
            )
            results.append({
                "session_id": sid,
                "audio_path": final_wav,
                "status": f"json_save_failed: {e}",
            })
            continue

        # Only delete temps AFTER both the wav and json landed.
        _safe_unlink(mic)
        if lb:
            _safe_unlink(lb)

        logger.info(
            f"Recovered session {sid}: {duration_s:.1f}s → {final_wav}"
        )
        results.append({
            "session_id": sid,
            "audio_path": final_wav,
            "duration_s": f"{duration_s:.1f}",
            "status": "recovered",
        })

    # STUCK-FINALIZING SWEEP (field repro 2026-08-14): the loop above only
    # resolves sessions scan_orphans() found a leftover temp WAV for. A
    # narrower race is also possible: the finalize subprocess already
    # returned successfully and wrote the final session_<id>.wav, the
    # `finally` block in stop_recording() already deleted the temps, and
    # the backend crashed in the brief window BETWEEN that and
    # server.py's session_svc.save(session) call that would have
    # persisted the completed state. The on-disk JSON in that window
    # still says finalize_status="finalizing" (from the pre-finalize
    # stub write) and scan_orphans() sees nothing at all — no temp file
    # survives for it to report. Without this pass such a session would
    # report itself as "still finalizing" forever, even though the audio
    # is sitting right there, complete, on disk.
    #
    # Walk every session still claiming an in-flight finalize_status
    # ("finalizing", or "queued" — a session that crashed while waiting
    # behind another finalize on the process-wide gate, see utils/
    # finalize_gate.py, never even started its own subprocess but is
    # exactly as stuck-looking to a reader) that the orphan loop above
    # didn't already touch, and resolve it directly from what's
    # actually on disk:
    #   - final WAV present  -> finalize actually succeeded; clear the
    #     stale marker.
    #   - final WAV absent   -> genuinely crashed mid-finalize (or while
    #     still queued) with nothing left to recover from; mark it
    #     failed (with a reason) instead of leaving it stuck.
    _IN_FLIGHT_STATES = ("finalizing", "queued")
    try:
        for row in session_svc.list_sessions():
            sid = row.get("session_id")
            if not sid or sid in handled_sids:
                continue
            if row.get("finalize_status") not in _IN_FLIGHT_STATES:
                continue
            handled_sids.add(sid)
            session = session_svc.load_full(sid)
            if session is None or session.finalize_status not in _IN_FLIGHT_STATES:
                continue
            audio_path = session.audio_path
            if audio_path and Path(audio_path).exists():
                logger.warning(
                    f"Session {sid} was left marked "
                    f"'{session.finalize_status}' by a backend restart, "
                    f"but its audio file is present and complete at "
                    f"{audio_path} — finalize actually succeeded; "
                    f"clearing the stale marker."
                )
                session.finalize_status = None
                session.finalize_started_at = None
                session.finalize_error = None
                session_svc.save(session)
                results.append({
                    "session_id": sid,
                    "status": "finalize_marker_cleared_audio_present",
                })
            else:
                logger.error(
                    f"Session {sid} was left marked "
                    f"'{session.finalize_status}' by a backend restart "
                    f"and no audio was recoverable (no orphan temp "
                    f"found, no completed WAV on disk) — marking "
                    f"finalize failed instead of leaving it stuck."
                )
                session.finalize_status = "failed"
                session.finalize_error = (
                    "The backend restarted while this recording was "
                    "finalizing (or queued behind another finalize) and "
                    "no raw capture could be found to recover from."
                )
                session_svc.save(session)
                results.append({
                    "session_id": sid,
                    "status": "finalize_marked_failed_no_recovery",
                })
    except Exception as e:
        logger.exception(f"Stuck-finalizing sweep failed: {e}")

    # Also clean up ancient orphan `_lb16k_*.tmp.wav` files from aborted
    # pre-resample passes — they're always disposable. Scan both
    # legacy (recordings_dir) and current (%TEMP%) locations; the temp
    # moved to %TEMP% in v2.11.1 alongside the segfault fix.
    cap_path = Path(capture_dir) if capture_dir else _local_capture_dir()
    for parent in (recs_path, cap_path):
        if not parent.exists():
            continue
        for leftover in parent.glob("_lb16k_*.tmp.wav"):
            _safe_unlink(str(leftover))

    return results


def _clear_stale_finalizing_marker(session_svc: SessionService, sid: str) -> None:
    """If ``sid``'s persisted session is still marked finalize_status ==
    "finalizing" (or a stale "failed"), and this function is being
    called from a branch that has independently established the audio
    is whole (``already_finalized`` in scan_orphans, or a fresh
    successful merge), clear the marker so /sessions/{id}/process and
    the UI stop reporting a finalize that is not actually in flight.

    Best-effort: a failure here must never abort the surrounding
    recovery pass."""
    try:
        session = session_svc.load_full(sid)
        if session is None:
            return
        if session.finalize_status is None and session.finalize_error is None:
            return
        session.finalize_status = None
        session.finalize_started_at = None
        session.finalize_error = None
        session_svc.save(session)
    except Exception as e:
        logger.warning(f"Could not clear finalize marker for {sid}: {e}")


def _mark_finalize_failed_if_stuck(
    session_svc: SessionService, sid: str, reason: str
) -> None:
    """If ``sid``'s persisted session is marked finalize_status ==
    "finalizing" OR "queued" (crashed while waiting behind another
    finalize on the process-wide gate — see utils/finalize_gate.py —
    before its own subprocess ever started), this branch has just
    established the raw audio could NOT be recovered (empty capture,
    merge failure, or a truncated merge we refused to keep) — flip it
    to "failed" with ``reason`` instead of leaving it on disk forever
    with nothing left running to ever clear it.

    No-ops if the session was never marked finalizing/queued (e.g. this
    orphan predates the finalize-state feature, or crashed before
    finalize ever started) — those cases are unrelated to this failure
    mode and already handled by the pre-existing ghost-session / audio-
    integrity machinery.

    Best-effort: a failure here must never abort the surrounding
    recovery pass."""
    try:
        session = session_svc.load_full(sid)
        if session is None or session.finalize_status not in (
                "finalizing", "queued"):
            return
        session.finalize_status = "failed"
        session.finalize_error = reason
        session_svc.save(session)
    except Exception as e:
        logger.warning(f"Could not mark finalize failed for {sid}: {e}")


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
