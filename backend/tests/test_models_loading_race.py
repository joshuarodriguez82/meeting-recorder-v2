"""
Processing must not report "add your API keys" while the models load.

THE FIELD REPORT (2026-09-09)
-----------------------------
An install with API keys correctly configured was shown, in red:

    AI models not loaded. Add API keys in File > Settings and restart
    the app to enable transcription and diarization.

The keys were there. The activity log shows what actually happened, in
order::

    8m  Recording started — Session …
    8m  Loading AI models…
    8m  AI models not loaded. Add API keys …      ← the false alarm
    5m  Transcribing…
    5m  Identifying speakers…
    1m  Processing complete.

The models were LOADING, not missing. Three minutes later they finished
and the same recording processed perfectly.

TWO SEPARATE FAULTS
-------------------
**1. The wait is not a wait.** ``Services.ensure_models_loaded``
serialises loading with one loader — correctly, because two concurrent
torch/ctranslate2 inits in one process segfaulted (the 2026-07-21 crash
loop). But when a load is already in flight it returns IMMEDIATELY. Its
own docstring says so: *"every other caller returns immediately and
polls models_ready."*

``process_full`` does not poll. It calls ``ensure_models_loaded``, gets
an instant return while another thread is still loading, proceeds to
``process_session``, finds the engines still None, and fails. The
contract was documented and the only caller that mattered did not
honour it.

**2. The message blames the user.** ``RecordingService`` knows only
``can_process`` — whether the engine objects exist. It cannot tell
"still loading" from "no API keys" from "loading raised", so it asserts
the most alarming of the three and tells someone to re-enter
credentials they already entered and restart an app that was about to
work.

Diagnosing a state you cannot observe is how a temporary condition gets
reported as a permanent misconfiguration.
"""

from __future__ import annotations

import threading
import time

import pytest

from core.model_status import ModelStatus, describe_unavailable


# ── The message ─────────────────────────────────────────────────────

def test_loading_says_loading_not_add_your_keys():
    """The reported defect. A load in flight is a wait, not a
    misconfiguration, and must never tell someone to re-enter working
    credentials."""
    msg = describe_unavailable(ModelStatus(loading=True, configured=True))
    assert "api key" not in msg.lower()
    assert "restart" not in msg.lower()
    assert "loading" in msg.lower()


def test_missing_keys_still_says_to_add_keys():
    """The message was right for the case it was written for; that case
    must keep working."""
    msg = describe_unavailable(ModelStatus(loading=False, configured=False))
    assert "api key" in msg.lower()


def test_a_load_that_failed_reports_its_own_error():
    """"Add API keys" over a CUDA failure or a gated-model download
    sends someone to the wrong place entirely."""
    msg = describe_unavailable(ModelStatus(
        loading=False, configured=True,
        error="Could not download pyannote/speaker-diarization-3.1"))
    assert "pyannote" in msg
    assert "api key" not in msg.lower()


def test_configured_and_idle_and_not_loaded_is_still_reported():
    """No keys missing, no load running, no error — an unexpected state,
    but returning an empty string would render a failure as silence."""
    msg = describe_unavailable(ModelStatus(loading=False, configured=True))
    assert msg.strip()


def test_loading_wins_over_a_stale_error():
    """A previous failure followed by a retry that is now in flight is a
    wait, not a failure. Reporting the old error would be reporting the
    past as the present."""
    msg = describe_unavailable(ModelStatus(
        loading=True, configured=True, error="an earlier failure"))
    assert "loading" in msg.lower()
    assert "earlier failure" not in msg


def test_missing_keys_wins_over_loading():
    """A load that is running without keys is going to fail; saying
    "please wait" would be waiting for a certainty."""
    msg = describe_unavailable(ModelStatus(loading=True, configured=False))
    assert "api key" in msg.lower()


# ── The wait ────────────────────────────────────────────────────────

def test_wait_returns_true_once_a_load_completes():
    """What process_full needed and did not have: block until the
    in-flight load finishes, rather than returning instantly and then
    failing on engines that were seconds away."""
    from core.model_status import wait_for_models

    state = {"ready": False, "loading": True}
    def _finish():
        time.sleep(0.05)
        state["ready"], state["loading"] = True, False
    threading.Thread(target=_finish, daemon=True).start()

    assert wait_for_models(
        is_ready=lambda: state["ready"],
        is_loading=lambda: state["loading"],
        timeout_s=5.0, poll_s=0.01) is True


def test_wait_returns_immediately_when_already_ready():
    """The common case must cost nothing."""
    from core.model_status import wait_for_models
    started = time.monotonic()
    assert wait_for_models(is_ready=lambda: True, is_loading=lambda: False,
                           timeout_s=5.0, poll_s=0.01) is True
    assert time.monotonic() - started < 0.1


def test_wait_gives_up_rather_than_hanging_forever():
    """A load that never finishes must not hold a request open until the
    HTTP client times out with no explanation."""
    from core.model_status import wait_for_models
    assert wait_for_models(
        is_ready=lambda: False, is_loading=lambda: True,
        timeout_s=0.2, poll_s=0.01) is False


def test_wait_stops_when_the_load_fails_rather_than_waiting_out_the_clock():
    """Loading went false and ready never went true — the load failed.
    Sitting out the remaining timeout delays the real error for no
    reason."""
    from core.model_status import wait_for_models
    started = time.monotonic()
    assert wait_for_models(
        is_ready=lambda: False, is_loading=lambda: False,
        timeout_s=5.0, poll_s=0.01) is False
    assert time.monotonic() - started < 0.5


def test_wait_does_not_spin():
    """poll_s is honoured; a tight loop would burn a core while the
    models load."""
    from core.model_status import wait_for_models
    calls = []
    wait_for_models(
        is_ready=lambda: (calls.append(1), False)[1],
        is_loading=lambda: True,
        timeout_s=0.15, poll_s=0.05)
    assert len(calls) < 10, f"polled {len(calls)} times in 150ms — spinning"
