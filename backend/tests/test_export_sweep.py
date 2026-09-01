"""
Which sessions a periodic export sweep should re-check.

THE FIELD REPORT (2026-09-01)
-----------------------------
"I took a meeting a couple hours ago and it's still not synced."

Two hours is far outside anything the export path can explain on its
own. Its retry schedule is 5s / 30s / 120s and then the job is DROPPED
with a log line — so roughly two and a half minutes after the last
failure, the session is owed a copy that nothing will ever attempt
again.

What was supposed to catch that is `export_reconcile`, whose own
docstring promises:

    A missed trigger becomes a delay, never a permanent hole.

It does not keep that promise, because nothing calls it on a schedule.
It runs at startup, when a folder is set, on a client rename, and when
the user presses Sync now. Between those, a dropped export is a
permanent hole — exactly the state the module was written to make
impossible. The guarantee was designed and then only wired to events.

So reconciliation becomes periodic. The reason it wasn't is cost:
`missing_artifacts` stats files, and on a Google Drive File Stream
mount a stat is a network round-trip. Sweeping 190 sessions across 21
clients every couple of minutes would be the same stat storm this repo
already refuses to inflict for knowledge indexing.

This module is the bounding rule that makes a frequent sweep cheap.
It is pure — no clock, no filesystem — so every branch is testable
without waiting for anything.
"""

from __future__ import annotations

import datetime

from services import export_sweep


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat()


def _row(session_id: str, ended: datetime.datetime | None,
         *, started: datetime.datetime | None = None) -> dict:
    row: dict = {"session_id": session_id}
    if ended is not None:
        row["ended_at"] = _iso(ended)
    if started is not None:
        row["started_at"] = _iso(started)
    return row


NOW = datetime.datetime(2026, 9, 1, 12, 0, 0)
NOW_EPOCH = NOW.timestamp()


def _candidates(rows, *, window_hours=24, max_sessions=40):
    return export_sweep.sweep_candidates(
        rows, now_epoch=NOW_EPOCH, window_hours=window_hours,
        max_sessions=max_sessions)


def test_a_meeting_from_two_hours_ago_is_swept():
    """The reported case, stated as a test."""
    row = _row("RECENT", NOW - datetime.timedelta(hours=2))
    assert [r["session_id"] for r in _candidates([row])] == ["RECENT"]


def test_a_meeting_outside_the_window_is_left_to_startup_reconcile():
    """The bound. A month-old library must not be re-stated every tick
    just to catch this morning's meeting."""
    row = _row("OLD", NOW - datetime.timedelta(days=9))
    assert _candidates([row]) == []


def test_the_window_edge_is_inclusive():
    row = _row("EDGE", NOW - datetime.timedelta(hours=24))
    assert [r["session_id"] for r in _candidates([row])] == ["EDGE"]


def test_a_session_recorded_in_the_future_is_still_swept():
    """A machine whose clock is ahead, or a session synced from one,
    must not fall out of the sweep — that is a permanent hole caused by
    a clock, which is the least defensible kind."""
    row = _row("FUTURE", NOW + datetime.timedelta(hours=3))
    assert [r["session_id"] for r in _candidates([row])] == ["FUTURE"]


def test_an_undateable_session_is_swept_rather_than_skipped():
    """The house rule, applied to a timestamp: something you could not
    read must never render as something that isn't there. Skipping a
    session whose dates won't parse would hide it from the sweep
    forever, which is the exact failure this sweep exists to end."""
    rows = [
        {"session_id": "NO-DATES"},
        {"session_id": "JUNK", "ended_at": "not a timestamp"},
        {"session_id": "EMPTY", "ended_at": ""},
        {"session_id": "WRONG-TYPE", "ended_at": 12345},
    ]
    got = [r["session_id"] for r in _candidates(rows)]
    assert got == ["NO-DATES", "JUNK", "EMPTY", "WRONG-TYPE"]


def test_started_at_is_used_when_ended_at_is_missing():
    """A session that never recorded an end time still has a start; use
    it before falling back to 'undateable'."""
    old = _row("OLD-START", None,
               started=NOW - datetime.timedelta(days=30))
    assert _candidates([old]) == []

    fresh = _row("NEW-START", None,
                 started=NOW - datetime.timedelta(minutes=10))
    assert [r["session_id"] for r in _candidates([fresh])] == ["NEW-START"]


def test_ended_at_wins_over_started_at():
    """A long meeting started outside the window but ended inside it is
    recent work, and it is the END that determines when its artifacts
    were written."""
    row = _row("LONG", NOW - datetime.timedelta(hours=1),
               started=NOW - datetime.timedelta(days=3))
    assert [r["session_id"] for r in _candidates([row])] == ["LONG"]


def test_the_pass_is_capped_however_big_the_window_is():
    """The whole point of the module. A cap that can be exceeded is not
    a bound, and an unbounded sweep on a cloud mount is the stat storm
    this design refuses."""
    rows = [_row(f"S{i}", NOW - datetime.timedelta(minutes=i))
            for i in range(100)]
    got = _candidates(rows, max_sessions=40)
    assert len(got) == 40


def test_the_cap_keeps_the_newest_because_the_caller_orders_them():
    """list_sessions() returns newest-first and this preserves that
    order, so truncation drops the oldest — never today's meeting."""
    rows = [_row(f"S{i}", NOW - datetime.timedelta(minutes=i))
            for i in range(10)]
    got = [r["session_id"] for r in _candidates(rows, max_sessions=3)]
    assert got == ["S0", "S1", "S2"]


def test_undateable_sessions_do_not_crowd_out_dated_ones_past_the_cap():
    """A library full of undateable legacy rows must not consume the
    entire budget and starve the recent meetings behind them. Ordering
    is the caller's; the cap is applied after selection, so this is
    really a statement that selection preserves input order."""
    rows = ([_row("RECENT", NOW - datetime.timedelta(minutes=5))]
            + [{"session_id": f"LEGACY{i}"} for i in range(50)])
    got = [r["session_id"] for r in _candidates(rows, max_sessions=3)]
    assert got[0] == "RECENT"
    assert len(got) == 3


def test_an_empty_library_sweeps_nothing():
    assert _candidates([]) == []


def test_rows_are_returned_not_copied():
    """The caller needs the whole summary — client tag, display_name,
    the has_* flags — to decide what the session owes its folder. A
    trimmed dict would make expected_artifacts() report nothing owed
    and the sweep would silently converge on doing nothing."""
    row = _row("KEEP", NOW - datetime.timedelta(minutes=1))
    row["client"] = "Acme"
    row["has_transcript"] = True
    got = _candidates([row])
    assert got[0] is row
