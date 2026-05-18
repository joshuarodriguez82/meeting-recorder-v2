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
    """{total_bytes, session_count, wav_count} for everything retention
    manages — the main recordings folder PLUS the audio copies it placed
    in clients' Designated Folders. Counting only the main folder made
    the figure understate real usage and hid the effect of cleaning the
    client-folder copies."""
    path = Path(recordings_dir)
    if not path.exists():
        return {"total_bytes": 0, "session_count": 0, "wav_count": 0}
    total = 0
    wav_count = 0
    session_count = 0
    counted: set[str] = set()
    for p in path.iterdir():
        try:
            if p.is_file():
                counted.add(str(p.resolve()))
                total += p.stat().st_size
                if p.suffix.lower() == ".wav":
                    wav_count += 1
                if p.name.startswith("session_") and p.suffix == ".json":
                    session_count += 1
        except OSError:
            continue

    # Add the client-folder audio copies recorded on each session.
    for json_path in path.glob("session_*.json"):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for extra in data.get("exported_audio_paths") or []:
            try:
                ep = Path(extra)
                if not ep.is_file():
                    continue
                rp = str(ep.resolve())
                if rp in counted:
                    continue
                counted.add(rp)
                total += ep.stat().st_size
                if ep.suffix.lower() == ".wav":
                    wav_count += 1
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

        # The glob `session_*.json` also matches sidecars written next
        # to real sessions — session_<id>.commitments.json (a JSON
        # array) and session_<id>.item_status.json. Only the session
        # object itself (a dict with audio_path) is relevant here.
        # Without this guard, hitting a commitments sidecar raised
        # AttributeError: 'list' object has no attribute 'get', which
        # 500'd the endpoint and surfaced in the UI as "failed to
        # fetch" (and silently killed every auto-retention pass).
        if not isinstance(data, dict):
            continue

        # Collect every audio file this session owns: the primary in
        # the recordings dir + every copy placed in a client's
        # Designated Folder. We do NOT bail when the primary is gone —
        # a previous pass may have deleted it while the (often larger)
        # client-folder copies linger. Skipping here is exactly why
        # client folders never got cleaned.
        candidates: list[tuple[Path, bool]] = []  # (path, is_client_copy)
        audio_path_str = data.get("audio_path")
        if audio_path_str:
            try:
                ap = Path(audio_path_str)
                if ap.is_file():
                    candidates.append((ap, False))
            except Exception:
                pass
        for extra_path_str in data.get("exported_audio_paths") or []:
            try:
                ep = Path(extra_path_str)
            except Exception:
                continue
            if ep.is_file() and all(ep != c[0] for c in candidates):
                candidates.append((ep, True))
        if not candidates:
            continue

        # Age: prefer started_at; else the oldest candidate's mtime
        # (works even when the primary is already gone).
        age_days: Optional[float] = None
        started = data.get("started_at")
        if started:
            try:
                dt = datetime.datetime.fromisoformat(started)
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                age_days = (now - dt).total_seconds() / 86400
            except ValueError:
                pass
        if age_days is None:
            try:
                oldest = min(c[0].stat().st_mtime for c in candidates)
                age_days = (now - datetime.datetime.fromtimestamp(
                    oldest)).total_seconds() / 86400
            except (OSError, ValueError):
                continue

        processed = _is_processed(data)
        threshold = processed_days if processed else unprocessed_days
        if threshold <= 0:
            continue
        if age_days < threshold:
            continue

        for fpath, is_client_copy in candidates:
            try:
                size = fpath.stat().st_size
                if not dry_run:
                    fpath.unlink()
                bytes_freed += size
                deleted_count += 1
                if processed:
                    processed_deleted += 1
                else:
                    unprocessed_deleted += 1
                where = f"client-folder copy {fpath}" if is_client_copy \
                    else fpath.name
                logger.info(
                    f"Retention: {'(dry run) ' if dry_run else ''}deleted "
                    f"{where} "
                    f"({'processed' if processed else 'unprocessed'}, "
                    f"{age_days:.1f} days old, {format_bytes(size)})"
                )
            except OSError as e:
                logger.warning(f"Retention: could not delete {fpath}: {e}")

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
