"""
Auto-generated prep-brief cache — one entry per upcoming meeting, per day.

When `auto_prep_brief_enabled` is on, a backend loop generates a
pre-meeting brief shortly before each calendar meeting and stores it
here. The frontend polls for un-notified entries to fire a native
"prep brief ready" notification, and reads the cached markdown so
opening the brief is instant (no regeneration).

Backend-driven on purpose: the lesson from the auto-process and
watchdog bugs is that critical work must NOT depend on the Record view
being open. The loop runs on a backend timer; the frontend only fires
the notification.

Per-day JSON storage mirrors DailyBriefingService. Keyed by a stable
meeting key (subject + start) so a meeting is briefed once even across
multiple loop ticks.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import hashlib
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _today_iso() -> str:
    return date_cls.today().isoformat()


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def meeting_key(subject: str, start_iso: str) -> str:
    """Stable key for a meeting occurrence. Hash so it's filesystem- and
    JSON-key-safe regardless of subject characters."""
    raw = f"{(subject or '').strip().lower()}|{(start_iso or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class PrepBriefCacheService:
    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "prep_briefs"
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, date_iso: str) -> Path:
        try:
            date_cls.fromisoformat(date_iso)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid date: {date_iso!r}")
        return self._root / f"{date_iso}.json"

    def _read_locked(self, date_iso: str) -> Dict[str, dict]:
        p = self._path_for(date_iso)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"prep_brief cache {date_iso} unreadable ({e})")
            return {}

    def _write_locked(self, date_iso: str, data: Dict[str, dict]) -> None:
        p = self._path_for(date_iso)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── API ───────────────────────────────────────────────────────────

    def has(self, key: str, date_iso: Optional[str] = None) -> bool:
        d = date_iso or _today_iso()
        with self._lock:
            return key in self._read_locked(d)

    def put(self, entry: dict, date_iso: Optional[str] = None) -> dict:
        """Store a generated brief. entry must include `key`, `subject`,
        `start_iso`, `markdown`. Sets generated_at + notified=False."""
        d = date_iso or _today_iso()
        key = entry.get("key")
        if not key:
            raise ValueError("prep-brief cache entry needs a key")
        entry = dict(entry)
        entry.setdefault("generated_at", _now_iso_utc())
        entry.setdefault("notified", False)
        with self._lock:
            data = self._read_locked(d)
            data[key] = entry
            self._write_locked(d, data)
        return entry

    def list_today(self) -> List[dict]:
        with self._lock:
            data = self._read_locked(_today_iso())
        items = list(data.values())
        items.sort(key=lambda e: e.get("start_iso") or "")
        return items

    def pending_notifications(self) -> List[dict]:
        """Entries generated but not yet notified to the user."""
        return [e for e in self.list_today() if not e.get("notified")]

    def mark_notified(self, key: str, date_iso: Optional[str] = None) -> None:
        d = date_iso or _today_iso()
        with self._lock:
            data = self._read_locked(d)
            if key in data:
                data[key]["notified"] = True
                self._write_locked(d, data)
