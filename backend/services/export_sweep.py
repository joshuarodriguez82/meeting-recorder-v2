"""
Bounding rule for the periodic Designated Folder export sweep.

WHY THIS EXISTS
---------------
``export_reconcile`` compares what a session OWES its Designated Folder
against what is on disk, and enqueues the difference. Its docstring
promises:

    A missed trigger becomes a delay, never a permanent hole.

That promise was not kept, because nothing ran it on a schedule. It
fired at startup, on folder-set, on client rename, and from the Sync
now button — and nowhere else. Meanwhile the export worker gives up
after three retries (5s / 30s / 120s) and logs an error, so any session
whose copy failed for ~2.5 minutes was owed artifacts that nothing
would ever attempt again. Field report 2026-09-01: a meeting recorded
two hours earlier had still not reached its folder, and would not have
until the app restarted.

Making reconciliation periodic closes that. The reason it wasn't
periodic is cost: ``missing_artifacts`` stats files, and on a Google
Drive File Stream mount a stat is a network round-trip, not a local
call. Re-checking a 190-session library across 21 clients every couple
of minutes is precisely the stat storm this repo already refuses to
inflict for knowledge indexing (see knowledge_index_schedule,
constraint 3).

So the sweep is bounded, and this module is the bound. Pure — no clock,
no filesystem, everything passed in — so the selection rules are
testable without waiting for anything and the loop that acts on them
stays thin.

THE TWO RULES
-------------
1. **Recent work only.** A session that ended inside the window is a
   candidate. Older ones are left to the startup pass and the Sync now
   button, which still sweep everything.

2. **A hard cap, applied last.** ``list_sessions()`` returns
   newest-first and selection preserves that order, so truncating keeps
   today's meetings and drops the stale tail. A cap that can be
   exceeded is not a bound.

THE ONE JUDGEMENT CALL
----------------------
A session whose timestamps cannot be read is a CANDIDATE, not a skip.
Skipping it would hide it from the sweep permanently — a hole created
by an unreadable field, which is this repo's recurring defect shape:
something you could not read rendering as something that isn't there.
The cap is what keeps that safe direction affordable.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Sequence

#: How far back a routine sweep looks. Wide enough to cover a laptop
#: closed overnight with a meeting still unsynced, narrow enough that
#: the pass stays small on a library that has been running for months.
DEFAULT_WINDOW_HOURS = 24

#: Hard ceiling on sessions stat-ed per pass. At ~5 stats per session
#: this is a couple of hundred round-trips in the worst case, against a
#: mount that serves the same volume for one folder listing.
DEFAULT_MAX_SESSIONS = 40


def _as_epoch(value: Any) -> Optional[float]:
    """Epoch seconds from a session summary's ISO timestamp, or None
    when it is absent, blank, not a string, or unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def session_epoch(row: Dict[str, Any]) -> Optional[float]:
    """When this session last produced artifacts.

    ``ended_at`` first: it is the end of a meeting that determines when
    its transcript and summary were written, so a long call that began
    three days ago and finished an hour ago is recent work.
    ``started_at`` is the fallback for a session that never recorded an
    end. None means neither could be read.
    """
    return _as_epoch(row.get("ended_at")) or _as_epoch(row.get("started_at"))


def is_candidate(row: Dict[str, Any], *, now_epoch: float,
                 window_hours: int) -> bool:
    """Whether this session is recent enough to re-check.

    Undateable => yes (see the module docstring). Dated in the future
    => also yes: a clock ahead of ours, or a session synced from a
    machine whose clock is, must not drop out of the sweep. A permanent
    hole caused by a clock skew is the least defensible kind there is.
    """
    when = session_epoch(row)
    if when is None:
        return True
    return when >= now_epoch - window_hours * 3600.0


def sweep_candidates(
    summaries: Sequence[Dict[str, Any]],
    *,
    now_epoch: float,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> List[Dict[str, Any]]:
    """The sessions worth stat-ing this tick, newest-first.

    Rows are returned as they came in, not copied or trimmed: the caller
    needs the client tag, the display name and the ``has_*`` flags to
    work out what each session owes its folder. Handing back a reduced
    dict would make ``expected_artifacts`` report nothing owed and the
    sweep would quietly converge on doing nothing at all.
    """
    picked: List[Dict[str, Any]] = []
    for row in summaries:
        if not isinstance(row, dict):
            continue
        if is_candidate(row, now_epoch=now_epoch, window_hours=window_hours):
            picked.append(row)
            # Cap applied during selection, so a huge library costs a
            # bounded walk rather than a full pass that is thrown away.
            if len(picked) >= max_sessions:
                break
    return picked
