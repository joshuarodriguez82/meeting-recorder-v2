"""
"What I missed" — a catch-up brief for a meeting you weren't in.

WHY
---
A summary is written for the record: everything that happened, evenly.
Someone handed the recording of a meeting they missed needs something
narrower and in a different order — what was decided, what is now
expected of them or their team, what is still open, and what is
different from the last time they met this client — and where in the
recording to look if they want to hear it for themselves.

So this is its own short brief, made automatically for imported
meetings (the ones most likely to have been missed) and on request for
any other. It is written for the reader: the asks are the ones aimed at
them or their organisation, which the app knows only from the reader's
own email address (Settings → follow-up email), so that is what it is
told.

The pure parts live here — which earlier meeting to compare with, the
reader's context, how a reply is applied, the exported document. The
model call is Summarizer.catch_up.
"""

from __future__ import annotations

import re
from typing import List, Optional

MAX_ITEMS = 8
MAX_TEXT = 400
#: The previous meeting's summary sent for "what changed".
MAX_PREVIOUS_CHARS = 4000

_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def reader_context(email: str) -> str:
    """Who the brief is for, as far as the app knows: their email's
    organisation domain. "" when no email is set."""
    email = (email or "").strip()
    if "@" not in email:
        return ""
    domain = email.rsplit("@", 1)[1].lower()
    return (f"The reader works at the organisation whose email domain is "
            f"{domain}. Treat people from that organisation as the "
            f"reader's team.")


def previous_meeting(rows: List[dict], session) -> Optional[dict]:
    """The most recent earlier meeting with the same client that has a
    summary, from list_sessions() rows; None if there isn't one."""
    client = (getattr(session, "client", "") or "").strip().lower()
    started = getattr(session, "started_at", None)
    if not client or started is None:
        return None
    mine = getattr(session, "session_id", None)
    started_iso = started.isoformat() if hasattr(started, "isoformat") \
        else str(started)
    best = None
    for r in rows:
        if r.get("session_id") == mine or not r.get("summary"):
            continue
        if (r.get("client") or "").strip().lower() != client:
            continue
        when = str(r.get("started_at") or "")
        if not when or when >= started_iso:
            continue
        if best is None or when > str(best.get("started_at") or ""):
            best = r
    return best


def previous_context(row: Optional[dict]) -> str:
    if not row:
        return ""
    text = str(row.get("summary") or "")[:MAX_PREVIOUS_CHARS]
    when = str(row.get("started_at") or "")[:10]
    name = row.get("display_name") or "the previous meeting"
    return (f"The previous meeting with this client was \"{name}\" on "
            f"{when}. Its summary:\n{text}")


def _items(value) -> List[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, dict):
            v = " — ".join(str(x) for x in v.values() if x)
        s = " ".join(str(v or "").split())
        if s:
            out.append(s[:MAX_TEXT])
    return out[:MAX_ITEMS]


def _moments(value) -> List[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if not isinstance(v, dict):
            continue
        at = str(v.get("at") or "").strip()
        why = " ".join(str(v.get("why") or "").split())[:MAX_TEXT]
        if _TIME.match(at) and why:
            out.append({"at": at, "why": why})
    return out[:5]


def apply_reply(reply: dict, previous: Optional[dict]) -> dict:
    """The stored brief, from a model reply. Every field is checked: a
    reply is never trusted to have the right shape."""
    reply = reply if isinstance(reply, dict) else {}
    return {
        "headline": " ".join(str(reply.get("headline") or "").split())[:300],
        "decisions": _items(reply.get("decisions")),
        "asks_of_you": _items(reply.get("asks_of_you")),
        "open_questions": _items(reply.get("open_questions")),
        "changes_since_last": (_items(reply.get("changes_since_last"))
                               if previous else []),
        "worth_hearing": _moments(reply.get("worth_hearing")),
        "compared_with": ({"session_id": previous.get("session_id"),
                           "display_name": previous.get("display_name"),
                           "started_at": previous.get("started_at")}
                          if previous else None),
    }


def is_empty(brief: Optional[dict]) -> bool:
    if not brief:
        return True
    return not (brief.get("headline") or any(
        brief.get(k) for k in ("decisions", "asks_of_you", "open_questions",
                               "changes_since_last", "worth_hearing")))


def render_markdown(session, brief: Optional[dict] = None) -> str:
    """The catch-up document; "" when there is none."""
    brief = brief if brief is not None else getattr(session, "catch_up", None)
    if is_empty(brief):
        return ""
    title = (getattr(session, "display_name", "") or "").strip()
    lines = [f"# What I missed — {title}" if title else "# What I missed", ""]
    if brief.get("headline"):
        lines += [brief["headline"], ""]
    sections = [
        ("Decided", "decisions"),
        ("Asked of you or your team", "asks_of_you"),
        ("Still open", "open_questions"),
    ]
    if brief.get("compared_with"):
        prev = brief["compared_with"]
        sections.append(
            (f"Changed since {prev.get('display_name') or 'last time'} "
             f"({str(prev.get('started_at') or '')[:10]})",
             "changes_since_last"))
    for label, key in sections:
        if brief.get(key):
            lines.append(f"## {label}")
            lines += [f"- {x}" for x in brief[key]]
            lines.append("")
    if brief.get("worth_hearing"):
        lines.append("## Worth hearing yourself")
        lines += [f"- {m['at']} — {m['why']}" for m in brief["worth_hearing"]]
        lines.append("")
    lines.append("_Written by AI from the transcript — check before acting "
                 "on it._")
    return "\n".join(lines).rstrip() + "\n"
