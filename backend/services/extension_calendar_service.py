"""
Extension-sourced calendar events — the SECOND source for the Record
tab's "Upcoming Meetings" panel.

WHY THIS EXISTS
---------------
Two calendar pipelines grew up independently and never met:

  1. The Chrome extension (chrome-extension/ at repo root) scrapes
     Outlook Web + Teams in the user's REAL browser and POSTs the text
     to ``/briefing/extension-import``. That feeds the DAILY BRIEFING on
     the Today tab.
  2. The Record tab's Upcoming Meetings panel calls
     ``/calendar/upcoming``, which reads the LOCAL calendar — Outlook
     COM on Windows, EventKit on macOS.

A meeting the extension can see but local Outlook cannot (the whole
reason the extension exists: IT policy blocks Graph/COM access on the
user's personal machine, and OWA-only calendars never reach the local
store) therefore never appeared on the Record tab and could not be
used to start a recording. Field report 2026-08-11.

This service closes that gap. On every extension import we lift the
structured events out of the parsed briefing and persist them here;
``/calendar/upcoming`` then merges them into the local list.

WHAT THE BRIEFING PARSE ACTUALLY GIVES US
-----------------------------------------
``parse_daily_briefing`` produced agenda items with a *display* time
string ("9:30 AM") and a duration string ("30 min") — no date, no
timezone, no ISO timestamp, no join link. That is not enough to place
an event on a timeline or to dedupe it against a local Outlook event.
So the parse was extended (core/summarizer.py) to ALSO emit
``start_iso`` / ``end_iso`` / ``join_url`` per agenda item, and
``DailyBriefingService._normalize_agenda`` now retains them.

Because that comes from an LLM it is not trustworthy on its own, so
``events_from_briefing`` below falls back to deriving the timestamp
from the briefing's date + the human ``time`` + ``duration`` strings
whenever ``start_iso`` is missing or unparseable. Both paths are pure
functions with no LLM and no I/O, so they're directly testable.

STRUCTURED EVENTS (v2.28 — the "1 of 5" field report, 2026-08-13)
------------------------------------------------------------------
The LLM-parse path above was never the calendar's primary path in
practice: the extension only ever scraped the OWA DAY view (today
only — structurally incapable of filling the Record tab's 168h
window) and dumped a char-budget of grid TEXT for the LLM to
regex-guess events out of. In the field that captured 1 of 5 real
meetings visible on a single day.

``chrome-extension/background.js`` now reads each event's own
``aria-label`` directly out of the Outlook Web WEEK view (current +
next) — Outlook must expose "Subject, 9:30 AM to 10:00 AM, ..." there
for screen readers, so that contract is far more stable than any CSS
selector, and there's no free-text heuristic left to lose events to.
``events_from_structured`` below just validates/coerces that already-
parsed JSON — no LLM, no regex-over-a-blob involved on this path.
The OWA-day-view → LLM → ``events_from_briefing`` path above is kept
as: (a) backward compatibility for an un-upgraded extension that only
ever sends ``owa_text``, and (b) the extension's own last-resort
fallback when its structured DOM scan finds nothing at all (Outlook
Web redesign, etc — see ``captureCalendarTab`` in background.js).

RETENTION
---------
The store is replaced on each import (see ``replace_all``) and clipped
to now-1d .. now+14d. Stale events age out on their own: the extension
only ever reports what OWA is showing right now, so an event that
disappears from Outlook would otherwise linger in our JSON forever and
keep showing up on the Record tab as a ghost.

A capture that finds FEWER events than the store already holds within
the same window is treated with suspicion rather than trusted outright
— see ``replace_all``'s merge-instead-of-shrink guard. A capture that
finds MORE (or an equal count) replaces normally, which is how a
cancelled meeting still disappears promptly.

STORAGE
-------
``recordings_dir/extension_calendar.json`` — same directory and the
same temp-file + ``os.replace`` atomic write as ClientConfigService,
read back through ``_cloud_sync.read_text_hydrated`` so an
un-downloaded cloud placeholder surfaces as an error instead of
silently reading as "no events".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from services._cloud_sync import CloudFileNotReadyError, read_text_hydrated
from utils.logger import get_logger

logger = get_logger(__name__)

STORE_FILENAME = "extension_calendar.json"

SOURCE_EXTENSION = "extension"
SOURCE_LOCAL = "outlook"

# Retention window applied at import time. A day of lookback keeps
# this-morning's meetings visible for "what was I just in"; two weeks
# forward covers the Record tab's 7-day default with slack for a user
# who bumps the window.
RETAIN_PAST = timedelta(days=1)
RETAIN_FUTURE = timedelta(days=14)

# Dedup tolerance. Outlook and OWA agree on the minute for the same
# invite, but the LLM-derived timestamps drift: it rounds "9:30-10"
# to 09:30 while Outlook holds 09:29:47 for a rescheduled series, and
# an OWA row rendered as "10 AM" can land on the hour when the invite
# is 10:05. Five minutes absorbs that without merging genuinely
# different back-to-back meetings (a 15-min stand-up followed by a
# different call still reads as two events).
DEDUP_TOLERANCE = timedelta(minutes=5)

# Reply / forward / update noise Outlook and OWA prepend to the SAME
# meeting. "Updated!" is OWA's own decoration on a changed invite, so
# without stripping it the local copy and the extension copy of one
# meeting look like two meetings. Anchored, repeated (an invite can
# accumulate "FW: RE: ").
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:re|fw|fwd|updated!?|canceled|cancelled|accepted|declined|tentative)"
    r"\s*[:!]\s*",
    re.IGNORECASE,
)

# CANCELLATION IS A STATUS, NOT NOISE.
#
# `_SUBJECT_PREFIX_RE` above strips "Canceled:" along with "RE:" and
# "FW:", because for DEDUPE purposes they are all the same thing: the
# same meeting wearing a different subject line. That is correct for
# matching and catastrophic on its own — it scrubs a cancelled
# meeting's subject clean, so it becomes indistinguishable from the
# live one and keeps its place on the Record tab as something to
# record.
#
# Field report 2026-08-20: a cancelled call rendered struck-through and
# CANCELLED on the Today screen (which reads the briefing agenda, whose
# `status` field `events_from_briefing` honours) while the SAME meeting
# sat in Upcoming Meetings on Record as a live, recordable row. Two
# paths, one of which knew.
#
# So the prefix is detected BEFORE it is stripped, and the event is
# dropped — matching `events_from_briefing`'s existing rule and the
# reason stated there: a cancelled meeting must not resurface as
# something to record.
#
# Deliberately narrower than the strip list: "accepted", "declined" and
# "tentative" are RESPONSE states — the meeting is still happening, and
# the user may still want to record it. Only cancellation removes the
# meeting itself.
_CANCELLED_PREFIX_RE = re.compile(
    r"^\s*(?:canceled|cancelled)\s*[:!]", re.IGNORECASE)


def is_cancelled_subject(subject: str) -> bool:
    """True when a subject line marks the meeting as cancelled.

    Outlook and OWA both prefix the subject rather than exposing a
    separate flag on the calendar grid, so the subject IS the signal on
    this path — there is nothing else to read.
    """
    return bool(_CANCELLED_PREFIX_RE.match(str(subject or "")))


_WS_RE = re.compile(r"\s+")

# "9:30 AM", "9 AM", "09:30", "9.30 pm" — the shapes the briefing's
# free-form `time` field actually arrives in.
_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?m?\.?\s*$",
    re.IGNORECASE,
)

# "30 min", "1 hr", "1.5 hours", "45m", "1h30"
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(h(?:ou)?rs?|hrs?|h|m(?:in(?:ute)?s?)?)",
    re.IGNORECASE,
)

DEFAULT_DURATION_MIN = 30


# Lines Outlook's event card contributes that are never part of an
# invite. Matched against the WHOLE trimmed line, case-insensitively —
# never as a substring — so an invite that discusses "the change
# process" or "acceptance criteria" keeps its text.
_INVITE_CHROME_EXACT = frozenset({
    "join", "chat", "accepted", "declined", "tentative", "change",
    "no location added", "prepare for this meeting", "rsvp",
    "add to calendar", "forward", "edit", "cancel", "save", "close",
    "what are key talking points?", "help me prepare for this meeting",
    "help me understand the risks", "show more", "show less",
    "more options", "copy link",
})

_INVITE_CHROME_PATTERNS = (
    # "Jordan Poe invited you." / "... invited you"
    re.compile(r"^.{1,80}\binvited you\.?$", re.IGNORECASE),
    # "Accepted 1, Didn't respond 5" — the attendee tally.
    re.compile(r"^(accepted|declined|tentative|didn'?t respond|no response)"
               r"\s*\d+([,;].*)?$", re.IGNORECASE),
    # A bare avatar monogram: "GG", "JP".
    re.compile(r"^[A-Z]{1,3}$"),
    # The card's own date line: "Fri 8/21/2026 10:00 AM - 11:00 AM".
    re.compile(r"^[A-Za-z]{3,9}\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+"
               r"\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\s*[-–—]\s*"
               r"\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?$"),
)

# Unicode Private Use Area: Outlook's toolbar icons are a private-use
# font, so every icon is a character innerText returns and every font
# renders as a hollow box. Plus the replacement char.
_PUA_RE = re.compile("[\uE000-\uF8FF\uFFFD]")


def clean_invite_body(text: Any) -> str:
    """Strip Outlook's own UI out of an invite body, at READ time.

    Extension 1.17.0 fixes this at capture time and structurally: the
    body is read out of the invite's own frame, where Outlook's UI is
    definitionally not present. That is the real fix. But it only helps
    text captured afterwards, and every body already in the store stays
    exactly as ugly until something re-captures that meeting — while
    the user, reasonably, judges the app by what it is showing right
    now (field screenshot, v2.59.0: a body consisting of two hollow
    boxes, an RSVP tally, an avatar monogram and three Copilot prompt
    suggestions, with one real sentence in the middle).

    This is the lesson v2.52.0 already paid for with the attendee
    scrub: a cleanup of data the APP OWNS must not be held hostage by
    whether a browser is running, or by which extension version the
    user managed to reload.

    DROP-ONLY, by construction: it removes characters and whole lines,
    and never adds or rewrites. The worst case on an unfamiliar tenant
    is text that is still there.
    """
    if not text:
        return ""
    out: List[str] = []
    for raw_line in str(text).split("\n"):
        line = _PUA_RE.sub("", raw_line).strip()
        if not line:
            # Preserve paragraph breaks, but never lead with one.
            if out and out[-1] != "":
                out.append("")
            continue
        if line.lower() in _INVITE_CHROME_EXACT:
            continue
        if any(p.match(line) for p in _INVITE_CHROME_PATTERNS):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# ── pure helpers (no I/O, no LLM) ───────────────────────────────────

def normalize_subject(subject: Any) -> str:
    """Canonical form used for dedup: strip reply/forward/update
    prefixes, collapse internal whitespace, trim, casefold.

    ``casefold`` rather than ``lower`` because Outlook subjects carry
    real-world non-ASCII (German ß, Turkish İ) from international
    attendees, and ``lower`` doesn't fold those to the same key.
    """
    s = str(subject or "")
    # Repeated because "FW: RE: Weekly Sync" is one meeting.
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if stripped == s:
            break
        s = stripped
    return _WS_RE.sub(" ", s).strip().casefold()


def to_naive_local(value: Any) -> Optional[datetime]:
    """Accept a datetime or an ISO string; return a NAIVE-LOCAL
    datetime or None.

    Naive-local is the convention the rest of the calendar stack uses —
    ``_calendar_outlook`` hands back naive local datetimes and
    ``auto_record_service`` compares them against ``datetime.now()``.
    An aware timestamp from the LLM (it likes appending "Z") is
    converted to local and stripped, so we never mix aware and naive
    and blow up on a comparison.

    PUBLIC because ``services/calendar_feed.py`` re-asserts this exact
    invariant across BOTH calendar sources before either the Record
    tab's panel or the auto-record trigger sees a meeting — mixing an
    aware start with ``datetime.now()`` raises ``TypeError`` inside the
    auto-record tick, which is swallowed as "tick failed" and silently
    disables auto-record for good. ``_coerce_dt`` remains as the
    module-internal alias so the call sites below read unchanged.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# Module-internal alias — every call site below predates the rename and
# reads better short. Same function object.
_coerce_dt = to_naive_local


def parse_duration_minutes(text: Any,
                           default: int = DEFAULT_DURATION_MIN) -> int:
    """"30 min" / "1 hr" / "1.5 hours" / "45m" → minutes."""
    m = _DURATION_RE.search(str(text or ""))
    if not m:
        return default
    try:
        value = float(m.group(1))
    except ValueError:
        return default
    unit = m.group(2).lower()
    minutes = value * 60 if unit.startswith("h") else value
    minutes = int(round(minutes))
    return minutes if minutes > 0 else default


def parse_clock_time(text: Any) -> Optional[tuple]:
    """"9:30 AM" → (9, 30). None when it isn't a clock time (the
    briefing's `time` field also holds things like "All day")."""
    raw = str(text or "").strip()
    if not raw:
        return None
    m = _TIME_RE.match(raw)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = m.group(3).lower()
        if hour == 12:
            hour = 0
        if meridiem == "p":
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
        return None
    # 24-hour, no meridiem: "09:30", "14:00"
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return (hour, minute)
    return None


def events_from_briefing(briefing: Dict[str, Any],
                         date_iso: Optional[str] = None,
                         stats: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Lift calendar events out of a parsed daily briefing.

    Prefers the LLM's ``start_iso`` / ``end_iso``; falls back to
    briefing-date + the human ``time`` / ``duration`` strings when they
    are absent or unparseable (which is the common case on older
    briefings written before the parse was extended). Items with no
    usable start time and cancelled items are dropped — a cancelled
    meeting must not resurface on the Record tab as something to
    record.

    ``stats``, if given a dict, is mutated in place with ``raw``
    (agenda items seen), ``kept`` (events returned), and a
    ``dropped_<reason>`` count per drop reason — this is what lets the
    server log *why* a briefing-fallback parse lost events, not just
    that it did (field report chain culminating 2026-08-14: two
    calendar-parse paths produced identically-shaped output, so neither
    the stored JSON nor the log could ever say which one ran or why it
    came up short).

    Pure: no I/O, no clock read except through ``date_iso``.
    """
    if not isinstance(briefing, dict):
        if stats is not None:
            stats.update(raw=0, kept=0)
        return []
    day_iso = str(date_iso or briefing.get("date") or "").strip()
    try:
        day = date_cls.fromisoformat(day_iso)
    except (TypeError, ValueError):
        day = None

    agenda = briefing.get("agenda") or []
    out: List[dict] = []
    dropped_no_subject = 0
    dropped_cancelled = 0
    dropped_no_start = 0
    for item in agenda:
        if not isinstance(item, dict):
            dropped_no_subject += 1
            continue
        subject = str(item.get("title") or "").strip()
        if not subject:
            dropped_no_subject += 1
            continue
        if str(item.get("status") or "").strip().lower() == "cancelled":
            dropped_cancelled += 1
            continue

        start = _coerce_dt(item.get("start_iso"))
        if start is None and day is not None:
            clock = parse_clock_time(item.get("time"))
            if clock is not None:
                start = datetime(day.year, day.month, day.day,
                                 clock[0], clock[1])
        if start is None:
            # No timeline position → useless for the Record tab, which
            # is entirely ordered by start time. Skip rather than
            # inventing a slot.
            dropped_no_start += 1
            continue

        end = _coerce_dt(item.get("end_iso"))
        if end is None or end <= start:
            end = start + timedelta(
                minutes=parse_duration_minutes(item.get("duration")))

        duration_min = max(1, int((end - start).total_seconds() // 60))
        attendees = [str(a).strip() for a in (item.get("attendees") or [])
                     if str(a).strip()][:50]
        out.append({
            "subject": subject,
            "start": start,
            "end": end,
            # Same key names the local Outlook/EventKit backends emit —
            # record-view.tsx and the prep-brief path read these, so a
            # merged extension row must be shape-identical.
            "location": str(item.get("client") or "").strip(),
            "organizer": "",
            "attendees": attendees,
            "duration": duration_min,
            "join_url": str(item.get("join_url") or "").strip(),
            "source": SOURCE_EXTENSION,
        })
    if stats is not None:
        stats.update(
            raw=len(agenda),
            kept=len(out),
            dropped_no_subject=dropped_no_subject,
            dropped_cancelled=dropped_cancelled,
            dropped_no_start=dropped_no_start,
        )
    return out


def events_from_structured(events: Iterable[Any],
                           date_iso: Optional[str] = None,
                           stats: Optional[Dict[str, Any]] = None) -> List[dict]:
    """Turn structured event objects the extension parsed CLIENT-SIDE
    (out of Outlook Web's own ``aria-label`` accessibility strings —
    see ``chrome-extension/background.js``'s ``parseMeetingLabel`` /
    ``extractEventsFromCandidates``) into the same shape
    ``events_from_briefing`` produces.

    No LLM and no free-text heuristic on this path — the extension
    already resolved subject/start/end, so this is a straight
    validate-and-coerce pass. ``date_iso`` is accepted for symmetry
    with ``events_from_briefing`` but unused: every event here already
    carries its own full start/end, never a bare display time that
    needs a day anchored under it.

    ``stats``, if given a dict, is mutated in place with ``raw``,
    ``kept``, and per-reason ``dropped_*`` counts — same contract as
    ``events_from_briefing``'s ``stats``, so the server can log both
    paths identically regardless of which one ran.

    Tolerant of a genuinely hostile payload (wrong types, missing
    keys, garbage strings) — a malformed structured event degrades to
    "skipped", never a 500, since this feeds a client-controlled HTTP
    endpoint. Pure: no I/O, no LLM.
    """
    events = list(events or [])
    out: List[dict] = []
    dropped_not_dict = 0
    dropped_no_subject = 0
    dropped_no_start = 0
    dropped_cancelled = 0
    for item in events:
        if not isinstance(item, dict):
            dropped_not_dict += 1
            continue
        subject = str(item.get("subject") or "").strip()
        if not subject:
            dropped_no_subject += 1
            continue
        # Before any normalisation — `_SUBJECT_PREFIX_RE` would remove
        # the only evidence this meeting was cancelled.
        if is_cancelled_subject(subject):
            dropped_cancelled += 1
            continue
        start = _coerce_dt(item.get("start"))
        if start is None:
            dropped_no_start += 1
            continue
        end = _coerce_dt(item.get("end"))
        if end is None or end <= start:
            end = start + timedelta(minutes=DEFAULT_DURATION_MIN)
        attendees = [str(a).strip() for a in (item.get("attendees") or [])
                     if str(a).strip()][:50]
        out.append({
            "subject": subject,
            "start": start,
            "end": end,
            "location": str(item.get("location") or "").strip(),
            "organizer": str(item.get("organizer") or "").strip(),
            "attendees": attendees,
            "duration": max(1, int((end - start).total_seconds() // 60)),
            "join_url": str(item.get("join_url") or "").strip(),
            # THE FIELD THAT WAS DROPPED AT THE DOOR. The extension has
            # sent the invite body since 1.7, the store has serialized
            # it since v2.43.0, and the display has rendered it since
            # the same release — but this function, the one every
            # extension POST passes through on its way to the store,
            # was never taught the field. Every body ever captured was
            # discarded right here, so "(No description on this
            # invite.)" was guaranteed no matter what the extension
            # did, through six releases of fixing the extension.
            #
            # The lesson is the drop-list pattern again, inverted from
            # replace_all's: a whitelist-shaped constructor silently
            # deletes any field added after it was written. Same cap as
            # the store's, since this now precedes it.
            "body": str(item.get("body") or "").strip()[:8000],
            # Why the click pass did or didn't get detail for this
            # meeting ("opened" / "opened_empty" / "no_tile" /
            # "budget" / ""). Carried so the UI can name the cause —
            # and added HERE at the same time as everywhere else,
            # because this constructor silently deleting late-added
            # fields is the defect that hid invite bodies for six
            # releases.
            "detail_status": str(item.get("detail_status") or "")[:32],
            "source": SOURCE_EXTENSION,
        })
    if stats is not None:
        stats.update(
            raw=len(events),
            kept=len(out),
            dropped_not_dict=dropped_not_dict,
            dropped_no_subject=dropped_no_subject,
            dropped_no_start=dropped_no_start,
            dropped_cancelled=dropped_cancelled,
        )
    return out


def describe_structured_source(raw_calendar_events: Optional[Any]) -> str:
    """Classify what the extension actually sent for ``calendar_events``
    on THIS request, so a briefing-fallback parse can say WHY it fell
    back rather than just that it did.

    Three states, deliberately not collapsed to a boolean:

      - "absent" — the key was never sent at all (an extension older
        than the structured-capture change, or a request that never
        built one — e.g. background.js's ``captureCalendarTab``
        threw). This means "we have no client-side extraction to
        trust at all."
      - "empty"  — the key WAS sent, as an empty list: a current
        extension whose structured DOM scan ran and found zero
        candidates this capture (see ``classifyZeroReason`` in
        background.js). This means "the extraction ran and came up
        genuinely empty," a materially different situation from
        "absent" even though both end up taking the same fallback
        code path server-side.
      - "present" — at least one structured event was sent; no
        fallback needed (this function is really only interesting for
        its other two answers, but included for completeness/logging
        symmetry).

    Pure: no I/O.
    """
    if raw_calendar_events is None:
        return "absent"
    try:
        n = len(raw_calendar_events)
    except TypeError:
        return "absent"
    return "empty" if n == 0 else "present"


def _drop_rescheduled(prior: List[dict], fresh: Iterable[dict]) -> List[dict]:
    """Remove prior events this capture has RESCHEDULED.

    A prior event is dropped when the fresh capture contains the same
    normalised subject on the same calendar day at a different time.
    That is a moved meeting — an Outlook "Exception to recurring event"
    is the common source — and the fresh capture is the newer truth.

    Everything else is left alone. In particular a prior event on a day
    the fresh capture returned NOTHING for survives untouched: that is
    the partial-capture case the count guard in `replace_all` exists to
    protect, and this must not become a back door around it.

    Same-subject-same-day-same-time is not a reschedule, it is the same
    meeting, and dedupe downstream handles it.

    Pure: no I/O, inputs not mutated.
    """
    by_day: Dict[tuple, set] = {}
    for m in fresh:
        start = _coerce_dt(m.get("start"))
        if start is None:
            continue
        by_day.setdefault(
            (normalize_subject(m.get("subject")), start.date()), set()
        ).add(start)

    out: List[dict] = []
    for m in prior:
        start = _coerce_dt(m.get("start"))
        if start is None:
            out.append(m)
            continue
        times = by_day.get((normalize_subject(m.get("subject")), start.date()))
        if times is None:
            # This capture said nothing about this subject on this day —
            # not evidence of anything. Keep it.
            out.append(m)
            continue
        if any(abs(start - t) <= DEDUP_TOLERANCE for t in times):
            # Same meeting, same slot. Keep; dedupe handles it.
            out.append(m)
            continue
        # Same subject, same day, a time this capture does not have:
        # the meeting moved. Drop the stale copy.
        logger.info(
            "Extension calendar: dropping a prior event whose time changed "
            "in this capture (same subject, same day, different start)")
    return out


def merge_meetings(local: Iterable[dict],
                   extension: Iterable[dict]) -> List[dict]:
    """Merge extension-sourced events into the LOCAL list.

    Local is authoritative and always wins a collision: it carries
    resolved attendee names, the invite body, the organizer and the
    real join link, none of which the scrape reconstructs. Collision
    rule is normalized-subject equality AND start times within
    ``DEDUP_TOLERANCE``.

    Every returned meeting carries a ``source`` field
    ("outlook" | "extension"); no existing field is renamed or
    repurposed — record-view.tsx and auto_record_service read them.
    """
    merged: List[dict] = []
    local_keys: List[tuple] = []

    for m in local:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        # Don't clobber a source another layer already set.
        item.setdefault("source", SOURCE_LOCAL)
        merged.append(item)
        start = _coerce_dt(item.get("start"))
        if start is not None:
            local_keys.append((normalize_subject(item.get("subject")), start))

    for e in extension:
        if not isinstance(e, dict):
            continue
        start = _coerce_dt(e.get("start"))
        if start is None:
            continue
        key = normalize_subject(e.get("subject"))
        if any(k == key and abs(start - s) <= DEDUP_TOLERANCE
               for k, s in local_keys):
            continue
        item = dict(e)
        item["start"] = start
        end = _coerce_dt(item.get("end"))
        item["end"] = end if end is not None else start + timedelta(
            minutes=DEFAULT_DURATION_MIN)
        item["source"] = SOURCE_EXTENSION
        merged.append(item)

    def _sort_key(m: dict):
        dt = _coerce_dt(m.get("start"))
        return dt if dt is not None else datetime.max

    merged.sort(key=_sort_key)
    return merged


# ── store ───────────────────────────────────────────────────────────

class ExtensionCalendarService:
    """Thread-safe JSON-on-disk store of the events the Chrome
    extension last reported. Replaced wholesale per import."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / STORE_FILENAME
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _read_locked(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(read_text_hydrated(self._path)) or {}
        except CloudFileNotReadyError:
            # Same call as ClientConfigService: an un-downloaded cloud
            # placeholder must NOT read as "no events" — that's how a
            # synced file becomes invisible.
            logger.error(
                f"{STORE_FILENAME} is an un-downloaded cloud placeholder "
                f"— surfacing instead of dropping every extension event")
            raise
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"{STORE_FILENAME} unreadable ({e}); treating as empty")
            return {}

    def _write_locked(self, data: Dict[str, Any]) -> None:
        # Temp file in the SAME directory + fsync + os.replace, so a
        # crash mid-write can never leave a half-written JSON that
        # reads as zero events on next boot (ClientConfigService /
        # SessionService use the identical pattern).
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _serialize(event: dict) -> dict:
        start = _coerce_dt(event.get("start"))
        end = _coerce_dt(event.get("end"))
        return {
            "subject": str(event.get("subject") or "").strip(),
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "location": str(event.get("location") or ""),
            "organizer": str(event.get("organizer") or ""),
            "attendees": [str(a) for a in (event.get("attendees") or [])],
            "duration": int(event.get("duration") or DEFAULT_DURATION_MIN),
            "join_url": str(event.get("join_url") or ""),
            # The invite body/agenda, new in v2.43.0. Empty for every
            # meeting captured before extension 1.7 recorded Outlook's
            # own calendar responses — the grid has never rendered a
            # description. Capped here as well as in the extension:
            # this is the only place a hostile or runaway body reaches
            # the store, and "the extension already trims it" stops
            # being true the moment anything else POSTs.
            "body": str(event.get("body") or "")[:8000],
            "detail_status": str(event.get("detail_status") or "")[:32],
            "source": SOURCE_EXTENSION,
        }

    @staticmethod
    def _deserialize(raw: dict) -> Optional[dict]:
        start = _coerce_dt(raw.get("start"))
        if start is None:
            return None
        end = _coerce_dt(raw.get("end")) or start + timedelta(
            minutes=DEFAULT_DURATION_MIN)
        return {
            "subject": str(raw.get("subject") or "").strip(),
            "start": start,
            "end": end,
            "location": str(raw.get("location") or ""),
            "organizer": str(raw.get("organizer") or ""),
            "attendees": [str(a) for a in (raw.get("attendees") or [])],
            "duration": int(raw.get("duration") or DEFAULT_DURATION_MIN),
            "join_url": str(raw.get("join_url") or ""),
            # Absent from every event stored before this field existed,
            # which reads as "" — the same value an event with no
            # description has — so an old store loads unchanged.
            #
            # Cleaned on the way OUT, every read: a body captured by an
            # older extension still carries Outlook's RSVP control,
            # attendee tally, Copilot prompts and toolbar-icon
            # characters, and the user should not have to re-capture a
            # meeting to stop seeing them. See clean_invite_body.
            "body": clean_invite_body(raw.get("body"))[:8000],
            "detail_status": str(raw.get("detail_status") or "")[:32],
            "source": SOURCE_EXTENSION,
        }

    def record_capture_diag(self, diag: Optional[dict]) -> None:
        """Store the extension's last capture-recorder counters.

        Counts and booleans only — the extension sends no URL, subject,
        attendee or body text in this payload, and nothing here adds
        any. Written on every POST that carries one, overwriting the
        previous capture: the useful question is always "what did the
        most recent capture do", and keeping a history would put a
        growing diagnostic log in the calendar store.

        Exists because v1.7 computed these counters and kept them
        inside the extension, where the app's diagnostics bundle could
        not see them. An empty attendee list then looked identical
        whether the recorder never installed, saw no responses at all,
        or saw responses it could not read — three causes needing three
        different fixes, and no way to tell them apart from a bug
        report. That ambiguity is what turned this into four
        release/reinstall/re-run cycles.

        Best-effort by construction: bookkeeping must never fail the
        calendar import it rides along with.
        """
        if not isinstance(diag, dict):
            return
        # Scalars only. A nested structure here would be the extension
        # deciding what this file stores, and this payload is opaque
        # by design — cheap to bound, and it keeps a future extension
        # from writing something large or unexpected into the store.
        clean = {
            str(k)[:64]: v
            for k, v in diag.items()
            if isinstance(v, (bool, int, float)) or v is None
        }
        with self._lock:
            data = self._read_locked()
            data["last_capture_diag"] = clean
            data["last_capture_diag_at"] = datetime.now().isoformat()
            self._write_locked(data)

    def last_capture_diag(self) -> dict:
        """The most recent capture-recorder counters, or {} if none.

        {} means no capture has reported any since this field existed —
        NOT that a capture ran and found nothing. The diagnostics
        bundle renders those differently for exactly the reason this
        method exists.
        """
        with self._lock:
            data = self._read_locked()
        out = data.get("last_capture_diag")
        if not isinstance(out, dict):
            return {}
        at = data.get("last_capture_diag_at")
        return {**out, "at": at} if at else dict(out)

    def record_extension_version(self, version: Optional[str],
                                 now: Optional[datetime] = None) -> None:
        """Record the extension version that just POSTed, plus when.

        Called on EVERY extension POST — narrative capture, the
        calendar-only refresh alarm, or a manual Capture & Send — from
        `/briefing/extension-import`, independent of whether that POST
        also carried usable events/text. That's deliberate: a stale
        extension that fails to produce anything useful is exactly the
        case we most need reflected here, not the one case to skip.

        ``version`` absent or blank (an extension older than 1.2.0 —
        background.js only started sending
        ``chrome.runtime.getManifest().version`` in 1.2.0) is recorded
        as ``None``, never coerced to "current". The whole point of
        this bookkeeping is telling "definitely current" apart from
        "we don't actually know" — see ``extension_bundle_service.
        extension_version_status``.

        Best-effort: an un-downloaded cloud placeholder degrades to a
        skipped write (logged), never an exception — this must not be
        able to fail the POST it's piggybacking on.
        """
        ref = now or datetime.now()
        v = str(version).strip() if version not in (None, "") else None
        v = v or None
        with self._lock:
            try:
                raw = self._read_locked()
            except CloudFileNotReadyError:
                logger.warning(
                    f"{STORE_FILENAME} is an un-downloaded cloud placeholder; "
                    f"skipping extension-version bookkeeping for this POST")
                return
            raw = dict(raw or {})
            raw["last_seen_version"] = v
            raw["last_seen_version_at"] = ref.isoformat()
            self._write_locked(raw)

    def replace_all(self, events: Iterable[dict],
                    now: Optional[datetime] = None,
                    import_meta: Optional[Dict[str, Any]] = None) -> List[dict]:
        """Replace the store with ``events`` clipped to the retention
        window. Returns the events actually kept (as dicts with real
        datetimes).

        ``import_meta``, if given, is an observability record of HOW
        ``events`` was produced — ``{"path": "structured"|"text-fallback",
        "raw": int, "kept": int, "dropped": int, "fallback_reason":
        str|None}`` — persisted alongside the events (see
        ``capture_status``) so the Record tab's calendar panel can say
        "2 meetings from the extension's structured capture" instead of
        rendering identically regardless of which of the two calendar-
        parse paths actually ran (field report chain culminating
        2026-08-14: that ambiguity caused several wrong diagnoses
        because neither the stored JSON nor the log revealed the path).
        Preserved across a write that doesn't supply it, the same way
        ``last_seen_version`` is preserved below — this method owns
        "events"/"updated_at" and must not blank out import bookkeeping
        a DIFFERENT call already recorded.

        SHRINK GUARD (field report 2026-08-13): if this capture found
        FEWER events than the store already holds within the same
        window, that's more likely a bad/partial capture (Outlook Web
        slow to render, a DOM regression, the user's laptop asleep
        mid-scrape) than genuine mass-cancellation, and blowing away
        a good store on every such capture is exactly how a user's
        Upcoming Meetings panel goes to zero without anyone noticing.
        So a shrinking capture is MERGED with what's already there
        (new data wins on a subject+time collision, old-only survivors
        are kept) instead of replacing it outright. Stale-but-complete
        beats fresh-but-empty.

        A capture that finds an equal-or-larger count replaces
        normally — that's how a genuinely cancelled meeting still
        disappears promptly, and it's the path every existing caller
        exercises (first import into an empty store always qualifies).
        """
        ref = now or datetime.now()
        lo, hi = ref - RETAIN_PAST, ref + RETAIN_FUTURE

        kept: List[dict] = []
        dropped = 0
        for e in events or []:
            start = _coerce_dt((e or {}).get("start"))
            subject = str((e or {}).get("subject") or "").strip()
            if start is None or not subject:
                dropped += 1
                continue
            if start < lo or start > hi:
                dropped += 1
                continue
            item = dict(e)
            item["start"] = start
            item["end"] = _coerce_dt(e.get("end")) or start + timedelta(
                minutes=parse_duration_minutes(e.get("duration")))
            item["source"] = SOURCE_EXTENSION
            kept.append(item)

        with self._lock:
            try:
                raw_before = self._read_locked()
            except CloudFileNotReadyError:
                raw_before = {}
            prior_kept = self._prior_kept_from_raw(raw_before, lo, hi)
            # ONE-TIME SCRUB of attendees produced by the deleted
            # name-shape scanner (extensions 1.11–1.12).
            #
            # That scanner harvested any 2-4-word title-case label on
            # the page as a person, and the field result was stored
            # attendee lists reading "Skip to main content", "App
            # launcher", "Chat with Copilot" — and the user's own
            # account button. The v2.50.0 enrichment ratchet, doing its
            # job, would preserve that junk FOREVER: every bare capture
            # inherits it.
            #
            # Junk and genuine display names are indistinguishable in
            # the store (both are just strings without "@"), so the
            # scrub keeps only address-shaped entries and drops the
            # rest — accepting that real response-derived names are
            # dropped with them, because they refill on the next
            # capture from Outlook's own detail responses (field
            # diagnostics: 19 of 23 meetings), while the junk, with its
            # producer deleted, would never be replaced by anything.
            #
            # Flag-gated so it runs once: names stored AFTER the scrub
            # come only from response data and must not be wiped.
            if not (raw_before or {}).get("attendee_scrub_v1"):
                for pm in prior_kept:
                    kept_addrs = [a for a in (pm.get("attendees") or [])
                                  if "@" in str(a)]
                    if len(kept_addrs) != len(pm.get("attendees") or []):
                        pm["attendees"] = kept_addrs
            # ENRICHMENT RATCHETS UP, NEVER DOWN.
            #
            # The grid labels a capture starts from never carry
            # attendees, an invite body, or (for Teams) a join link —
            # those come from the detail passes, which are best-effort:
            # a click that times out, a pane that fails to render, a
            # regression in the extension (field incident 2026-08-20,
            # extension 1.11) all produce a capture whose EVENTS are
            # fine but whose detail is empty.
            #
            # Before this, such a capture was worse than no capture:
            # its bare events collided with the store's enriched ones,
            # fresh won the collision, and the join links the user
            # already HAD disappeared. "You broke it even more — no
            # links at all now" was this exact mechanism: one capture
            # with a broken detail pass erased every previously-earned
            # field.
            #
            # So: a fresh event that carries a detail field wins (it is
            # newer truth — an invite really can drop its link), but a
            # fresh event whose detail field is EMPTY inherits the
            # stored value. Empty here does not mean "the invite has
            # none"; the grid path cannot tell "none" from "not
            # fetched", and while that is true, deleting data on empty
            # is indefensible.
            prior_by_key: Dict[tuple, dict] = {}
            for pm in prior_kept:
                pk = (normalize_subject(pm.get("subject")), pm.get("start"))
                prior_by_key[pk] = pm
            backfilled = 0
            for e in kept:
                pm = prior_by_key.get(
                    (normalize_subject(e.get("subject")), e.get("start")))
                if pm is None:
                    continue
                gained = False
                if not e.get("attendees") and pm.get("attendees"):
                    e["attendees"] = list(pm["attendees"])
                    gained = True
                for f in ("body", "join_url", "organizer", "location"):
                    if not e.get(f) and pm.get(f):
                        e[f] = pm[f]
                        gained = True
                if gained:
                    backfilled += 1
            if backfilled:
                logger.info(
                    f"Extension calendar store: carried detail forward "
                    f"onto {backfilled} event(s) the fresh capture "
                    f"brought back bare")
            # A fresh capture is AUTHORITATIVE about a day it can see.
            #
            # The count guard below exists so a bad/partial capture can't
            # wipe good data, and that is worth keeping. But on its own
            # it has no way out: meetings get moved, cancelled, or
            # declined, so an honest capture legitimately returns fewer
            # events than an accumulated store. Once the store holds
            # more than any correct capture returns, EVERY capture is
            # "fewer", every capture merges, and nothing ever leaves.
            #
            # Field evidence 2026-08-20 — the same warning three times in
            # one afternoon ("returned fewer events (36) than the store
            # already holds (43)"), and two copies of one recurring
            # training block rendering as simultaneously LIVE on the
            # Record tab: a series instance had been MOVED (an
            # "Exception to recurring event" shifting it 1:30→2:00), so
            # the capture carried the new time while the merge kept
            # resurrecting the old one.
            #
            # So a prior event is dropped when this capture covers the
            # same day and holds the same subject at a different time —
            # that is a reschedule, and the capture is the newer truth.
            # Deliberately narrow: same subject AND same day. A prior
            # event on a day this capture returned nothing for is still
            # protected, which is the partial-capture case the guard was
            # built for.
            # Filtered for the MERGE, but the merge-vs-replace decision
            # below still weighs the ORIGINAL count.
            #
            # Judging the decision on the filtered list changes which
            # branch runs: dropping two ghosts can make a partial
            # capture look complete, flip it to the replace branch, and
            # delete real events on days the capture never covered. A
            # test caught exactly that — one day's moved instance
            # deleting the same series on another day.
            #
            # Keeping the decision on `prior_kept` means this change can
            # only ever remove a stale duplicate from the merge; it can
            # never make the store shrink in a case where it previously
            # would not have.
            prior_for_merge = _drop_rescheduled(prior_kept, kept)
            if len(kept) < len(prior_kept):
                merged = merge_meetings(kept, prior_for_merge)
                logger.warning(
                    f"Extension calendar capture returned fewer events "
                    f"({len(kept)}) than the store already holds in "
                    f"this window ({len(prior_kept)}); merging instead "
                    f"of replacing so a bad/partial capture can't wipe "
                    f"good data.")
                kept = merged
            else:
                kept.sort(key=lambda m: m["start"])

            # START FROM WHAT IS ALREADY THERE, then overwrite only the
            # keys this method owns.
            #
            # This used to be built from `{}` with a hand-listed set of
            # keys copied forward, which made the file a whole-file
            # REPLACEMENT and every unlisted key a silent deletion.
            # `record_capture_diag` writes on the SAME request,
            # milliseconds before this call (see server.py's
            # extension-import handler), so its key was not merely going
            # stale — it was recorded and then destroyed on every POST.
            #
            # Field evidence 2026-08-20: a bundle exported minutes after
            # three v1.9.0 captures reported `extension_capture_diag:
            # {}` while `extension_last_seen_version` in the same file
            # said "1.9.0". The version survived because it happened to
            # be on the list; the capture counters did not because they
            # were not. Two releases went out unable to say which of
            # four detail-capture failures was occurring, and the answer
            # had been computed and deleted on every capture.
            #
            # Inheriting first inverts the failure direction: a key this
            # method does not know about is now KEPT by default rather
            # than dropped by default. Preserving something stale is a
            # visible wrong value; deleting it is invisible, and this
            # module has now paid for that difference twice.
            payload = dict(raw_before or {})
            payload["attendee_scrub_v1"] = True
            payload.update({
                "updated_at": ref.isoformat(),
                "events": [self._serialize(e) for e in kept],
                # Preserve extension-version bookkeeping (see
                # `record_extension_version`) across a calendar-only
                # write — this method owns "events"/"updated_at", not
                # "last_seen_version*", and must not clobber whichever
                # POST last recorded it.
                "last_seen_version": (raw_before or {}).get("last_seen_version"),
                "last_seen_version_at": (raw_before or {}).get("last_seen_version_at"),
                # Same reasoning, and the reason it is spelled out here
                # rather than left to a future reader to notice:
                #
                # THIS PAYLOAD IS A WHOLE-FILE REPLACEMENT. Every key
                # not named here is deleted. `record_capture_diag` runs
                # on the SAME request, milliseconds before this write
                # (see server.py's extension-import handler), so a key
                # missing from this dict is not merely stale — it is
                # destroyed on every single POST, immediately after
                # being recorded.
                #
                # Field evidence 2026-08-20: a diagnostics bundle
                # exported minutes after three v1.9.0 captures reported
                # `extension_capture_diag: {}`, while
                # `extension_last_seen_version` in the same file said
                # "1.9.0" — the version survived because it is listed
                # above, and the capture counters did not because they
                # were not. Two releases were spent unable to tell
                # which of four detail-capture failures was happening,
                # and the answer had been computed, sent, stored and
                # deleted on each of those captures.
                "last_capture_diag": (raw_before or {}).get("last_capture_diag"),
                "last_capture_diag_at": (raw_before or {}).get("last_capture_diag_at"),
            })
            if import_meta is not None:
                payload["last_import_path"] = import_meta.get("path")
                payload["last_import_raw"] = import_meta.get("raw")
                payload["last_import_kept"] = import_meta.get("kept")
                payload["last_import_dropped"] = import_meta.get("dropped")
                payload["last_import_fallback_reason"] = import_meta.get("fallback_reason")
                payload["last_import_at"] = ref.isoformat()
            else:
                for k in ("last_import_path", "last_import_raw", "last_import_kept",
                          "last_import_dropped", "last_import_fallback_reason",
                          "last_import_at"):
                    payload[k] = (raw_before or {}).get(k)
            self._write_locked(payload)
        logger.info(
            f"Extension calendar store: kept {len(kept)} event(s), "
            f"dropped {dropped} outside {lo.date()}..{hi.date()}")
        return kept

    @staticmethod
    def _prior_kept_from_raw(raw: Dict[str, Any], lo: datetime,
                             hi: datetime) -> List[dict]:
        """What ``raw`` (an already-read store dict) would contribute
        to a capture with window [lo, hi] — used only by
        ``replace_all``'s shrink guard. Pure: no I/O, no locking (the
        caller already holds ``self._lock`` and already did the read)."""
        out: List[dict] = []
        for entry in (raw or {}).get("events") or []:
            if not isinstance(entry, dict):
                continue
            item = ExtensionCalendarService._deserialize(entry)
            if item is None or not item["subject"]:
                continue
            if lo <= item["start"] <= hi:
                out.append(item)
        return out

    def get_events(self, within_hours: Optional[int] = None,
                   now: Optional[datetime] = None) -> List[dict]:
        """Stored events, optionally clipped to ``now .. now+hours``.

        Never raises on a corrupt/absent file — the Record tab's local
        calendar must keep working when the extension store is broken.
        A cloud-placeholder read DOES propagate (see ``_read_locked``);
        the endpoint catches it so the local list still renders.
        """
        with self._lock:
            raw = self._read_locked()
            # THE SCRUB RUNS HERE, AT READ TIME — not only on the next
            # extension capture.
            #
            # v2.51.0 gated the junk-attendee cleanup on `replace_all`,
            # i.e. on the extension POSTing again. That was a design
            # mistake the field found within hours: the user installed
            # the fix, opened the app, and saw the exact same
            # "Attendees (24)" wall of Outlook buttons — because the
            # 30-minute capture alarm had not fired yet (and never
            # would, with Chrome closed). A cleanup of data the APP
            # OWNS must not be held hostage by whether a browser is
            # running. Same flag, so whichever side reaches the store
            # first does the work and the other finds it done.
            if raw and raw.get("events") and not raw.get("attendee_scrub_v1"):
                changed = False
                for entry in raw.get("events") or []:
                    if not isinstance(entry, dict):
                        continue
                    atts = entry.get("attendees") or []
                    kept_atts = [a for a in atts if "@" in str(a)]
                    if len(kept_atts) != len(atts):
                        entry["attendees"] = kept_atts
                        changed = True
                raw["attendee_scrub_v1"] = True
                try:
                    self._write_locked(raw)
                    if changed:
                        logger.info(
                            "Extension calendar store: scrubbed "
                            "name-shape-scanner attendee junk at read "
                            "time (no capture required)")
                except Exception as e:  # noqa: BLE001
                    # Reading must never fail because cleanup could not
                    # persist; the scrubbed copy still serves this call.
                    logger.warning(f"Attendee scrub could not persist: {e}")
        out: List[dict] = []
        for entry in (raw or {}).get("events") or []:
            if not isinstance(entry, dict):
                continue
            item = self._deserialize(entry)
            if item is None or not item["subject"]:
                continue
            out.append(item)

        if within_hours is not None:
            ref = now or datetime.now()
            hi = ref + timedelta(hours=within_hours)
            # Keep in-progress meetings (end still ahead) the same way
            # the panel's "LIVE" row expects, but drop anything already
            # finished or past the requested horizon.
            out = [m for m in out if m["end"] > ref and m["start"] <= hi]

        out.sort(key=lambda m: m["start"])
        return out

    def has_events(self) -> bool:
        """True if the extension has ever reported at least one
        (still-retained) event. Used by GET /calendar/available when
        `calendar_source` is "extension" — with Outlook COM off the
        table entirely, "is the calendar available" has to mean "has
        the extension actually synced something" instead of "can we
        reach Outlook". Deliberately NOT time-windowed (unlike
        `get_events`): a user whose next meeting is tomorrow morning
        should still see the calendar as configured/available today.
        Never raises — mirrors `get_events`'s corrupt/absent-file
        tolerance.
        """
        try:
            return len(self.get_events()) > 0
        except CloudFileNotReadyError:
            # An un-downloaded cloud placeholder means we can't tell —
            # treat as "available" so the user isn't told to reconnect
            # a store that's actually fine, just not synced down yet.
            return True

    def capture_status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Honest summary of what the extension has actually delivered,
        for the Record tab's empty state (field report 2026-08-13: an
        empty extension-only calendar rendered identically to "nothing
        on today", so the user had no way to tell the mode itself was
        broken from a genuinely free calendar).

        Returns:
          updated_at: ISO string of the last successful ``replace_all``
            (i.e. the last time a capture — structured or text-fallback
            — was actually written), or None if the extension has never
            imported anything.
          event_count: total events currently retained (any time).
          future_event_count: events still ahead of ``now``.
          last_seen_version: the extension version string from the most
            recent POST (see ``record_extension_version``), or None if
            either nothing has ever posted or the extension that posted
            was older than 1.2.0 and never sent one.
          last_seen_version_at: ISO string of when that version was
            last recorded, or None if nothing has ever posted. Distinct
            from ``last_seen_version`` being None so "never posted" and
            "posted but pre-1.2.0" don't collapse into the same state —
            see ``extension_bundle_service.extension_version_status``.
          last_import_path: "structured" | "text-fallback" | None — which
            calendar-parse path produced the CURRENTLY RETAINED events
            (see ``replace_all``'s ``import_meta``). None if nothing
            has imported yet.
          last_import_raw / last_import_kept / last_import_dropped: the
            counts that path saw, for the same reason last_import_path
            exists — see the server's "Extension calendar: path=..."
            log line, which carries the same numbers.
          last_import_fallback_reason: why a "text-fallback" import fell
            back — e.g. "extension sent no structured events" vs.
            "extension's structured scan found zero events" — or None
            when the path was "structured" (no fallback happened) or
            nothing has imported yet.
          last_import_at: ISO string of when that import happened.

        Never raises — mirrors ``get_events``/``has_events``: a
        cloud-placeholder or corrupt store degrades to "nothing yet"
        rather than taking the Record tab's readiness panel down.
        """
        ref = now or datetime.now()
        empty_import_meta = {
            "last_import_path": None, "last_import_raw": None,
            "last_import_kept": None, "last_import_dropped": None,
            "last_import_fallback_reason": None, "last_import_at": None,
        }
        try:
            with self._lock:
                raw = self._read_locked()
        except CloudFileNotReadyError:
            return {
                "updated_at": None, "event_count": 0, "future_event_count": 0,
                "last_seen_version": None, "last_seen_version_at": None,
                **empty_import_meta,
            }
        events = (raw or {}).get("events") or []
        future = 0
        for entry in events:
            if not isinstance(entry, dict):
                continue
            item = self._deserialize(entry)
            if item and item["subject"] and item["end"] > ref:
                future += 1
        return {
            "updated_at": (raw or {}).get("updated_at") or None,
            "event_count": len(events),
            "future_event_count": future,
            "last_seen_version": (raw or {}).get("last_seen_version") or None,
            "last_seen_version_at": (raw or {}).get("last_seen_version_at") or None,
            "last_import_path": (raw or {}).get("last_import_path"),
            "last_import_raw": (raw or {}).get("last_import_raw"),
            "last_import_kept": (raw or {}).get("last_import_kept"),
            "last_import_dropped": (raw or {}).get("last_import_dropped"),
            "last_import_fallback_reason": (raw or {}).get("last_import_fallback_reason"),
            "last_import_at": (raw or {}).get("last_import_at"),
        }
