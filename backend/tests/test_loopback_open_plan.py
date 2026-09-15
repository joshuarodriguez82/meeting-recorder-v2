"""
The loopback ladder has to vary the thing that was wrong.

THE FIELD LOG (2026-09-15)
--------------------------
A Windows install recorded a full meeting with **no far-end audio**::

    [FAIL] Loopback buffer=0    failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    [FAIL] Loopback buffer=1024 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    [FAIL] Loopback buffer=4096 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    [FAIL] Loopback buffer=2048 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
    System audio capture unavailable: … Mic only.

The user's own voice was captured; nobody else's was. The app said so
in one WARNING and carried on.

``AUDCLNT_E_UNSUPPORTED_FORMAT`` means the rate/channel pair is not the
endpoint's mix format. The ladder varied ``frames_per_buffer`` only, so
every rung asked the same rejected question — and two rungs were
literally identical, because the open mapped ``buf=0`` and ``buf=1024``
to the same call.

These tests are about the ladder, not about PortAudio: the decision is
what was wrong, and it is the part that can be checked without a sound
card.
"""

from __future__ import annotations

import pytest

from core.loopback_open_plan import (
    BUFFER_SIZES,
    FALLBACK_RATES,
    MAX_CHANNELS,
    build_loopback_open_plan,
    describe_attempt,
)


def _formats(plan):
    """(samplerate, channels) pairs, in order, deduplicated."""
    seen, out = set(), []
    for a in plan:
        key = (a["samplerate"], a["channels"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── The reported defect ─────────────────────────────────────────────

def test_the_plan_varies_the_format_not_just_the_buffer():
    """The whole bug. Four rungs that differed only in buffer size
    cannot answer a format rejection."""
    plan = build_loopback_open_plan(48000, 2)
    assert len(_formats(plan)) > 1, (
        "every attempt uses the same rate/channel pair — this is the "
        "shipped ladder, which failed four times identically")


def test_a_stereo_device_is_also_tried_in_mono():
    """Drivers advertise stereo and accept mono. Same shape as the mic
    failure that produced core/mic_open_plan."""
    plan = build_loopback_open_plan(48000, 2)
    assert (48000, 1) in _formats(plan)


def test_a_refused_rate_is_followed_by_another_rate():
    plan = build_loopback_open_plan(48000, 2)
    rates = [sr for sr, _ in _formats(plan)]
    assert len(set(rates)) > 1, f"only one rate attempted: {rates}"
    assert 44100 in rates


# ── Ordering: don't change what already works ───────────────────────

def test_the_first_attempt_is_what_the_app_tries_today():
    """A machine that works today must not take a different path to the
    same success. Today's first open is the device's own rate and
    channel count at a 1024-frame buffer."""
    first = build_loopback_open_plan(48000, 2)[0]
    assert first == {"samplerate": 48000, "channels": 2,
                     "frames_per_buffer": 1024}


def test_the_devices_own_rate_leads():
    """Shared-mode WASAPI loopback captures at the endpoint's mix
    format, so opening with a guess wastes the first rung."""
    plan = build_loopback_open_plan(44100, 2)
    assert plan[0]["samplerate"] == 44100


def test_stereo_leads_mono():
    """The second channel is what channel attribution reads. Mono is the
    fallback, not the default."""
    formats = _formats(build_loopback_open_plan(48000, 2))
    assert formats.index((48000, 2)) < formats.index((48000, 1))


# ── Cost: a failing open must stay fast ─────────────────────────────

def test_only_the_first_format_sweeps_buffer_sizes():
    """A format rejection is not a buffer problem. Trying every buffer
    against every format turns one fast failure into eighteen slow ones
    while a meeting is starting."""
    plan = build_loopback_open_plan(48000, 2)
    by_format = {}
    for a in plan:
        by_format.setdefault((a["samplerate"], a["channels"]), []).append(a)
    first_key = (plan[0]["samplerate"], plan[0]["channels"])
    assert len(by_format[first_key]) == len(BUFFER_SIZES)
    for key, attempts in by_format.items():
        if key != first_key:
            assert len(attempts) == 1, f"{key} sweeps buffers unnecessarily"


def test_the_whole_plan_stays_short():
    """This runs when someone is about to join a call."""
    assert len(build_loopback_open_plan(48000, 2)) <= 10


def test_no_two_attempts_are_identical():
    """The shipped ladder wasted a rung: buf=0 and buf=1024 both opened
    at 1024 frames, so four attempts were three."""
    plan = build_loopback_open_plan(48000, 2)
    keys = [(a["samplerate"], a["channels"], a["frames_per_buffer"])
            for a in plan]
    assert len(keys) == len(set(keys)), f"duplicate attempts: {keys}"


# ── Unreadable device info ──────────────────────────────────────────

@pytest.mark.parametrize("rate,channels", [
    (None, None),      # device info unreadable
    (0, 0),            # reported, but as zeros
    (-1, -1),          # nonsense
    (48000, None),     # partial
    (None, 2),         # partial the other way
])
def test_unusable_device_info_still_produces_a_ladder(rate, channels):
    """"We could not read the device" is not "there is nothing to try".
    The reported failure began with `[Errno -9996] Invalid device info`,
    so this is the exact state the ladder has to survive."""
    plan = build_loopback_open_plan(rate, channels)
    assert plan, "no attempts at all"
    for a in plan:
        assert a["samplerate"] in FALLBACK_RATES or a["samplerate"] == rate
        assert 1 <= a["channels"] <= MAX_CHANNELS
        assert a["frames_per_buffer"] > 0


def test_a_mono_device_is_never_asked_for_stereo():
    """Asking a 1-channel endpoint for 2 is the -9998 shape the mic
    ladder exists for. Don't reintroduce it here."""
    plan = build_loopback_open_plan(48000, 1)
    assert all(a["channels"] == 1 for a in plan)


def test_a_device_claiming_eight_channels_is_capped():
    """Loopback feeds the far-end track and channel attribution;
    neither reads beyond two, and asking for eight is a new way to be
    refused."""
    plan = build_loopback_open_plan(48000, 8)
    assert max(a["channels"] for a in plan) == MAX_CHANNELS


def test_the_devices_rate_is_not_duplicated_among_the_fallbacks():
    """48000 is both the device's rate and a standard fallback. It must
    appear as ONE rung of the rate ladder, not two — a repeated rate is
    a wasted open while someone is joining a call.

    Rates legitimately recur across channel counts, so the check is on
    the order the rates are first tried, not on every (rate, channel)
    pair."""
    plan = build_loopback_open_plan(48000, 2)
    first_seen = []
    for attempt in plan:
        if attempt["samplerate"] not in first_seen:
            first_seen.append(attempt["samplerate"])
    assert first_seen == [48000, 44100], first_seen


# ── The log line ────────────────────────────────────────────────────

def test_the_log_label_names_the_format():
    """The shipped log said `Loopback attempt buffer=4096` — the one
    field that never mattered. Four of those in a row give no hint that
    the rate was the problem."""
    label = describe_attempt({"samplerate": 44100, "channels": 1,
                              "frames_per_buffer": 4096})
    assert "44100" in label and "ch=1" in label
