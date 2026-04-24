"""
Retention / cleanup service.

Deletes WAV audio files based on age:
  - Processed recordings (has transcript): delete after N days
  - Unprocessed recordings (no transcript): delete after M days
  - Orphaned temp files (_recording_*.wav, _loopback_*.wav) > 1 day old

Session JSON files are NEVER deleted — only the audio is removed.
Transcripts, summaries, action items, decisions all live in the JSON
and remain searchable forever.
"""

import datetime
import json
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def folder_stats(recordings_dir: str) -> Dict[str, int]:
    """Return {total_bytes, session_count, wav_count} for the recordings folder."""
    path = Path(recordings_dir)
    if not path.exists():
        return {"total_bytes": 0, "session_count": 0, "wav_count": 0}
    total = 0
    wav_count = 0
    session_count = 0
    for p in path.iterdir():
        try:
            if p.is_file():
                total += p.stat().st_size
                if p.suffix.lower() == ".wav":
                    wav_count += 1
                if p.name.startswith("session_") and p.suffix == ".json":
                    session_count += 1
        except OSError:
            continue
    return {
        "total_bytes": total,
        "session_count": session_count,
        "wav_count": wav_count,
    }


def format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_processed(session_data: dict) -> bool:
    """A session is 'processed' if it has a transcript (segments)."""
    return bool(session_data.get("segments"))


def cleanup(
    recordings_dir: str,
    processed_days: int = 7,
    unprocessed_days: int = 30,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    Apply retention policy. Returns stats dict:
      {deleted_count, bytes_freed, processed_deleted, unprocessed_deleted,
       orphans_deleted}

    - processed_days <= 0  →  never delete processed audio
    - unprocessed_days <= 0 →  never delete unprocessed audio
    """
    path = Path(recordings_dir)
    if not path.exists():
        return {"deleted_count": 0, "bytes_freed": 0,
                "processed_deleted": 0, "unprocessed_deleted": 0,
                "orphans_deleted": 0}

    now = datetime.datetime.now()
    deleted_count = 0
    bytes_freed = 0
    processed_deleted = 0
    unprocessed_deleted = 0
    orphans_deleted = 0

    # 1. Walk session JSONs and delete their WAV based on processing status
    for json_path in path.glob("session_*.json"):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Retention: could not read {json_path.name}: {e}")
            continue

        audio_path_str = data.get("audio_path")
        if not audio_path_str:
            continue
        audio_path = Path(audio_path_str)
        if not audio_path.exists():
            continue

        # Determine age from started_at if present, else file mtime
        age_days: Optional[float] = None
        started = data.get("started_at")
        if started:
            try:
                dt = datetime.datetime.fromisoformat(started)
                # Drop tz info for consistent comparison with naive now
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                age_days = (now - dt).total_seconds() / 86400
            except ValueError:
                pass
        if age_days is None:
            try:
                age_days = (now - datetime.datetime.fromtimestamp(
                    audio_path.stat().st_mtime)).total_seconds() / 86400
            except OSError:
                continue

        processed = _is_processed(data)
        threshold = processed_days if processed else unprocessed_days
        if threshold <= 0:
            continue
        if age_days < threshold:
            continue

        try:
            size = audio_path.stat().st_size
            if not dry_run:
                audio_path.unlink()
            bytes_freed += size
            deleted_count += 1
            if processed:
                processed_deleted += 1
            else:
                unprocessed_deleted += 1
            logger.info(
                f"Retention: {'(dry run) ' if dry_run else ''}deleted "
                f"{audio_path.name} "
                f"({'processed' if processed else 'unprocessed'}, "
                f"{age_days:.1f} days old, {format_bytes(size)})"
            )
        except OSError as e:
            logger.warning(f"Retention: could not delete {audio_path}: {e}")

        # Audio copies that landed in a client's Designated Folder are
        # tracked on the session. They age out on the same schedule as
        # the primary file — "processed" sessions have their transcript
        # + summary exported alongside, so retention only removes the
        # big WAV and leaves the text artifacts in place. Missing paths
        # (user renamed or moved the copy) are silently skipped.
        for extra_path_str in data.get("exported_audio_paths") or []:
            try:
                extra = Path(extra_path_str)
            except Exception:
                continue
            if not extra.exists():
                continue
            try:
                size = extra.stat().st_size
                if not dry_run:
                    extra.unlink()
                bytes_freed += size
                deleted_count += 1
                # Count under the same processed/unprocessed bucket the
                # primary file was in so the user can see what age policy
                # removed them.
                if processed:
                    processed_deleted += 1
                else:
                    unprocessed_deleted += 1
                logger.info(
                    f"Retention: {'(dry run) ' if dry_run else ''}deleted "
                    f"client-folder copy {extra} "
                    f"({age_days:.1f} days old, {format_bytes(size)})"
                )
            except OSError as e:
                logger.warning(f"Retention: could not delete {extra}: {e}")

    # 2. Orphaned temp files older than 1 day
    for pattern in ("_recording_*.wav", "_loopback_*.wav"):
        for orphan in path.glob(pattern):
            try:
                mtime = datetime.datetime.fromtimestamp(orphan.stat().st_mtime)
                age_days = (now - mtime).total_seconds() / 86400
                if age_days < 1:
                    continue
                size = orphan.stat().st_size
                if not dry_run:
                    orphan.unlink()
                bytes_freed += size
                orphans_deleted += 1
                deleted_count += 1
                logger.info(
                    f"Retention: {'(dry run) ' if dry_run else ''}deleted "
                    f"orphan {orphan.name} ({format_bytes(size)})"
                )
            except OSError as e:
                logger.warning(f"Retention: could not delete {orphan}: {e}")

    return {
        "deleted_count": deleted_count,
        "bytes_freed": bytes_freed,
        "processed_deleted": processed_deleted,
        "unprocessed_deleted": unprocessed_deleted,
        "orphans_deleted": orphans_deleted,
    }
