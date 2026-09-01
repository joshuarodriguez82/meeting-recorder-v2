"""
Pipeline progress as structured stages, not a prose string.

THE BUG (field report 2026-09-01, with a screenshot)
----------------------------------------------------
The sidebar read:

    Transcription completeIdentifying s…

Two labels welded together, then truncated mid-word. Not a rendering
glitch — the backend produced exactly that string.

``recording_service`` emits a single token string when one stage ends
and the next begins:

    self._on_status("__stage:transcribe:done____stage:diarize:active__")

and ``Services._record_status`` translated it by running
``str.replace`` once per known token over the whole message. Two
substitutions into one string, no separator between them, so
"Transcription complete" and "Identifying speakers…" were concatenated.
The sidebar then truncated the result to one line and what the user saw
was a word that does not exist.

WHY THIS MODULE, RATHER THAN INSERTING A SEPARATOR
--------------------------------------------------
A separator would fix the screenshot and leave the real problem: the UI
gets prose and has to guess at meaning. It cannot say which stage is
running, how many remain, or whether the run finished — so it renders a
spinner and a sentence, and every question a user actually has ("is it
nearly done?", "did diarization run?") is unanswerable.

The pipeline is a fixed, ordered list of stages. Modelling it as one
makes the display honest and the string a rendering of the model rather
than the model itself.

THE ORDERING RULE
-----------------
A stage going active implies every earlier stage is done. The emitters
do not send a "done" for every stage — ``diarize:done`` is sent, but
if a run skips straight to a later stage the earlier ones must not be
left showing as pending forever. Progress that can go stale is progress
nobody trusts.
"""

from __future__ import annotations

from services import pipeline_progress as pp


def _fresh() -> pp.PipelineProgress:
    return pp.PipelineProgress()


# ── The reported bug ────────────────────────────────────────────────

def test_two_tokens_in_one_message_do_not_weld_together():
    """The exact string from the screenshot. This is the regression."""
    p = _fresh()
    p.apply("__stage:transcribe:done____stage:diarize:active__")
    assert p.label() == "Identifying speakers…"
    assert "completeIdentifying" not in p.label()


def test_the_label_names_the_stage_that_is_running_not_the_one_that_ended():
    """What a user wants from a progress line is what is happening NOW.
    'Transcription complete' as the headline while diarization runs is
    technically true and practically useless."""
    p = _fresh()
    p.apply("__stage:transcribe:done____stage:diarize:active__")
    assert p.label() == "Identifying speakers…"

    p.apply("__stage:diarize:done____stage:speakers:active__")
    assert p.label() == "Assigning speakers to segments…"


# ── Stage states ────────────────────────────────────────────────────

def test_stages_start_pending():
    p = _fresh()
    assert [s["state"] for s in p.stages()] == ["pending"] * len(pp.STAGES)


def test_an_active_stage_marks_the_earlier_ones_done():
    """Emitters do not send a 'done' for every stage. Without this an
    early stage sits on 'pending' for the whole run and the display
    silently under-reports how far along it is."""
    p = _fresh()
    p.apply("__stage:speakers:active__")
    states = {s["key"]: s["state"] for s in p.stages()}
    assert states["transcribe"] == "done"
    assert states["diarize"] == "done"
    assert states["speakers"] == "active"


def test_a_stage_never_goes_backwards():
    """A late duplicate token must not un-finish completed work — the
    counter would jump backwards and read as a restart that never
    happened."""
    p = _fresh()
    p.apply("__stage:speakers:active__")
    p.apply("__stage:transcribe:active__")
    states = {s["key"]: s["state"] for s in p.stages()}
    assert states["transcribe"] == "done"
    assert states["speakers"] == "active"


def test_completion_marks_every_stage_done():
    p = _fresh()
    p.complete()
    assert all(s["state"] == "done" for s in p.stages())
    assert p.percent() == 100
    assert p.active_key() is None


def test_failure_marks_the_running_stage_and_stops_there():
    """A failed run must not render as a finished one. The house rule
    again: something that did not happen must never look like it did."""
    p = _fresh()
    p.apply("__stage:diarize:active__")
    p.fail("Diarization ran out of memory")
    states = {s["key"]: s["state"] for s in p.stages()}
    assert states["transcribe"] == "done"
    assert states["diarize"] == "failed"
    assert states["speakers"] == "pending"
    assert p.percent() < 100
    assert p.error() == "Diarization ran out of memory"


def test_failing_with_no_active_stage_is_recorded_without_inventing_one():
    p = _fresh()
    p.fail("Backend died before transcription started")
    assert p.error() == "Backend died before transcription started"
    assert all(s["state"] != "failed" for s in p.stages())


# ── Percentage ──────────────────────────────────────────────────────

def test_percent_counts_finished_stages():
    p = _fresh()
    assert p.percent() == 0
    p.apply("__stage:transcribe:active__")
    assert p.percent() == 0
    p.apply("__stage:transcribe:done____stage:diarize:active__")
    assert 0 < p.percent() < 100


def test_percent_never_exceeds_one_hundred():
    p = _fresh()
    for _ in range(5):
        p.complete()
    assert p.percent() == 100


# ── Free text ───────────────────────────────────────────────────────

def test_a_plain_message_with_no_tokens_becomes_the_label():
    """Most status messages are ordinary sentences, not stage tokens.
    They still have to reach the user."""
    p = _fresh()
    p.apply("Loading AI models…")
    assert p.label() == "Loading AI models…"


def test_a_plain_message_does_not_disturb_stage_states():
    p = _fresh()
    p.apply("__stage:diarize:active__")
    p.apply("Terminology correction pass running")
    assert p.label() == "Terminology correction pass running"
    states = {s["key"]: s["state"] for s in p.stages()}
    assert states["diarize"] == "active"


def test_an_empty_message_changes_nothing():
    p = _fresh()
    p.apply("__stage:diarize:active__")
    before = p.label()
    p.apply("   ")
    assert p.label() == before


def test_mixed_prose_and_tokens_keeps_both():
    """Belt and braces: a caller that ever prefixes a token with text
    must not lose either half."""
    p = _fresh()
    p.apply("Retrying: __stage:transcribe:active__")
    assert "Retrying" in p.label()


# ── The payload the API returns ─────────────────────────────────────

def test_payload_shape_is_what_the_frontend_reads():
    """Asserted here so a rename cannot silently empty the UI. Every key
    below is consumed by the activity centre."""
    p = _fresh()
    p.apply("__stage:transcribe:done____stage:diarize:active__")
    payload = p.payload()

    assert set(payload) == {"stages", "label", "percent", "active", "error",
                            "done"}
    assert payload["active"] == "diarize"
    assert payload["label"] == "Identifying speakers…"
    assert payload["done"] is False
    assert payload["error"] is None
    for stage in payload["stages"]:
        assert set(stage) == {"key", "label", "state"}


def test_payload_reports_done_only_when_finished():
    p = _fresh()
    assert p.payload()["done"] is False
    p.complete()
    assert p.payload()["done"] is True


def test_reset_clears_a_previous_run():
    """A second recording must not inherit the first run's stages —
    that would show a fresh session as already finished."""
    p = _fresh()
    p.complete()
    p.reset()
    assert p.percent() == 0
    assert p.error() is None
    assert all(s["state"] == "pending" for s in p.stages())


def test_stage_labels_are_stable_and_human():
    """These strings are what the user reads. Pinned so a refactor of
    the token table cannot quietly turn them back into tokens."""
    labels = [s["label"] for s in _fresh().stages()]
    assert labels == ["Transcribing", "Identifying speakers",
                      "Assigning speakers"]
