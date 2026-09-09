"""
Why the ML models are unavailable, and waiting for a load in flight.

THE FAILURE THIS EXISTS FOR (2026-09-09)
----------------------------------------
An install with API keys correctly configured was shown, in red:

    AI models not loaded. Add API keys in File > Settings and restart
    the app to enable transcription and diarization.

The keys were there. The activity log records what happened::

    8m  Recording started — Session …
    8m  Loading AI models…
    8m  AI models not loaded. Add API keys …      ← the false alarm
    5m  Transcribing…
    1m  Processing complete.

The models were LOADING. Three minutes later the same recording
processed perfectly.

Two faults produced that, and this module addresses both.

**The wait was not a wait.** ``Services.ensure_models_loaded``
serialises loading behind one loader — correctly, because two
concurrent torch/ctranslate2 inits in one process segfaulted (the
2026-07-21 crash loop). But when a load is already in flight it returns
IMMEDIATELY, and its own docstring says the caller is then expected to
poll ``models_ready``. ``process_full`` never polled: it called
``ensure_models_loaded``, got an instant return, and went straight on to
processing with engines that were still None. A documented contract
that the only caller who mattered did not honour.

**The message diagnosed a state it could not see.**
``RecordingService`` knows only whether the engine objects exist. It
cannot distinguish "still loading" from "no API keys" from "the load
raised", so it asserted the most alarming of the three — telling
someone to re-enter working credentials and restart an app that was
seconds from working.

Both halves are pure and live here so they are testable without loading
torch, which is the reason neither had coverage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

#: How long a caller will wait for an in-flight load before giving up.
#: A cold start imports torch, faster-whisper and pyannote and builds
#: two engines; on a slow machine that is tens of seconds. Long enough
#: to cover it, short enough that a wedged load surfaces as an error
#: rather than a request that never returns.
DEFAULT_WAIT_S = 120.0

#: Poll interval while waiting. Model loading is measured in seconds;
#: checking more often than this only burns CPU that the load wants.
DEFAULT_POLL_S = 0.25


@dataclass
class ModelStatus:
    """Everything needed to say WHY the models are not available."""

    loading: bool
    configured: bool
    error: Optional[str] = None


def describe_unavailable(status: ModelStatus) -> str:
    """A sentence naming the actual reason.

    Order matters, and each branch below is a case the old single
    message got wrong:

    1. **Not configured** wins even during a load, because a load
       running without keys is going to fail — asking someone to wait
       for a certainty wastes their time twice.
    2. **Loading** beats a recorded error: a previous failure followed
       by a retry now in flight is a wait, and reporting the old error
       would be reporting the past as the present.
    3. **An error** is reported verbatim. "Add API keys" over a CUDA
       out-of-memory or a gated-model download failure sends someone to
       entirely the wrong place.
    """
    if not status.configured:
        return ("AI models not loaded: no API keys are configured. Add them "
                "in File > Settings, then try again.")
    if status.loading:
        return ("The AI models are still loading — this takes up to a minute "
                "on first use. Nothing is wrong; try again once loading "
                "finishes.")
    if status.error:
        return f"The AI models could not be loaded: {status.error}"
    # Configured, not loading, no recorded error, yet unavailable.
    # Unexpected — but an empty string here would render a failure as
    # silence, which is the defect this whole module exists to end.
    return ("The AI models are not loaded, and no reason was recorded. "
            "Restarting the app will retry loading them.")


def wait_for_models(
    *,
    is_ready: Callable[[], bool],
    is_loading: Callable[[], bool],
    timeout_s: float = DEFAULT_WAIT_S,
    poll_s: float = DEFAULT_POLL_S,
) -> bool:
    """Block until the models are ready, or until waiting is pointless.

    Returns True when they became ready, False otherwise.

    Returns False EARLY — without sitting out the timeout — when the
    load stops running without becoming ready, because that means it
    failed and the caller should surface that error now rather than in
    two minutes.

    The predicates are injected rather than reading Services directly,
    so the timing logic is testable without torch, a lock, or a
    background thread doing real work.
    """
    if is_ready():
        return True
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_ready():
            return True
        if not is_loading():
            # Not ready and nobody is loading: the load finished without
            # succeeding, or never started. Either way waiting longer
            # cannot change the answer.
            return is_ready()
        time.sleep(poll_s)
    return is_ready()
