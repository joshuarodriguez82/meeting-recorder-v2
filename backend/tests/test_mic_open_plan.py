"""
Which (device, channels, samplerate) combinations the mic open tries.

THE FIELD REPORT (2026-09-09)
-----------------------------
A Windows install could not start a recording at all:

    Failed to start audio capture: All mic configurations failed. Last
    error: Error opening InputStream: Invalid number of channels
    [PaErrorCode -9998]

Two failures stacked, and the log separates them cleanly.

First, the selected device — a USB headset, index 17, reported by
PortAudio as 2 input channels at 48 kHz — refused to open three times
with a driver-level error::

    Error starting stream: Unanticipated host error [PaErrorCode -9999]:
    'WdmSyncIoctl: DeviceIoControl GLE = 0x00000492 …'

then twice more with ``Invalid sample rate`` for the 44100 and 16000
rungs, because a shared-mode WASAPI stream only accepts the device's
own rate. Nothing in the app can fix that driver; the host-API fallback
exists precisely so it does not have to.

Then the fallback ran, and could not have worked. ``channels`` was
computed ONCE from the user-selected device::

    dev_info = sd.query_devices(self._mic_idx)
    max_ch   = int(dev_info["max_input_channels"])
    channels = min(2, max_ch)               # ← from device 17 only
    ...
    for dev_idx in device_candidates:       # 17, then 1 (MME), 8 (DS)
        candidate = sd.InputStream(device=dev_idx, channels=channels, …)

The same physical headset enumerates under several host APIs, and they
do not agree about how many input channels it has — MME in particular
routinely reports fewer than WASAPI. So every attempt on every
alternative asked for the primary's channel count and got -9998, five
times per device, identically. The ladder varied sample rate, block
size and latency; the one parameter that was wrong was the one it never
varied.

THE RULE
--------
A channel count belongs to the device it was read from. Every candidate
device is asked what IT supports, and a device that claims two is also
tried at one — a driver that advertises stereo and accepts only mono is
exactly the shape of this failure.

Kept pure so the ladder is testable without PortAudio, a sound card, or
Windows: the plan is data, and executing it is the caller's job.
"""

from __future__ import annotations

from core.mic_open_plan import build_mic_open_plan


def _dev(name, max_in, sr=48000.0):
    return {"name": name, "max_input_channels": max_in, "default_samplerate": sr}


HEADSET = "Microphone (Acme Link 380)"


# ── The reported failure ────────────────────────────────────────────

def test_each_device_is_tried_with_its_own_channel_count():
    """The bug, stated directly. The primary reports 2; the MME entry
    for the same headset reports 1. Asking the MME entry for 2 is
    -9998 every time, which is what the field log shows."""
    plan = build_mic_open_plan(
        [(17, _dev(HEADSET, 2)), (1, _dev(HEADSET, 1))])

    for_17 = {a["channels"] for a in plan if a["device"] == 17}
    for_1 = {a["channels"] for a in plan if a["device"] == 1}
    assert 2 in for_17
    assert for_1 == {1}, (
        f"the 1-channel device was offered {for_1}; asking a mono device "
        f"for stereo is the -9998 in the field log")


def test_a_stereo_device_is_also_tried_in_mono():
    """A driver that advertises two channels and accepts only one is the
    same failure wearing a different hat, and costs one extra attempt to
    rule out."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2))])
    assert {a["channels"] for a in plan} == {2, 1}


def test_stereo_is_tried_before_mono():
    """Mono works everywhere but throws away the second channel, which
    channel attribution uses. It is the fallback, not the default."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2))])
    channels_in_order = [a["channels"] for a in plan]
    assert channels_in_order.index(2) < channels_in_order.index(1)


def test_a_device_reporting_many_channels_is_capped_at_two():
    """A 4-channel array opened at 4 gives us three channels of nothing
    to downmix. Two is all the pipeline consumes."""
    plan = build_mic_open_plan([(2, _dev("Microphone Array", 4))])
    assert max(a["channels"] for a in plan) == 2


# ── Device ordering ─────────────────────────────────────────────────

def test_the_selected_device_is_tried_before_any_alternative():
    """The user picked it. An alternative that happens to open first
    would silently record from a different microphone."""
    plan = build_mic_open_plan(
        [(17, _dev(HEADSET, 2)), (1, _dev(HEADSET, 1))])
    assert plan[0]["device"] == 17


def test_every_config_for_a_device_is_exhausted_before_moving_on():
    """Interleaving devices would leave the selected one half-tried
    while recording from something else."""
    plan = build_mic_open_plan(
        [(17, _dev(HEADSET, 2)), (1, _dev(HEADSET, 1))])
    devices_in_order = [a["device"] for a in plan]
    first_alt = devices_in_order.index(1)
    assert 17 not in devices_in_order[first_alt:]


# ── Sample rates ────────────────────────────────────────────────────

def test_the_devices_own_rate_is_tried_first():
    """A shared-mode WASAPI stream only accepts the device's own rate —
    the field log shows 44100 and 16000 rejected outright on a 48 kHz
    device. The native rate has to lead."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2, sr=44100.0))])
    assert plan[0]["samplerate"] == 44100


def test_fallback_rates_are_still_offered():
    """They cost microseconds and rescue drivers that refuse their own
    advertised rate."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2))])
    rates = {a["samplerate"] for a in plan}
    assert {48000, 44100, 16000} <= rates


def test_mono_at_the_native_rate_beats_stereo_at_a_rate_that_cannot_work():
    """The ordering that matters. On shared-mode WASAPI a non-native
    rate is a near-certain failure, so the two plausible attempts —
    stereo then mono, both at the device's own rate — have to come
    before any of them. Channels-outermost would burn six guaranteed
    failures first."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2, sr=48000.0))])
    mono_native = next(i for i, a in enumerate(plan)
                       if a["channels"] == 1 and a["samplerate"] == 48000)
    stereo_offrate = next(i for i, a in enumerate(plan)
                          if a["channels"] == 2 and a["samplerate"] != 48000)
    assert mono_native < stereo_offrate


def test_no_duplicate_attempts():
    """A 48 kHz device must not be offered 48000 twice — every wasted
    attempt is another driver round-trip before the user is told."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2))])
    keys = [(a["device"], a["channels"], a["samplerate"],
             a["blocksize"], a["latency"]) for a in plan]
    assert len(keys) == len(set(keys))


# ── Degenerate input ────────────────────────────────────────────────

def test_a_device_with_no_input_channels_is_not_offered():
    """It cannot record. Attempting it only delays the real error."""
    plan = build_mic_open_plan(
        [(17, _dev(HEADSET, 0)), (1, _dev(HEADSET, 1))])
    assert {a["device"] for a in plan} == {1}


def test_no_usable_device_yields_an_empty_plan():
    """Empty is the honest answer; the caller reports it rather than
    looping over nothing and blaming the last attempt."""
    assert build_mic_open_plan([(17, _dev(HEADSET, 0))]) == []
    assert build_mic_open_plan([]) == []


def test_an_unreadable_rate_falls_back_rather_than_raising():
    """query_devices has returned junk in the field. A missing or
    unparseable rate must not stop the plan being built at all."""
    plan = build_mic_open_plan(
        [(17, {"name": HEADSET, "max_input_channels": 2,
               "default_samplerate": None})])
    assert plan, "a bad rate emptied the plan"
    assert all(isinstance(a["samplerate"], int) for a in plan)


def test_every_attempt_carries_the_full_argument_set():
    """These land straight in sd.InputStream. A missing key is a
    TypeError at record time and nowhere else."""
    plan = build_mic_open_plan([(17, _dev(HEADSET, 2))])
    for attempt in plan:
        assert set(attempt) == {
            "device", "channels", "samplerate", "blocksize", "latency"}
