"""
The Co-Pilot runs in the backend, not in a browser tab.

WHY
---
The tick loop used to live in the Record tab's panel. Leave that tab —
to read a past meeting, check Today, open Settings — and coaching simply
stopped: no ticks, nothing saved to the session, and the post-meeting
summary missing every observation from that stretch. It also meant a
changed interval took effect only on the next recording, and Pause
existed only in one tab's memory.

The runner owns the cadence. While a recording is in progress and the
Co-Pilot is enabled it ticks on the configured interval (re-read every
pass, so a Settings change applies at once), keeps the board
(core/copilot_board) on the session, and records the last error. The
panel only reads that state and sends the user's actions.

Dependencies come in through ``deps()`` so this is testable with no
recording, no transcriber and no model.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.copilot_board import CopilotBoard
from core.copilot_context import meeting_context
from utils.logger import get_logger

logger = get_logger(__name__)

WIDE_WINDOW_S = 270.0
HOT_WINDOW_S = 90.0
MIN_WIDE_INTERVAL_S = 15
MIN_HOT_INTERVAL_S = 5


@dataclass
class TickDeps:
    """Everything one tick needs, resolved fresh each pass."""
    session: Any
    transcriber: Any
    coach: Any
    wide_interval_s: int
    hot_interval_s: int
    mode_name: str = "SA"
    mode_prompt: str = ""
    type_name: str = "General"
    type_prompt: str = ""
    custom_context: str = ""


class CopilotRunner:
    def __init__(self, deps: Callable[[], Optional[TickDeps]],
                 clock: Callable[[], float] = time.monotonic):
        self._deps = deps
        self._clock = clock
        self._lock = asyncio.Lock()
        self._session_id: Optional[str] = None
        self._board = CopilotBoard()
        self._last_wide: Optional[float] = None
        self._last_hot: Optional[float] = None
        self.paused = False
        self.last_error: Optional[str] = None
        self.last_error_detail: Optional[str] = None
        self.last_tick_at: Optional[str] = None
        self.last_segment_count = 0

    # ── per-recording state ──────────────────────────────────────────

    def _bind(self, session) -> None:
        """Start fresh for a new recording; resume for the same one."""
        sid = getattr(session, "session_id", None)
        if sid == self._session_id:
            return
        self._session_id = sid
        self._board = CopilotBoard(getattr(session, "copilot_board", None))
        now = self._clock()
        # First coaching after one interval: before that there is
        # nothing said to coach on.
        self._last_wide = now
        self._last_hot = now
        self.paused = False
        self.last_error = self.last_error_detail = None
        self.last_tick_at = None
        self.last_segment_count = 0

    def _persist(self, session) -> None:
        if session is not None:
            session.copilot_board = self._board.to_list()

    @property
    def board(self) -> CopilotBoard:
        return self._board

    # ── ticking ──────────────────────────────────────────────────────

    async def tick(self, hot: bool = False) -> Optional[Dict[str, Any]]:
        """Run one tick now. None when there is nothing to coach (not
        recording, Co-Pilot off, no model). Never raises."""
        deps = self._deps()
        if deps is None:
            return None
        self._bind(deps.session)
        async with self._lock:
            window = HOT_WINDOW_S if hot else WIDE_WINDOW_S
            segments = deps.transcriber.recent_segments(last_seconds=window)
            interval = (max(MIN_HOT_INTERVAL_S, deps.hot_interval_s) if hot
                        else max(MIN_WIDE_INTERVAL_S, deps.wide_interval_s))
            provider = getattr(deps.coach, "_provider", "anthropic")
            base = (10.0 if hot else 20.0) if provider == "anthropic" \
                else (15.0 if hot else 35.0)
            timeout = max(6.0 if hot else 8.0,
                          min(base, float(interval) - (3.0 if hot else 5.0)))
            try:
                result = await deps.coach.coach_tick(
                    segments=segments,
                    meeting_name=getattr(deps.session, "display_name", "") or "",
                    meeting_context=meeting_context(deps.session),
                    custom_context=deps.custom_context,
                    prior_ticks=list(getattr(deps.session, "copilot_ticks",
                                             None) or []),
                    board_memory=self._board.prompt_memory(),
                    mode_name=deps.mode_name, mode_prompt=deps.mode_prompt,
                    meeting_type_name=deps.type_name,
                    meeting_type_prompt=deps.type_prompt,
                    hot=hot, timeout_s=timeout)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Co-Pilot tick failed: {type(e).__name__}: {e}")
                result = {"error": "error",
                          "error_detail": f"{type(e).__name__}: {e}"[:200]}

            now_iso = datetime.now().isoformat(timespec="seconds")
            payload = {
                "clarifying_questions": result.get("clarifying_questions") or [],
                "risks": result.get("risks") or [],
                "follow_ups": result.get("follow_ups") or [],
                "error": result.get("error"),
                "error_detail": result.get("error_detail"),
                "segment_count": len(segments),
                "generated_at": datetime.now().isoformat(),
            }
            if hot:
                payload["hot"] = True
            self.last_tick_at = now_iso
            self.last_segment_count = len(segments)
            self.last_error = payload["error"]
            self.last_error_detail = payload["error_detail"]
            if hot:
                self._last_hot = self._clock()
            else:
                self._last_wide = self._clock()

            if (payload["clarifying_questions"] or payload["risks"]
                    or payload["follow_ups"]):
                # Kept as before: the post-meeting summary reads ticks.
                deps.session.copilot_ticks.append(payload)
                # Meaning-level matching loads a sentence model the first
                # time: off the event loop. merge() itself is instant.
                await asyncio.to_thread(self._board.prepare, payload)
                self._board.merge(payload, now=now_iso)
                self._persist(deps.session)
            payload["board"] = self._board.to_list()
            return payload

    def due(self, deps: TickDeps) -> Optional[bool]:
        """Which tick is due now: False = wide, True = hot, None = none."""
        if self.paused:
            return None
        now = self._clock()
        wide = max(MIN_WIDE_INTERVAL_S, deps.wide_interval_s)
        if self._last_wide is None or now - self._last_wide >= wide:
            return False
        if deps.hot_interval_s and deps.hot_interval_s > 0:
            hot = max(MIN_HOT_INTERVAL_S, deps.hot_interval_s)
            if self._last_hot is None or now - self._last_hot >= hot:
                return True
        return None

    def seconds_to_next(self, deps: Optional[TickDeps]) -> Optional[int]:
        if deps is None or self.paused or self._last_wide is None:
            return None
        wide = max(MIN_WIDE_INTERVAL_S, deps.wide_interval_s)
        return max(0, int(round(wide - (self._clock() - self._last_wide))))

    async def step(self) -> Optional[Dict[str, Any]]:
        """One pass of the loop: tick if something is due."""
        deps = self._deps()
        if deps is None:
            return None
        self._bind(deps.session)
        which = self.due(deps)
        if which is None or self._lock.locked():
            return None
        return await self.tick(hot=which)

    async def run_forever(self, poll_s: float = 2.0) -> None:
        while True:
            try:
                await self.step()
            except Exception as e:  # noqa: BLE001
                # The loop must outlive any one bad tick.
                logger.warning(f"Co-Pilot loop error: {e}")
            await asyncio.sleep(poll_s)

    # ── user actions ─────────────────────────────────────────────────

    def set_item_status(self, item_id: str, status: str):
        deps = self._deps()
        item = self._board.set_status(item_id, status)
        if deps is not None:
            self._persist(deps.session)
        return item

    def state(self) -> Dict[str, Any]:
        deps = self._deps()
        if deps is not None:
            self._bind(deps.session)
        return {
            "active": deps is not None,
            "paused": self.paused,
            "board": self._board.to_list() if deps is not None else [],
            "last_tick_at": self.last_tick_at,
            "next_tick_in_s": self.seconds_to_next(deps),
            "wide_interval_s": (max(MIN_WIDE_INTERVAL_S, deps.wide_interval_s)
                                if deps else None),
            "segment_count": self.last_segment_count,
            "error": self.last_error,
            "error_detail": self.last_error_detail,
            "qa": list(getattr(deps.session, "copilot_qa", None) or [])
            if deps is not None else [],
        }

