"""
Turning a microphone open attempt into something a person can act on.

WHY THIS EXISTS
---------------
The Record tab showed a green **Ready to record** while the selected
microphone could not be opened at all::

    const micReady = micIdx !== null && !!selectedMic;

That is true when a device is SELECTED IN A DROPDOWN. Nothing had
attempted to open it. So the app asserted a readiness it had never
tested, and the first time anyone learned otherwise was when Start
Recording failed and the meeting had already begun (field report
2026-09-09).

It is the same defect as the v2.79.0 regression where a failed
processing run painted itself green: a status derived from what we
intended rather than from what happened.

The only way to know a device opens is to open it. This module holds
the part of that which is a decision rather than a PortAudio call — so
the wording, the caveats and the failure text are testable without a
sound card, which is what the capture module's `sounddevice` import
otherwise prevents.

WHAT THE PROBE MUST NOT DO
--------------------------
Run while recording. Touching PortAudio underneath a live capture is
the 2026-08-20 crash — two threads inside the library at once, an
access violation, sixteen respawn cycles. Knowing whether a mic is free
is not worth any part of that risk, and during a recording the answer
is already known.

It also reuses ``build_mic_open_plan``, so what the probe proves is
what Start Recording will actually attempt. A probe that tested a
different ladder would be worse than no probe, because it would be
confidently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ProbeOutcome:
    """What the readiness card should say.

    ``message`` is the headline; ``detail`` is the caveat, and is None
    when there is nothing to add — a device that opened exactly as asked
    needs no commentary, and inventing some would train people to ignore
    the field that matters.
    """

    ok: bool
    message: str
    detail: Optional[str] = None
    device: Optional[int] = None
    channels: Optional[int] = None
    samplerate: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "detail": self.detail,
            "device": self.device,
            "channels": self.channels,
            "samplerate": self.samplerate,
        }


def summarize_probe(
    *,
    opened: Optional[Dict[str, Any]],
    attempts_tried: int,
    total_attempts: int,
    selected_device: Optional[int],
    error: Optional[str],
) -> ProbeOutcome:
    """Describe a probe result.

    ``opened`` is the attempt dict that succeeded (see
    core/mic_open_plan), or None when nothing did.

    Two caveats are worth raising on an otherwise-successful open,
    because both change what the recording will contain and neither is
    visible afterwards:

      * It opened in MONO on a device that offers stereo. The recording
        works; channel-based speaker attribution has one channel instead
        of two.

      * It opened on a DIFFERENT device entry than the one selected.
        Still the same physical microphone, reached through another
        Windows audio subsystem — but a silent substitution is how
        someone ends up recording through something they did not choose.
    """
    if opened is None:
        if total_attempts == 0:
            return ProbeOutcome(
                ok=False,
                message="This device reports no input channels, so it "
                        "cannot record.",
                detail="Pick a different microphone, or reconnect this one "
                       "and refresh the device list.",
            )
        reason = (error or "").strip()
        return ProbeOutcome(
            ok=False,
            message="This microphone could not be opened.",
            detail=(f"{attempts_tried} configuration"
                    f"{'' if attempts_tried == 1 else 's'} tried. "
                    + (f"Last error: {reason}" if reason
                       else "No further detail was reported by the audio "
                            "driver.")),
        )

    device = opened.get("device")
    channels = opened.get("channels")
    samplerate = opened.get("samplerate")

    notes = []
    if device is not None and selected_device is not None \
            and device != selected_device:
        notes.append(
            "opened through another audio subsystem for the same device "
            "(the one you selected refused)")
    if channels == 1:
        notes.append(
            "opened in mono — the recording is fine, but speaker separation "
            "has one channel to work with instead of two")

    return ProbeOutcome(
        ok=True,
        message="Microphone ready — opened and released successfully.",
        detail=("This microphone " + "; ".join(notes) + ".") if notes else None,
        device=device,
        channels=channels,
        samplerate=samplerate,
    )
