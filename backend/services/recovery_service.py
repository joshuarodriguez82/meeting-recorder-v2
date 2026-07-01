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

    for orphan in scan_orphans(recordings_dir, capture_dir=capture_dir):
        sid = orphan["session_id"]
        mic = orphan["mic"]
        lb = orphan["loopback"] or None

        if orphan.get("already_finalized"):
            # Session is whole; just purge leftover temps.
            _safe_unlink(mic)
            if lb:
                _safe_unlink(lb)
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
