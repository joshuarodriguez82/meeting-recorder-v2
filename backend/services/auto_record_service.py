"""
AutoRecordService — calendar-driven auto-start of recordings.

When enabled in Settings, a single asyncio task wakes every 30 seconds,
pulls today's meetings from `calendar_service`, and when an event's
window is currently open (`start <= now < end`) it starts a recording
via the same code path as the manual Start button. Every timed meeting
is recorded — the only exclusions are all-day events and meetings the
user blocklisted; there is intentionally NO conference-link
requirement. The auto-stop watchdog (silence + overrun) handles the
stop side, so this service is intentionally start-only.

Design notes:
  - The loop is event-driven by wall-clock, not by calendar webhooks.
    Outlook COM has no push notification surface we can rely on, and
    EventKit's observers don't survive sandboxing well — a 30s poll on
    a list that's already cached for 5min by calendar_service is cheap.
  - Dedupe key is (subject, start_iso). We only auto-start a given
    event once per backend lifetime; if the user manually stops the
    recording mid-meeting we don't immediately re-start it.
  - Manual recordings always win. If `recording_svc.is_recording` is
    True when our window fires, we mark the event as "handled" so we
    don't pounce the moment the user hits Stop.
  - We deliberately keep the LLM/calendar/recording imports lazy
    (passed in via the constructor) so this module stays cheap to
    import at backend startup.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any, Callable, Optional, Tuple

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
        get_upcoming_meetings: Callable[[int], list[dict]],
        is_recording: Callable[[], bool],
        start_recording: Callable[[dict], Any],
        is_enabled: Callable[[], bool],
        get_todays_meetings: Optional[Callable[[], list[dict]]] = None,
        is_blocked: Optional[Callable[[dict], bool]] = None,
    ):
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
        # (subject, start_iso) tuples we've already attempted in this
        # backend lifetime. Bounded growth — one entry per meeting per day.
        self._handled: set[Tuple[str, str]] = set()
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

    async def _tick(self) -> None:
        if not self._is_enabled():
            self._next_event = None
            return
        now = _dt.datetime.now()

        # Pull a small look-ahead so the "next meeting" hint remains
        # populated even if the next call is tomorrow morning. 24h is
        # enough — overnight the user will have closed the laptop.
        upcoming_raw = await asyncio.to_thread(self._get_upcoming, 24)
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
            today_raw = await asyncio.to_thread(self._get_todays)
        else:
            today_raw = upcoming_raw
        qualifying = [m for m in today_raw if self._qualifies(m)]
        qualifying.sort(key=lambda m: m["start"])

        # In-window event? Start it (unless we already handled this one
        # or a recording is in progress).
        for m in qualifying:
            if not (m["start"] <= now < m["end"]):
                continue
            key = self._dedup_key(m)
            if key in self._handled:
                continue
            if self._is_recording():
                # Manual wins — mark handled so we don't pounce on Stop.
                self._handled.add(key)
                logger.info(
                    f"auto-record: '{m.get('subject')}' is in window but a "
                    f"recording is already active; skipping.")
                continue
            self._handled.add(key)
            logger.info(
                f"auto-record: starting '{m.get('subject')}' "
                f"(scheduled {m['start']} → {m['end']})")
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
    def _dedup_key(m: dict) -> Tuple[str, str]:
        start = m.get("start")
        start_iso = start.isoformat() if isinstance(start, _dt.datetime) else str(start)
        return (str(m.get("subject", "")), start_iso)
