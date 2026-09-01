"""
The processing pipeline's progress, as structured stages.

WHY THIS EXISTS
---------------
The sidebar showed a user this (screenshot, 2026-09-01):

    Transcription completeIdentifying s…

Two labels welded together and then truncated mid-word. The backend
produced that string. ``recording_service`` emits one token string when
a stage ends and the next begins::

    self._on_status("__stage:transcribe:done____stage:diarize:active__")

and the old translator ran ``str.replace`` once per known token over the
whole message — two substitutions into one string with nothing between
them.

Adding a separator would have fixed the screenshot and left the actual
problem in place: the UI was handed prose and had to infer meaning from
it. It could not say which stage was running, how many were left, or
whether the run had finished, so it rendered a spinner and a sentence
and every question a user actually has went unanswered.

The pipeline is a fixed, ordered list of stages. This models it as one.
The human-readable string becomes a RENDERING of that model rather than
being the model itself, which is what let two of them collide in the
first place.

DESIGN NOTES
------------
* **Pure.** No clock, no I/O, no logging. The caller owns those. Every
  transition below is testable without running a transcription.

* **Monotonic.** A stage never goes backwards. A duplicate or
  out-of-order token must not un-finish completed work, because a
  progress bar that jumps back reads as a restart that never happened.

* **Implied completion.** A stage going active marks every earlier
  stage done. Emitters do not send a "done" for every stage, and a
  stage left on "pending" for an entire run under-reports progress —
  which is the same class of lie as the welded string, just quieter.

* **Failure is visible.** A failed run stops where it failed and says
  so. It must never render as a completed one.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: The pipeline, in order. `key` is what the API and the frontend match
#: on; `label` is what a person reads. Labels are deliberately bare —
#: the UI adds its own "…" and tense — so the same string works in a
#: progress line, a stage list and a completed-activity entry.
STAGES: List[Dict[str, str]] = [
    {"key": "transcribe", "label": "Transcribing"},
    {"key": "diarize", "label": "Identifying speakers"},
    {"key": "speakers", "label": "Assigning speakers"},
]

_STAGE_ORDER = [s["key"] for s in STAGES]
_STAGE_LABELS = {s["key"]: s["label"] for s in STAGES}

#: Present-tense sentence for the stage that is currently running. Kept
#: separate from the bare label so the stage list can stay terse while
#: the headline reads as a sentence.
_ACTIVE_SENTENCES = {
    "transcribe": "Transcribing…",
    "diarize": "Identifying speakers…",
    "speakers": "Assigning speakers to segments…",
}

_TOKEN_RE = re.compile(r"__stage:([a-z_]+):(active|done)__")

PENDING = "pending"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"

#: Rank used to keep transitions monotonic. A state may only move to a
#: higher rank; FAILED is terminal for the stage it lands on.
_RANK = {PENDING: 0, ACTIVE: 1, DONE: 2, FAILED: 3}


class PipelineProgress:
    """Mutable stage states for one processing run.

    Not thread-safe by itself — the caller (Services) already holds a
    lock around status updates, and putting a second one here would
    invite the two to disagree.
    """

    def __init__(self) -> None:
        self.reset()

    # ── mutation ────────────────────────────────────────────────────

    def reset(self) -> None:
        """Start a fresh run. A second recording must not inherit the
        first one's stages, which would show a new session as already
        finished before it had done anything."""
        self._states: Dict[str, str] = {k: PENDING for k in _STAGE_ORDER}
        self._label: str = ""
        self._error: Optional[str] = None
        self._complete: bool = False

    def apply(self, message: str) -> None:
        """Fold one status message into the model.

        Handles a message carrying several tokens (the case that
        produced the welded string), a message of ordinary prose, or a
        mix of both. An empty message is ignored rather than blanking a
        label that is still true.
        """
        raw = (message or "").strip()
        if not raw:
            return

        matches = list(_TOKEN_RE.finditer(raw))
        for match in matches:
            key, state = match.group(1), match.group(2)
            if key in self._states:
                self._advance(key, ACTIVE if state == "active" else DONE)

        leftover = _TOKEN_RE.sub(" ", raw).strip()
        if leftover:
            # Prose the caller sent alongside (or instead of) tokens.
            # It is the most specific thing anyone said, so it wins the
            # headline.
            self._label = leftover
        elif matches:
            self._label = self._derived_label()

    def complete(self) -> None:
        """The run finished successfully: every stage is done."""
        for key in _STAGE_ORDER:
            if self._states[key] != FAILED:
                self._states[key] = DONE
        self._complete = True
        self._error = None

    def fail(self, error: str) -> None:
        """The run stopped. The stage that was running is marked failed
        and the rest stay pending, so the display cannot pass a broken
        run off as a finished one."""
        self._error = error or "Processing failed"
        self._complete = False
        active = self.active_key()
        if active is not None:
            self._states[active] = FAILED

    # ── reads ───────────────────────────────────────────────────────

    def stages(self) -> List[Dict[str, str]]:
        return [
            {"key": key, "label": _STAGE_LABELS[key], "state": self._states[key]}
            for key in _STAGE_ORDER
        ]

    def active_key(self) -> Optional[str]:
        for key in _STAGE_ORDER:
            if self._states[key] == ACTIVE:
                return key
        return None

    def label(self) -> str:
        """The one line a cramped surface can show."""
        return self._label or self._derived_label()

    def percent(self) -> int:
        """Whole percent of stages finished, 0-100.

        Counts DONE only. A stage that is merely running has not
        produced anything yet, and crediting it would make the bar
        overstate what exists — the same overclaim as the welded label,
        expressed as a number.
        """
        if not _STAGE_ORDER:
            return 100
        done = sum(1 for key in _STAGE_ORDER if self._states[key] == DONE)
        return max(0, min(100, round(done * 100 / len(_STAGE_ORDER))))

    def error(self) -> Optional[str]:
        return self._error

    def is_done(self) -> bool:
        return self._complete and self._error is None

    def payload(self) -> Dict[str, Any]:
        """What ``GET /recording/status`` returns under ``pipeline``.

        The key set is pinned by a test: the activity centre reads every
        one of these, so a rename here empties the UI rather than
        breaking it loudly.
        """
        return {
            "stages": self.stages(),
            "label": self.label(),
            "percent": self.percent(),
            "active": self.active_key(),
            "error": self._error,
            "done": self.is_done(),
        }

    # ── internals ───────────────────────────────────────────────────

    def _advance(self, key: str, state: str) -> None:
        """Move a stage forward, never back.

        Going ACTIVE also finishes every earlier stage: the emitters
        skip some "done" tokens, and a stage stuck on pending for a
        whole run misreports how far along the work is.
        """
        current = self._states.get(key, PENDING)
        if _RANK[state] <= _RANK[current]:
            return
        self._states[key] = state
        if state in (ACTIVE, DONE):
            for earlier in _STAGE_ORDER:
                if earlier == key:
                    break
                if self._states[earlier] in (PENDING, ACTIVE):
                    self._states[earlier] = DONE

    def _derived_label(self) -> str:
        """A sentence for the current position when nobody supplied one.

        Names the stage that is RUNNING, never the one that just ended:
        "Transcription complete" as a headline while diarization runs is
        true and useless, and it is what the user was shown.
        """
        active = self.active_key()
        if active is not None:
            return _ACTIVE_SENTENCES.get(active, _STAGE_LABELS[active])
        if self._error:
            return self._error
        if self._complete:
            return "Processing complete"
        return ""
