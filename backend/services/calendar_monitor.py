"""
Background calendar poller. Notifies when a meeting is about to start.
"""

import datetime
import threading
import time
from typing import Callable, Optional, Set

from services.calendar_service import get_todays_meetings
from utils.logger import get_logger

logger = get_logger(__name__)


def _meeting_key(meeting: dict) -> str:
    return f"{meeting['subject']}|{meeting['start'].isoformat()}"


class CalendarMonitor:
    """
    Polls Outlook calendar every minute. Fires on_upcoming() when a
    meeting is about to start (within `notify_minutes_before`).
    """

    def __init__(
        self,
        on_upcoming: Callable[[dict], None],
        notify_minutes_before: int = 2,
        poll_interval: int = 60,
    ):
        self._on_upcoming = on_upcoming
        self._notify_seconds = max(0, notify_minutes_before) * 60
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._notified: Set[str] = set()
        self._dismissed: Set[str] = set()

    def start(self) -> None:
        if self._running or self._notify_seconds == 0:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Calendar monitor started (notify {self._notify_seconds // 60} min before)")

    def stop(self) -> None:
        self._running = False

    def dismiss(self, meeting: dict) -> None:
        """Mark a meeting as dismissed so we don't notify again."""
        self._dismissed.add(_meeting_key(meeting))

    def _loop(self) -> None:
        while self._running:
            try:
                self._check_once()
            except Exception as e:
                logger.warning(f"Calendar monitor tick failed: {e}")
            # Sleep in small increments so stop() is responsive
            for _ in range(self._poll_interval):
                if not self._running:
                    return
                time.sleep(1)

    def _check_once(self) -> None:
        if self._notify_seconds == 0:
            return
        meetings = get_todays_meetings()
        now = datetime.datetime.now()
        for m in meetings:
            key = _meeting_key(m)
            if key in self._notified or key in self._dismissed:
                continue
            seconds_until = (m["start"] - now).total_seconds()
            # Fire if within window AND not already started long ago
            if 0 <= seconds_until <= self._notify_seconds:
                logger.info(
                    f"Upcoming meeting: {m['subject']} "
                    f"(starts in {int(seconds_until)}s)")
                self._notified.add(key)
                try:
                    self._on_upcoming(m)
                except Exception as e:
                    logger.error(f"on_upcoming handler failed: {e}")
