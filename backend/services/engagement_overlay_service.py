"""
Per-engagement manual overlay (stored in recordings_dir/engagement_overlays.json).

The engagement register auto-rolls everything it can derive from
recorded meetings (last meeting date, open commitments, decisions
made). But some context only exists in the SA's head — current
status, exec sponsor on the customer side, next milestone date, free-
form notes about where the engagement actually stands.

This service stores those manual fields per engagement, keyed by
`{client}__{project}` (matching how EngagementService scopes
registers). The register response merges the overlay in so the
frontend gets one cohesive payload.

Single user, single file, atomic write. Same shape pattern as
auto_record_blocklist_service / template_service.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# Canonical status strings — kept as a closed enum so the UI can
# render colored badges without guessing the universe of values.
# Unknown values are still allowed (they just render as raw text);
# this list is the suggestion / dropdown source.
KNOWN_STATUSES = [
    "active",         # currently working it
    "on-hold",        # paused / blocked
    "at-risk",        # in trouble, watch closely
    "won",            # closed / signed / shipped
    "lost",           # closed / dropped / no-go
    "archived",       # historical, no current work
]


@dataclass
class EngagementOverlay:
    """Manual fields the SA pins on an engagement, separate from the
    auto-rolled register data. All optional."""
    status: str = ""              # one of KNOWN_STATUSES, or free text
    exec_sponsor: str = ""        # customer-side decision-maker
    next_milestone: str = ""      # free-text or ISO date
    notes: str = ""               # everything else
    updated_at: str = ""          # ISO timestamp of last write

    def to_dict(self) -> dict:
        return asdict(self)


def _scope_key(client: str, project: str = "") -> str:
    c = (client or "").strip().lower()
    p = (project or "").strip().lower()
    return f"{c}__{p}" if p else c


def _safe_key(key: str) -> str:
    """Filesystem / dict-safe form of the scope key."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", key)


class EngagementOverlayService:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "engagement_overlays.json"
        self._lock = threading.Lock()
        self._cache: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._cache = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._cache = data
            else:
                self._cache = {}
            logger.info(
                f"Loaded {len(self._cache)} engagement overlay entries "
                f"from {self._path}")
        except Exception as e:
            logger.warning(
                f"Could not load engagement_overlays.json ({e}); "
                "starting empty.")
            self._cache = {}

    def _save_locked(self) -> None:
        """Atomic write via tmp + replace. Caller must hold _lock."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._cache, indent=2, ensure_ascii=False),
                encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            logger.exception(
                f"Could not save engagement_overlays.json: {e}")

    # ── Public API ───────────────────────────────────────────────────

    def get(self, client: str, project: str = "") -> EngagementOverlay:
        """Return the overlay for a given engagement scope. Always
        returns an EngagementOverlay; empty-fields when none is stored."""
        key = _safe_key(_scope_key(client, project))
        if not key:
            return EngagementOverlay()
        with self._lock:
            entry = self._cache.get(key) or {}
        return EngagementOverlay(
            status=str(entry.get("status", "") or ""),
            exec_sponsor=str(entry.get("exec_sponsor", "") or ""),
            next_milestone=str(entry.get("next_milestone", "") or ""),
            notes=str(entry.get("notes", "") or ""),
            updated_at=str(entry.get("updated_at", "") or ""),
        )

    def put(
        self, client: str, project: str = "",
        status: str = "",
        exec_sponsor: str = "",
        next_milestone: str = "",
        notes: str = "",
    ) -> EngagementOverlay:
        """Replace the overlay for a given engagement scope. Any
        empty string clears that field."""
        key = _safe_key(_scope_key(client, project))
        if not key:
            raise ValueError("client is required")
        now = datetime.now().isoformat()
        with self._lock:
            self._cache[key] = {
                "status": (status or "").strip(),
                "exec_sponsor": (exec_sponsor or "").strip(),
                "next_milestone": (next_milestone or "").strip(),
                "notes": (notes or "").strip(),
                "updated_at": now,
            }
            self._save_locked()
        logger.info(f"engagement overlay updated: {key}")
        return self.get(client, project)
