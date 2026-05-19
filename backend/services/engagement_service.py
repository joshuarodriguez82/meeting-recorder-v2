"""Engagement register — Phase 2.

Phase 1 turned each session's extractions into typed records. One SA
engagement is *many* sessions for a client (optionally scoped to a
project). This service rolls those per-session records up into a
single living register: open requirements, a decision/ADR log, open
action items, and open questions — deduped across sessions with
provenance (which session, when) preserved.

Dedupe is deliberately conservative: normalized-text exact match only
(lowercase, collapse whitespace, strip surrounding quotes / trailing
punctuation). Near-duplicate detection via embeddings is an explicit
future refinement — over-eager merging silently drops a real
requirement, which is worse than a visible near-dup the SA can eyeball
and merge by hand later.

The register is a derived view: recomputed on demand from the session
JSONs (cheap — an engagement is tens of sessions, not thousands) and
written to recordings_dir/engagement_<key>.register.json as a cache
artifact that Phase 3 export / handoff can consume without recomputing.
"""

from __future__ import annotations

import datetime
import json
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from models.session import Session
from utils.logger import get_logger

logger = get_logger(__name__)

# Records whose status is one of these are considered resolved and
# drop out of the "open" counts (but stay in the list, annotated, so
# the history isn't lost).
_RESOLVED = {"met", "dropped", "done", "answered"}


def _norm(text: str) -> str:
    """Dedupe key: lowercase, collapse internal whitespace, strip
    surrounding quotes and trailing sentence punctuation. Conservative
    on purpose — only collapses things that are the *same* item phrased
    with trivial differences, never paraphrases."""
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.strip("\"'“”‘’ ")
    s = re.sub(r"[.;:,]+$", "", s)
    return s


def _scope_key(client: str, project: str = "") -> str:
    c = (client or "").strip().lower()
    p = (project or "").strip().lower()
    return f"{c}__{p}" if p else c


class EngagementService:
    def __init__(self, session_svc, client_cfg_svc=None):
        self._sessions = session_svc
        self._client_cfg = client_cfg_svc

    # ---- public API -------------------------------------------------

    def build_register(self, client: str, project: str = "") -> dict:
        """Compute the register for client (+ optional project), write
        the cache artifact, and return it."""
        client_key = (client or "").strip().lower()
        project_key = (project or "").strip().lower()
        if not client_key:
            raise ValueError("client is required")

        # Find matching sessions via the cheap summary scan, oldest
        # first so "first seen" provenance is the earliest occurrence.
        matched = [
            s for s in self._sessions.list_sessions()
            if (s.get("client") or "").strip().lower() == client_key
            and (not project_key
                 or (s.get("project") or "").strip().lower() == project_key)
        ]
        matched.sort(key=lambda s: s.get("started_at") or "")

        loaded: List[Tuple[dict, Session]] = []
        for meta in matched:
            full = self._sessions.load_full(meta["session_id"])
            if full is not None:
                loaded.append((meta, full))

        body = self._aggregate(loaded)
        register = {
            "client": self._display_client(client),
            "project": project or "",
            "generated_at": datetime.datetime.now().isoformat(),
            "session_count": len(loaded),
            **body,
        }
        self._write_cache(client_key, project_key, register)
        return register

    # ---- pure aggregation core (unit-tested without the server) -----

    @staticmethod
    def _aggregate(loaded: List[Tuple[dict, Session]]) -> dict:
        """Roll per-session typed records up into deduped, provenanced
        lists. `loaded` must be ordered oldest-session first."""

        def provenance(meta: dict, rec) -> dict:
            return {
                "session_id": meta.get("session_id", ""),
                "display_name": meta.get("display_name", "") or "",
                "at": rec.created_at or meta.get("started_at") or "",
            }

        def rollup(items_by_session, key_of, base_of):
            """items_by_session: list of (meta, [records]).
            Collapses by key_of(record); first occurrence is canonical;
            every occurrence is recorded; status resolves to a terminal
            value if any occurrence reached one."""
            agg: Dict[str, dict] = {}
            for meta, recs in items_by_session:
                for rec in recs:
                    k = key_of(rec)
                    if not k:
                        continue
                    if k not in agg:
                        entry = base_of(rec)
                        entry["occurrences"] = []
                        entry["status"] = getattr(rec, "status", "open") or "open"
                        agg[k] = entry
                    entry = agg[k]
                    entry["occurrences"].append(provenance(meta, rec))
                    st = getattr(rec, "status", "open") or "open"
                    # A terminal status anywhere wins — if it was ever
                    # marked met/done it stays resolved in the roll-up.
                    if st in _RESOLVED:
                        entry["status"] = st
            return list(agg.values())

        reqs = rollup(
            [(m, s.requirements_struct) for m, s in loaded],
            key_of=lambda r: _norm(r.text),
            base_of=lambda r: {
                "id": r.id, "text": r.text, "kind": r.kind,
                "source": r.source,
            },
        )
        acts = rollup(
            [(m, s.action_items_struct) for m, s in loaded],
            key_of=lambda r: _norm(r.text),
            base_of=lambda r: {
                "id": r.id, "text": r.text, "owner": r.owner,
                "due": r.due, "source": r.source,
            },
        )
        qs = rollup(
            [(m, s.open_questions) for m, s in loaded],
            key_of=lambda r: _norm(r.text),
            base_of=lambda r: {"id": r.id, "text": r.text, "source": r.source},
        )
        # Decisions dedupe on title+decided (a decision restated in a
        # later call is the same decision, not a new one) and surface
        # newest-first — the ADR log reads most-recent-decision-down.
        decs = rollup(
            [(m, s.decisions_struct) for m, s in loaded],
            key_of=lambda d: _norm(d.title + "|" + d.decided),
            base_of=lambda d: {
                "id": d.id, "title": d.title, "decided": d.decided,
                "rationale": d.rationale, "alternatives": d.alternatives,
                "owner": d.owner, "impact": d.impact, "source": d.source,
            },
        )
        decs.sort(
            key=lambda d: d["occurrences"][-1]["at"] if d["occurrences"] else "",
            reverse=True,
        )

        def open_first(rows):
            # Open items float to the top; resolved ones sink but stay
            # for history.
            rows.sort(key=lambda r: r.get("status", "open") in _RESOLVED)
            return rows

        reqs, acts, qs = open_first(reqs), open_first(acts), open_first(qs)
        is_open = lambda r: r.get("status", "open") not in _RESOLVED
        return {
            "counts": {
                "open_requirements": sum(1 for r in reqs if is_open(r)),
                "decisions": len(decs),
                "open_action_items": sum(1 for a in acts if is_open(a)),
                "open_questions": sum(1 for q in qs if is_open(q)),
            },
            "requirements": reqs,
            "decisions": decs,
            "action_items": acts,
            "open_questions": qs,
        }

    # ---- helpers ----------------------------------------------------

    def _display_client(self, client: str) -> str:
        if self._client_cfg is not None:
            try:
                cfg = self._client_cfg.get(client)
                if cfg and getattr(cfg, "display_name", ""):
                    return cfg.display_name
            except Exception:
                pass
        return (client or "").strip()

    def _write_cache(self, client_key: str, project_key: str,
                     register: dict) -> None:
        """Atomic write so a reader (Phase 3 export) never sees a
        half-written register. Best-effort: a cache write failure must
        not fail the request — the register is already computed."""
        try:
            rec_dir: Path = self._sessions.recordings_dir
            name = f"engagement_{_scope_key(client_key, project_key)}.register.json"
            # Keep the filename filesystem-safe (client names are free
            # text — slashes, colons, etc.).
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            dest = rec_dir / name
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=rec_dir, delete=False,
                suffix=".tmp",
            ) as tf:
                json.dump(register, tf, ensure_ascii=False, indent=2)
                tmp = Path(tf.name)
            tmp.replace(dest)
        except Exception as e:
            logger.warning(f"engagement register cache write failed: {e}")
