"""
"Ready to record" is a claim, so it has to be verified.

THE DEFECT (2026-09-09, from a field report and a support thread)
-----------------------------------------------------------------
The Record tab showed a green **Ready to record** while the selected
microphone could not be opened at all. Pressing Start then failed
outright with "All mic configurations failed".

The indicator was::

    const micReady = micIdx !== null && !!selectedMic;

which is true when a device is SELECTED IN A DROPDOWN. Nothing had
attempted to open it. So the app asserted readiness it had never
tested, someone believed it, and found out at the moment it cost them a
meeting.

That is the same shape as the v2.79.0 regression where a failed
processing run painted itself green — a status derived from what we
intended rather than from what happened.

WHAT A PROBE IS AND IS NOT
--------------------------
Opening a device and closing it again is the only way to know it opens.
It is not free, so:

* It runs on demand and on device change, never on a poll.
* It NEVER runs while recording. Touching PortAudio underneath a live
  capture is the 2026-08-20 boot-loop crash, and a probe is not worth
  any part of that risk.
* It reuses `build_mic_open_plan`, so what the probe proves is what
  Start Recording will actually attempt — a probe that tested a
  different ladder would be worse than none, because it would be
  confidently wrong.

The decision logic lives here, apart from the PortAudio call, so all of
that is testable without a sound card.
"""

from __future__ import annotations

from core.mic_probe import ProbeOutcome, summarize_probe


def _ok(**kw):
    base = dict(device=17, channels=2, samplerate=48000,
                blocksize=0, latency="high")
    base.update(kw)
    return base


# ── A device that opens ─────────────────────────────────────────────

def test_a_device_that_opens_is_reported_ready():
    out = summarize_probe(opened=_ok(), attempts_tried=1, total_attempts=45,
                          selected_device=17, error=None)
    assert out.ok is True
    assert "ready" in out.message.lower()


def test_opening_on_the_first_attempt_says_nothing_alarming():
    """The overwhelmingly common case. It must not editorialise."""
    out = summarize_probe(opened=_ok(), attempts_tried=1, total_attempts=45,
                          selected_device=17, error=None)
    assert out.detail is None


def test_a_device_that_only_opens_in_mono_says_so():
    """Worth surfacing: the recording will work, but channel-based
    speaker attribution has one channel to work with instead of two.
    Silence here would make a degraded capture look identical to a
    full one."""
    out = summarize_probe(opened=_ok(channels=1), attempts_tried=4,
                          total_attempts=45, selected_device=17, error=None)
    assert out.ok is True
    assert out.detail and "mono" in out.detail.lower()


def test_falling_back_to_another_device_entry_is_surfaced():
    """It still records — but not through the entry the user picked, and
    a silent substitution is how someone ends up recording the wrong
    microphone without ever being told."""
    out = summarize_probe(opened=_ok(device=1), attempts_tried=10,
                          total_attempts=45, selected_device=17, error=None)
    assert out.ok is True
    assert out.detail and "another" in out.detail.lower()


def test_a_stereo_open_on_the_selected_device_needs_no_caveat():
    out = summarize_probe(opened=_ok(device=17, channels=2),
                          attempts_tried=2, total_attempts=45,
                          selected_device=17, error=None)
    assert out.detail is None


# ── A device that does not open ─────────────────────────────────────

def test_a_device_that_never_opens_is_not_ready():
    out = summarize_probe(opened=None, attempts_tried=45, total_attempts=45,
                          selected_device=17,
                          error="Invalid number of channels")
    assert out.ok is False


def test_the_failure_reason_reaches_the_message():
    """The whole point. A red light that does not say why sends someone
    to reinstall a driver they did not need to touch."""
    out = summarize_probe(opened=None, attempts_tried=45, total_attempts=45,
                          selected_device=17,
                          error="Invalid input channel count")
    assert "Invalid input channel count" in (out.message + (out.detail or ""))


def test_no_attempts_at_all_says_the_device_is_unusable():
    """An empty plan — the device reports no input channels. Saying
    "all attempts failed" when none were made sends the reader looking
    for a failure that never happened."""
    out = summarize_probe(opened=None, attempts_tried=0, total_attempts=0,
                          selected_device=17, error=None)
    assert out.ok is False
    assert "no input" in out.message.lower() or "unusable" in out.message.lower()


def test_a_failure_with_no_error_text_still_reports_something():
    """Never render a red state with an empty explanation."""
    out = summarize_probe(opened=None, attempts_tried=45, total_attempts=45,
                          selected_device=17, error=None)
    assert out.ok is False
    assert out.message.strip()


# ── The shape the API returns ───────────────────────────────────────

def test_the_outcome_serialises_to_the_keys_the_ui_reads():
    """Pinned so a rename empties the card rather than breaking loudly."""
    out = summarize_probe(opened=_ok(), attempts_tried=1, total_attempts=45,
                          selected_device=17, error=None)
    assert set(out.as_dict()) == {
        "ok", "message", "detail", "device", "channels", "samplerate"}


def test_a_failed_probe_reports_no_channels_or_rate():
    """Nothing opened, so there is no configuration to report. Emitting
    the requested one would read as though it had worked."""
    out = summarize_probe(opened=None, attempts_tried=45, total_attempts=45,
                          selected_device=17, error="nope")
    d = out.as_dict()
    assert d["channels"] is None
    assert d["samplerate"] is None


def test_a_successful_probe_reports_what_actually_opened():
    out = summarize_probe(opened=_ok(device=1, channels=1, samplerate=44100),
                          attempts_tried=10, total_attempts=45,
                          selected_device=17, error=None)
    d = out.as_dict()
    assert (d["device"], d["channels"], d["samplerate"]) == (1, 1, 44100)


def test_outcome_is_a_plain_dataclass_not_a_dict_subclass():
    """Callers type-check against it; a dict would let a typo pass."""
    out = summarize_probe(opened=_ok(), attempts_tried=1, total_attempts=45,
                          selected_device=17, error=None)
    assert isinstance(out, ProbeOutcome)
