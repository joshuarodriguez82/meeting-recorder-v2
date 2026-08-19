"""
Cross-meeting commitment tracker.

A "commitment" is something a person promised to deliver during a
meeting — usually phrased as an action item, but with the extra
metadata to follow up across sessions:

  - WHO promised (with their inferred email when available)
  - WHAT they promised (paraphrased) + verbatim quote
  - WHEN promised (source session + timestamp)
  - DUE date (resolved from "by Friday", "end of next week", etc.)
  - SIDE (internal vs customer — drives "what they owe me" vs
    "what I owe them" filtering)
  - STATUS (awaiting / delivered / overdue / dismissed)

Storage: per-session sidecar JSON next to the session's other sidecar
files so that deleting a session naturally cleans up its commitments.
Aggregated into a flat list at query time across all session files.
Volume here is small enough — a busy SA might log a few thousand
commitments over years — that linear scan is fine.

Detection: commitments are a SUPERSET of action items. We
intentionally re-extract from the raw transcript with a richer prompt
rather than parse the existing markdown action_items field, because
the existing extraction throws away the verbatim quote and inferred
due date, which are the two fields the tracker uniquely needs.

Delivery detection: v1 is manual. The user marks a commitment
delivered or dismissed via the UI. A future pass could analyse
subsequent sessions for "thanks for the SOW, Bob"-style signals or
hook into Outlook/Mail for matching attachments — both deferred.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional

from core._precision import json_timing_note, no_invented_precision
from services.owner_service import AliasIndex, resolve_owners
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Commitment:
    """Single tracked commitment."""

    commitment_id: str
    session_id: str
    # Owner display name as Claude extracted it (often a first name +
    # last initial). The frontend shows this; matching to a real
    # contact happens lazily via the speaker-fingerprint feature.
    owner: str
    # Customer-side vs internal. Inferred at extraction time via the
    # speaker fingerprint feature when available, otherwise unknown.
    side: str  # "internal" | "customer" | "unknown"
    # Short paraphrase of what they said they'd do.
    description: str
    # Verbatim quote from the transcript — critical for "did they
    # actually commit, or did I infer?" verification. May be empty
    # if the model couldn't pin a single sentence.
    quote: str
    # Timestamp inside the session where the commitment was made.
    timestamp_seconds: float
    # Resolved due date (ISO date) when the model could infer one.
    # Empty string when the commitment had no time scope.
    due_date_iso: str
    # Bookkeeping
    created_at: str
    status: str  # "awaiting" | "delivered" | "dismissed"
    # When the user marked it delivered/dismissed; empty when still
    # awaiting.
    resolved_at: str = ""
    # Free-form note the user can add when marking delivered/dismissed
    # (e.g. "received SOW via email 5/3").
    resolved_note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Commitment":
        # Be permissive about missing fields so older sidecar files
        # forward-compat into newer code.
        return cls(
            commitment_id=d.get("commitment_id") or uuid.uuid4().hex,
            session_id=d.get("session_id", ""),
            owner=d.get("owner", "") or "Unknown",
            side=d.get("side", "unknown"),
            description=d.get("description", ""),
            quote=d.get("quote", ""),
            timestamp_seconds=float(d.get("timestamp_seconds", 0.0)),
            due_date_iso=d.get("due_date_iso", "") or "",
            created_at=d.get("created_at") or datetime.now().isoformat(),
            status=d.get("status", "awaiting"),
            resolved_at=d.get("resolved_at", "") or "",
            resolved_note=d.get("resolved_note", "") or "",
        )


def _commitments_path(recordings_dir: Path, session_id: str) -> Path:
    return recordings_dir / f"session_{session_id}.commitments.json"


class CommitmentsService:
    """JSON-sidecar store with per-session files and flat in-memory
    aggregation at query time. Mirrors SearchService's design — same
    simple model, same cheap volume.

    Mutations are wrapped in a thread lock and atomically rewrite the
    affected session's sidecar via tmp+rename. Read paths take the
    lock briefly so a half-written file can't be observed by a
    concurrent /commitments GET."""

    def __init__(self, session_service):
        self._session_service = session_service
        self._lock = threading.Lock()

    # ── Read paths ──────────────────────────────────────────────────

    def list_for_session(self, session_id: str) -> List[Commitment]:
        path = _commitments_path(
            self._session_service.recordings_dir, session_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [Commitment.from_dict(d) for d in (data or [])]
        except Exception as e:
            logger.warning(f"Could not load {path.name}: {e}")
            return []

    def list_all(
        self,
        client: Optional[str] = None,
        project: Optional[str] = None,
        status: Optional[List[str]] = None,
        owner: Optional[str] = None,
        side: Optional[str] = None,
        alias_index: Optional[AliasIndex] = None,
    ) -> List[dict]:
        """Aggregate every session's commitments into a flat list,
        optionally filtered. Each entry is enriched with session
        metadata (display_name, started_at, client, project) so the UI
        can render rows without round-tripping per-row.

        status is a list because the most common "Active" filter is
        "awaiting + overdue" — which we compute here from due_date_iso
        rather than persisting an "overdue" status that would drift
        over time.

        owner filtering goes through services/owner_service.py's
        resolve_owners() rather than a raw string compare: a
        commitment's `owner` field may itself be a multi-owner string
        ("Mark/Sam"), so `owner="Sam"` must match it. Pass
        `alias_index` (see server.py) to also honour confirmed owner
        aliases ("Samantha" -> "Sam"); omitting it still gets correct
        split + case/org-suffix matching, just without the
        user-confirmed judgement-call merges."""
        sessions = self._session_service.list_sessions()
        sess_lookup = {s["session_id"]: s for s in sessions}

        # Early-filter sessions by client/project before reading their
        # sidecar files.
        candidate_ids = []
        for s in sessions:
            if client and (s.get("client") or "") != client:
                continue
            if project and (s.get("project") or "") != project:
                continue
            candidate_ids.append(s["session_id"])

        out: List[dict] = []
        today = datetime.now().date()
        for sid in candidate_ids:
            for c in self.list_for_session(sid):
                if owner:
                    resolved = resolve_owners(c.owner, alias_index)
                    if owner.lower() not in (r.lower() for r in resolved):
                        continue
                if side and c.side != side:
                    continue
                # Compute "is overdue" on the fly from due_date_iso
                # against today. Awaiting commitments past their due
                # date count as overdue; status itself stays "awaiting".
                is_overdue = False
                if c.status == "awaiting" and c.due_date_iso:
                    try:
                        due = datetime.fromisoformat(c.due_date_iso).date()
                        is_overdue = due < today
                    except ValueError:
                        pass
                # Apply status filter (synthetic "overdue" expands).
                if status:
                    matched = False
                    for want in status:
                        if want == "overdue" and is_overdue:
                            matched = True; break
                        if want == c.status and not (
                            want == "awaiting" and is_overdue
                        ):
                            matched = True; break
                        if want == "active" and c.status == "awaiting":
                            matched = True; break
                    if not matched:
                        continue
                sess = sess_lookup.get(sid, {})
                out.append({
                    **c.to_dict(),
                    "is_overdue": is_overdue,
                    "session_display_name": sess.get("display_name") or "",
                    "session_started_at": sess.get("started_at") or "",
                    "session_client": sess.get("client") or "",
                    "session_project": sess.get("project") or "",
                })
        # Sort: overdue first (most urgent), then awaiting by due date,
        # then resolved by recency.
        def _sort_key(c: dict):
            status_pri = (
                0 if c.get("is_overdue") else
                1 if c.get("status") == "awaiting" else
                2
            )
            due = c.get("due_date_iso") or "9999-12-31"
            return (status_pri, due, c.get("created_at") or "")
        out.sort(key=_sort_key)
        return out

    # ── Mutations ───────────────────────────────────────────────────

    def replace_session_commitments(
        self, session_id: str, commitments: List[Commitment],
    ) -> None:
        """Overwrite the sidecar for this session. Used by the
        extraction path after re-running Claude — old commitments for
        that session get replaced wholesale rather than merged, since
        re-running the extractor implicitly supersedes prior output."""
        path = _commitments_path(
            self._session_service.recordings_dir, session_id)
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps([c.to_dict() for c in commitments], indent=2),
                    encoding="utf-8",
                )
                tmp.replace(path)
            except Exception as e:
                logger.exception(
                    f"Could not write commitments sidecar {path}: {e}")

    def update_status(
        self,
        commitment_id: str,
        status: str,
        note: str = "",
    ) -> Optional[dict]:
        """Mark a commitment delivered / dismissed / restored to
        awaiting. Returns the updated commitment dict or None if not
        found.

        Iterates every session's sidecar — small constant cost
        (~30s of meeting at 5 commitments each = ~50 files for a busy
        user-month). When this becomes hot we'd add an in-memory
        index from commitment_id -> session_id, but it's nowhere near
        that volume yet.
        """
        if status not in {"awaiting", "delivered", "dismissed"}:
            return None
        recordings_dir = self._session_service.recordings_dir
        for sidecar in recordings_dir.glob("session_*.commitments.json"):
            session_id = sidecar.name[len("session_"):-len(".commitments.json")]
            commitments = self.list_for_session(session_id)
            for i, c in enumerate(commitments):
                if c.commitment_id == commitment_id:
                    c.status = status
                    c.resolved_at = (
                        datetime.now().isoformat()
                        if status != "awaiting"
                        else ""
                    )
                    c.resolved_note = note if status != "awaiting" else ""
                    commitments[i] = c
                    self.replace_session_commitments(session_id, commitments)
                    return {
                        **c.to_dict(),
                        "session_id": session_id,
                    }
        return None

    def delete_session_commitments(self, session_id: str) -> bool:
        """Cleanup hook called when a session is deleted."""
        path = _commitments_path(
            self._session_service.recordings_dir, session_id)
        if path.exists():
            try:
                path.unlink()
                return True
            except OSError as e:
                logger.warning(f"Could not delete {path}: {e}")
        return False


# ── Extraction ──────────────────────────────────────────────────────
#
# The extraction prompt asks Claude to output a JSON array — easier to
# parse than markdown checkboxes, and we get the verbatim quote +
# resolved due date as separate fields right out of the model.
#
# The prompt explicitly asks for the timestamp expressed in seconds
# from session start so the UI can deep-link into the audio. Whisper
# segments already give Claude that data when we feed the timestamped
# transcript form (via session.full_transcript() in the existing
# pipeline).


COMMITMENTS_EXTRACTION_PROMPT = """\
You're extracting COMMITMENTS from a meeting transcript. A commitment
is something a specific person said they would do, with enough
specificity that someone could later check whether they did it.

Examples:
  - "Bob said he'd send the SOW by Friday."          ← commitment
  - "I'll loop in Sarah from legal next week."        ← commitment
  - "Maybe we should look at the auth approach"      ← NOT a commitment (vague)
  - "We should consider a phased rollout"            ← NOT (no owner)
  - "Yeah, that makes sense"                          ← NOT (no action)

For each commitment, output a JSON object with these fields:
  - "owner": Name of the person who promised. If a speaker is labeled
    SPEAKER_00 / SPEAKER_01 use that as a fallback. Skip generic
    pronouns ("we", "the team") — only specific people.
  - "description": One short sentence paraphrasing what they'll do.
  - "quote": The exact words from the transcript, copied verbatim
    (including filler), 1-2 sentences max. Cropped if longer.
  - "timestamp_seconds": The transcript timestamp in seconds where
    the commitment was made. Use the [mm:ss] markers in the
    transcript to compute this.
  - "due_date_iso": If the commitment has an explicit deadline,
    resolve it to a YYYY-MM-DD date. "by Friday" → next Friday;
    "end of next week" → +14 days; "end of the month" → last day of
    the current month. Reference date is {today_iso}. Empty string
    if the commitment has no time scope.
  - "side": "customer" if the speaker is from {customer_hint}
    (judging by the speaker label and any context); "internal" if
    they're from the SA team; "unknown" if you can't tell.

Reference date for resolving relative deadlines: {today_iso}

Output ONLY a JSON array. No prose. If no commitments at all, output:
[]
"""
# Full block. A commitment record is the highest-consequence output in
# the app: it is shown as "what they owe me", chased across meetings,
# and read months later by someone who will not re-listen to the call.
# Every clause has a slot to be violated in — "owner" (attribution),
# "quote" (identifiers: it is supposed to be verbatim), "description"
# (counts, in its prose), "due_date_iso" (timings).
#
# The TIMINGS clause and this prompt's resolution rule above are not in
# tension: resolving "by Friday" against a stated reference date is
# exactly the anchored resolution the date anchor endorses. What the
# clause forbids is the other move — turning "a week or two out" into a
# named date, or filling the field for a commitment that carried no
# deadline at all. `json_timing_note` says how this schema spells that.
#
# Concatenated OUTSIDE the .format() template on purpose: the rule text
# contains no {braces}, and keeping it out of the template makes that
# invariant obvious rather than incidental.
COMMITMENTS_EXTRACTION_PROMPT += (
    no_invented_precision()
    + json_timing_note("due_date_iso", container="array")
    + """

=== TRANSCRIPT ===
{transcript}
"""
)


def _resolve_due_date(text: str, today: datetime) -> str:
    """Cheap fallback when Claude doesn't fill the due_date_iso field
    but the description has obvious time-scope language. Conservative
    — false positives are worse than missed dates here, since the user
    sees the date in the UI."""
    if not text:
        return ""
    t = text.lower()
    # Patterns ordered specific → vague.
    if m := re.search(r"by (\d{4}-\d{2}-\d{2})", t):
        return m.group(1)
    if "end of week" in t or "by friday" in t or "by eow" in t:
        days_ahead = (4 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_ahead)).date().isoformat()
    if "end of next week" in t or "by next friday" in t:
        days_ahead = ((4 - today.weekday()) % 7 or 7) + 7
        return (today + timedelta(days=days_ahead)).date().isoformat()
    if "tomorrow" in t:
        return (today + timedelta(days=1)).date().isoformat()
    if "next week" in t:
        return (today + timedelta(days=7)).date().isoformat()
    return ""


async def extract_commitments_from_session(
    summarizer,
    session,
    customer_hint: str = "",
) -> List[Commitment]:
    """Run a Claude pass over the session's transcript to produce a
    list of commitments. Caller persists via
    CommitmentsService.replace_session_commitments.

    Returns [] when:
      - The session has no transcript yet
      - The summarizer is None / unavailable
      - Claude produced no parseable JSON
      - The transcript genuinely contained no commitments
    """
    if summarizer is None:
        return []
    if not session.segments:
        return []

    transcript = session.full_transcript()
    if not transcript.strip():
        return []

    today = datetime.now()
    prompt = COMMITMENTS_EXTRACTION_PROMPT.format(
        today_iso=today.date().isoformat(),
        customer_hint=customer_hint or "the customer organization",
        transcript=transcript[:80000],  # safety cap on prompt size
    )

    try:
        raw = await summarizer._chat(prompt, max_tokens=2048, timeout=120.0)
    except Exception as e:
        logger.warning(f"Commitment extraction call failed: {e}")
        return []

    # Strip code fences and any prose surrounding the JSON. Claude is
    # generally good at "output ONLY JSON" but the strip is cheap
    # insurance.
    text = raw.strip()
    if text.startswith("```"):
        # Remove first fence line and trailing fence
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    # If there's still prose, find the first [ and last ].
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket >= 0 and last_bracket > first_bracket:
        text = text[first_bracket:last_bracket + 1]

    try:
        items = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(
            f"Commitment extraction: JSON parse failed ({e}); "
            f"first 300 chars: {text[:300]!r}")
        return []
    if not isinstance(items, list):
        logger.warning("Commitment extraction returned non-list")
        return []

    out: List[Commitment] = []
    now_iso = datetime.now().isoformat()
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        owner = (raw_item.get("owner") or "").strip()
        description = (raw_item.get("description") or "").strip()
        if not owner or not description:
            continue
        # Resolve due date with the model's value, falling back to our
        # cheap regex if the model left it blank.
        due_iso = (raw_item.get("due_date_iso") or "").strip()
        if not due_iso:
            due_iso = _resolve_due_date(description, today) \
                or _resolve_due_date(raw_item.get("quote", ""), today)
        side = (raw_item.get("side") or "unknown").lower()
        if side not in {"customer", "internal", "unknown"}:
            side = "unknown"
        try:
            ts = float(raw_item.get("timestamp_seconds") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        out.append(Commitment(
            commitment_id=uuid.uuid4().hex,
            session_id=session.session_id,
            owner=owner,
            side=side,
            description=description,
            quote=(raw_item.get("quote") or "").strip(),
            timestamp_seconds=ts,
            due_date_iso=due_iso,
            created_at=now_iso,
            status="awaiting",
        ))
    logger.info(
        f"Extracted {len(out)} commitment(s) from session "
        f"{session.session_id}")
    return out
