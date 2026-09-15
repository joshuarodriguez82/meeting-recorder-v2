"""
Which (samplerate, channels, buffer) combinations to try for system audio.

THE FAILURE THIS EXISTS FOR (field log 2026-09-15)
--------------------------------------------------
A Windows install recorded a whole meeting with **no far-end audio**.
The loopback stream never opened::

    [FAIL] Loopback buffer=0    failed: Unanticipated host error
        [PaErrorCode -9999]: 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    [FAIL] Loopback buffer=1024 failed: … AUDCLNT_E_UNSUPPORTED_FORMAT
    [FAIL] Loopback buffer=4096 failed: … AUDCLNT_E_UNSUPPORTED_FORMAT
    [FAIL] Loopback buffer=2048 failed: … AUDCLNT_E_UNSUPPORTED_FORMAT
    System audio capture unavailable: [Errno -9996] Invalid device
        info. Mic only.

Four attempts, one error, repeated. The ladder varied ``frames_per_
buffer`` and nothing else — ``channels`` and ``rate`` were read once
from the device and reused on every rung::

    self._loopback_channels = int(dev_info["maxInputChannels"])
    self._loopback_sr       = int(dev_info["defaultSampleRate"])
    for buf in [0, 1024, 4096, 2048]:
        self._pa.open(channels=self._loopback_channels,
                      rate=self._loopback_sr, frames_per_buffer=...)

``AUDCLNT_E_UNSUPPORTED_FORMAT`` is WASAPI saying *this rate/channel
pair is not the endpoint's mix format*. No buffer size answers that, so
all four rungs asked the identical rejected question.

Two of them were not even distinct: the open used
``frames_per_buffer=buf if buf else 1024``, so ``buf=0`` and
``buf=1024`` produced the same call. A four-rung ladder with three
real rungs, none of which varied the one parameter that was wrong.

This is the same defect ``core/mic_open_plan`` was written for, one
layer over — and that module's own summary says it best: *the ladder
varied sample rate, block size and latency; the one parameter that was
wrong was the one it never varied.* The mic got a real ladder in
v2.80.3. System audio never did.

THE RULES
---------
1. **The endpoint's own rate leads.** Shared-mode WASAPI loopback
   captures at the render endpoint's mix format, so the device's
   reported rate is the likeliest to work and must be the first rung —
   a ladder that opens with a guess wastes it.

2. **A device claiming two channels is also tried at one.** Same shape
   as the mic: drivers advertise stereo and accept mono. Stereo leads
   because the second channel is what channel attribution reads.

3. **Buffer size is varied only on the FIRST format.** A format
   rejection is not a buffer problem, and trying three buffers against
   each of six formats turns a fast failure into eighteen slow ones
   while a meeting is starting. The first combination — which is what
   the app tries today — keeps its full buffer sweep, because that is
   the rung where a buffer quirk actually shows up.

4. **The first rung is exactly today's first attempt.** Whatever else
   changes, a machine that works today must not start taking a
   different path to the same success.

Pure decision logic, no PortAudio: ``core/audio_capture`` imports
``sounddevice`` and ``pyaudiowpatch`` at module scope, which makes
anything living there unreachable from the test suite — and this ladder
is exactly the part that was wrong in the field.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

#: Rates to fall back to when the endpoint's own rate is refused. These
#: two are what Windows shared-mode mix formats use in practice; adding
#: more lengthens a failing open without improving the odds.
FALLBACK_RATES = (48000, 44100)

#: Never ask for more than stereo. Loopback feeds the far-end track and
#: channel attribution, neither of which reads beyond two.
MAX_CHANNELS = 2

#: Buffer sizes for the first format, most-likely first. Deduplicated on
#: purpose: the shipped ladder had 0 and 1024, which the open mapped to
#: the same call.
BUFFER_SIZES = (1024, 4096, 2048)


def build_loopback_open_plan(
    device_rate: Optional[int],
    device_channels: Optional[int],
    *,
    fallback_rates: Sequence[int] = FALLBACK_RATES,
    max_channels: int = MAX_CHANNELS,
    buffer_sizes: Sequence[int] = BUFFER_SIZES,
) -> List[Dict[str, int]]:
    """Ordered attempts for opening a WASAPI loopback stream.

    ``device_rate`` / ``device_channels`` are what the device reported
    (``defaultSampleRate`` / ``maxInputChannels``). Either may be None or
    nonsense — a device info dict that cannot be read is exactly the
    situation this ladder exists to survive, so both are coerced rather
    than trusted.

    Returns dicts of ``samplerate`` / ``channels`` / ``frames_per_buffer``
    in the order they should be attempted. Never empty: with no usable
    device info at all it still yields the standard fallbacks, because
    "we could not read the device" is not the same as "there is nothing
    to try".
    """
    rates: List[int] = []
    if device_rate and int(device_rate) > 0:
        rates.append(int(device_rate))
    for rate in fallback_rates:
        if rate > 0 and rate not in rates:
            rates.append(rate)

    channels: List[int] = []
    try:
        top = min(int(device_channels or 0), max_channels)
    except (TypeError, ValueError):
        top = 0
    if top < 1:
        # Unreadable or zero channel count. Stereo first, then mono —
        # the same order a working device would produce, so a device we
        # could not interrogate still gets the normal ladder.
        top = max_channels
    for count in range(top, 0, -1):
        channels.append(count)

    buffers = [b for b in buffer_sizes if b and b > 0]
    if not buffers:
        buffers = [BUFFER_SIZES[0]]

    plan: List[Dict[str, int]] = []
    for rate in rates:
        for count in channels:
            # Rule 3: the full buffer sweep belongs to the first format
            # only; every later format gets one attempt.
            widths = buffers if not plan else buffers[:1]
            for buf in widths:
                plan.append({
                    "samplerate": rate,
                    "channels": count,
                    "frames_per_buffer": buf,
                })
    return plan


def describe_attempt(attempt: Dict[str, int]) -> str:
    """One-line label for a log entry.

    The shipped log said ``Loopback attempt buffer=4096``, which is the
    one field that never mattered — reading four of those in a row gives
    no hint that the rate was the problem.
    """
    return (f"sr={attempt.get('samplerate')} "
            f"ch={attempt.get('channels')} "
            f"buffer={attempt.get('frames_per_buffer')}")
