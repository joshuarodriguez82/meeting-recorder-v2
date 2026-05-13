"""
AutoRecordService — calendar-driven auto-start of recordings.

When enabled in Settings, a single asyncio task wakes every 30 seconds,
pulls today's meetings from `calendar_service`, filters out events the
user said they don't want, and when an event's window is currently open
(`start <= now < end`) it starts a recording via the same code path as
the manual Start button. The auto-stop watchdog (silence + overrun)
handles the stop side, so this service is intentionally start-only.

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
import re
from typing import Any, Callable, Optional, Tuple

from utils.logger import get_logger

logger = get_logger(__name__)


# Conference-link detection. Outlook stuffs join URLs into Location,
# Subject, or the meeting body; we only see Location reliably from the
# Outlook COM backend, so we match the common providers there. The
# regex is intentionally forgiving — anything with these hosts counts.
_CONF_LINK_RE = re.compile(
    r"https?://[^\s]*?("
    r"teams\.microsoft\.com|teams\.live\.com|"
    r"zoom\.us|zoomgov\.com|"
    r"meet\.google\.com|"
    r"webex\.com|"
    r"gotomeeting\.com|gotomeet\.me|"
    r"bluejeans\.com|"
    r"whereby\.com"
    r")", re.IGNORECASE)


def _has_conference_link(meeting: dict) -> bool:
    blob = " ".join(
        str(meeting.get(k, "") or "")
        for k in ("location", "subject", "body", "organizer"))
    return bool(_CONF_LINK_RE.search(blob))


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
    ):
        self._get_upcoming = get_upcoming_meetings
        self._is_recording = is_recording
        self._start_recording = start_recording
        self._is_enabled = is_enabled
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
        # Pull today + a small look-ahead so "next meeting" remains
        # populated even if the next call is tomorrow morning. 24h is
        # enough — overnight the user will have closed the laptop.
        meetings = await asyncio.to_thread(self._get_upcoming, 24)
        qualifying = [m for m in meetings if self._qualifies(m)]
        qualifying.sort(key=lambda m: m["start"])
        now = _dt.datetime.now()

        # Refresh next_event for the UI.
        upcoming = [m for m in qualifying if m["start"] > now]
        self._next_event = upcoming[0] if upcoming else None

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

    @staticmethod
    def _qualifies(m: dict) -> bool:
        if _is_all_day(m):
            return False
        if not _has_conference_link(m):
            return False
        return True

    @staticmethod
    def _dedup_key(m: dict) -> Tuple[str, str]:
        start = m.get("start")
        start_iso = start.isoformat() if isinstance(start, _dt.datetime) else str(start)
        return (str(m.get("subject", "")), start_iso)
