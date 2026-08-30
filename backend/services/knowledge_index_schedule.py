"""
When the automatic knowledge-folder indexer should run, and on which client.

Pure decision logic, deliberately separated from the loop that acts on
it. No clock, no event loop, no filesystem — everything is passed in, so
every branch below is testable without waiting for anything. The loop
in server.py is then thin enough to read in one screen.

WHY THE FEATURE EXISTS
----------------------
Documents were indexed when a client's knowledge folder was SET, and
never again. An install ran for months with 20 clients reporting 0
indexed documents while their folders held SOWs and proposals. v2.76
made the gap visible; this closes it.

The cost is low enough that automatic is the right default: the
embedding model is local (no API spend at all) and ``index_folder``
already skips unchanged files by mtime, so a repeat pass over a settled
folder is stat calls and nothing else.

THE THREE CONSTRAINTS
---------------------
None of them are about money.

1. Never while recording or processing. Extraction and embedding
   compete with transcription and diarization for CPU. This repo
   already carries a "run diarization on CPU" setting that exists
   solely because those two contending made recordings vanish; a
   background indexer that ignored that would reintroduce it.

2. One client per pass, least-recently-indexed first. Twenty folders on
   a network drive is a long sweep. Doing them all in one tick makes
   the work unbounded and starves whichever client sorts last;
   round-robin keeps each pass short enough to interrupt and gives
   every client the same attention.

3. There is a floor on the interval. A ``stat()`` against a Google
   Drive File Stream path is a network round-trip, not a local call, so
   "as often as possible" is a steady stat storm across every folder —
   for no gain, because a document added this morning is not found
   sooner by polling every ten seconds.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

#: Below this, the sweep is pure overhead against a network drive. See
#: constraint 3 — this is the honest version of "as often as possible".
MIN_INTERVAL_MINUTES = 5

#: Above this the setting has stopped meaning "automatic".
MAX_INTERVAL_MINUTES = 24 * 60

#: Frequent enough that a document added between meetings is searchable
#: by the next one, infrequent enough to be invisible.
DEFAULT_INTERVAL_MINUTES = 15


def clamp_interval(minutes) -> int:
    """A usable interval from whatever the setting holds.

    Junk falls back to the default rather than disabling the feature or
    raising: a malformed setting should not silently stop indexing, and
    it should not crash a background loop either.
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES
    return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, value))


def should_run(
    *,
    enabled: bool,
    busy: bool,
    last_run_epoch: Optional[float],
    interval_minutes: int,
    now_epoch: float,
) -> bool:
    """Whether a sweep should start right now.

    `busy` (recording or processing) wins over everything, including a
    pass that is hours overdue. There is no deadline here worth a
    dropped recording — see constraint 1.
    """
    if not enabled or busy:
        return False
    if last_run_epoch is None:
        return True
    return (now_epoch - last_run_epoch) >= clamp_interval(interval_minutes) * 60


def next_client(
    clients: Sequence[str],
    last_indexed: Dict[str, float],
) -> Optional[str]:
    """The client to sweep this pass: least recently indexed first.

    A client that has never been indexed outranks every client that
    has, however stale — that is the state this feature exists to
    clear. Ties break on the caller's order, so rotation always makes
    forward progress instead of ping-ponging between two clients that
    share a timestamp.
    """
    if not clients:
        return None
    # Timestamps for clients that no longer exist are ignored by
    # construction: we only ever look up names from `clients`.
    return min(
        enumerate(clients),
        key=lambda pair: (last_indexed.get(pair[1]) is not None,
                          last_indexed.get(pair[1], 0.0),
                          pair[0]),
    )[1]
