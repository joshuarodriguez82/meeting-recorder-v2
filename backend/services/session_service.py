"""
Persists and loads session data as JSON.
Uses atomic write (temp file + rename) to prevent corrupt JSON on crash.
"""

import datetime
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from models.session import Session
from services._cloud_sync import CloudFileNotReadyError, read_text_hydrated
from utils.logger import get_logger

logger = get_logger(__name__)


class SessionService:
    """Handles JSON serialization of Session objects."""

    def __init__(self, recordings_dir: str):
        self._recordings_dir = Path(recordings_dir)
        self._recordings_dir.mkdir(parents=True, exist_ok=True)

    @property
    def recordings_dir(self) -> Path:
        """Public read-only accessor — used by SearchService to find
        per-session embedding sidecar files (session_<id>.embeddings.pkl)."""
        return self._recordings_dir

    def save(self, session: Session) -> str:
        """
        Serialize a session to a JSON file using an atomic write.

        Writes to a temporary file first, then renames it to the final path.
        This ensures the target file is never left in a half-written state
        if the process is interrupted mid-write.

        Args:
            session: The completed Session object.

        Returns:
            The path of the saved JSON file.

        Raises:
            OSError: If writing or renaming fails.
        """
        final_path = self._recordings_dir / f"session_{session.session_id}.json"
        data = json.dumps(session.to_dict(), indent=2, ensure_ascii=False)

        # FIX #10: write to temp file in same directory, then atomic rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self._recordings_dir,
                suffix=".json.tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())  # Flush OS buffers before rename
                os.replace(tmp_path, final_path)  # Atomic on POSIX & Windows
            except Exception:
                # Clean up temp file if rename or write fails
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            raise OSError(f"Failed to save session {session.session_id}: {e}") from e

        logger.info(f"Session atomically saved: {final_path}")
        return str(final_path)

    def load(self, session_id: str) -> Optional[dict]:
        """Load a session JSON by ID. Returns raw dict.

        Reads with ``utf-8-sig`` so a leading UTF-8 BOM (which PowerShell's
        ``Set-Content -Encoding UTF8`` emits by default, and which external
        recovery scripts therefore tend to inadvertently produce) doesn't
        cause a silent skip in ``list_sessions()``. Field repro
        2026-06-15: a recovered session was invisible in the UI because
        the recovery script wrote the JSON with a BOM, and the previous
        ``open(... encoding='utf-8')`` here raised JSONDecodeError on the
        BOM byte. ``utf-8-sig`` is a strict superset — files without a
        BOM still decode identically."""
        path = self._recordings_dir / f"session_{session_id}.json"
        if not path.exists():
            logger.warning(f"Session file not found: {path}")
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt session file {path}: {e}") from e

    def load_full(self, session_id: str) -> Optional[Session]:
        """Load a session and rebuild the full Session object."""
        data = self.load(session_id)
        if data is None:
            return None
        return Session.from_dict(data)

    def list_sessions(self) -> List[dict]:
        """
        Scan recordings dir for all session_*.json files.
        Returns a list of summaries sorted newest first.
        """
        results: List[dict] = []
        for path in self._recordings_dir.glob("session_*.json"):
            # Skip sidecar files (session_<id>.commitments.json,
            # session_<id>.item_status.json, …) that share the
            # session_*.json glob but aren't canonical session records.
            # Canonical session IDs are dotless hex; a dot in the stem
            # means a sidecar suffix.
            if "." in path.stem:
                continue
            try:
                data = json.loads(read_text_hydrated(path))
            except CloudFileNotReadyError as e:
                # Synced from another device but not downloaded to this
                # disk yet — the "new session created today doesn't show
                # up on the Mac" bug. Log loud + actionable instead of a
                # quiet skip so the cause is obvious.
                logger.error(
                    f"Session {path.name} is an un-downloaded cloud "
                    f"placeholder; it will appear once the sync client "
                    f"finishes downloading it. {e}")
                continue
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Skipping unreadable session {path.name}: {e}")
                continue
            if not isinstance(data, dict):
                # Belt-and-braces: anything that loads as valid JSON but
                # isn't a dict (legacy list-rooted file, future sidecar
                # we forgot to add to the dotted-stem skip above) gets
                # skipped instead of 500ing the whole endpoint.
                logger.warning(
                    f"Skipping non-dict session file {path.name} "
                    f"(root is {type(data).__name__}, expected dict)")
                continue

            session_id = data.get("session_id") or path.stem.replace("session_", "")
            audio_path = data.get("audio_path")
            audio_exists = bool(audio_path) and Path(audio_path).exists()

            # Duration from started/ended_at
            duration_s = 0
            try:
                started = data.get("started_at")
                ended = data.get("ended_at")
                if started and ended:
                    s = datetime.datetime.fromisoformat(started)
                    e = datetime.datetime.fromisoformat(ended)
                    duration_s = max(0, int((e - s).total_seconds()))
            except Exception:
                pass

            results.append({
                "session_id": session_id,
                "display_name": data.get("display_name") or f"Session {session_id}",
                "started_at": data.get("started_at"),
                "ended_at": data.get("ended_at"),
                "duration_s": duration_s,
                "audio_path": audio_path,
                "audio_exists": audio_exists,
                "has_transcript": bool(data.get("segments")),
                "has_summary": bool(data.get("summary")),
                "has_action_items": bool(data.get("action_items")),
                "has_requirements": bool(data.get("requirements")),
                "has_decisions": bool(data.get("decisions")),
                "client": data.get("client", "") or "",
                "project": data.get("project", "") or "",
                "action_items": data.get("action_items", "") or "",
                "summary": data.get("summary", "") or "",
                "decisions": data.get("decisions", "") or "",
                "requirements": data.get("requirements", "") or "",
                # Attendees are lightweight (email strings) and let the
                # Record view auto-tag client from the current calendar
                # meeting's attendees without an extra per-session fetch.
                "attendees": list(data.get("attendees") or []),
                # Compact map of speaker_id -> display_name so list-side
                # consumers (Follow-ups, Commitments) can resolve labels
                # like "SPEAKER_03" that the LLM extracted into a typed
                # human name when the user has renamed that speaker.
                # Omitting empty display_names so the wire is small —
                # the resolver treats a missing key as "no rename yet".
                "speakers": {
                    sid: sd.get("display_name", "")
                    for sid, sd in (data.get("speakers") or {}).items()
                    if sd.get("display_name")
                },
                # Audio integrity flags — set when WAV duration disagrees
                # significantly with the recording window. Lets the
                # Sessions list render a warning chip without loading
                # the full session JSON.
                "audio_integrity_warning":
                    data.get("audio_integrity_warning"),
                "audio_actual_duration_s":
                    data.get("audio_actual_duration_s"),
                "audio_expected_duration_s":
                    data.get("audio_expected_duration_s"),
                # Auto-process failure (retries exhausted). Lets the
                # Sessions list badge a failed session so it doesn't sit
                # silently unprocessed.
                "processing_error": data.get("processing_error"),
                # Read-only sync-integrity finding (mic/loopback drift or
                # dropped frames vs wall-clock). Surfaced as an info chip.
                "sync_warning": data.get("sync_warning"),
                "json_path": str(path),
            })

        # Sort newest first
        results.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return results

    def delete(self, session_id: str) -> None:
        """Delete session JSON, WAV, and session log if they exist."""
        for suffix in (".json", ".wav", ".log"):
            p = self._recordings_dir / f"session_{session_id}{suffix}"
            if p.exists():
                try:
                    p.unlink()
                    logger.info(f"Deleted {p.name}")
                except OSError as e:
                    logger.warning(f"Could not delete {p}: {e}")

    def import_from_file(
        self,
        source_path: str,
        display_name: str = "",
        client: str = "",
        project: str = "",
    ) -> Session:
        """
        Import an existing audio file (WAV) as a new session.

        Copies (doesn't move) the source into the recordings directory
        using the standard `session_<id>.wav` naming, creates a Session
        with metadata from the file, and writes the session JSON. The
        returned session has no transcript/summary yet — the user will
        run processing from the UI like any freshly-recorded session.
        """
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"File not found: {source_path}")
        if src.suffix.lower() not in (
                ".wav", ".mp3", ".m4a", ".flac", ".mp4", ".mov"):
            raise ValueError(
                f"Unsupported audio format: {src.suffix}. "
                "Use .wav, .mp3, .m4a, .flac, .mp4, or .mov.")

        session_id = uuid.uuid4().hex[:8].upper()
        # Keep the original extension so downstream tooling doesn't
        # assume .wav when the user imported, say, an .m4a from Teams.
        dst = self._recordings_dir / f"session_{session_id}{src.suffix.lower()}"
        shutil.copy2(src, dst)

        session = Session(session_id=session_id)
        session.display_name = (display_name or src.stem).strip()
        session.started_at = datetime.datetime.fromtimestamp(src.stat().st_mtime)
        session.ended_at = session.started_at
        session.audio_path = str(dst)
        session.client = client
        session.project = project
        self.save(session)
        logger.info(f"Imported external file {src.name} as session {session_id}")
        return session
