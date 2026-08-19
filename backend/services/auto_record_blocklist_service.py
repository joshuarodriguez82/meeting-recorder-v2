"""
Auto-record blocklist — "never auto-record this meeting" list.

The user can permanently flag meetings they don't want auto-recorded
(1:1s, standups, personal blocks, sensitive calls). The flag has to
survive restarts and apply to the whole recurring series, so we key on
the normalized meeting subject rather than a single occurrence's
(subject, start) tuple — a recurring "Weekly 1:1" should stay blocked
every week once the user flags it once.

WHY SUBJECT IS THE RIGHT KEY FOR EXTENSION-SOURCED MEETINGS TOO
---------------------------------------------------------------
Extension-sourced events (the Chrome extension's Outlook Web scrape)
carry NO stable identifier: the store is replaced wholesale on every
capture, there is no Outlook EntryID / iCal UID in what the scrape can
see, and even the start time can move by a minute or two between
imports. The subject is the only thing that survives a re-import — so
it is what this list matches on, exactly as it already did for local
meetings. An opt-out that reset itself on the next capture would be
worse than no opt-out at all.

One wrinkle that only shows up on the extension path: Outlook Web
DECORATES a changed invite's subject ("Updated! Weekly Sync"), and a
forwarded/replied invite arrives as "FW: "/"RE: ". A user who blocked
"Weekly Sync" must stay blocked when the next capture calls it
"Updated! Weekly Sync". So alongside the original exact key we also
match on the CANONICAL subject — `extension_calendar_service.
normalize_subject`, the same prefix-stripping/casefolding used to decide
two calendar rows are the same meeting. Both keys are checked, so no
pre-existing blocklist entry stops matching what it matched before.

Storage mirrors speaker_profile_service: a small JSON file in the user
data dir, atomically rewritten under a lock. Volume is tiny (a handful
of recurring meetings), so a flat list is plenty. The on-disk shape is
unchanged — the canonical index is derived at load time, never
persisted, so a downgrade reads the same file fine.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

from services.extension_calendar_service import (
    normalize_subject as canonical_subject,
)
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
        # Exact-match entries — what the user picked from a specific
        # meeting tile or typed verbatim.
        self._entries: dict[str, str] = {}
        # Case-insensitive substring patterns — a meeting is blocked
        # if ANY pattern occurs anywhere in its (raw) subject. Stored
        # as the user typed them (case preserved for display); matched
        # case-insensitive at check time. Example pattern: "canceled"
        # blocks "Canceled: Weekly Sync", "Project X (Canceled)", etc.
        self._patterns: list[str] = []
        # Derived, never persisted: canonical_subject() of every entry's
        # display value. Lets a blocked "Weekly Sync" keep matching when
        # a re-import decorates it as "Updated! Weekly Sync" — see the
        # module docstring. Rebuilt on every mutation.
        self._canonical: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            self._patterns = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # Accept three shapes — current dict, legacy bare list, and
            # the new dict-with-patterns shape — so old files keep
            # working without migration.
            if isinstance(data, dict) and "entries" in data:
                # New shape: {"entries": {...}, "patterns": [...]}.
                raw_entries = data.get("entries") or {}
                if isinstance(raw_entries, dict):
                    self._entries = {
                        _normalize(k): str(v)
                        for k, v in raw_entries.items() if _normalize(k)
                    }
                raw_patterns = data.get("patterns") or []
                if isinstance(raw_patterns, list):
                    self._patterns = [
                        str(p).strip() for p in raw_patterns
                        if str(p).strip()
                    ]
            elif isinstance(data, dict):
                # Legacy current shape: bare {normalized: display}.
                self._entries = {
                    _normalize(k): str(v)
                    for k, v in data.items() if _normalize(k)
                }
                self._patterns = []
            elif isinstance(data, list):
                self._entries = {
                    _normalize(s): str(s) for s in data if _normalize(s)
                }
                self._patterns = []
            else:
                self._entries = {}
                self._patterns = []
            self._rebuild_canonical()
            logger.info(
                f"Loaded {len(self._entries)} auto-record blocklist "
                f"entries + {len(self._patterns)} patterns "
                f"from {self.path}")
        except Exception as e:
            logger.exception(f"Could not load auto-record blocklist: {e}. "
                             f"Starting empty; file left in place.")
            self._entries = {}
            self._patterns = []
            self._canonical = set()

    def _rebuild_canonical(self) -> None:
        """Derive the canonical-subject index from the entries. Caller
        holds _lock (or is still in __init__)."""
        self._canonical = {
            c for c in (canonical_subject(v) for v in self._entries.values())
            if c
        }

    def _save(self) -> None:
        """Atomic write via tmp + rename. Caller must hold _lock."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"entries": self._entries, "patterns": self._patterns}
            tmp.write_text(
                json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.exception(f"Could not save auto-record blocklist: {e}")

    # ── Public API ───────────────────────────────────────────────────

    def is_blocked(self, meeting: dict) -> bool:
        """True for a meeting the user flagged "never auto-record".

        Source-blind: an extension-sourced meeting is matched by exactly
        the same rules as a local one, which is what makes the
        per-meeting opt-out on an Outlook-Web-only row real rather than
        decorative.

        Two subject keys are tried, in this order:
          1. the historical exact key (whitespace-collapsed, lowercased)
             — so every entry written by any previous version keeps
             matching precisely what it used to;
          2. the CANONICAL key (`extension_calendar_service.
             normalize_subject`: also strips "Updated!"/"RE:"/"FW:"
             decoration and casefolds) — so a block survives Outlook
             Web re-decorating the subject on the next capture. Without
             this the opt-out silently resets itself, which is the one
             failure mode worse than not offering it.
        """
        raw = str(meeting.get("subject", ""))
        key = _normalize(raw)
        if not key:
            return False
        with self._lock:
            if key in self._entries:
                return True
            canon = canonical_subject(raw)
            if canon and canon in self._canonical:
                return True
            raw_lower = raw.lower()
            for pat in self._patterns:
                if pat and pat.lower() in raw_lower:
                    return True
            return False

    def list_all(self) -> List[str]:
        with self._lock:
            return sorted(self._entries.values(), key=str.lower)

    def list_patterns(self) -> List[str]:
        with self._lock:
            return sorted(self._patterns, key=str.lower)

    def add(self, subject: str) -> bool:
        key = _normalize(subject)
        if not key:
            return False
        canon = canonical_subject(subject)
        with self._lock:
            if key in self._entries:
                return True
            if canon and canon in self._canonical:
                # Already blocked under a differently-decorated form of
                # the same subject ("Updated! Weekly Sync" vs "Weekly
                # Sync"). is_blocked() already says True for both, so
                # adding a second entry would only give the user two
                # rows to remove before the block actually lifts.
                return True
            self._entries[key] = subject.strip()
            self._rebuild_canonical()
            self._save()
        logger.info(f"auto-record blocklist + '{subject.strip()}'")
        return True

    def remove(self, subject: str) -> bool:
        """Un-block. Falls back to the canonical subject so a block the
        user can SEE (is_blocked matched it through the canonical index)
        is always one they can actually lift — otherwise a meeting whose
        subject picked up an "Updated!" prefix since it was blocked
        would render as blocked with a button that does nothing."""
        key = _normalize(subject)
        canon = canonical_subject(subject)
        with self._lock:
            removed: List[str] = []
            if key in self._entries:
                removed.append(self._entries.pop(key))
            elif canon:
                for k in [k for k, v in self._entries.items()
                          if canonical_subject(v) == canon]:
                    removed.append(self._entries.pop(k))
            if not removed:
                return False
            self._rebuild_canonical()
            self._save()
        logger.info(f"auto-record blocklist - {', '.join(repr(r) for r in removed)}")
        return True

    def add_pattern(self, pattern: str) -> bool:
        p = (pattern or "").strip()
        if not p:
            return False
        with self._lock:
            # Case-insensitive dedupe so "Canceled" and "canceled" don't
            # both land in the list.
            existing = {x.lower() for x in self._patterns}
            if p.lower() in existing:
                return True
            self._patterns.append(p)
            self._save()
        logger.info(f"auto-record blocklist + pattern '{p}'")
        return True

    def remove_pattern(self, pattern: str) -> bool:
        p = (pattern or "").strip().lower()
        if not p:
            return False
        with self._lock:
            before = len(self._patterns)
            self._patterns = [x for x in self._patterns if x.lower() != p]
            if len(self._patterns) == before:
                return False
            self._save()
        logger.info(f"auto-record blocklist - pattern '{pattern}'")
        return True
