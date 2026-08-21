"""
Exports transcripts and summaries to text files.
Uses meeting display name if available for clean filenames.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional

from models.session import Session
from utils.logger import get_logger

logger = get_logger(__name__)


class ExportService:

    def __init__(self, recordings_dir: str):
        self._dir = Path(recordings_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _base_name(self, session: Session) -> str:
        if session.display_name:
            safe = "".join(
                c if c.isalnum() or c in " -_" else ""
                for c in session.display_name
            ).strip()
            return safe or session.session_id
        return f"session_{session.session_id}"

    @staticmethod
    def _write_text_if_changed(path: Path, content: str) -> bool:
        """Write only when the file does not already say exactly this.
        Returns True if it wrote.

        FIELD REPORT 2026-08-21: every app install re-exported every
        session into the user's synced Drive folder, so all 79 files
        jumped to the top of a Date-modified sort carrying the install's
        timestamp. The bytes were identical; only the mtimes moved.

        That is not cosmetic. "Date modified" is how someone finds what
        they were last working on, and a sync client re-uploads every
        file it sees touched — so an install cost a full folder re-sync
        and destroyed the ordering that makes it navigable.

        An export writes what the session says. If the file already
        says that, the export is already done.
        """
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            # Missing, unreadable, or written in some other encoding —
            # all cases where writing is the right answer.
            pass
        path.write_text(content, encoding="utf-8")
        return True

    def _resolve_target_dir(self, override: Optional[str]) -> Path:
        if override:
            p = Path(override).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            return p
        return self._dir

    def export_all(
        self,
        session: Session,
        target_dir: Optional[str] = None,
        copy_audio: bool = True,
        strict: bool = False,
    ) -> List[str]:
        """
        Write every available artifact for this session into target_dir.

        When `target_dir` is None we fall back to the recordings dir, which
        matches the old per-file export methods. When called via a client's
        designated folder (Clients view) the user gets a single clean drop
        of: transcript, summary, action items, decisions, requirements,
        plus an audio copy when `copy_audio` is true.

        Silently skips any artifact the session doesn't have yet rather
        than raising — a session with only a transcript still produces a
        useful export.

        `strict` (used by the background export worker): re-raise a failed
        AUDIO copy instead of swallowing it, so the worker's retry
        schedule fires on a transient cloud-mount stall. Without this the
        worker could never retry the one artifact — the multi-hundred-MB
        WAV — the whole async design exists to protect. SameFileError is
        still swallowed (copying onto ourselves is a no-op, not a
        failure). This method does NOT mutate shared instance state
        beyond the try/finally `_dir` swap; the worker uses a dedicated
        ExportService instance so that swap can't race request-path
        exports.
        """
        out: List[str] = []
        orig_dir = self._dir
        try:
            self._dir = self._resolve_target_dir(target_dir)
            # Transcript is the one artifact we always export when available,
            # because it's the only thing not reproducible from the audio.
            if session.segments:
                out.append(self.export_transcript(session))
            if session.summary:
                out.append(self.export_summary(session))
            if session.action_items:
                out.append(self.export_action_items(session))
            if session.decisions:
                out.append(self.export_decisions(session))
            if session.requirements:
                out.append(self.export_requirements(session))
            if copy_audio and session.audio_path and Path(session.audio_path).exists():
                src = Path(session.audio_path)
                dst = self._dir / f"{self._base_name(session)}{src.suffix.lower()}"
                try:
                    shutil.copy2(src, dst)
                    out.append(str(dst))
                except shutil.SameFileError as e:
                    # target_dir == recordings_dir; copying onto ourselves
                    # is a no-op, not a failure — safe to ignore.
                    logger.warning(f"Audio copy skipped: {e}")
                except OSError as e:
                    logger.warning(f"Audio copy failed: {e}")
                    if strict:
                        raise
        finally:
            self._dir = orig_dir
        return out

    def export_transcript(self, session: Session) -> str:
        name = self._base_name(session)
        path = self._dir / f"transcript_{name}.txt"
        lines = []
        if session.display_name:
            lines.append(f"Meeting: {session.display_name}")
            lines.append("=" * 60)
            lines.append("")
        lines.append(session.full_transcript())
        self._write_text_if_changed(path, "\n".join(lines))
        logger.info(f"Transcript exported: {path}")
        return str(path)

    def export_summary(self, session: Session) -> str:
        if not session.summary:
            raise ValueError("No summary to export.")
        name = self._base_name(session)
        path = self._dir / f"summary_{name}.txt"
        lines = []
        if session.display_name:
            lines.append(f"Meeting: {session.display_name}")
            lines.append("=" * 60)
            lines.append("")
        lines.append(session.summary)
        self._write_text_if_changed(path, "\n".join(lines))
        logger.info(f"Summary exported: {path}")
        return str(path)

    def export_action_items(self, session: Session) -> str:
        if not session.action_items:
            raise ValueError("No action items to export.")
        name = self._base_name(session)
        path = self._dir / f"action_items_{name}.txt"
        lines = []
        if session.display_name:
            lines.append(f"Meeting: {session.display_name}")
            lines.append("=" * 60)
            lines.append("")
        lines.append(session.action_items)
        self._write_text_if_changed(path, "\n".join(lines))
        logger.info(f"Action items exported: {path}")
        return str(path)

    def export_decisions(self, session: Session) -> str:
        if not session.decisions:
            raise ValueError("No decisions to export.")
        name = self._base_name(session)
        path = self._dir / f"decisions_{name}.txt"
        lines = []
        if session.display_name:
            lines.append(f"Meeting: {session.display_name}")
            lines.append("=" * 60)
            lines.append("")
        lines.append(session.decisions)
        self._write_text_if_changed(path, "\n".join(lines))
        logger.info(f"Decisions exported: {path}")
        return str(path)

    def export_requirements(self, session: Session) -> str:
        if not session.requirements:
            raise ValueError("No requirements to export.")
        name = self._base_name(session)
        path = self._dir / f"requirements_{name}.txt"
        lines = []
        if session.display_name:
            lines.append(f"Meeting: {session.display_name}")
            lines.append("=" * 60)
            lines.append("")
        lines.append(session.requirements)
        self._write_text_if_changed(path, "\n".join(lines))
        logger.info(f"Requirements exported: {path}")
        return str(path)
