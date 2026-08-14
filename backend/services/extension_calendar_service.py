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


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Accept a datetime or an ISO string; return a NAIVE-LOCAL
    datetime or None.

    Naive-local is the convention the rest of the calendar stack uses —
    ``_calendar_outlook`` hands back naive local datetimes and
    ``auto_record_service`` compares them against ``datetime.now()``.
    An aware timestamp from the LLM (it likes appending "Z") is
    converted to local and stripped, so we never mix aware and naive
    and blow up on a comparison.
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
                         date_iso: Optional[str] = None) -> List[dict]:
    """Lift calendar events out of a parsed daily briefing.

    Prefers the LLM's ``start_iso`` / ``end_iso``; falls back to
    briefing-date + the human ``time`` / ``duration`` strings when they
    are absent or unparseable (which is the common case on older
    briefings written before the parse was extended). Items with no
    usable start time and cancelled items are dropped — a cancelled
    meeting must not resurface on the Record tab as something to
    record.

    Pure: no I/O, no clock read except through ``date_iso``.
    """
    if not isinstance(briefing, dict):
        return []
    day_iso = str(date_iso or briefing.get("date") or "").strip()
    try:
        day = date_cls.fromisoformat(day_iso)
    except (TypeError, ValueError):
        day = None

    out: List[dict] = []
    for item in briefing.get("agenda") or []:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("title") or "").strip()
        if not subject:
            continue
        if str(item.get("status") or "").strip().lower() == "cancelled":
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
    return out


def events_from_structured(events: Iterable[Any],
                           date_iso: Optional[str] = None) -> List[dict]:
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

    Tolerant of a genuinely hostile payload (wrong types, missing
    keys, garbage strings) — a malformed structured event degrades to
    "skipped", never a 500, since this feeds a client-controlled HTTP
    endpoint. Pure: no I/O, no LLM.
    """
    out: List[dict] = []
    for item in events or []:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        if not subject:
            continue
        start = _coerce_dt(item.get("start"))
        if start is None:
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
            "source": SOURCE_EXTENSION,
        })
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
            "source": SOURCE_EXTENSION,
        }

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
                    now: Optional[datetime] = None) -> List[dict]:
        """Replace the store with ``events`` clipped to the retention
        window. Returns the events actually kept (as dicts with real
        datetimes).

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
            if len(kept) < len(prior_kept):
                merged = merge_meetings(kept, prior_kept)
                logger.warning(
                    f"Extension calendar capture returned fewer events "
                    f"({len(kept)}) than the store already holds in "
                    f"this window ({len(prior_kept)}); merging instead "
                    f"of replacing so a bad/partial capture can't wipe "
                    f"good data.")
                kept = merged
            else:
                kept.sort(key=lambda m: m["start"])

            payload = {
                "updated_at": ref.isoformat(),
                "events": [self._serialize(e) for e in kept],
                # Preserve extension-version bookkeeping (see
                # `record_extension_version`) across a calendar-only
                # write — this method owns "events"/"updated_at", not
                # "last_seen_version*", and must not clobber whichever
                # POST last recorded it.
                "last_seen_version": (raw_before or {}).get("last_seen_version"),
                "last_seen_version_at": (raw_before or {}).get("last_seen_version_at"),
            }
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

        Never raises — mirrors ``get_events``/``has_events``: a
        cloud-placeholder or corrupt store degrades to "nothing yet"
        rather than taking the Record tab's readiness panel down.
        """
        ref = now or datetime.now()
        try:
            with self._lock:
                raw = self._read_locked()
        except CloudFileNotReadyError:
            return {
                "updated_at": None, "event_count": 0, "future_event_count": 0,
                "last_seen_version": None, "last_seen_version_at": None,
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
        }
