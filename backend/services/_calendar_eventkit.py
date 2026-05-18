"""
macOS Calendar reader via EventKit (pyobjc bridge).

Imported lazily by calendar_service.py when running on macOS. Do NOT
import directly from anywhere else — use calendar_service.* which routes
to this module or to _calendar_outlook.py based on the OS.

EventKit reads the user's local Calendar.app store, which already
aggregates iCloud, Exchange (incl. Microsoft 365 / Outlook for Mac), and
Google calendars that the user has connected via System Settings → Internet
Accounts. So the same code path covers everyone — we never have to know
which provider their meetings actually live in.

Permission model: the first call triggers a system permission prompt
("Meeting Recorder would like to access your calendar"). The user must
say Allow. macOS 14+ uses requestFullAccessToEventsWithCompletion;
earlier versions use requestAccessToEntityType_completion_. We try both.

If the user denied access, every fetch returns []. Re-prompting requires
the user to toggle Meeting Recorder back on in System Settings → Privacy
& Security → Calendars (we can't re-prompt programmatically).
"""

import datetime
import re
import threading
import time
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# ── Caches (mirror the Outlook backend's behaviour) ──────────────────
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 300
_cache: dict = {}
_inflight: dict = {}

# EventKit isn't quite as hostile to concurrent threads as Outlook STA —
# but EKEventStore prefers a single instance reused across calls, and the
# permission prompt callback isn't thread-safe. Serialize all access.
_EK_LOCK = threading.Lock()

# Singleton EKEventStore reused across calls. Created lazily.
_EVENT_STORE = None
_PERMISSION_GRANTED: Optional[bool] = None
_PERMISSION_LOCK = threading.Lock()


def _import_eventkit():
    """Lazy import of pyobjc bridges. Raises ImportError on Windows/Linux."""
    import EventKit  # noqa: F401
    import Foundation  # noqa: F401
    return EventKit, Foundation


def _get_event_store():
    """Singleton EKEventStore. Caller must hold _EK_LOCK."""
    global _EVENT_STORE
    if _EVENT_STORE is None:
        EventKit, _ = _import_eventkit()
        _EVENT_STORE = EventKit.EKEventStore.alloc().init()
    return _EVENT_STORE


def _ensure_permission(timeout_s: float = 30.0) -> bool:
    """
    Block until the EventKit access prompt resolves. Returns True if the
    user granted access. Cached after first decision (re-asking macOS
    after a deny would re-trigger the same deny silently).

    The async callback API requires a NSRunLoop to spin or a completion
    handler invoked on the main thread; we use threading.Event to bridge
    Cocoa's GCD callback back into our worker thread.
    """
    global _PERMISSION_GRANTED
    with _PERMISSION_LOCK:
        if _PERMISSION_GRANTED is not None:
            return _PERMISSION_GRANTED

        try:
            EventKit, _ = _import_eventkit()
        except ImportError as e:
            logger.warning(f"EventKit not available: {e}")
            _PERMISSION_GRANTED = False
            return False

        store = _get_event_store()

        # macOS 14+ replaced requestAccessToEntityType_completion_ with
        # requestFullAccessToEventsWithCompletion_. Both have the same
        # signature: a completion block taking (granted: BOOL, error: NSError?).
        method_new = getattr(store, "requestFullAccessToEventsWithCompletion_", None)
        method_old = getattr(store, "requestAccessToEntityType_completion_", None)

        result = {"granted": False}
        done = threading.Event()

        def _completion(granted, error):
            try:
                if error is not None:
                    logger.warning(f"EventKit access error: {error}")
                result["granted"] = bool(granted)
            finally:
                done.set()

        try:
            if method_new is not None:
                method_new(_completion)
            elif method_old is not None:
                # Entity type 0 = EKEntityTypeEvent (calendar events)
                method_old(0, _completion)
            else:
                logger.warning("EventKit has neither permission API — old macOS?")
                _PERMISSION_GRANTED = False
                return False
        except Exception as e:
            logger.warning(f"EventKit permission call raised: {e}")
            _PERMISSION_GRANTED = False
            return False

        if not done.wait(timeout=timeout_s):
            logger.warning(
                "EventKit permission prompt timed out. The user may not have "
                "responded to the system dialog yet — they can grant access "
                "in System Settings → Privacy & Security → Calendars.")
            return False

        _PERMISSION_GRANTED = result["granted"]
        if not _PERMISSION_GRANTED:
            logger.warning(
                "Calendar access denied. To enable: open System Settings → "
                "Privacy & Security → Calendars and toggle Meeting Recorder on.")
        return _PERMISSION_GRANTED


def _ns_date_from_datetime(dt: datetime.datetime):
    """Convert a Python naive-local datetime to NSDate (interpreted as local)."""
    _, Foundation = _import_eventkit()
    if dt.tzinfo is None:
        # Treat as local time — EventKit's predicate API does too.
        ts = time.mktime(dt.timetuple())
    else:
        ts = dt.timestamp()
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(ts)


def _datetime_from_nsdate(ns_date) -> datetime.datetime:
    """NSDate → naive local Python datetime."""
    if ns_date is None:
        return datetime.datetime.now()
    ts = ns_date.timeIntervalSince1970()
    return datetime.datetime.fromtimestamp(ts)


def _attendees_from_event(ek_event) -> List[str]:
    """Extract attendee strings (prefer email, fall back to name)."""
    attendees: List[str] = []
    try:
        participants = ek_event.attendees()
        if not participants:
            return []
        for p in participants:
            try:
                # EKParticipant.URL is a mailto: URL like "mailto:foo@bar.com"
                url = p.URL()
                addr = ""
                if url is not None:
                    raw = str(url)
                    if raw.lower().startswith("mailto:"):
                        addr = raw[len("mailto:"):]
                if addr:
                    attendees.append(addr)
                else:
                    name = p.name()
                    if name:
                        attendees.append(str(name))
            except Exception:
                continue
    except Exception:
        pass
    return attendees


def _serialize_event(ek_event) -> Optional[dict]:
    try:
        start = _datetime_from_nsdate(ek_event.startDate())
        end = _datetime_from_nsdate(ek_event.endDate())
        duration_min = max(1, int((end - start).total_seconds() / 60))
        title = str(ek_event.title()) if ek_event.title() else "Untitled Meeting"
        location = ""
        try:
            loc = ek_event.location()
            if loc:
                location = str(loc)
        except Exception:
            pass
        organizer = ""
        try:
            org = ek_event.organizer()
            if org is not None:
                org_name = org.name()
                if org_name:
                    organizer = str(org_name)
        except Exception:
            pass
        return {
            "subject": title,
            "start": start,
            "end": end,
            "location": location,
            "organizer": organizer,
            "attendees": _attendees_from_event(ek_event),
            "duration": duration_min,
        }
    except Exception as e:
        logger.debug(f"Skipping EK event: {e}")
        return None


def _fetch_events(start_dt: datetime.datetime,
                  end_dt: datetime.datetime) -> List[dict]:
    """Run an EKEventStore predicate over [start_dt, end_dt). All-or-nothing
    — returns [] on permission denial or any EventKit error."""
    if not _ensure_permission():
        return []
    try:
        EventKit, _ = _import_eventkit()
    except ImportError:
        return []

    with _EK_LOCK:
        store = _get_event_store()

        # Diagnostic snapshot of what EventKit thinks it has — helps
        # distinguish "no calendars" from "predicate returned empty" from
        # "auth dropped silently". Cheap; runs every fetch.
        try:
            auth_status = EventKit.EKEventStore.authorizationStatusForEntityType_(0)
            cals = store.calendarsForEntityType_(0)
            cal_count = len(cals) if cals is not None else 0
            logger.info(
                f"EventKit auth_status={auth_status} "
                f"calendars_visible={cal_count}")
        except Exception as e:
            logger.warning(f"EventKit diagnostic call failed: {e}")
            cals = None

        # If EventKit reports zero visible calendars even though the user
        # granted Full Access, the singleton store is stale (this happens
        # on macOS 14+ when the store was constructed before the auth
        # callback fully propagated). Recreate it once and retry.
        if cals is not None and len(cals) == 0:
            global _EVENT_STORE
            logger.info(
                "EventKit returned zero calendars — recreating event "
                "store to clear stale auth state.")
            _EVENT_STORE = None
            store = _get_event_store()
            try:
                cals = store.calendarsForEntityType_(0)
                logger.info(
                    f"EventKit calendars after recreate: "
                    f"{len(cals) if cals else 0}")
            except Exception:
                cals = None

        try:
            ns_start = _ns_date_from_datetime(start_dt)
            ns_end = _ns_date_from_datetime(end_dt)
            # Pass the explicit calendar list rather than nil. nil is
            # supposed to mean "all calendars", but on macOS 14+ a
            # nil-calendars predicate occasionally returns empty when
            # the store has freshly transitioned from notDetermined →
            # fullAccess. Passing the explicit array is the documented
            # workaround Apple's sample code uses.
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
                ns_start, ns_end, cals)
            ek_events = store.eventsMatchingPredicate_(predicate)
            raw_count = len(ek_events) if ek_events is not None else 0
            logger.info(
                f"EventKit predicate "
                f"[{start_dt.isoformat()} → {end_dt.isoformat()}] "
                f"returned {raw_count} events")
        except Exception as e:
            logger.error(f"EventKit query failed: {e}", exc_info=True)
            return []

        results: List[dict] = []
        for ek in ek_events or []:
            entry = _serialize_event(ek)
            if entry is not None:
                results.append(entry)
        results.sort(key=lambda m: m["start"])
        return results


# ── Rust-EventKit cache reader ──────────────────────────────────────
#
# The Python venv lives outside the .app bundle, so EventKit calls from
# pyobjc are attributed by macOS Tahoe TCC to a separate identity that
# has no usage description and no calendar grant — auth_status comes
# back as 0 (NotDetermined) even when the user has explicitly toggled
# Full Access on for Meeting Recorder. The Rust binary at
# Meeting Recorder.app/Contents/MacOS/meeting-recorder IS part of the
# bundle, so it reads EventKit successfully and dumps events to a JSON
# sidecar at ~/Library/Application Support/MeetingRecorder/
# calendar_cache.json. We read from that here, falling back to the
# pyobjc path (which mostly works in `npx tauri dev` because Terminal
# inherits its TCC) if the cache is missing.
#
# See src-tauri/src/calendar_macos.rs for the writer side.

import json as _json
from pathlib import Path

_RUST_CACHE_PATH = (
    Path.home() / "Library" / "Application Support" / "MeetingRecorder"
    / "calendar_cache.json"
)
# How long after the Rust writer's last successful poll we still trust
# the cache. Rust polls every 5 minutes (POLL_INTERVAL_SECS in
# calendar_macos.rs), so ~15 minutes covers two missed polls before we
# flag the data as stale and fall through to pyobjc.
_RUST_CACHE_MAX_AGE_SECONDS = 15 * 60


def _load_from_rust_cache(
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> Optional[List[dict]]:
    """Return events from the Rust-written cache, or None if the cache
    is missing/stale/unreadable. Caller falls back to pyobjc on None."""
    try:
        if not _RUST_CACHE_PATH.exists():
            return None
        raw = _json.loads(_RUST_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"calendar cache unreadable: {e}")
        return None

    generated_at = raw.get("generated_at", "")
    try:
        gen_dt = datetime.datetime.strptime(
            generated_at, "%Y-%m-%dT%H:%M:%S")
        age = (datetime.datetime.now() - gen_dt).total_seconds()
        if age > _RUST_CACHE_MAX_AGE_SECONDS:
            logger.info(
                f"calendar cache is {age:.0f}s old (> "
                f"{_RUST_CACHE_MAX_AGE_SECONDS}s); falling back to pyobjc")
            return None
    except Exception:
        # Bad timestamp; treat as stale.
        return None

    auth = raw.get("auth_status", 0)
    if auth == 0:
        # Rust reported NotDetermined — user hasn't responded to the
        # prompt yet. Don't return an empty list (which would look like
        # "no meetings") — fall through so pyobjc can take a fresh
        # crack and the prompt can re-fire if needed.
        logger.info(
            "calendar cache reports auth_status=0; falling back to pyobjc")
        return None

    out: List[dict] = []
    for ev in raw.get("events", []):
        try:
            start = datetime.datetime.strptime(
                ev["start"], "%Y-%m-%dT%H:%M:%S")
            end = datetime.datetime.strptime(
                ev["end"], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            continue
        if end < start_dt or start >= end_dt:
            continue
        out.append({
            "subject": ev.get("subject", "Untitled Meeting"),
            "start": start,
            "end": end,
            "location": ev.get("location", "") or "",
            "organizer": ev.get("organizer", "") or "",
            "attendees": list(ev.get("attendees") or []),
            "duration": int(
                ev.get("duration") or
                max(1, (end - start).total_seconds() // 60)
            ),
        })
    out.sort(key=lambda m: m["start"])
    logger.info(
        f"Loaded {len(out)} events from Rust calendar cache "
        f"(generated_at={generated_at}, auth_status={auth})")
    return out


# Wrap the existing _fetch_events with a Rust-cache-first lookup. The
# original is still used for `npx tauri dev` (Terminal-inherited TCC
# happens to work there) and as a defensive fallback if the Rust
# polling thread hasn't written its first cache yet.
_fetch_events_pyobjc = _fetch_events


def _fetch_events(
    start_dt: datetime.datetime,
    end_dt: datetime.datetime,
) -> List[dict]:
    cached = _load_from_rust_cache(start_dt, end_dt)
    if cached is not None:
        return cached
    return _fetch_events_pyobjc(start_dt, end_dt)


# ── Public surface (matches _calendar_outlook.py) ────────────────────

def invalidate_calendar_cache():
    with _CACHE_LOCK:
        _cache.clear()


def _cache_get(key):
    with _CACHE_LOCK:
        entry = _cache.get(key)
        if not entry:
            return None
        exp, val = entry
        if time.time() > exp:
            _cache.pop(key, None)
            return None
        return val


def _cache_put(key, val, ttl=_CACHE_TTL_SECONDS):
    with _CACHE_LOCK:
        _cache[key] = (time.time() + ttl, val)


def get_meetings_for_date(target_date: datetime.date) -> List[dict]:
    start_dt = datetime.datetime.combine(target_date, datetime.time.min)
    end_dt = datetime.datetime.combine(target_date, datetime.time.max)
    return _fetch_events(start_dt, end_dt)


def get_todays_meetings() -> List[dict]:
    return get_meetings_for_date(datetime.datetime.now().date())


def get_upcoming_meetings(hours_ahead: int = 168) -> List[dict]:
    """Cached, dedup-on-inflight upcoming meetings. Mirrors the Outlook
    backend's contract so calendar_service.py can switch transparently."""
    cache_key = ("upcoming", hours_ahead)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"Calendar cache hit ({hours_ahead}h)")
        return cached

    wait_event = None
    are_owner = False
    with _CACHE_LOCK:
        if cache_key in _inflight:
            wait_event = _inflight[cache_key]
        else:
            wait_event = threading.Event()
            _inflight[cache_key] = wait_event
            are_owner = True
    if not are_owner:
        wait_event.wait(timeout=10)
        cached = _cache_get(cache_key)
        return cached if cached is not None else []

    try:
        now = datetime.datetime.now()
        end = now + datetime.timedelta(hours=hours_ahead)
        results = _fetch_events(now, end)
        # Drop already-started events (Outlook backend does the same).
        results = [m for m in results if m["start"] >= now]
        _cache_put(cache_key, results)
        for m in results:
            logger.info(f"  UPCOMING: {m['subject']} @ {m['start']}")
        logger.info(f"Found {len(results)} upcoming meetings ({hours_ahead}h)")
        return results
    finally:
        with _CACHE_LOCK:
            _inflight.pop(cache_key, None)
        wait_event.set()


_URL_RE = re.compile(r"""https?://[^\s<>"'\)\]]+""", re.IGNORECASE)
_CONF_HOSTS = (
    "teams.microsoft.com", "teams.live.com",
    "zoom.us", "zoomgov.com",
    "meet.google.com",
    "webex.com",
    "gotomeeting.com", "gotomeet.me",
    "bluejeans.com", "whereby.com",
    "chime.aws", "ringcentral.com",
)
_BODY_CAP = 6000


def _extract_join_url(*texts: str) -> Optional[str]:
    for t in texts:
        if not t:
            continue
        for u in _URL_RE.findall(t):
            if any(h in u.lower() for h in _CONF_HOSTS):
                return u.rstrip(".,);]>\"'")
    return None


def get_meeting_detail(subject: str, start_iso: str) -> dict:
    """Lazy per-meeting detail (body/agenda + attendees + join link),
    scoped to the meeting's own day. Best-effort: empty fields on any
    miss, never raises. Mirrors the Outlook backend's contract."""
    empty = {"attendees": [], "body": "", "join_url": None}
    try:
        target = datetime.datetime.fromisoformat(start_iso)
        if target.tzinfo is not None:
            target = target.replace(tzinfo=None)
    except ValueError:
        return empty
    want_subj = (subject or "").strip().lower()

    if not _ensure_permission():
        return empty
    try:
        _import_eventkit()
    except ImportError:
        return empty

    day = target.date()
    lo = datetime.datetime.combine(day, datetime.time.min)
    hi = datetime.datetime.combine(day, datetime.time.max)
    try:
        with _EK_LOCK:
            store = _get_event_store()
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
                _ns_date_from_datetime(lo),
                _ns_date_from_datetime(hi),
                None,
            )
            ek_events = store.eventsMatchingPredicate_(predicate)
            for ev in (ek_events or []):
                try:
                    s = _datetime_from_nsdate(ev.startDate())
                except Exception:
                    continue
                if abs((s - target).total_seconds()) > 90:
                    continue
                title = str(ev.title()) if ev.title() else ""
                if title.strip().lower() != want_subj:
                    continue
                body = ""
                try:
                    n = ev.notes()
                    if n:
                        body = str(n).strip()
                except Exception:
                    body = ""
                location = ""
                try:
                    loc = ev.location()
                    if loc:
                        location = str(loc)
                except Exception:
                    pass
                url_str = ""
                try:
                    u = ev.URL()
                    if u is not None:
                        url_str = str(u.absoluteString())
                except Exception:
                    pass
                join_url = _extract_join_url(url_str, location, body)
                if len(body) > _BODY_CAP:
                    body = body[:_BODY_CAP] + "\n…(truncated)"
                return {
                    "attendees": _attendees_from_event(ev),
                    "body": body,
                    "join_url": join_url,
                }
        return empty
    except Exception as e:
        logger.warning(f"meeting-detail failed: {e}")
        return empty


def is_outlook_available() -> bool:
    """
    Name kept for compatibility with _calendar_outlook.py. On macOS this
    answers "is calendar access available" — true once the user has
    granted EventKit permission.
    """
    try:
        _import_eventkit()
    except ImportError:
        return False
    return _ensure_permission()


def make_session_name(meeting: dict) -> str:
    date_str = meeting["start"].strftime("%Y-%m-%d")
    time_str = meeting["start"].strftime("%H%M")
    subject = meeting["subject"]
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in subject)
    safe = safe.strip().replace("  ", " ")[:48]
    return f"{date_str} {time_str} {safe}"
