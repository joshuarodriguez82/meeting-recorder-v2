"""
Per-session "item status" overlay for follow-ups and decisions.

Follow-ups and decisions are not first-class records in our session
storage — they're parsed at render time from the LLM's markdown
(`session.action_items` and `session.decisions`). The user wants to be
able to "check off" individual items, which means we need a place to
persist a small bit of state per item without rewriting the markdown
itself (which would force a re-extraction to pick up our edits and
would corrupt the LLM-emitted format).

Solution: a sidecar JSON file per session that maps a stable hash of
each item's text to its current status. The frontend computes the
same hash with the same normalization rules and looks up overrides.

Schema (`session_<id>.item_status.json`):
    {
      "follow_ups": {
        "<sha1>": {"done": true, "updated_at": "2026-05-07T17:32:00"}
      },
      "decisions": {
        "<sha1>": {"status": "implemented", "updated_at": "..."}
      }
    }

Hash input is `_normalize_item_text(text)` — lowercased, whitespace
collapsed, trailing punctuation stripped — so minor LLM rewordings
don't drop a "done" mark on re-extraction.

Decision statuses: "active" (default if no override), "implemented",
"superseded". Follow-up status is just a boolean — done or not.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


VALID_DECISION_STATUSES = {"active", "implemented", "superseded"}


def _normalize_item_text(text: str) -> str:
    """Stable normalization shared with the frontend. Keep these rules
    in lock-step with `computeItemHash` in src/lib/api.ts."""
    if not text:
        return ""
    s = text.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\s\.\,\;\:\!\?]+$", "", s)
    return s


def compute_item_hash(text: str) -> str:
    """SHA-1 of the normalized text. Hex digest, lowercased.

    Frontend matches this with crypto.subtle.digest('SHA-1', utf8(text))
    over the same normalized string."""
    norm = _normalize_item_text(text)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _path_for(recordings_dir: Path, session_id: str) -> Path:
    return recordings_dir / f"session_{session_id}.item_status.json"


def _empty_doc() -> dict:
    return {"follow_ups": {}, "decisions": {}}


class ItemStatusService:
    """Read/write the per-session item-status sidecar. Tiny enough that
    we don't need an in-memory cache — sessions are O(few hundred) and
    each file is sub-kilobyte."""

    def __init__(self, session_service):
        self._session_service = session_service
        self._lock = threading.Lock()

    def get(self, session_id: str) -> dict:
        path = _path_for(
            self._session_service.recordings_dir, session_id)
        if not path.exists():
            return _empty_doc()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Defensive: missing buckets default to empty.
            return {
                "follow_ups": data.get("follow_ups") or {},
                "decisions": data.get("decisions") or {},
            }
        except Exception as e:
            logger.warning(f"Could not read {path.name}: {e}")
            return _empty_doc()

    def set_follow_up(
        self, session_id: str, item_hash: str, done: bool,
    ) -> dict:
        return self._mutate(session_id, "follow_ups", item_hash, {
            "done": bool(done),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    def set_decision(
        self, session_id: str, item_hash: str, status: str,
    ) -> dict:
        if status not in VALID_DECISION_STATUSES:
            raise ValueError(
                f"decision status must be one of "
                f"{sorted(VALID_DECISION_STATUSES)}; got {status!r}")
        return self._mutate(session_id, "decisions", item_hash, {
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    def _mutate(
        self, session_id: str, bucket: str, item_hash: str, value: dict,
    ) -> dict:
        if not item_hash or not re.fullmatch(r"[0-9a-f]{40}", item_hash):
            raise ValueError("item_hash must be a 40-char SHA-1 hex digest")
        with self._lock:
            doc = self.get(session_id)
            doc.setdefault(bucket, {})[item_hash] = value
            self._write(session_id, doc)
            return doc

    def _write(self, session_id: str, doc: dict) -> None:
        path = _path_for(
            self._session_service.recordings_dir, session_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(doc, indent=2, ensure_ascii=False),
                encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.warning(f"Could not write {path.name}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def delete_for_session(self, session_id: str) -> bool:
        path = _path_for(
            self._session_service.recordings_dir, session_id)
        try:
            if path.exists():
                path.unlink()
                return True
        except Exception as e:
            logger.warning(f"Could not delete {path.name}: {e}")
        return False

    def list_all(self) -> dict:
        """Aggregate every session's overrides keyed by session_id.
        Used by the cross-session FollowUps/Decisions views which need
        all overrides in one shot."""
        recordings_dir: Path = self._session_service.recordings_dir
        out: dict = {}
        for sidecar in recordings_dir.glob("session_*.item_status.json"):
            session_id = sidecar.name[
                len("session_"):-len(".item_status.json")]
            try:
                doc = json.loads(sidecar.read_text(encoding="utf-8"))
                out[session_id] = {
                    "follow_ups": doc.get("follow_ups") or {},
                    "decisions": doc.get("decisions") or {},
                }
            except Exception:
                continue
        return out
