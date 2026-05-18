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
import re
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# Audio extensions the recorder ever writes as a client-folder copy.
_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac")

# Recognises a recorder-created audio file by name when its session is
# gone (orphan). Two strong signals, both low false-positive:
#   - "session_<hex>"           → ExportService._base_name with no
#                                  display name
#   - "… - YYYY-MM-DD"          → the app's session-naming convention;
#                                  the trailing date also gives us the
#                                  file's age without a session record
_ORPHAN_NAME_RE = re.compile(
    r"(?:^session_[0-9a-fA-F]{4,}$)|(?:.+ - (\d{4}-\d{2}-\d{2})$)")


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


def _base_name(data: dict) -> str:
    """Recreate ExportService._base_name from a session JSON so we can
    recognise the recorder's own audio copies sitting in a client's
    Designated Folder. MUST stay in sync with
    services/export_service.py:_base_name."""
    display = (data.get("display_name") or "").strip()
    if display:
        safe = "".join(
            c if c.isalnum() or c in " -_" else "" for c in display
        ).strip()
        return safe or str(data.get("session_id") or "")
    return f"session_{data.get('session_id') or ''}"


def cleanup(
    recordings_dir: str,
    processed_days: int = 7,
    unprocessed_days: int = 30,
    dry_run: bool = False,
    client_export_dirs: Optional[List[str]] = None,
) -> Dict[str, int]:
    """
    Apply retention policy. Returns stats dict:
      {deleted_count, bytes_freed, processed_deleted, unprocessed_deleted,
       orphans_deleted}

    - processed_days <= 0  →  never delete processed audio
    - unprocessed_days <= 0 →  never delete unprocessed audio

    `client_export_dirs`: the configured per-client Designated Folders.
    Following only the `exported_audio_paths` recorded on a session
    misses copies an older app version made before that tracking
    existed (or whose session JSON was since deleted) — they orphan in
    the client folder forever. When these dirs are supplied we also
    sweep them directly, but ONLY delete files whose name exactly
    matches a recorder-generated export name (reconstructed from a
    session's display name) and that are older than the policy — never
    the user's own files that happen to live in that folder.
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

    # 1b. Sweep configured client Designated Folders for the recorder's
    #     own audio copies that the per-session pass above can't see
    #     (untracked: older app version, or the session JSON is gone).
    #     Safety: a file is deleted ONLY if its name exactly matches an
    #     export name we can reconstruct from a session JSON and it's
    #     past the age policy — the user's unrelated files in that
    #     folder are never touched.
    if client_export_dirs:
        # name -> (processed, age_days_from_started_at_or_None)
        expected: Dict[str, tuple] = {}
        for json_path in path.glob("session_*.json"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            ap = data.get("audio_path") or ""
            suffix = Path(ap).suffix.lower() or ".wav"
            fname = f"{_base_name(data)}{suffix}"
            base_age: Optional[float] = None
            started = data.get("started_at")
            if started:
                try:
                    dt = datetime.datetime.fromisoformat(started)
                    if dt.tzinfo is not None:
                        dt = dt.replace(tzinfo=None)
                    base_age = (now - dt).total_seconds() / 86400
                except ValueError:
                    pass
            expected[fname] = (_is_processed(data), base_age)

        seen_dirs: set = set()
        rec_resolved = str(path.resolve())
        for d in client_export_dirs:
            if not d:
                continue
            try:
                cdir = Path(d)
                rkey = str(cdir.resolve())
            except Exception:
                continue
            # Skip the recordings dir itself (handled above) and dupes.
            if rkey == rec_resolved or rkey in seen_dirs:
                continue
            seen_dirs.add(rkey)
            if not cdir.is_dir():
                continue
            for fpath in cdir.iterdir():
                try:
                    if not fpath.is_file():
                        continue
                    meta = expected.get(fpath.name)
                    is_orphan = False
                    if meta is not None:
                        processed, age_days = meta
                        if age_days is None:
                            age_days = (now - datetime.datetime.fromtimestamp(
                                fpath.stat().st_mtime)).total_seconds() / 86400
                        threshold = (processed_days if processed
                                     else unprocessed_days)
                    else:
                        # No matching session. Only touch files that look
                        # like the recorder's own output — never the
                        # user's other files in the customer folder.
                        if fpath.suffix.lower() not in _AUDIO_EXTS:
                            continue
                        m = _ORPHAN_NAME_RE.match(fpath.stem)
                        if not m:
                            continue
                        is_orphan = True
                        processed = True  # a finished export → processed
                        age_days = None
                        date_str = m.group(1)
                        if date_str:
                            try:
                                d = datetime.datetime.fromisoformat(date_str)
                                age_days = (now - d).total_seconds() / 86400
                            except ValueError:
                                age_days = None
                        if age_days is None:
                            age_days = (now - datetime.datetime.fromtimestamp(
                                fpath.stat().st_mtime)).total_seconds() / 86400
                        threshold = processed_days
                    if threshold <= 0 or age_days < threshold:
                        continue
                    size = fpath.stat().st_size
                    if not dry_run:
                        fpath.unlink()
                    bytes_freed += size
                    deleted_count += 1
                    if processed:
                        processed_deleted += 1
                    else:
                        unprocessed_deleted += 1
                    logger.info(
                        f"Retention: {'(dry run) ' if dry_run else ''}deleted "
                        f"{'orphaned (no session) ' if is_orphan else 'untracked '}"
                        f"client-folder copy {fpath} "
                        f"({age_days:.1f} days old, {format_bytes(size)})"
                    )
                except OSError as e:
                    logger.warning(
                        f"Retention: could not sweep {fpath}: {e}")

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
