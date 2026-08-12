"""
Reads today's Outlook calendar appointments via COM. Windows-only.

Imported lazily by calendar_service.py when running on Windows. Do not
import directly from anywhere else — use calendar_service.* which routes
to this module or to _calendar_eventkit.py based on the OS.

Uses default calendar first (fast path), falls back to recursive scan.
Works with Classic Outlook. Gracefully fails with New Outlook.
"""

import datetime
import re
import time
import threading
import win32com.client      # noqa: F401  Windows-only
from typing import List, Optional
from utils.com_worker import run_com
from utils.logger import get_logger

logger = get_logger(__name__)

# Module-level cache. Outlook COM is very slow on accounts with Exchange
# resource / shared calendars (each folder enumeration is 5-7s), so we
# memoize aggressively and background-refresh. 5 minutes is long enough
# that polling never re-hits COM in a normal session.
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache: dict = {}  # key -> (expires_epoch, result)

# Per-key in-flight dedup. If the pre-warm thread and a real API request
# both ask for the same key at the same time, we want one Outlook call,
# not two. _inflight[key] -> Event that's set when the result is cached.
_inflight: dict = {}

# Outlook COM under STA (single-threaded apartment) does not tolerate
# concurrent access from multiple Python threads — calls can hang for
# minutes waiting on the other thread's message pump. We serialize every
# Outlook interaction behind this lock. Each fetch is fast (<1s) so the
# serialization cost is trivial.
_OUTLOOK_LOCK = threading.Lock()

# Folders to skip entirely — these never have meetings the user cares
# about but are slow to enumerate via Exchange. Matched case-insensitively
# as a substring against the folder / store name.
_SKIP_FOLDER_KEYWORDS = (
    "resource",       # "Development Resources Calendar", etc.
    "birthday",
    "holiday",
    "us holidays",
    "contacts",
    "public folders",
    "shared folders",
)

# Top-level store names to skip. Personal archive stores etc.
_SKIP_STORE_KEYWORDS = (
    "public folders",
    "internet calendars",
)


def _should_skip_folder(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(kw in low for kw in _SKIP_FOLDER_KEYWORDS)


def _should_skip_store(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(kw in low for kw in _SKIP_STORE_KEYWORDS)


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


def invalidate_calendar_cache():
    """Called by the /calendar/upcoming endpoint when the user hits Refresh."""
    with _CACHE_LOCK:
        _cache.clear()

# Outlook default folder constants
OL_FOLDER_CALENDAR = 9
OL_APPOINTMENT_ITEM = 26


def _get_outlook(retries: int = 3, delay: float = 1.0):
    """
    Connect to Outlook. Tries GetActiveObject first (fast if Outlook is
    already open), falls back to Dispatch (starts Outlook if needed).

    Must be called from inside a run_com() job (utils/com_worker.py) —
    this only touches win32com, it does not itself initialize COM. The
    process's single COM worker thread initializes its apartment once,
    for the life of the process, and never uninitializes it.
    """
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            outlook = win32com.client.GetActiveObject("Outlook.Application")
            logger.info("Connected to running Outlook instance")
            return outlook
        except Exception as e:
            last_err = e
            logger.debug(f"GetActiveObject attempt {attempt+1} failed: {e}")
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                logger.info("Connected via Dispatch (started Outlook)")
                return outlook
            except Exception as e2:
                last_err = e2
                logger.debug(f"Dispatch attempt {attempt+1} failed: {e2}")
            if attempt < retries - 1:
                time.sleep(delay)

    logger.warning(
        f"Could not connect to Outlook: {last_err}. "
        "If using New Outlook, switch to Classic or meetings won't load."
    )
    return None


def _looks_smtp(s: str) -> bool:
    return "@" in s and not s.startswith(("/", "EX:", "ex:"))


def _recipient_label(r, resolve_smtp: bool = False) -> str:
    """A readable attendee label.

    Outlook returns the internal X500 directory path
    (`/o=ExchangeLabs/ou=…/cn=…`) as `.Address` for Exchange
    recipients, which is useless in the UI and poisons the AI brief +
    domain-based client auto-tagging. Prefer a real SMTP address, then
    the display name; never surface a raw X500 string.

    `resolve_smtp=True` does the (COM-expensive) AddressEntry lookup —
    only used on the lazy per-meeting detail path, never in the bulk
    calendar scan.
    """
    try:
        name = str(getattr(r, "Name", "") or "").strip()
    except Exception:
        name = ""
    try:
        addr = str(getattr(r, "Address", "") or "").strip()
    except Exception:
        addr = ""

    email = addr if _looks_smtp(addr) else ""
    if not email and resolve_smtp:
        try:
            ae = r.AddressEntry
            try:
                eu = ae.GetExchangeUser()
                if eu is not None:
                    smtp = str(getattr(eu, "PrimarySmtpAddress", "") or "")
                    if _looks_smtp(smtp):
                        email = smtp
            except Exception:
                pass
            if not email:
                # PR_SMTP_ADDRESS — works for contacts / non-EX entries.
                try:
                    pa = ae.PropertyAccessor
                    smtp = str(pa.GetProperty(
                        "http://schemas.microsoft.com/mapi/proptag/"
                        "0x39FE001F") or "")
                    if _looks_smtp(smtp):
                        email = smtp
                except Exception:
                    pass
        except Exception:
            pass

    if name and email:
        return f"{name} <{email}>"
    if email:
        return email
    if name:
        return name
    return addr[:80] if addr else ""


def _parse_appointment(item, today: datetime.date) -> Optional[dict]:
    """Extract meeting info from an Outlook AppointmentItem."""
    try:
        start_local = _to_local_naive(item.Start)
        end_local = _to_local_naive(item.End)

        if start_local.date() != today:
            return None

        duration_min = max(1, int((end_local - start_local).total_seconds() / 60))

        # Extract attendees if available
        attendees = []
        try:
            for r in item.Recipients:
                try:
                    label = _recipient_label(r)
                    if label:
                        attendees.append(label)
                except Exception:
                    continue
        except Exception:
            pass

        return {
            "subject":  str(item.Subject) if item.Subject else "Untitled Meeting",
            "start":    start_local,
            "end":      end_local,
            "location": str(getattr(item, "Location", "") or ""),
            "organizer": str(getattr(item, "Organizer", "") or ""),
            "attendees": attendees,
            "duration": duration_min,
        }
    except Exception as e:
        logger.debug(f"Skipping appointment: {e}")
        return None


def _read_appointments_range(
    folder, start_date: datetime.date, end_date: datetime.date
) -> List[dict]:
    """
    Read appointments from a folder between start_date and end_date
    (inclusive) in ONE pass. Before, we called this per-date which
    multiplied the COM round-trips.
    """
    meetings: List[dict] = []
    try:
        items = folder.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True

        yesterday = start_date - datetime.timedelta(days=1)
        day_after = end_date + datetime.timedelta(days=2)
        restriction = (
            f"[Start] >= '{yesterday.strftime('%m/%d/%Y')} 12:00 AM' AND "
            f"[Start] <= '{day_after.strftime('%m/%d/%Y')} 11:59 PM'"
        )
        try:
            filtered = items.Restrict(restriction)
            filtered.Sort("[Start]")
            filtered.IncludeRecurrences = True
        except Exception as e:
            logger.debug(f"Restrict failed, iterating all items: {e}")
            filtered = items

        count = 0
        for item in filtered:
            count += 1
            if count > 1000:
                logger.warning(f"Folder '{folder.Name}' >1000 items, truncating")
                break
            parsed = _parse_appointment_any_date(item, start_date, end_date)
            if parsed:
                meetings.append(parsed)

        logger.info(
            f"  '{folder.Name}': {len(meetings)} meetings in "
            f"{start_date}..{end_date} (scanned {count})")
    except Exception as e:
        logger.warning(
            f"Could not read folder '{getattr(folder, 'Name', '?')}': {e}")
    return meetings


def _to_local_naive(dt) -> datetime.datetime:
    """
    Convert an Outlook COM datetime (pywintypes.datetime) to a naive
    local datetime.

    IMPORTANT: pywin32 on Windows stamps the tzinfo as UTC on
    pywintypes.datetime objects, but the numeric field values are
    actually already in LOCAL time (what Outlook displays). Calling
    astimezone() shifts them a second time, which caused 4:40 PM
    meetings to appear as 11:40 AM. Trust the raw fields instead.
    """
    return datetime.datetime(
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _parse_appointment_any_date(item, start_date, end_date):
    """Same as _parse_appointment but accepts any date in [start_date, end_date]."""
    try:
        start_local = _to_local_naive(item.Start)
        end_local = _to_local_naive(item.End)
        d = start_local.date()
        if d < start_date or d > end_date:
            return None
        duration_min = max(1, int((end_local - start_local).total_seconds() / 60))
        attendees = []
        try:
            for r in item.Recipients:
                try:
                    label = _recipient_label(r)
                    if label:
                        attendees.append(label)
                except Exception:
                    continue
        except Exception:
            pass
        return {
            "subject":  str(item.Subject) if item.Subject else "Untitled Meeting",
            "start":    start_local,
            "end":      end_local,
            "location": str(getattr(item, "Location", "") or ""),
            "organizer": str(getattr(item, "Organizer", "") or ""),
            "attendees": attendees,
            "duration": duration_min,
        }
    except Exception as e:
        logger.debug(f"Skipping appointment: {e}")
        return None


def _read_appointments(folder, target_date: datetime.date) -> List[dict]:
    """Backwards-compat wrapper."""
    return _read_appointments_range(folder, target_date, target_date)


def _scan_folder_recursively(folder, today: datetime.date,
                              seen: set, results: List[dict], depth: int = 0):
    """Walk folder tree looking for calendar folders."""
    if depth > 5:
        return
    try:
        # DefaultItemType 1 = calendar items
        if getattr(folder, "DefaultItemType", -1) == 1:
            name = folder.Name.lower()
            # Skip noise calendars
            if not any(skip in name for skip in
                       ("birthday", "holiday", "contacts", "us holidays")):
                for m in _read_appointments(folder, today):
                    key = (m["subject"], m["start"].isoformat())
                    if key not in seen:
                        seen.add(key)
                        results.append(m)

        # Recurse into subfolders
        for sub in folder.Folders:
            _scan_folder_recursively(sub, today, seen, results, depth + 1)
    except Exception as e:
        logger.debug(f"Skipping folder at depth {depth}: {e}")


def get_meetings_for_date(target_date: datetime.date) -> List[dict]:
    """Return all meetings on a specific date across every calendar.

    Cached (short TTL): the auto-record loop calls this every 30s, and
    an uncached COM scan that often saturates Outlook's single-threaded
    lock and starves the UI's 7-day fetch (it then times out → blank
    calendar). 2-minute freshness is plenty for an auto-start trigger
    that only needs to notice a meeting within a poll or two."""
    cache_key = ("date", target_date.isoformat())
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"Calendar cache hit (date {target_date})")
        return cached

    # All Outlook COM access happens inside this closure, which runs on
    # the process's single COM worker thread (utils/com_worker.py) — see
    # that module for why. Only plain dicts are returned; no COM object
    # crosses back out of _fetch.
    def _fetch() -> List[dict]:
        outlook = _get_outlook()
        if not outlook:
            return []

        try:
            ns = outlook.GetNamespace("MAPI")
            all_meetings: List[dict] = []
            seen: set = set()

            # Fast path: default calendar
            try:
                default_cal = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
                logger.info(f"Reading default calendar for {target_date}: "
                            f"{default_cal.Name}")
                for m in _read_appointments(default_cal, target_date):
                    key = (m["subject"], m["start"].isoformat())
                    if key not in seen:
                        seen.add(key)
                        all_meetings.append(m)
            except Exception as e:
                logger.warning(f"Could not read default calendar: {e}")

            # Slow path: scan all stores for shared/resource calendars
            try:
                for store in ns.Stores:
                    try:
                        root = store.GetRootFolder()
                        for folder in root.Folders:
                            _scan_folder_recursively(
                                folder, target_date, seen, all_meetings)
                    except Exception as e:
                        logger.debug(f"Store scan failed: {e}")
            except Exception as e:
                logger.debug(f"Could not enumerate stores: {e}")

            all_meetings.sort(key=lambda m: m["start"])
            logger.info(f"Found {len(all_meetings)} meetings for {target_date}")
            _cache_put(cache_key, all_meetings, ttl=120)
            return all_meetings

        except Exception as e:
            logger.error(f"Failed to read calendar: {e}", exc_info=True)
            return []

    try:
        return run_com(_fetch, timeout=90.0)
    except Exception as e:
        logger.error(f"get_meetings_for_date: COM worker call failed: {e}")
        return []


# Conference-link extraction for the lazy meeting-detail view: return
# the full clickable URL so the UI can offer a one-click Join button.
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
# Invite bodies can be enormous (full reply chains). Cap so the API
# payload and the LLM brief context stay sane.
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
    """Lazy per-meeting detail: body/agenda + attendees + join link.

    Deliberately scoped to the meeting's own day on the default
    calendar so it stays fast — the bulk list omits bodies on purpose
    (Outlook .Body access is slow and the text is large). Best-effort:
    returns empty fields on any miss and never raises."""
    empty = {"attendees": [], "body": "", "join_url": None}
    try:
        target = datetime.datetime.fromisoformat(start_iso)
        if target.tzinfo is not None:
            target = target.replace(tzinfo=None)
    except ValueError:
        return empty
    want_subj = (subject or "").strip().lower()

    # Cache so a meeting opened twice (or re-rendered) is instant and
    # doesn't re-queue behind the periodic calendar scan for the single
    # Outlook COM lock — that contention is what made first-vs-repeat
    # opens feel ~30s slow.
    cache_key = ("detail", want_subj, target.isoformat())
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if not _OUTLOOK_LOCK.acquire(timeout=30):
        logger.warning("meeting-detail: Outlook lock timeout")
        return empty

    # All Outlook COM access happens inside this closure, which runs on
    # the process's single COM worker thread (utils/com_worker.py). Only
    # a plain dict is returned; no COM object crosses back out of _fetch.
    def _fetch() -> dict:
        outlook = _get_outlook()
        if not outlook:
            return empty
        try:
            ns = outlook.GetNamespace("MAPI")
            cal = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
            items = cal.Items
            items.Sort("[Start]")
            items.IncludeRecurrences = True
            day = target.date()
            lo = (day - datetime.timedelta(days=1)).strftime("%m/%d/%Y")
            hi = (day + datetime.timedelta(days=1)).strftime("%m/%d/%Y")
            try:
                items = items.Restrict(
                    f"[Start] >= '{lo} 12:00 AM' AND [Start] <= '{hi} 11:59 PM'")
                items.Sort("[Start]")
                items.IncludeRecurrences = True
            except Exception:
                pass

            count = 0
            for item in items:
                count += 1
                if count > 1000:
                    break
                try:
                    s = _to_local_naive(item.Start)
                except Exception:
                    continue
                # Recurring instances share a subject; match the occurrence
                # by start time (90s slack absorbs tz/rounding).
                if abs((s - target).total_seconds()) > 90:
                    continue
                if str(getattr(item, "Subject", "") or "").strip().lower() != want_subj:
                    continue

                attendees: List[str] = []
                try:
                    # Resolve to real names/emails here (per-meeting, on
                    # demand) — worth the COM cost for a usable brief +
                    # attendee panel. Cap so a 200+ person invite can't
                    # make the detail call crawl through hundreds of
                    # AddressEntry lookups.
                    _ATT_CAP = 25
                    idx = 0
                    for r in item.Recipients:
                        idx += 1
                        if idx > _ATT_CAP:
                            attendees.append(f"(+ more attendees not shown)")
                            break
                        try:
                            label = _recipient_label(r, resolve_smtp=True)
                            if label:
                                attendees.append(label)
                        except Exception:
                            continue
                except Exception:
                    pass

                try:
                    body = str(getattr(item, "Body", "") or "").strip()
                except Exception:
                    body = ""
                location = str(getattr(item, "Location", "") or "")
                join_url = _extract_join_url(location, body)
                if len(body) > _BODY_CAP:
                    body = body[:_BODY_CAP] + "\n…(truncated)"
                result = {
                    "attendees": attendees,
                    "body": body,
                    "join_url": join_url,
                }
                _cache_put(cache_key, result)
                return result
            # No match — cache the miss briefly so repeated clicks during a
            # slow Outlook window don't all pile onto the COM lock.
            _cache_put(cache_key, empty, ttl=30)
            return empty
        except Exception as e:
            logger.warning(f"meeting-detail failed: {e}")
            return empty

    try:
        return run_com(_fetch, timeout=60.0)
    except Exception as e:
        logger.warning(f"meeting-detail: COM worker call failed: {e}")
        return empty
    finally:
        try:
            _OUTLOOK_LOCK.release()
        except RuntimeError:
            pass


def get_todays_meetings() -> List[dict]:
    """Return today's meetings from Outlook calendar."""
    return get_meetings_for_date(datetime.datetime.now().date())


def get_upcoming_meetings(hours_ahead: int = 168) -> List[dict]:
    """
    Return meetings from now through `hours_ahead` hours ahead.
    ONE Outlook connection. ONE pass per folder. Cached 5 min so polling
    never re-hits COM. Concurrent callers share a single in-flight
    request via _inflight so pre-warm + real request don't compete.
    """
    cache_key = ("upcoming", hours_ahead)
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"Calendar cache hit ({hours_ahead}h)")
        return cached

    # Dedup concurrent requests — if another thread is already fetching
    # this same key, wait for it instead of kicking off a second COM call.
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
        logger.info(f"Calendar fetch in progress, waiting ({hours_ahead}h)")
        # Short wait — if the in-flight fetch is wedged, we'd rather return
        # [] and let the UI retry than hang the HTTP request. Normal fetch
        # takes <1s; 10s is plenty of slack for Exchange latency.
        wait_event.wait(timeout=10)
        cached = _cache_get(cache_key)
        return cached if cached is not None else []

    t0 = time.time()
    # Serialize ALL Outlook COM access. STA + concurrent python threads = hangs.
    lock_acquired = _OUTLOOK_LOCK.acquire(timeout=60)
    if not lock_acquired:
        logger.warning("Timed out waiting for Outlook lock — another fetch is stuck")
        with _CACHE_LOCK:
            _inflight.pop(cache_key, None)
        wait_event.set()
        return []

    # All Outlook COM access happens inside this closure, which runs on
    # the process's single COM worker thread (utils/com_worker.py) — see
    # that module's docstring for why two COM subsystems sharing the
    # default asyncio.to_thread pool caused STATUS_ACCESS_VIOLATION
    # crashes. Only a plain list of dicts (or None for "no Outlook") is
    # returned; no COM object crosses back out of _fetch.
    def _fetch() -> Optional[List[dict]]:
        outlook = _get_outlook()
        if not outlook:
            return None

        try:
            ns = outlook.GetNamespace("MAPI")
            now = datetime.datetime.now()
            end = now + datetime.timedelta(hours=hours_ahead)
            start_date = now.date()
            end_date = end.date()

            all_meetings: List[dict] = []
            seen: set = set()
            scanned_entry_ids: set = set()  # dedup folders across stores

            dropped_past = 0
            dropped_future = 0
            dropped_samples: List[str] = []

            def consume(folder):
                nonlocal dropped_past, dropped_future
                for m in _read_appointments_range(folder, start_date, end_date):
                    # Drop meetings whose END is in the past, not whose
                    # START is in the past. The old "start < now" filter
                    # made an in-progress meeting invisible the moment it
                    # started, which collapsed the Upcoming Meetings panel
                    # to empty while auto-record was actively capturing
                    # that meeting — the user saw "recording in progress"
                    # at the top with no entry in the panel below.
                    # AutoRecordService applies its own `start > now`
                    # filter for the "next event" hint, so keeping
                    # in-progress meetings here doesn't double-trigger
                    # anything.
                    if m["end"] < now:
                        dropped_past += 1
                        if len(dropped_samples) < 5:
                            dropped_samples.append(
                                f"PAST: {m['subject']} @ {m['start']} (now={now})")
                        continue
                    if m["start"] > end:
                        dropped_future += 1
                        if len(dropped_samples) < 5:
                            dropped_samples.append(
                                f"FUTURE: {m['subject']} @ {m['start']} (end={end})")
                        continue
                    key = (m["subject"], m["start"].isoformat())
                    if key not in seen:
                        seen.add(key)
                        all_meetings.append(m)

            # Default calendar — fast path
            default_entry_id = None
            try:
                default_cal = ns.GetDefaultFolder(OL_FOLDER_CALENDAR)
                try:
                    default_entry_id = default_cal.EntryID
                except Exception:
                    pass
                logger.info(f"Reading default calendar: {default_cal.Name}")
                consume(default_cal)
                if default_entry_id:
                    scanned_entry_ids.add(default_entry_id)
            except Exception as e:
                logger.warning(f"Default calendar read failed: {e}")

            # Other stores/shared calendars — ONE walk. Skip stores we
            # don't care about (resource calendars, public folders)
            # entirely so we don't pay the per-folder Exchange latency
            # cost.
            try:
                for store in ns.Stores:
                    try:
                        store_name = getattr(store, "DisplayName", "") or ""
                        if _should_skip_store(store_name):
                            logger.info(f"(skip store: {store_name})")
                            continue
                        logger.info(f"Walking store: {store_name}")
                        root = store.GetRootFolder()
                        for folder in root.Folders:
                            _scan_folder_recursively_range(
                                folder, start_date, end_date,
                                seen, all_meetings, scanned_entry_ids)
                    except Exception as e:
                        logger.warning(f"Store scan failed for '{store_name}': {e}")
            except Exception as e:
                logger.warning(f"Could not enumerate stores: {e}")

            all_meetings.sort(key=lambda m: m["start"])
            elapsed = time.time() - t0
            logger.info(
                f"Found {len(all_meetings)} upcoming meetings ({hours_ahead}h) "
                f"in {elapsed:.1f}s  [dropped: {dropped_past} already-started, "
                f"{dropped_future} beyond {hours_ahead}h]")
            if dropped_samples:
                logger.info(f"Filter drop samples: {dropped_samples}")
            # Log the subjects/starts we're returning so we can see if the
            # user's just-added meeting made it through.
            for m in all_meetings:
                logger.info(f"  UPCOMING: {m['subject']} @ {m['start']}")
            return all_meetings

        except Exception as e:
            logger.error(f"Failed to read calendar: {e}", exc_info=True)
            return []

    try:
        result = run_com(_fetch, timeout=120.0)
    except Exception as e:
        logger.error(f"get_upcoming_meetings: COM worker call failed: {e}")
        result = []
    finally:
        # Release any waiting requests — both on success and on error.
        with _CACHE_LOCK:
            _inflight.pop(cache_key, None)
        wait_event.set()
        try:
            _OUTLOOK_LOCK.release()
        except RuntimeError:
            pass  # not held on early-return paths

    if result is None:
        _cache_put(cache_key, [], ttl=30)  # also cache the "no Outlook" miss briefly
        return []
    _cache_put(cache_key, result)
    return result


def _scan_folder_recursively_range(
    folder, start_date, end_date, seen, results,
    scanned_entry_ids: Optional[set] = None, depth: int = 0,
):
    if depth > 3:  # Capped recursion — calendar folders are always near the root
        return
    try:
        name = getattr(folder, "Name", "") or ""
        if _should_skip_folder(name):
            logger.info(f"  (skip folder by keyword: {name})")
            return
        is_cal = getattr(folder, "DefaultItemType", -1) == 1
        if is_cal:
            logger.info(f"  (found calendar folder: {name} at depth {depth})")
        if is_cal:  # keep existing condition flow below untouched
            # Skip folders we already scanned (e.g. default calendar
            # encountered again via the store walk).
            entry_id = None
            if scanned_entry_ids is not None:
                try:
                    entry_id = folder.EntryID
                except Exception:
                    pass
                if entry_id and entry_id in scanned_entry_ids:
                    return
                if entry_id:
                    scanned_entry_ids.add(entry_id)
            for m in _read_appointments_range(folder, start_date, end_date):
                key = (m["subject"], m["start"].isoformat())
                if key not in seen:
                    seen.add(key)
                    results.append(m)
        # Only recurse if this folder isn't itself a calendar — calendar
        # folders don't have useful subfolders and iterating them is slow.
        else:
            for sub in folder.Folders:
                _scan_folder_recursively_range(
                    sub, start_date, end_date, seen, results,
                    scanned_entry_ids, depth + 1)
    except Exception as e:
        logger.debug(f"Skipping folder at depth {depth}: {e}")


def is_outlook_available() -> bool:
    """Quick check — true if we can connect to Outlook COM."""
    def _check() -> bool:
        try:
            win32com.client.GetActiveObject("Outlook.Application")
            return True
        except Exception:
            try:
                win32com.client.Dispatch("Outlook.Application")
                return True
            except Exception:
                return False

    try:
        return run_com(_check, timeout=30.0)
    except Exception as e:
        logger.warning(f"is_outlook_available: COM worker call failed: {e}")
        return False


def make_session_name(meeting: dict) -> str:
    """Generate a clean session name from a meeting dict."""
    date_str = meeting["start"].strftime("%Y-%m-%d")
    time_str = meeting["start"].strftime("%H%M")
    subject = meeting["subject"]
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in subject)
    safe = safe.strip().replace("  ", " ")[:48]
    return f"{date_str} {time_str} {safe}"
