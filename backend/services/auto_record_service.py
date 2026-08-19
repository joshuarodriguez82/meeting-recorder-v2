"""
AutoRecordService — calendar-driven auto-start of recordings.

When enabled in Settings, a single asyncio task wakes every 30 seconds,
pulls today's meetings from its injected calendar source, and when an
event's window is currently open (`start <= now < end`) it starts a
recording via the same code path as the manual Start button. Every timed
meeting is recorded — the only exclusions are all-day events and
meetings the user blocklisted; there is intentionally NO conference-link
requirement. The auto-stop watchdog (silence + overrun) handles the
stop side, so this service is intentionally start-only.

WHICH MEETINGS THIS SERVICE CAN SEE
-----------------------------------
It sees exactly what the Record tab's Upcoming Meetings panel shows,
because server.py now injects the SAME merged view both consume —
`services/calendar_feed.py` (local Outlook/EventKit + the Chrome
extension's Outlook Web scrape, gated on `calendar_source`, deduped,
timestamps normalized to naive-local). Before that, this service polled
`calendar_service.get_todays_meetings` — the LOCAL calendar only — so a
user whose entire calendar comes from the extension had a working
auto-record toggle with literally nothing it could act on, while the
panel happily listed every one of those meetings. Read calendar_feed's
module docstring before changing anything about where meetings come
from: a display path and a trigger path disagreeing about what exists
is the bug this arrangement exists to prevent.

Design notes:
  - The loop is event-driven by wall-clock, not by calendar webhooks.
    Outlook COM has no push notification surface we can rely on, and
    EventKit's observers don't survive sandboxing well — a 30s poll on
    a list that's already cached for 5min by calendar_service is cheap.
  - Dedupe key is (normalized subject, start) matched within
    ±DEDUP_TOLERANCE — the SAME rule calendar_feed uses to merge the
    two sources, so a meeting whose extension-side start jitters by a
    couple of minutes between re-imports is still recognized as one we
    already handled. We only auto-start a given event once per backend
    lifetime; if the user manually stops the recording mid-meeting we
    don't immediately re-start it.
  - Manual recordings always win. If `recording_svc.is_recording` is
    True when our window fires, we mark the event as "handled" so we
    don't pounce the moment the user hits Stop.
  - We deliberately keep the LLM/calendar/recording imports lazy
    (passed in via the constructor) so this module stays cheap to
    import at backend startup. The two names imported from
    extension_calendar_service below are pure string/time helpers with
    no I/O and no LLM.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Callable, List, Optional, Tuple

from services.extension_calendar_service import (
    DEDUP_TOLERANCE, normalize_subject,
)
from utils.logger import get_logger

logger = get_logger(__name__)


def _is_all_day(meeting: dict) -> bool:
    """Heuristic — Outlook's _parse_appointment doesn't surface
    `AllDayEvent`, but all-day events have a duration that's an exact
    multiple of 24h and start at midnight local. That catches the
    common cases (birthdays, OOO blocks) without a backend change."""
    start = meeting.get("start")
    end = meeting.get("end")
    if not isinstance(start, _dt.datetime) or not isinstance(end, _dt.datetime):
        return False
    if start.hour == 0 and start.minute == 0:
        delta = (end - start).total_seconds()
        if delta >= 23 * 3600 and delta % (24 * 3600) < 60:
            return True
    return False


class AutoRecordService:
    POLL_INTERVAL_S = 30

    def __init__(
        self,
        *,
        get_upcoming_meetings: Callable[[int], Any],
        is_recording: Callable[[], bool],
        start_recording: Callable[[dict], Any],
        is_enabled: Callable[[], bool],
        get_todays_meetings: Optional[Callable[[], Any]] = None,
        is_blocked: Optional[Callable[[dict], bool]] = None,
    ):
        # Both meeting getters may be either a blocking callable (run on
        # a worker thread, the original contract — Outlook COM must
        # never run on the event loop) or an async one (awaited
        # directly). server.py injects the async `calendar_feed`
        # windows, which already do their own thread offload for each
        # source and run the two concurrently; tests inject plain
        # functions. See `_fetch`.
        self._get_upcoming = get_upcoming_meetings
        # `get_upcoming_meetings` deliberately drops events that have
        # already started (see _calendar_*backends). That filtering made
        # the in-window check below impossible to satisfy — by the time
        # an event's window is open it's no longer "upcoming", so
        # auto-record never actually fired. Today's-meetings includes
        # in-progress events, so we scan THAT for the start trigger and
        # keep upcoming only for the "next: …" UI hint.
        self._get_todays = get_todays_meetings
        self._is_recording = is_recording
        self._start_recording = start_recording
        self._is_enabled = is_enabled
        # Returns True for meetings the user flagged "never auto-record"
        # (permanent skip — survives restarts, matches recurring series
        # by subject). Default: nothing is blocked.
        self._is_blocked = is_blocked or (lambda _m: False)
        self._task: Optional[asyncio.Task] = None
        # (normalized subject, start) pairs we've already attempted in
        # this backend lifetime — the "don't re-record" ledger. NOT a
        # set of exact keys: extension-sourced events are re-imported on
        # every capture and their start can move by a minute or two
        # between imports (OWA rounding, an LLM-recovered timestamp), so
        # an exact key would read as a NEW meeting and auto-start the
        # same call a second time the moment the user stopped the first
        # recording. Matched with the same ±DEDUP_TOLERANCE rule
        # calendar_feed uses to decide two rows are one meeting.
        # Pruned each tick to yesterday-onward, so growth stays bounded
        # at roughly one entry per meeting per day.
        self._handled: List[Tuple[str, _dt.datetime]] = []
        self._next_event: Optional[dict] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def next_event(self) -> Optional[dict]:
        """The nearest upcoming qualifying event, for the status endpoint.
        Returned as a JSON-safe dict (datetimes ISO-stringified)."""
        m = self._next_event
        if not m:
            return None
        return {
            "subject": m.get("subject"),
            "start": m["start"].isoformat() if isinstance(m.get("start"), _dt.datetime) else m.get("start"),
            "end": m["end"].isoformat() if isinstance(m.get("end"), _dt.datetime) else m.get("end"),
            "location": m.get("location", ""),
        }

    def start(self) -> None:
        if self.running:
            return
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run(), name="auto-record-loop")
        logger.info("AutoRecordService loop started")

    async def stop(self) -> None:
        if not self.running:
            return
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            logger.info("AutoRecordService loop stopped")

    async def _run(self) -> None:
        # First tick fires immediately so the user sees "next: …" right
        # after flipping the toggle on, without waiting 30s.
        try:
            while True:
                try:
                    await self._tick()
                except Exception as e:
                    # Never let a transient calendar error (Outlook COM
                    # hiccup, EventKit permission flap) kill the loop.
                    logger.warning(f"auto-record tick failed: {e}")
                await asyncio.sleep(self.POLL_INTERVAL_S)
        except asyncio.CancelledError:
            raise

    @staticmethod
    async def _fetch(fn: Callable[..., Any], *args: Any) -> list:
        """Call an injected meetings getter, sync or async.

        A blocking one goes to a worker thread (Outlook COM on the event
        loop would stall every HTTP request for the length of the
        fetch); an async one — `calendar_feed`'s merged windows, which
        already offload each source and run both concurrently — is
        awaited directly rather than wrapped in a thread that would just
        hand back an un-awaited coroutine.
        """
        if asyncio.iscoroutinefunction(fn):
            got = await fn(*args)
        else:
            got = await asyncio.to_thread(fn, *args)
        return list(got or [])

    async def _tick(self) -> None:
        if not self._is_enabled():
            self._next_event = None
            return
        now = _dt.datetime.now()
        self._prune_handled(now)

        # Pull a small look-ahead so the "next meeting" hint remains
        # populated even if the next call is tomorrow morning. 24h is
        # enough — overnight the user will have closed the laptop.
        upcoming_raw = await self._fetch(self._get_upcoming, 24)
        upcoming = [m for m in upcoming_raw
                    if self._qualifies(m) and m["start"] > now]
        upcoming.sort(key=lambda m: m["start"])
        self._next_event = upcoming[0] if upcoming else None

        # The start trigger scans TODAY's meetings, which (unlike
        # get_upcoming_meetings) still includes events that have already
        # started. Without this, an event was never visible while its
        # window was actually open. Fall back to upcoming if the host
        # didn't supply a today's-meetings source.
        if self._get_todays is not None:
            today_raw = await self._fetch(self._get_todays)
        else:
            today_raw = upcoming_raw
        qualifying = [m for m in today_raw if self._qualifies(m)]
        qualifying.sort(key=lambda m: m["start"])

        # In-window event? Start it (unless we already handled this one
        # or a recording is in progress).
        for m in qualifying:
            # Past meetings fail this on `now < m["end"]`, future ones on
            # `m["start"] <= now` — the single window check is what keeps
            # auto-record from ever re-recording something already over.
            if not (m["start"] <= now < m["end"]):
                continue
            if self._already_handled(m):
                continue
            if self._is_recording():
                # Manual wins — mark handled so we don't pounce on Stop.
                # Also the "don't start a second recording" guarantee:
                # nothing below this line can run while a recording (auto
                # or manual) is in progress.
                self._mark_handled(m)
                logger.info(
                    f"auto-record: '{m.get('subject')}' is in window but a "
                    f"recording is already active; skipping.")
                continue
            self._mark_handled(m)
            logger.info(
                f"auto-record: starting '{m.get('subject')}' "
                f"(source={m.get('source') or 'outlook'}, "
                f"scheduled {m['start']} → {m['end']})")
            try:
                await asyncio.to_thread(self._start_recording, m)
            except Exception as e:
                logger.exception(f"auto-record start failed: {e}")
            # Only one auto-start per tick; if two events somehow
            # overlap we'll catch the next one on the following poll.
            break

    def _qualifies(self, m: dict) -> bool:
        # Deliberately NOT gated on a conference link. Outlook only puts
        # the join URL in the meeting *body*, which the bulk calendar
        # parse omits for speed — so link-gating silently skipped every
        # normal Teams invite. Per product decision, auto-record fires
        # for every timed meeting; the auto-stop watchdog (silence /
        # overrun) ends it, and the all-day filter + blocklist below are
        # the only exclusions.
        #
        # Source-blind by design: an extension-sourced row is a meeting
        # like any other here. The only thing that ever differed was
        # whether this service could SEE it — see the module docstring.
        if not isinstance(m, dict):
            return False
        start, end = m.get("start"), m.get("end")
        if not (isinstance(start, _dt.datetime)
                and isinstance(end, _dt.datetime)):
            # calendar_feed normalizes both fields to naive-local
            # datetimes; anything that survived as a string is a
            # timestamp nothing could parse. Skipping it keeps the sort
            # and the window comparison below from blowing up on a
            # mixed-type list (which the tick would swallow as "tick
            # failed", silently disabling auto-record for every OTHER
            # meeting too).
            return False
        if start.tzinfo is not None or end.tzinfo is not None:
            # Same reasoning, tighter: the window check compares against
            # a naive `datetime.now()`, and aware-vs-naive raises
            # TypeError. calendar_feed guarantees naive-local for both
            # sources, so reaching here means something bypassed it —
            # drop THIS meeting rather than let it take down the tick
            # (and with it every other meeting on the list).
            logger.warning(
                f"auto-record: '{m.get('subject')}' has a timezone-aware "
                f"start/end, which cannot be compared against local wall "
                f"clock; skipping. Meetings must arrive through "
                f"services/calendar_feed.py.")
            return False
        if _is_all_day(m):
            return False
        try:
            if self._is_blocked(m):
                return False
        except Exception as e:
            # A flaky blocklist lookup must not silently disable
            # auto-record for every meeting — log and treat as not
            # blocked.
            logger.warning(f"auto-record blocklist check failed: {e}")
        return True

    @staticmethod
    def _dedup_key(m: dict) -> Tuple[str, Optional[_dt.datetime]]:
        """Identity of one meeting OCCURRENCE for the "already handled"
        ledger.

        Subject is normalized exactly like the calendar merge normalizes
        it (`extension_calendar_service.normalize_subject`: strips
        RE:/FW:/"Updated!" decoration, collapses whitespace, casefolds)
        — OWA decorates a changed invite's subject, so the raw string is
        not stable across re-imports even for the same occurrence.
        The start datetime is compared with tolerance by
        `_already_handled`, never by equality.
        """
        start = m.get("start")
        if not isinstance(start, _dt.datetime):
            start = None
        return (normalize_subject(m.get("subject")), start)

    def _already_handled(self, m: dict) -> bool:
        """True if we've already auto-started (or deliberately skipped)
        this occurrence in this backend lifetime.

        Same collision rule as the two-source merge: normalized subject
        equality AND starts within DEDUP_TOLERANCE. That is what makes
        "the same meeting arrived from both sources" and "the extension
        re-imported the same meeting a minute off" both resolve to ONE
        auto-start.
        """
        key, start = self._dedup_key(m)
        for seen_key, seen_start in self._handled:
            if seen_key != key:
                continue
            if start is None or seen_start is None:
                return True
            if abs(start - seen_start) <= DEDUP_TOLERANCE:
                return True
        return False

    def _mark_handled(self, m: dict) -> None:
        if self._already_handled(m):
            return
        self._handled.append(self._dedup_key(m))

    def _prune_handled(self, now: _dt.datetime) -> None:
        """Forget occurrences that are comfortably in the past. Keeps
        the ledger bounded across a long-running backend without ever
        expiring an entry while its meeting could still be in window
        (24h back is far longer than any meeting we'd still be inside
        of)."""
        cutoff = now - _dt.timedelta(days=1)
        self._handled = [
            (k, s) for k, s in self._handled
            if s is None or s >= cutoff
        ]
