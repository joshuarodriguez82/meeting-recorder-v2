"""
calendar_feed — the SINGLE answer to "which meetings exist right now".

WHY THIS MODULE EXISTS
----------------------
Two callers need that answer and they used to compute it differently:

  1. ``GET /calendar/upcoming`` (the Record tab's 7-day panel) merged
     the LOCAL calendar (Outlook COM / EventKit) with the Chrome
     extension's Outlook Web scrape, gated on ``calendar_source``.
  2. ``AutoRecordService`` (the thing that actually starts a recording)
     polled ``calendar_service.get_todays_meetings`` — the LOCAL
     calendar ONLY.

So a user whose entire calendar comes from the extension (local Outlook
unreachable — the exact case the extension exists for) saw every meeting
in the panel and auto-record could trigger on NONE of them. The toggle
worked; it just had nothing it could see.

A display path and a trigger path disagreeing about what exists is the
bug class this repo has paid for repeatedly, so the fix is not "teach
auto-record to also read the extension store" — that would be a second
implementation of the merge, free to drift. Both callers now go through
``merged_upcoming`` / ``merged_today`` below, which are two windows onto
ONE implementation (``_merged``). If the panel shows a meeting,
auto-record sees the same dict; if auto-record ignores one, the panel
cannot be showing it as recordable.

WHAT THE ONE IMPLEMENTATION OWNS
--------------------------------
  * ``calendar_source`` gating — "off" consults neither source,
    "extension" never touches Outlook COM / EventKit (a single stray
    ``Dispatch("Outlook.Application")`` re-triggers a locked-down
    tenant's sign-in prompt; see config/settings.py).
  * Concurrency — the local fetch and the extension fetch run under one
    ``asyncio.gather`` so a slow/hung Outlook never withholds extension
    events that are already on disk (field report 2026-08-14).
  * Degradation — either side failing yields an empty list from THAT
    side and a log line, never an exception that takes the other side
    (or the caller) down with it. Auto-record must keep firing on
    extension rows while Outlook is broken; that is the whole point.
  * Dedup — ``extension_calendar_service.merge_meetings``: normalized
    subject + start within ±5 minutes, local copy always preferred
    (it carries attendees, organizer, body, resolved join link).
  * TIME NORMALIZATION — every returned ``start``/``end`` is a NAIVE
    LOCAL ``datetime``. See ``_normalize_times``.

TIMEZONES (read this before touching anything here)
---------------------------------------------------
Observed types on the two real paths:

  * LOCAL, Windows — ``_calendar_outlook._to_local_naive`` rebuilds the
    pywintypes value field-by-field into a plain ``datetime``: NAIVE,
    already local wall-clock (pywin32 mislabels the tzinfo as UTC while
    the fields are local, so astimezone() would double-shift it — that's
    the documented "4:40 PM showed as 11:40 AM" bug).
  * LOCAL, macOS — ``_calendar_eventkit._datetime_from_nsdate`` uses
    ``datetime.fromtimestamp(ts)``: NAIVE, local.
  * EXTENSION — stored on disk as naive-local ISO strings
    (``"start": "2026-08-14T08:30:00"``) and rehydrated through
    ``to_naive_local``, which converts an aware value with
    ``astimezone()`` then strips tzinfo. NAIVE, local.

So all three agree today, and ``AutoRecordService`` compares against
``datetime.now()`` (also naive local). ``_normalize_times`` re-asserts
that invariant on every meeting anyway, because the failure mode when it
breaks is silent and total: comparing an aware datetime to a naive one
raises ``TypeError``, the auto-record tick swallows it as "tick failed",
and auto-record simply never fires again with no visible symptom. One
backend swapped for a tz-aware one, or one test fixture built with
``timezone.utc``, is all it would take.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Callable, List, Optional

from services.extension_calendar_service import merge_meetings, to_naive_local
from utils.logger import get_logger

logger = get_logger(__name__)

# Ceiling on the LOCAL fetch only. A user with many shared/resource
# Exchange calendars sees ~30s first-fetch times, so the old 15s cap
# timed out every cold start; 45s clears the realistic slow case while
# still bounding a genuine hang. The extension side is a local JSON
# read and is never timed out.
LOCAL_FETCH_TIMEOUT_S = 45.0

# How far ahead the trigger view looks. Auto-record only ever acts on a
# meeting whose window is ALREADY OPEN, so this just has to be wide
# enough to keep in-progress + imminent events in the list; it is not a
# scheduling horizon.
TRIGGER_HORIZON_HOURS = 24


def _normalize_times(meetings: List[dict]) -> List[dict]:
    """Coerce every ``start``/``end`` to a naive-local ``datetime``.

    Applied to the merged list, so the display path and the trigger path
    are handed identical values — see this module's TIMEZONES section
    for the observed types and for why an aware value slipping through
    is a silent, total failure of auto-record rather than a visible
    error.

    A value that cannot be coerced at all (a garbage string from a
    third-party backend) is left EXACTLY as-is rather than dropped: the
    panel keeps rendering whatever it can, and ``AutoRecordService``
    skips any meeting whose start/end isn't a real datetime instead of
    crashing its sort.
    """
    out: List[dict] = []
    for m in meetings:
        if not isinstance(m, dict):
            continue
        item = dict(m)
        for field in ("start", "end"):
            coerced = to_naive_local(item.get(field))
            if coerced is not None:
                item[field] = coerced
        out.append(item)
    return out


async def _merged(
    *,
    source: str,
    fetch_local: Callable[[], List[dict]],
    fetch_extension: Optional[Callable[[], List[dict]]],
    local_timeout: Optional[float],
    window: str,
) -> List[dict]:
    """The one implementation. Both public windows below delegate here.

    ``fetch_local`` / ``fetch_extension`` are zero-arg BLOCKING callables
    (they're run on threads) already bound to whatever window the caller
    wants. Everything else — gating, concurrency, failure handling,
    dedup, time normalization — lives here so it cannot differ between
    callers.
    """
    src = (source or "auto").strip().lower()

    async def _local() -> List[dict]:
        # "extension" and "off" must not merely return empty — they must
        # never make the call. Attaching to Outlook is itself what
        # trips a locked-down tenant's conditional-access challenge.
        if src in ("extension", "off"):
            return []
        try:
            call = asyncio.to_thread(fetch_local)
            got = (await asyncio.wait_for(call, timeout=local_timeout)
                   if local_timeout else await call)
            return list(got or [])
        except asyncio.TimeoutError:
            logger.warning(
                f"Local calendar fetch ({window}) exceeded "
                f"{local_timeout}s — omitted this round. Outlook/Exchange "
                f"likely slow to respond; the background thread keeps "
                f"going and populates the 5-min cache.")
            return []
        except Exception as e:  # noqa: BLE001
            # Degrade to "extension only" instead of propagating. A
            # broken local calendar must not be able to withhold the
            # extension events — that combination (no local access at
            # all, everything coming from the extension) is precisely
            # the user this module exists for.
            logger.warning(
                f"Local calendar fetch ({window}) failed ({e}); "
                f"continuing with extension events only.")
            return []

    async def _extension() -> List[dict]:
        if src == "off" or fetch_extension is None:
            return []
        try:
            return list(await asyncio.to_thread(fetch_extension) or [])
        except Exception as e:  # noqa: BLE001
            # Additive source: a broken/unreadable store degrades to
            # "local calendar only" rather than failing the caller.
            logger.warning(
                f"Extension calendar events unavailable ({window}: {e}); "
                f"continuing with the local calendar only.")
            return []

    local, extension = await asyncio.gather(_local(), _extension())
    return _normalize_times(merge_meetings(local, extension))


def _extension_fetcher(
        extension_svc: Any,
        hours: Optional[int]) -> Optional[Callable[[], List[dict]]]:
    if extension_svc is None:
        return None
    return lambda: extension_svc.get_events(hours)


async def merged_upcoming(
    hours: int,
    *,
    source: str,
    fetch_local: Callable[[], List[dict]],
    extension_svc: Any = None,
    local_timeout: Optional[float] = LOCAL_FETCH_TIMEOUT_S,
) -> List[dict]:
    """PANEL WINDOW — everything from now through ``hours`` ahead.

    Feeds ``GET /calendar/upcoming`` (7 days by default) and
    auto-record's "next: …" status hint. Note this window deliberately
    excludes meetings that have already STARTED on the local side (the
    backends drop them), which is exactly why it cannot be the trigger
    view — see ``merged_today``.
    """
    return await _merged(
        source=source,
        fetch_local=fetch_local,
        fetch_extension=_extension_fetcher(extension_svc, hours),
        local_timeout=local_timeout,
        window=f"upcoming {hours}h",
    )


async def merged_today(
    *,
    source: str,
    fetch_local: Callable[[], List[dict]],
    extension_svc: Any = None,
    horizon_hours: int = TRIGGER_HORIZON_HOURS,
    local_timeout: Optional[float] = LOCAL_FETCH_TIMEOUT_S,
) -> List[dict]:
    """TRIGGER WINDOW — today's meetings, INCLUDING ones already in
    progress.

    This is the view ``AutoRecordService`` scans for its start trigger.
    It has to include in-progress events: by the time a meeting's window
    is open (``start <= now < end``) it is no longer "upcoming", so the
    panel window above can never satisfy the in-window check.

    Local side: ``calendar_service.get_todays_meetings`` (date-based,
    keeps events that already started). Extension side:
    ``ExtensionCalendarService.get_events(horizon_hours)``, which keeps
    anything whose END is still ahead — same "in progress still counts"
    semantics, from the other source.
    """
    return await _merged(
        source=source,
        fetch_local=fetch_local,
        fetch_extension=_extension_fetcher(extension_svc, horizon_hours),
        local_timeout=local_timeout,
        window="today",
    )


def is_datetime(value: Any) -> bool:
    """True for a usable datetime. Exported so AutoRecordService can
    skip a meeting whose timestamps never coerced rather than crash its
    sort on a mixed-type list."""
    return isinstance(value, _dt.datetime)
