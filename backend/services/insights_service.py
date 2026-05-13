"""
Cross-meeting analytics for the "Insights" view.

Computes three families of aggregates from data already on disk
(session JSONs + commitments sidecars + item-status sidecars). No
new storage; every value is derived at query time. The dataset
size here — a few hundred to a few thousand sessions — is well
under the threshold where a linear scan matters, and the cost of
caching across saves/edits is higher than the cost of recomputing.

Three sections:

  1. Time allocation
     - hours per client over the requested window
     - hours per template (General, Requirements Gathering, …)
     - hours per project, scoped to a single client when one is
       passed (so the "Acme projects" breakdown doesn't include
       projects from other clients with the same name)

  2. Recurring topics
     - Per client: top noun-phrase-ish tokens from session
       summaries, weighted by recency. We intentionally do NOT use
       the embedding index for clustering yet — that would
       sometimes group dissimilar things (transcripts of two
       different "AWS" conversations) and erode trust in the
       dashboard. A simple capitalized-multi-word frequency count
       with a stoplist is more predictable for v1; the endpoint
       shape is forward-compatible if we want to swap impls later.

  3. Open-loops health
     - Stale commitments: awaiting + (overdue OR older than 30d)
     - Decisions un-implemented: active (default) older than 30d
     - Unchecked action items: follow-ups without [x] in the
       markdown AND without a "done" override, older than 30d

The markdown parsing here MUST stay in lock-step with the
frontend parsers in src/components/follow-ups-view.tsx and
src/components/decisions-view.tsx because we use the same
hashSource format to look up item-status overrides written by the
existing UI. Changes to either side need to land together.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

from services.item_status_service import compute_item_hash
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Markdown parsers (mirror the frontend) ────────────────────────────

# Follow-up: "- [ ] **Owner**: description (Due: YYYY-MM-DD)"
_FOLLOWUP_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<status>[ xX])\]\s*(?P<rest>.+)$", re.MULTILINE)
_OWNER_RE = re.compile(r"\*\*(?P<owner>[^*]+)\*\*\s*:\s*(?P<desc>.+)")
_DUE_RE = re.compile(r"\(Due:\s*(?P<due>[^)]+)\)", re.IGNORECASE)

# Decision: "## Decision: Title\n- **Decided**: …\n- **Rationale**: …"
_DECISION_BLOCK_RE = re.compile(
    r"##\s*(?:Decision:?\s*)?(?P<title>.+?)\n(?P<body>(?:[-*].*(?:\n|$))+)",
    re.IGNORECASE)
_DECISION_BULLET_RE = re.compile(
    r"^[-*]\s*\*\*(?P<key>[^:*]+)(?::|\*\*:)\s*\*?\*?\s*(?P<value>.*)$",
    re.MULTILINE)


# ── Topic extraction ──────────────────────────────────────────────────

# Tokens we treat as noise. Common verbs/nouns in meeting summaries
# that aren't proper topics. Add as you spot false positives.
_TOPIC_STOP = {
    "the", "and", "for", "with", "from", "this", "that", "they", "them",
    "team", "meeting", "discussion", "discussed", "decision", "next",
    "steps", "step", "follow", "ups", "action", "items", "summary",
    "overview", "review", "session", "call", "agenda", "notes",
    "topic", "topics", "context", "details", "general", "key", "main",
    "today", "tomorrow", "yesterday", "week", "month", "monday",
    "tuesday", "wednesday", "thursday", "friday",
}

# Match a capitalized run of 1-4 words. Picks up "AWS Connect",
# "SimpliSafe", "Salesforce Marketing Cloud" etc. while ignoring
# sentence-leading "The …".
_TOPIC_PHRASE_RE = re.compile(
    r"(?<![A-Za-z])"
    r"(?:[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3})"
    r"(?![A-Za-z])")


def _extract_topics(summary: str) -> List[str]:
    """Return candidate noun-phrase topics from a single summary."""
    if not summary:
        return []
    out: List[str] = []
    for m in _TOPIC_PHRASE_RE.finditer(summary):
        phrase = m.group(0).strip()
        # Drop single-word matches that are purely stop-words
        # (sentence starters like "The", "We"). Keep multi-word
        # phrases unconditionally — those almost never collide
        # with stop-word noise.
        lower = phrase.lower()
        if " " not in phrase and lower in _TOPIC_STOP:
            continue
        # Strip sentence-trailing punctuation that survived the regex.
        phrase = phrase.rstrip(".,;:!?)")
        if not phrase:
            continue
        out.append(phrase)
    return out


# ── Helpers ───────────────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _in_window(
    started_at: Optional[_dt.datetime],
    since: Optional[_dt.datetime],
    until: Optional[_dt.datetime],
) -> bool:
    if started_at is None:
        return False
    if since and started_at < since:
        return False
    if until and started_at >= until:
        return False
    return True


class InsightsService:
    """Aggregator. Holds no state of its own — every call re-reads
    from the underlying services. The cost is a directory scan plus
    one JSON-load per session per call, which is fine at the volumes
    this app sees (and the result is cacheable upstream if it ever
    becomes hot)."""

    def __init__(
        self,
        session_service,
        commitments_service,
        item_status_service,
    ):
        self._sessions = session_service
        self._commitments = commitments_service
        self._item_status = item_status_service

    # ── Public entrypoint ─────────────────────────────────────────────

    def summary(
        self,
        since: Optional[_dt.datetime] = None,
        until: Optional[_dt.datetime] = None,
        client: Optional[str] = None,
        topic_limit: int = 10,
        stale_threshold_days: int = 30,
    ) -> dict:
        """Build the full insights payload in one pass over sessions.
        Returns a JSON-serializable dict matching the shape consumed by
        src/components/insights-view.tsx."""
        sessions = self._sessions.list_sessions()
        now = _dt.datetime.now()
        stale_cutoff = now - _dt.timedelta(days=stale_threshold_days)

        # ── Pass 1: filter into window + client scope ─────────────────
        in_scope: List[dict] = []
        for s in sessions:
            started = _parse_iso(s.get("started_at"))
            if not _in_window(started, since, until):
                continue
            if client and (s.get("client") or "") != client:
                continue
            in_scope.append({**s, "_started": started})

        time_allocation = self._build_time_allocation(in_scope, scoped_to_client=client)
        topics = self._build_recurring_topics(in_scope, topic_limit=topic_limit)
        open_loops = self._build_open_loops(
            sessions, client=client, stale_cutoff=stale_cutoff, now=now)

        return {
            "window": {
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "client": client,
                "session_count": len(in_scope),
                "total_seconds": sum(int(s.get("duration_s") or 0) for s in in_scope),
            },
            "time_allocation": time_allocation,
            "topics": topics,
            "open_loops": open_loops,
        }

    # ── Section 1: time allocation ────────────────────────────────────

    def _build_time_allocation(
        self,
        sessions: List[dict],
        scoped_to_client: Optional[str],
    ) -> dict:
        by_client: Counter = Counter()
        by_template: Counter = Counter()
        by_project: Counter = Counter()
        for s in sessions:
            secs = int(s.get("duration_s") or 0)
            if secs <= 0:
                continue
            c = (s.get("client") or "").strip() or "Untagged"
            by_client[c] += secs
            tmpl = (s.get("template") or "General").strip() or "General"
            by_template[tmpl] += secs
            # Project breakdown only makes sense scoped to a client —
            # otherwise "Phase 1" from Client A and Client B collide.
            if scoped_to_client:
                p = (s.get("project") or "").strip()
                if p:
                    by_project[p] += secs

        def _to_rows(counter: Counter) -> List[dict]:
            return [
                {"label": label, "seconds": secs}
                for label, secs in counter.most_common()
            ]

        return {
            "by_client": _to_rows(by_client),
            "by_template": _to_rows(by_template),
            "by_project": _to_rows(by_project) if scoped_to_client else [],
        }

    # ── Section 2: recurring topics ───────────────────────────────────

    def _build_recurring_topics(
        self,
        sessions: List[dict],
        topic_limit: int,
    ) -> dict:
        """Per-client top topics. We bucket by client first so the UI
        can render a per-client list without re-aggregating."""
        per_client: Dict[str, Counter] = {}
        for s in sessions:
            c = (s.get("client") or "").strip() or "Untagged"
            phrases = _extract_topics(s.get("summary") or "")
            if not phrases:
                continue
            bucket = per_client.setdefault(c, Counter())
            # Deduplicate within one session — a phrase appearing
            # twice in the same summary shouldn't double-weight.
            for phrase in set(phrases):
                bucket[phrase] += 1

        out: Dict[str, List[dict]] = {}
        for c, counter in per_client.items():
            rows = [
                {"phrase": phrase, "count": count}
                for phrase, count in counter.most_common(topic_limit)
                # Drop singletons — they're almost always noise (a
                # proper name mentioned once in one summary), and
                # they make the list look cluttered.
                if count >= 2
            ]
            if rows:
                out[c] = rows
        return out

    # ── Section 3: open loops health ──────────────────────────────────

    def _build_open_loops(
        self,
        all_sessions: List[dict],
        client: Optional[str],
        stale_cutoff: _dt.datetime,
        now: _dt.datetime,
    ) -> dict:
        # Pre-build a quick session metadata lookup so each item gets
        # client/meeting/date without an extra pass.
        sess_lookup: Dict[str, dict] = {
            s["session_id"]: s for s in all_sessions
        }

        # ── Stale commitments ─────────────────────────────────────────
        stale_commitments: List[dict] = []
        try:
            rows = self._commitments.list_all(client=client) if self._commitments else []
        except Exception as e:
            logger.warning(f"Insights: commitments list_all failed: {e}")
            rows = []
        today = now.date()
        for c in rows:
            if c.get("status") != "awaiting":
                continue
            due_iso = c.get("due_date_iso") or ""
            is_overdue = False
            if due_iso:
                try:
                    is_overdue = _dt.datetime.fromisoformat(due_iso).date() < today
                except ValueError:
                    pass
            created = _parse_iso(c.get("created_at"))
            is_old = bool(created and created < stale_cutoff)
            if not (is_overdue or is_old):
                continue
            stale_commitments.append({
                "commitment_id": c.get("commitment_id"),
                "session_id": c.get("session_id"),
                "owner": c.get("owner"),
                "side": c.get("side"),
                "description": c.get("description"),
                "due_date_iso": due_iso,
                "is_overdue": is_overdue,
                "session_display_name": c.get("session_display_name"),
                "session_client": c.get("session_client"),
                "session_started_at": c.get("session_started_at"),
            })

        # ── Pull all overrides up front; matters because we then iterate
        # session markdown to find decisions and follow-ups. ───────────
        try:
            overrides = self._item_status.list_all() if self._item_status else {}
        except Exception as e:
            logger.warning(f"Insights: item_status list_all failed: {e}")
            overrides = {}

        # ── Decisions un-implemented ──────────────────────────────────
        un_implemented_decisions: List[dict] = []
        for s in all_sessions:
            if client and (s.get("client") or "") != client:
                continue
            started = _parse_iso(s.get("started_at"))
            if not started or started >= stale_cutoff:
                # Only flag decisions older than the stale cutoff.
                # Recent decisions get a grace period — they're
                # supposed to still be "active".
                continue
            md = s.get("decisions") or ""
            if not md:
                continue
            sid = s["session_id"]
            sess_overrides = (overrides.get(sid) or {}).get("decisions") or {}
            for d in self._parse_decisions(md):
                h = compute_item_hash(d["hash_source"])
                state = (sess_overrides.get(h) or {}).get("status") or "active"
                if state != "active":
                    continue
                un_implemented_decisions.append({
                    "session_id": sid,
                    "session_display_name": s.get("display_name"),
                    "session_client": s.get("client"),
                    "session_started_at": s.get("started_at"),
                    "title": d["title"],
                    "decided": d["decided"],
                    "item_hash": h,
                })

        # ── Unchecked follow-ups ──────────────────────────────────────
        unchecked_action_items: List[dict] = []
        for s in all_sessions:
            if client and (s.get("client") or "") != client:
                continue
            started = _parse_iso(s.get("started_at"))
            if not started or started >= stale_cutoff:
                continue
            md = s.get("action_items") or ""
            if not md:
                continue
            sid = s["session_id"]
            sess_overrides = (overrides.get(sid) or {}).get("follow_ups") or {}
            for item in self._parse_follow_ups(md):
                if item["done_from_markdown"]:
                    continue
                h = compute_item_hash(item["hash_source"])
                override = sess_overrides.get(h) or {}
                if override.get("done"):
                    continue
                unchecked_action_items.append({
                    "session_id": sid,
                    "session_display_name": s.get("display_name"),
                    "session_client": s.get("client"),
                    "session_started_at": s.get("started_at"),
                    "owner": item["owner"],
                    "description": item["description"],
                    "due": item["due"],
                    "item_hash": h,
                })

        return {
            "stale_commitments": stale_commitments,
            "un_implemented_decisions": un_implemented_decisions,
            "unchecked_action_items": unchecked_action_items,
            "counts": {
                "stale_commitments": len(stale_commitments),
                "un_implemented_decisions": len(un_implemented_decisions),
                "unchecked_action_items": len(unchecked_action_items),
            },
        }

    # ── Markdown parsers (mirror frontend; keep in lock-step) ─────────

    @staticmethod
    def _parse_follow_ups(text: str) -> List[dict]:
        out: List[dict] = []
        for m in _FOLLOWUP_LINE_RE.finditer(text):
            status = (m.group("status") or "").strip().lower()
            rest = (m.group("rest") or "").strip()
            owner = ""
            desc = rest
            om = _OWNER_RE.match(rest)
            if om:
                owner = om.group("owner").strip().strip("[]")
                desc = om.group("desc").strip()
            due = ""
            dm = _DUE_RE.search(desc)
            if dm:
                due = dm.group("due").strip()
                desc = _DUE_RE.sub("", desc).strip()
            out.append({
                "done_from_markdown": status == "x",
                "owner": owner,
                "description": desc,
                "due": due,
                "hash_source": f"{owner.strip()}|{desc.strip()}",
            })
        return out

    @staticmethod
    def _parse_decisions(text: str) -> List[dict]:
        if not text or "no decisions" in text.lower()[:80]:
            return []
        out: List[dict] = []
        for m in _DECISION_BLOCK_RE.finditer(text):
            title = re.sub(r"[*#:]", "", (m.group("title") or "").strip())
            body = m.group("body") or ""
            fields: Dict[str, str] = {}
            for bm in _DECISION_BULLET_RE.finditer(body):
                k = (bm.group("key") or "").strip().lower()
                v = (bm.group("value") or "").strip()
                fields[k] = v
            out.append({
                "title": title,
                "decided": fields.get("decided", ""),
                "hash_source": f"{title}|{fields.get('decided', '')}",
            })
        return out
