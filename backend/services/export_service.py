"""
Exports transcripts and summaries to text files.
Uses meeting display name if available for clean filenames.
"""

import os
from pathlib import Path
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
