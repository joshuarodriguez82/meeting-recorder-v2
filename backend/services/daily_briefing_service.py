"""
Daily Briefing storage — one parsed briefing per calendar date.

Backs the "Today" view. The user runs a scheduled prompt in Microsoft
365 Copilot every morning that produces a markdown-ish briefing
(priorities, today's agenda, items awaiting response, FYI items).
M365 Copilot has no API surface that exposes scheduled-prompt output
outside the Copilot UI, so the integration is intentionally manual:
user copies the briefing text → clicks Import in Meeting Recorder →
backend calls the LLM to parse the free-form text into structured
JSON → this service stores it and tracks per-action-item "done" state.

Storage: one file per date at recordings_dir/briefings/YYYY-MM-DD.json.
Per-date so re-importing replaces only today's briefing without
touching history, and so the file format degrades gracefully if we
ever add fields (a missing key on an old briefing just means it
wasn't captured back then).

Action-item completion state is stored INSIDE the briefing JSON
(under the action's `done_at` field) rather than in a sidecar — keeps
all of "what did Joshua see today + what did he finish" co-located.

Atomic write + threading lock match TemplateService / CoPilotModeService.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import date as date_cls, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _today_iso() -> str:
    """Local-date ISO string. Briefings are scoped to the user's
    calendar day, not UTC — running this at 11pm PT should still write
    today's briefing, not tomorrow's."""
    return date_cls.today().isoformat()


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_id(item: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"id": uuid.uuid4().hex[:12]}
    if not item.get("id"):
        item["id"] = uuid.uuid4().hex[:12]
    return item


def _normalize_briefing(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce LLM output (or stored history) into the canonical shape
    the Today view expects. Every list-of-objects field gets stable
    ids, every text field gets defaulted to '', missing list keys
    become []. Idempotent so we can run it on both fresh parses and
    rehydrated disk data."""
    if not isinstance(raw, dict):
        raw = {}

    out: Dict[str, Any] = {
        "date": str(raw.get("date") or _today_iso()),
        "imported_at": str(raw.get("imported_at") or _now_iso_utc()),
        "source": str(raw.get("source") or "m365-copilot"),
        "greeting": (raw.get("greeting") or "").strip(),
        "top_priority": _normalize_top_priority(raw.get("top_priority")),
        "needs_response": [
            _normalize_action(x) for x in (raw.get("needs_response") or [])
            if isinstance(x, dict)
        ],
        "agenda": [
            _normalize_agenda(x) for x in (raw.get("agenda") or [])
            if isinstance(x, dict)
        ],
        "schedule_notes": [
            str(x).strip() for x in (raw.get("schedule_notes") or [])
            if str(x).strip()
        ],
        "fyi": [
            _normalize_fyi(x) for x in (raw.get("fyi") or [])
            if isinstance(x, dict)
        ],
        "raw_text": raw.get("raw_text") or "",
    }
    return out


def _normalize_top_priority(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    return {
        "title": title[:200],
        "detail": (raw.get("detail") or "").strip()[:600],
        "why": (raw.get("why") or "").strip()[:400],
    }


def _normalize_action(raw: Dict[str, Any]) -> Dict[str, Any]:
    item = _ensure_id(dict(raw))
    item["title"] = (item.get("title") or "").strip()[:200]
    item["detail"] = (item.get("detail") or "").strip()[:600]
    item["who"] = (item.get("who") or "").strip()[:120]
    item["due"] = (item.get("due") or "").strip()[:80]
    item["source"] = (item.get("source") or "").strip()[:120]
    # done_at is None when open, ISO string when checked off.
    raw_done = item.get("done_at")
    item["done_at"] = raw_done if isinstance(raw_done, str) and raw_done else None
    return item


def _normalize_agenda(raw: Dict[str, Any]) -> Dict[str, Any]:
    item = _ensure_id(dict(raw))
    item["title"] = (item.get("title") or "").strip()[:200]
    item["time"] = (item.get("time") or "").strip()[:60]
    # start_iso / end_iso / join_url are ADDITIVE (v2.22.2). `time` is a
    # display string with no date and no timezone, which is fine for the
    # Today tab but can't place a meeting on the Record tab's timeline
    # or dedupe it against a local Outlook invite. Kept as raw strings —
    # validation and the date+time fallback live in
    # services/extension_calendar_service.py, so a malformed LLM value
    # degrades to "no event" rather than breaking the Today tab.
    # Missing keys on briefings stored before this change read as ""
    # (field report 2026-08-11).
    item["start_iso"] = (item.get("start_iso") or "").strip()[:40]
    item["end_iso"] = (item.get("end_iso") or "").strip()[:40]
    item["join_url"] = (item.get("join_url") or "").strip()[:1000]
    item["duration"] = (item.get("duration") or "").strip()[:40]
    item["role"] = (item.get("role") or "").strip().lower()[:30]
    item["meeting_type"] = (item.get("meeting_type") or "").strip().lower()[:30]
    item["client"] = (item.get("client") or "").strip()[:120]
    item["attendees"] = [
        str(a).strip() for a in (item.get("attendees") or [])
        if str(a).strip()
    ][:20]
    item["notes"] = (item.get("notes") or "").strip()[:600]
    # status: "scheduled" | "cancelled" | "now" | "done"
    raw_status = (item.get("status") or "scheduled").strip().lower()
    if raw_status not in {"scheduled", "cancelled", "now", "done"}:
        raw_status = "scheduled"
    item["status"] = raw_status
    return item


def _normalize_fyi(raw: Dict[str, Any]) -> Dict[str, Any]:
    item = _ensure_id(dict(raw))
    item["title"] = (item.get("title") or "").strip()[:200]
    item["detail"] = (item.get("detail") or "").strip()[:600]
    item["category"] = (item.get("category") or "").strip().lower()[:40]
    return item


class BriefingUnreadableError(RuntimeError):
    """The briefing file for a date EXISTS but could not be read/parsed.

    Deliberately distinct from "no briefing saved for this date".
    Collapsing the two into None meant a transient read failure (cloud
    sync stall, partial write, locked file) reached the Today tab as an
    empty {} — indistinguishable from a first run — so the tab rendered
    its "import a briefing" onboarding screen and looked like the day's
    briefing had been lost. It hadn't; it just couldn't be read that
    second.
    """


class DailyBriefingService:
    """Thread-safe per-date storage for parsed daily briefings."""

    def __init__(self, data_dir: Path):
        self._root = Path(data_dir) / "briefings"
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, date_iso: str) -> Path:
        # Defensive — only allow YYYY-MM-DD to avoid path traversal via
        # a maliciously crafted date string from the frontend.
        try:
            date_cls.fromisoformat(date_iso)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid date: {date_iso!r}")
        return self._root / f"{date_iso}.json"

    def _read_locked(self, date_iso: str,
                     strict: bool = False) -> Optional[Dict[str, Any]]:
        """Read one date's briefing.

        `strict=True` raises BriefingUnreadableError when the file is
        present but unreadable, so callers can tell "couldn't read it"
        apart from "there isn't one". Writers pass strict=False: a
        corrupt file must stay overwritable, not become permanent.
        """
        p = self._path_for(date_iso)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"briefing {date_iso} unreadable ({e})")
            if strict:
                raise BriefingUnreadableError(str(e)) from e
            return None

    def _write_locked(self, date_iso: str, data: Dict[str, Any]) -> None:
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

    # --- public API ---

    def get(self, date_iso: Optional[str] = None) -> Optional[Dict[str, Any]]:
        d = date_iso or _today_iso()
        with self._lock:
            raw = self._read_locked(d, strict=True)
        if raw is None:
            return None
        return _normalize_briefing(raw)

    def save_parsed(self, parsed: Dict[str, Any],
                    raw_text: str = "",
                    date_iso: Optional[str] = None) -> Dict[str, Any]:
        """Store a freshly-parsed briefing for the given date (default
        today). Returns the normalized briefing.

        If a briefing already exists for the date, merges the existing
        action-item `done_at` flags by title so re-importing doesn't
        wipe what the user has already checked off this morning."""
        d = date_iso or _today_iso()
        body = dict(parsed or {})
        body["date"] = d
        body["imported_at"] = _now_iso_utc()
        body["raw_text"] = raw_text or body.get("raw_text") or ""

        normalized = _normalize_briefing(body)

        with self._lock:
            prior = self._read_locked(d)
            if prior:
                prior_norm = _normalize_briefing(prior)
                done_by_title = {
                    (a.get("title") or "").strip().lower(): a.get("done_at")
                    for a in prior_norm.get("needs_response") or []
                    if a.get("done_at")
                }
                if done_by_title:
                    for a in normalized["needs_response"]:
                        key = (a.get("title") or "").strip().lower()
                        if key and key in done_by_title and not a.get("done_at"):
                            a["done_at"] = done_by_title[key]
            self._write_locked(d, normalized)

        logger.info(
            f"Stored briefing for {d}: "
            f"top_priority={'yes' if normalized.get('top_priority') else 'no'}, "
            f"agenda={len(normalized['agenda'])}, "
            f"needs_response={len(normalized['needs_response'])}, "
            f"fyi={len(normalized['fyi'])}"
        )
        return normalized

    def set_action_status(self, date_iso: str, action_id: str,
                          done: bool) -> Optional[Dict[str, Any]]:
        """Toggle a needs_response action's done state. Returns the
        updated briefing, or None if no briefing exists for that date.
        Unknown action_ids are silently ignored — the frontend lists
        actions from the same briefing, so any mismatch is a stale
        client state, not a hard error worth surfacing."""
        with self._lock:
            raw = self._read_locked(date_iso)
            if raw is None:
                return None
            normalized = _normalize_briefing(raw)
            for action in normalized["needs_response"]:
                if action.get("id") == action_id:
                    action["done_at"] = _now_iso_utc() if done else None
                    break
            self._write_locked(date_iso, normalized)
        return normalized

    def list_dates(self, limit: int = 30) -> List[str]:
        """Most-recent first. Used by future history UI; not on the
        critical path for v2.10."""
        out: List[str] = []
        for p in self._root.glob("*.json"):
            stem = p.stem
            try:
                date_cls.fromisoformat(stem)
            except ValueError:
                continue
            out.append(stem)
        out.sort(reverse=True)
        return out[:limit]
