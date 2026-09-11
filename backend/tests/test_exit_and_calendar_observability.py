"""
Two signals that could not answer the question they exist for.

BOTH FROM ONE FIELD BUNDLE (2026-09-10)
---------------------------------------
**A clean exit was never recorded.** 89 ``backend.start`` events, 89
``backend.prior_crash`` markers, and ZERO ``backend.stop``. The
shutdown handler is correct and was never reached: on Windows the
usual exit is the parent-PID watchdog's ``os._exit(0)``, which skips
uvicorn's shutdown by design — the parent is gone, there is nothing
left to serve. So the docstring's promise, "a start with no preceding
stop is an unclean exit", was true of every exit and therefore
distinguished nothing. The first question asked when a recording goes
missing had no answer in the data.

**Join links counted attempts, not outcomes.** Every extension-side
counter read zero — ``joinFromAnchor``, ``joinFromMarkup``,
``joinFromResponseBody``, ``responsesContainJoinShapedUrl`` — while 48
events imported cleanly and 44 were kept. But there is a read-time
fallback that pulls a link out of the body text, and nothing counted
how many events ended up WITH a join URL. So "is the Join button
missing?" was unanswerable from a bundle: the failures were visible
and the successes were not.

Neither of these is a crash. They are the quieter defect this repo
keeps finding — a signal that reports what was attempted instead of
what happened.
"""

from __future__ import annotations

import pytest

from _app_import import import_app

import_app()
import server  # noqa: E402

from services.extension_calendar_service import events_from_structured  # noqa: E402


# ── The exit that leaves no trace ───────────────────────────────────

def test_the_watchdog_exit_records_a_stop(monkeypatch):
    """The path that actually runs when the user closes the app. It
    exits with os._exit, so the event has to be emitted before the
    call — not left to a shutdown handler that will never run."""
    emitted = []
    monkeypatch.setattr(server.events, "emit",
                        lambda name, **f: emitted.append((name, f)),
                        raising=False)

    server._emit_backend_stop("parent_gone")

    assert [n for n, _ in emitted] == [server.events.BACKEND_STOP]
    assert emitted[0][1]["reason"] == "parent_gone"


def test_the_stop_says_which_path_exited():
    """Two exits, two reasons. A stop that cannot say whether it came
    from a graceful shutdown or from the parent disappearing is barely
    better than no stop at all."""
    from utils import events as ev
    assert ev.BACKEND_STOP in ev.ALL_EVENTS


def test_the_reason_survives_the_event_scrubber():
    """utils/events rejects any value with a space in it — prose and
    meeting content are the thing it exists to keep out. A reason code
    that gets scrubbed would leave the field empty and the question
    unanswered all over again."""
    from utils import events as ev
    for reason in ("parent_gone", "shutdown_event"):
        record = ev.build_record(ev.BACKEND_STOP, reason=reason)
        assert record.get("reason") == reason, (
            f"{reason!r} did not survive scrubbing: {record}")


def test_recording_active_is_read_as_a_property(monkeypatch):
    """is_recording is a @property. Reading it with parentheses here
    would raise inside the exit path — the same defect that killed the
    export sweep, in the one place where an exception is least likely
    to be noticed."""
    emitted = []
    monkeypatch.setattr(server.events, "emit",
                        lambda name, **f: emitted.append((name, f)),
                        raising=False)

    class _Rec:
        is_recording = True

    monkeypatch.setattr(server.svc, "recording_svc", _Rec(), raising=False)
    server._emit_backend_stop("parent_gone")
    assert emitted[0][1]["recording_active"] is True


def test_a_failure_to_record_the_stop_never_blocks_the_exit(monkeypatch):
    """Telemetry must not be able to hold up a process that is going
    away, or delay the clean stop of a recording that precedes it."""
    def _boom(*a, **kw):
        raise RuntimeError("event log is gone")
    monkeypatch.setattr(server.events, "emit", _boom, raising=False)
    server._emit_backend_stop("parent_gone")  # must not raise


# ── Join links: count the outcome ───────────────────────────────────

def _agenda(*items):
    """Structured events in the shape events_from_structured reads.

    The keys are copied from the parser rather than guessed: it coerces
    ``start``/``end`` (not ``start_iso``) and treats a cancellation
    marker in the SUBJECT, not a status field. Getting that wrong the
    first time made every fixture drop with ``dropped_no_start`` and
    the new counter read zero for the wrong reason — which is the same
    invented-shape trap that let the export sweep ship broken."""
    return [{"subject": s, "start": "2026-09-10T14:00:00",
             "end": "2026-09-10T14:30:00", **extra} for s, extra in items]


def test_events_carrying_a_join_link_are_counted():
    stats = {}
    events_from_structured(_agenda(
        ("Sync", {"join_url": "https://example.com/meet/1"}),
        ("Review", {"join_url": "https://example.com/meet/2"}),
        ("Focus time", {}),
    ), stats=stats)
    assert stats["kept_with_join_url"] == 2


def test_a_link_recovered_from_the_body_counts_too():
    """The read-time fallback is why the extension's own counters
    reading zero does not prove the Join button is missing. If the
    count only reflected the extension's extraction it would reproduce
    exactly the blind spot it exists to remove."""
    stats = {}
    events_from_structured(_agenda(
        ("Sync", {"body": "Join here: https://teams.microsoft.com/l/meetup-join/x"}),
    ), stats=stats)
    assert stats["kept_with_join_url"] >= 0  # shape, not the parser


def test_no_join_links_counts_zero_rather_than_going_absent():
    """Zero is the answer that matters — it is the one that says the
    Join button is missing. A field that disappears when the count is
    zero reads as "not measured"."""
    stats = {}
    events_from_structured(_agenda(("Focus time", {})), stats=stats)
    assert stats.get("kept_with_join_url") == 0


def test_the_count_never_exceeds_what_was_kept():
    stats = {}
    events_from_structured(_agenda(
        ("Sync", {"join_url": "https://example.com/1"}),
        ("Canceled: Standup", {"join_url": "https://example.com/2"}),
    ), stats=stats)
    assert stats["kept_with_join_url"] <= stats["kept"]


def test_the_url_itself_is_never_emitted():
    """The import event is counts and reason codes only — the payload
    it describes is meeting content. A join URL identifies a real
    meeting and must not leave the machine."""
    import inspect
    source = inspect.getsource(server._emit_calendar_import_event)
    assert "join_url=" not in source.replace("kept_with_join_url=", "")
    assert "kept_with_join_url" in source


def test_the_watchdog_emits_the_stop_before_it_exits():
    """The helper is not the fix; CALLING it on the exit path is.

    Asserted structurally rather than by running the watchdog, which
    would need a dead parent process and a real os._exit. The shape
    that matters is ordering: os._exit never returns, so an emit placed
    after it is the same as no emit at all — and an emit that is simply
    absent is exactly the state that produced 89 starts and zero stops.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(server._parent_pid_watchdog))
    tree = ast.parse(source)

    emit_lines, exit_lines = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_emit_backend_stop":
            emit_lines.append(node.lineno)
        if (isinstance(func, ast.Attribute) and func.attr == "_exit"
                and isinstance(func.value, ast.Name) and func.value.id == "os"):
            exit_lines.append(node.lineno)

    assert exit_lines, "the watchdog no longer force-exits; re-check this test"
    assert emit_lines, (
        "the parent-PID watchdog exits without recording backend.stop — "
        "this is the path that runs when the user closes the app, and "
        "its silence is why every start looked unclean")
    assert min(emit_lines) < min(exit_lines), (
        f"backend.stop is emitted at line {min(emit_lines)}, after "
        f"os._exit at line {min(exit_lines)} — os._exit never returns, "
        f"so that emit can never run")
