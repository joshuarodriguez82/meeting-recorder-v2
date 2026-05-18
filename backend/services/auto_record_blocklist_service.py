"""
Auto-record blocklist — "never auto-record this meeting" list.

The user can permanently flag meetings they don't want auto-recorded
(1:1s, standups, personal blocks, sensitive calls). The flag has to
survive restarts and apply to the whole recurring series, so we key on
the normalized meeting subject rather than a single occurrence's
(subject, start) tuple — a recurring "Weekly 1:1" should stay blocked
every week once the user flags it once.

Storage mirrors speaker_profile_service: a small JSON file in the user
data dir, atomically rewritten under a lock. Volume is tiny (a handful
of recurring meetings), so a flat list is plenty.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

from utils.logger import get_logger

logger = get_logger(__name__)


def _normalize(subject: str) -> str:
    """Case/whitespace-insensitive key so 'Weekly  1:1 ' and
    'weekly 1:1' collapse to the same blocklist entry."""
    return " ".join((subject or "").split()).strip().lower()


class AutoRecordBlocklistService:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "auto_record_blocklist.json"
        self._lock = threading.Lock()
        # Maps normalized subject -> original-cased subject (for display).
        self._entries: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # Accept both the current {normalized: display} shape and a
            # bare list of subjects (forward/backward tolerant).
            if isinstance(data, dict):
                self._entries = {
                    _normalize(k): str(v)
                    for k, v in data.items() if _normalize(k)
                }
            elif isinstance(data, list):
                self._entries = {
                    _normalize(s): str(s) for s in data if _normalize(s)
                }
            else:
                self._entries = {}
            logger.info(f"Loaded {len(self._entries)} auto-record "
                        f"blocklist entries from {self.path}")
        except Exception as e:
            logger.exception(f"Could not load auto-record blocklist: {e}. "
                             f"Starting empty; file left in place.")
            self._entries = {}

    def _save(self) -> None:
        """Atomic write via tmp + rename. Caller must hold _lock."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._entries, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.exception(f"Could not save auto-record blocklist: {e}")

    # ── Public API ───────────────────────────────────────────────────

    def is_blocked(self, meeting: dict) -> bool:
        key = _normalize(str(meeting.get("subject", "")))
        if not key:
            return False
        with self._lock:
            return key in self._entries

    def list_all(self) -> List[str]:
        with self._lock:
            return sorted(self._entries.values(), key=str.lower)

    def add(self, subject: str) -> bool:
        key = _normalize(subject)
        if not key:
            return False
        with self._lock:
            if key in self._entries:
                return True
            self._entries[key] = subject.strip()
            self._save()
        logger.info(f"auto-record blocklist + '{subject.strip()}'")
        return True

    def remove(self, subject: str) -> bool:
        key = _normalize(subject)
        with self._lock:
            if key not in self._entries:
                return False
            removed = self._entries.pop(key)
            self._save()
        logger.info(f"auto-record blocklist - '{removed}'")
        return True
