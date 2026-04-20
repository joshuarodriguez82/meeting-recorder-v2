"""
Startup crash-recovery for interrupted recordings.

If the backend was killed (OS crash, power loss, STATUS_ACCESS_VIOLATION,
force-quit) during stop_recording, temp files `_recording_<ID>.wav` and
`_loopback_<ID>.wav` are left behind with no `session_<ID>.wav` / `.json`
to back them. Without recovery, users lose the session entirely.

On each backend startup we scan the recordings dir for such orphans, merge
them with the same streaming path used by stop_recording, and write a
stub session JSON so the recording appears in the Session Browser ready
to be transcribed.
"""

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List

from models.session import Session
from services.session_service import SessionService
from utils.audio_utils import finalize_recording_streaming
from utils.logger import get_logger

logger = get_logger(__name__)


def scan_orphans(recordings_dir: str) -> List[Dict[str, str]]:
    """Return a list of orphan recordings as {session_id, mic, loopback}."""
    path = Path(recordings_dir)
    if not path.exists():
        return []
    orphans: List[Dict[str, str]] = []
    for mic_temp in path.glob("_recording_*.wav"):
        sid = mic_temp.stem.replace("_recording_", "")
        final_wav = path / f"session_{sid}.wav"
        final_json = path / f"session_{sid}.json"
        lb_temp = path / f"_loopback_{sid}.wav"
        if final_wav.exists() and final_json.exists():
            # Fully finalized — just stray temps. Will be cleaned below.
            orphans.append({
                "session_id": sid,
                "mic": str(mic_temp),
                "loopback": str(lb_temp) if lb_temp.exists() else "",
                "already_finalized": True,
            })
            continue
        orphans.append({
            "session_id": sid,
            "mic": str(mic_temp),
            "loopback": str(lb_temp) if lb_temp.exists() else "",
            "already_finalized": False,
        })
    return orphans


def recover_orphans(
    recordings_dir: str,
    session_svc: SessionService,
    target_sr: int = 16000,
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

    for orphan in scan_orphans(recordings_dir):
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

        # Build a stub Session so SessionService.save writes JSON in the
        # exact on-disk format used by finalized sessions.
        try:
            mic_mtime = datetime.datetime.fromtimestamp(
                Path(mic).stat().st_mtime)
        except OSError:
            mic_mtime = datetime.datetime.now()
        ended_at = mic_mtime
        started_at = mic_mtime - datetime.timedelta(seconds=duration_s)

        session = Session(session_id=sid)
        session.display_name = f"Recovered Session {sid}"
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
    # pre-resample passes — they're always disposable.
    for leftover in recs_path.glob("_lb16k_*.tmp.wav"):
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
