"""
Automatic knowledge-folder indexing: when to run, and when not to.

WHY IT EXISTS
-------------
Documents were indexed when a client's knowledge folder was SET and
never again. An install ran for months with 20 clients reporting 0
indexed documents while their folders held SOWs and proposals. v2.76
made that visible; this makes it stop happening.

The cost turned out to be low enough that "just do it" is the right
answer: the embedding model is local (no API spend), and index_folder
already skips unchanged files by mtime, so a repeat pass is stat calls.

WHAT THE SCHEDULING HAS TO GET RIGHT
------------------------------------
Three constraints, none of them about cost:

1. NEVER WHILE RECORDING OR PROCESSING. Extraction and embedding
   compete with transcription and diarization for CPU. This repo
   already carries a Settings toggle that exists solely because those
   two contending produced vanishing recordings — a background indexer
   that ignores that lesson would reintroduce it.

2. ONE CLIENT PER PASS, LEAST-RECENTLY-INDEXED FIRST. Twenty folders on
   a network drive is a long pass; doing them all in one tick makes the
   work unbounded and starves whichever client sorts last. Round-robin
   by last-indexed time gives every client the same attention and keeps
   each pass short enough to interrupt.

3. A `stat()` ON A DRIVE PATH IS A NETWORK ROUND-TRIP, not a local
   call. That is the real reason the interval has a floor rather than
   being "as often as possible" — a tight loop over eighteen folders is
   a steady stat storm against Google Drive, for no gain over polling
   every few minutes.

The decision logic is pure so all of that is testable without a clock,
an event loop, or a folder.
"""

from __future__ import annotations

from services import knowledge_index_schedule as sched


class TestShouldRun:
    def test_runs_when_due(self):
        assert sched.should_run(
            enabled=True, busy=False, last_run_epoch=0.0,
            interval_minutes=15, now_epoch=1_000.0) is True

    def test_does_not_run_before_the_interval_elapses(self):
        assert sched.should_run(
            enabled=True, busy=False, last_run_epoch=1_000.0,
            interval_minutes=15, now_epoch=1_000.0 + 60) is False

    def test_disabled_never_runs(self):
        assert sched.should_run(
            enabled=False, busy=False, last_run_epoch=0.0,
            interval_minutes=15, now_epoch=1e9) is False

    def test_busy_never_runs(self):
        """Recording or processing. Indexing competes with transcription
        and diarization for CPU, and this repo already has a setting
        that exists because those two contending lost people
        recordings."""
        assert sched.should_run(
            enabled=True, busy=True, last_run_epoch=0.0,
            interval_minutes=15, now_epoch=1e9) is False

    def test_busy_beats_being_overdue(self):
        """A pass that is hours late still waits. There is no deadline
        here worth a dropped recording."""
        assert sched.should_run(
            enabled=True, busy=True, last_run_epoch=0.0,
            interval_minutes=1, now_epoch=1e9) is False

    def test_first_run_is_due_immediately(self):
        assert sched.should_run(
            enabled=True, busy=False, last_run_epoch=None,
            interval_minutes=15, now_epoch=0.0) is True


class TestIntervalFloor:
    def test_honours_a_reasonable_interval(self):
        assert sched.clamp_interval(15) == 15
        assert sched.clamp_interval(60) == 60

    def test_floors_an_aggressive_one(self):
        """"As often as possible" is a stat storm against a network
        drive with no gain — a document added this morning is not more
        found by polling every ten seconds. The floor is the honest
        version of that setting."""
        assert sched.clamp_interval(0) == sched.MIN_INTERVAL_MINUTES
        assert sched.clamp_interval(1) == sched.MIN_INTERVAL_MINUTES
        assert sched.clamp_interval(-5) == sched.MIN_INTERVAL_MINUTES

    def test_caps_an_absurd_one(self):
        assert sched.clamp_interval(10_000) == sched.MAX_INTERVAL_MINUTES

    def test_junk_falls_back_to_the_default(self):
        for junk in (None, "", "abc", object()):
            assert sched.clamp_interval(junk) == sched.DEFAULT_INTERVAL_MINUTES


class TestClientRotation:
    def test_picks_the_least_recently_indexed(self):
        assert sched.next_client(
            ["Acme", "Globex", "Initech"],
            {"Acme": 500.0, "Globex": 100.0, "Initech": 900.0}) == "Globex"

    def test_a_never_indexed_client_wins_outright(self):
        """Never indexed is the state this whole feature exists to
        clear. It outranks anything with a timestamp, however old."""
        assert sched.next_client(
            ["Acme", "Globex"], {"Acme": 1.0}) == "Globex"

    def test_ties_are_stable_so_rotation_cannot_stall(self):
        """Two clients with identical timestamps must not ping-pong; a
        stable order guarantees forward progress through the list."""
        picked = sched.next_client(["Acme", "Globex"],
                                   {"Acme": 5.0, "Globex": 5.0})
        assert picked == "Acme"

    def test_no_clients_returns_none(self):
        assert sched.next_client([], {}) is None

    def test_ignores_timestamps_for_clients_that_no_longer_exist(self):
        """A deleted client's stale timestamp must not be picked and
        then fail to resolve."""
        assert sched.next_client(["Acme"], {"Deleted": 0.0}) == "Acme"
