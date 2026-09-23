"""
Telling someone, while it can still be fixed, that a recording is broken.

THE FIELD REPORT (2026-09-15)
-----------------------------
A 25-minute meeting was recorded with only the microphone. Everyone else
on the call was missing. The system-audio stream had failed to open four
times at the very start::

    [FAIL] Loopback buffer=0 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    …
    System audio capture unavailable: [Errno -9996] Invalid device info.
        Mic only.

The user found out mid-meeting, by chance, and only because the next
meeting was also affected. Three things kept it hidden:

1. **It waited.** The only warning path treated system audio as a
   stream that might have *stopped*: nothing was said until it had been
   silent for 45 seconds, although the app knew at second zero that it
   had never started.
2. **It said the wrong thing.** *"No system audio for 45 seconds —
   capture may have stopped. Consider stopping and restarting the
   recording."* Restarting re-opens the same device in the same format
   and is refused the same way. The message diagnosed a state it could
   not see — the defect core/model_status.py and core/fingerprint_status
   exist for, in a third place.
3. **It stayed on one page.** The warning was a banner on the Record
   tab. During an auto-recorded meeting the user is in the meeting app,
   not on that tab. Nothing reached them anywhere else, and afterwards
   the session carried no trace that the other participants were never
   recorded.

WHAT THIS MODULE DECIDES
------------------------
Given what capture knows, what to say — immediately when the cause is
known, in terms of that cause, with a stable ``code`` the UI can
de-duplicate on so a warning becomes one notification rather than one
per poll. Pure, so the wording and the precedence are testable without
PortAudio, which is the reason none of this had a test before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Stable codes. The UI keys notifications on these; renaming one
#: re-notifies every user once, so treat them as an API.
MIC_DEAD = "mic_dead"
SYSTEM_AUDIO_UNAVAILABLE = "system_audio_unavailable"
SYSTEM_AUDIO_DEAD = "system_audio_dead"

#: Why system audio could not be opened, classified from the PortAudio /
#: WASAPI error text. Codes, not prose: these go into events.jsonl,
#: whose scrubber rejects anything with a space in it.
REASON_UNSUPPORTED_FORMAT = "unsupported_format"
REASON_DEVICE_UNAVAILABLE = "device_unavailable"
REASON_DEVICE_IN_USE = "device_in_use"
REASON_PERMISSION = "permission_denied"
REASON_UNKNOWN = "unknown"

# Substrings as they actually appear in the field, most specific first.
# -9997/-9998 are PortAudio's invalid-sample-rate / invalid-channel-count;
# -9996 is invalid device.
_REASON_MARKERS = (
    # macOS native system audio (core/mac_system_audio.py): the Screen &
    # System Audio Recording permission is off.
    (REASON_PERMISSION, ("Screen & System Audio Recording", "-3801")),
    (REASON_DEVICE_IN_USE, ("AUDCLNT_E_DEVICE_IN_USE",
                            "AUDCLNT_E_EXCLUSIVE_MODE_NOT_ALLOWED",
                            "exclusive mode")),
    (REASON_UNSUPPORTED_FORMAT, ("AUDCLNT_E_UNSUPPORTED_FORMAT",
                                 "Invalid sample rate", "-9997",
                                 "Invalid number of channels", "-9998")),
    (REASON_DEVICE_UNAVAILABLE, ("AUDCLNT_E_DEVICE_INVALIDATED",
                                 "Invalid device", "-9996",
                                 "Device unavailable", "-9985")),
)


def classify_open_error(error: Optional[str]) -> str:
    """Reason code for a system-audio open failure.

    ``REASON_UNKNOWN`` rather than a guess when nothing matches: the
    message for an unknown cause says so and shows the raw error, which
    is more use to the reader than a confident wrong diagnosis.
    """
    text = (error or "").strip()
    if not text:
        return REASON_UNKNOWN
    lowered = text.lower()
    for reason, markers in _REASON_MARKERS:
        for marker in markers:
            if marker.lower() in lowered:
                return reason
    return REASON_UNKNOWN


def _remedy(reason: str, platform: str, raw: str) -> str:
    """What to do about it — specific to the cause, because the generic
    "stop and restart" is exactly what does not work for a refused
    format."""
    windows = platform.startswith("win")
    if reason == REASON_PERMISSION:
        return ("Turn on Meeting Recorder under System Settings → Privacy "
                "& Security → Screen & System Audio Recording, then stop "
                "and restart the recording.")
    if reason == REASON_UNSUPPORTED_FORMAT:
        if windows:
            # By the time this is shown the open ladder has already
            # tried the device's own rate plus 48000/44100, stereo and
            # mono — so what is left is a mix format outside that set
            # (a high rate, or a multichannel layout). A 2-channel
            # 48000 Hz Default Format puts the endpoint back inside it.
            return ("Windows refused this speaker's audio format. In Sound "
                    "settings, open the speaker's Properties → Advanced and "
                    "set Default Format to a 2 channel, 48000 Hz option, "
                    "then stop and restart the recording.")
        return ("The system refused this output device's audio format. "
                "Choose a different output device, then stop and restart "
                "the recording.")
    if reason == REASON_DEVICE_UNAVAILABLE:
        return ("The selected speaker could not be opened — it may have "
                "been unplugged or switched. Choose the speaker you are "
                "using now under System Audio, then stop and restart the "
                "recording.")
    if reason == REASON_DEVICE_IN_USE:
        if windows:
            return ("Another app has exclusive control of the speaker. In "
                    "Sound settings, open the speaker's Properties → "
                    "Advanced and turn off \"Allow applications to take "
                    "exclusive control\", then stop and restart the "
                    "recording.")
        return ("Another app has exclusive control of the output device. "
                "Close it, then stop and restart the recording.")
    detail = f" ({raw[:120]})" if raw else ""
    return (f"System audio could not be opened{detail}. Check the System "
            f"Audio device, then stop and restart the recording.")


@dataclass(frozen=True)
class CaptureIssue:
    """One thing wrong with the recording in progress."""

    code: str
    message: str


def live_capture_issue(
    *,
    elapsed_s: float,
    mic_silent_for_s: Optional[float],
    system_configured: bool,
    system_open_error: Optional[str],
    system_silent_for_s: Optional[float],
    platform: str,
    grace_s: float,
    dead_after_s: float,
) -> Optional[CaptureIssue]:
    """The single most important capture problem right now, or None.

    ``*_silent_for_s`` is seconds since that stream last delivered a
    chunk (None = it never has). Precedence, most severe first:

    1. **Microphone dead** — nothing is being recorded at all.
    2. **System audio never opened** — reported at once, not after
       ``dead_after_s``: the open already failed, so there is nothing to
       wait for, and every second of waiting is a second of the meeting
       lost.
    3. **System audio stopped** — it was flowing and went quiet. This is
       the only case where "may have stopped, try restarting" is honest.

    A recording configured WITHOUT system audio is a choice, not a
    failure, and never produces (2) or (3).
    """
    past_grace = elapsed_s > grace_s

    mic_silent = mic_silent_for_s if mic_silent_for_s is not None else elapsed_s
    if past_grace and mic_silent >= dead_after_s:
        return CaptureIssue(
            MIC_DEAD,
            f"No microphone audio for {int(mic_silent)} seconds — capture "
            f"may have stopped. Consider stopping and restarting the "
            f"recording.")

    if system_configured and system_open_error:
        reason = classify_open_error(system_open_error)
        return CaptureIssue(
            SYSTEM_AUDIO_UNAVAILABLE,
            "Other participants are NOT being recorded — only your "
            "microphone is. " + _remedy(reason, platform,
                                        system_open_error.strip()))

    if system_configured and past_grace:
        sys_silent = (system_silent_for_s if system_silent_for_s is not None
                      else elapsed_s)
        if sys_silent >= dead_after_s:
            return CaptureIssue(
                SYSTEM_AUDIO_DEAD,
                f"No system audio for {int(sys_silent)} seconds — capture "
                f"may have stopped. Consider stopping and restarting the "
                f"recording.")
    return None


def missing_system_audio_warning(
    *,
    system_configured: bool,
    system_samples: int,
    system_open_error: Optional[str],
) -> Optional[str]:
    """The line a SESSION carries afterwards when the other participants
    were never recorded, or None.

    This is the part the user reads the next day. Without it, a
    one-sided transcript and a summary that attributes everything to one
    person look exactly like a meeting in which one person talked.
    """
    if not system_configured or system_samples > 0:
        return None
    reason = classify_open_error(system_open_error)
    why = {
        REASON_UNSUPPORTED_FORMAT: "the speaker's audio format was refused",
        REASON_DEVICE_UNAVAILABLE: "the speaker could not be opened",
        REASON_DEVICE_IN_USE: "another app had exclusive control of the "
                              "speaker",
        REASON_PERMISSION: "macOS permission to record system audio is off",
    }.get(reason, "system audio never started")
    return ("Other participants were not recorded — " + why + ". Only your "
            "microphone was captured, so the transcript, summary and action "
            "items cover your side of the conversation only.")


def one_sided_transcript_note(capture_warning: Optional[str]) -> str:
    """Context for the summarizer when the other participants were not
    recorded, or "" when nothing is missing.

    The Sessions list shows the reader that a meeting is one-sided; the
    model writing its summary and action items could not see that, and
    wrote them as if the user's lines were the whole discussion — so
    every decision and commitment landed on the one person recorded.
    This rides in with the session notes, the channel every extractor
    already reads, rather than being threaded through each prompt.
    """
    if not (capture_warning or "").strip():
        return ""
    return ("RECORDING LIMITATION (stated by the app, not the user): the "
            "other participants' audio was not captured — this transcript "
            "is only the user's side of the conversation. Do not present "
            "it as the whole discussion; do not attribute decisions or "
            "commitments to other people unless the user's own words "
            "state them; where it matters, say that the other side was "
            "not recorded.")
