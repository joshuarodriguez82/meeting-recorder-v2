"""
Which (device, channels, samplerate) combinations to try when opening a mic.

WHY THIS IS ITS OWN MODULE
--------------------------
Pure decision logic, no PortAudio: the ladder can be tested without a
sound card, without Windows, and without `sounddevice` being installed
at all. `core/audio_capture` imports `sounddevice` at module scope, so
anything living there is unreachable from the test suite — and this
ladder is exactly the part that was wrong in the field.

THE FAILURE THIS EXISTS FOR (2026-09-09)
----------------------------------------
A Windows install could not start a recording::

    All mic configurations failed. Last error: Error opening
    InputStream: Invalid number of channels [PaErrorCode -9998]

The selected headset (index 17, reported as 2 channels at 48 kHz) hit a
driver-level error three times::

    Unanticipated host error [PaErrorCode -9999]:
    'WdmSyncIoctl: DeviceIoControl GLE = 0x00000492 …'

and then twice more on ``Invalid sample rate``, because a shared-mode
WASAPI stream only accepts the device's own rate. That driver is not
something the app can repair — which is the entire reason the host-API
fallback exists.

The fallback then failed for a reason that WAS the app's:
``channels`` had been computed once, from the user-selected device, and
was reused for every alternative::

    max_ch   = int(dev_info["max_input_channels"])   # device 17 → 2
    channels = min(2, max_ch)
    for dev_idx in device_candidates:                # 17, then 1, 8
        sd.InputStream(device=dev_idx, channels=channels, …)

The same physical headset enumerates under several host APIs and they
disagree about its channel count — MME routinely reports fewer than
WASAPI. So every attempt on every alternative asked for the primary's
count and got -9998, five times per device, identically. The ladder
varied sample rate, block size and latency. The one parameter that was
wrong was the one it never varied.

THE RULES
---------
1. **A channel count belongs to the device it was read from.** Every
   candidate is asked what IT supports.

2. **A device claiming two channels is also tried at one.** A driver
   that advertises stereo and accepts only mono is the same failure
   wearing a different hat, and costs one extra attempt to rule out.
   Stereo leads, because the second channel is what channel attribution
   uses; mono is the fallback, not the default.

3. **The device's own rate leads.** Shared-mode WASAPI accepts nothing
   else, so a ladder that opens with a guess wastes its first rung.

4. **The selected device is exhausted before any alternative.** An
   alternative that opened first would silently record the wrong
   microphone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

#: Rates to fall back to when the device's own is refused. Cheap to
#: try — a rejected rate fails in microseconds — and they rescue drivers
#: that will not honour the rate they advertise.
FALLBACK_RATES = (48000, 44100, 16000)

#: Used when a device reports no usable rate. query_devices has returned
#: junk in the field, and a missing rate must not empty the plan.
DEFAULT_RATE = 48000

#: More than this is downmixed to mono immediately by the capture
#: callback, so opening a 4-channel array at 4 buys three channels of
#: nothing.
MAX_CHANNELS = 2

#: (blocksize, latency) shapes, in the order they are tried. Driver
#: quirks live here: some refuse the automatic block size, some refuse
#: low latency.
_BUFFER_SHAPES = (
    (0, "high"),
    (0, "low"),
    (1024, "high"),
)


def _rate_of(dev: Dict[str, Any]) -> int:
    """The device's own rate, or the default when it is unreadable."""
    try:
        rate = int(float(dev.get("default_samplerate") or 0))
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return rate if rate > 0 else DEFAULT_RATE


def _channel_options(dev: Dict[str, Any]) -> List[int]:
    """Channel counts to try for one device, most capable first.

    Empty when the device reports no input channels — it cannot record,
    and attempting it only delays the real error.
    """
    try:
        max_ch = int(dev.get("max_input_channels") or 0)
    except (TypeError, ValueError):
        max_ch = 0
    if max_ch <= 0:
        return []
    top = min(MAX_CHANNELS, max_ch)
    return [top, 1] if top > 1 else [1]


def build_mic_open_plan(
    candidates: Sequence[Tuple[int, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Every attempt to make, in order.

    ``candidates`` is ``[(device_index, device_info), …]`` with the
    user-selected device first and same-hardware alternatives on other
    host APIs after it — device_info being whatever ``query_devices``
    returned for that index.

    Each entry of the result is a kwargs dict for ``sd.InputStream``:
    ``device``, ``channels``, ``samplerate``, ``blocksize``, ``latency``.
    The caller executes them in order and stops at the first that opens.

    An empty list means nothing here can record, which the caller should
    report as such rather than blaming whichever attempt happened to be
    last.
    """
    plan: List[Dict[str, Any]] = []
    seen = set()

    for device_index, dev in candidates:
        channel_options = _channel_options(dev or {})
        if not channel_options:
            continue

        native = _rate_of(dev or {})
        rates = [native] + [r for r in FALLBACK_RATES if r != native]

        # RATE OUTERMOST, CHANNELS INNER.
        #
        # A shared-mode WASAPI stream accepts only the device's own rate,
        # so every non-native rung is a near-certain failure — the field
        # log shows 44100 and 16000 rejected outright on a 48 kHz device.
        # Ordering channels outermost would therefore spend six
        # guaranteed-useless attempts before ever trying mono at the one
        # rate that can work.
        #
        # This way the first four attempts are the two plausible ones:
        # stereo at the native rate, then mono at the native rate.
        for rate in rates:
            for channels in channel_options:
                for blocksize, latency in _BUFFER_SHAPES:
                    key = (device_index, channels, rate, blocksize, latency)
                    if key in seen:
                        continue
                    seen.add(key)
                    plan.append({
                        "device": device_index,
                        "channels": channels,
                        "samplerate": rate,
                        "blocksize": blocksize,
                        "latency": latency,
                    })
    return plan
