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
                except (OSError, shutil.SameFileError) as e:
                    # SameFileError happens when target_dir == recordings_dir
                    # and we'd be copying onto ourselves — safe to ignore.
                    logger.warning(f"Audio copy skipped: {e}")
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
        path.write_text("\n".join(lines), encoding="utf-8")
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
        path.write_text("\n".join(lines), encoding="utf-8")
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
        path.write_text("\n".join(lines), encoding="utf-8")
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
        path.write_text("\n".join(lines), encoding="utf-8")
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
        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Requirements exported: {path}")
        return str(path)
