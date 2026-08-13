"""
Inspect each audio endpoint's shared-mode mix format on Windows.

WHY THIS EXISTS
---------------
Meeting Recorder captures the mic and the WASAPI loopback as two
independent streams. The Windows audio engine resamples each stream
into the configured shared-mode mix format BEFORE the capture path
sees it. If the two endpoints are configured at different default
formats — e.g. mic at 48 kHz / 16-bit vs. speakers at 48 kHz / 24-bit,
which is the v2.10.5 field repro — each side's resampler runs with
slightly different bit-rate-per-second timing characteristics, and the
discrepancy accumulates over a long recording as wall-clock drift
between the two streams. We saw ~31 s of drift on a 49-min real
session caused by a 16-bit / 24-bit mismatch.

`sounddevice.query_devices()` exposes PortAudio's view of the device,
which does NOT include the actual Windows mix format the engine uses —
it only reports `default_samplerate` (PortAudio's default, not WASAPI's
session format). To detect the exact mismatch that bit our user we
need WASAPI's `IAudioClient::GetMixFormat()`.

We use pycaw (pure Python over comtypes) because it's a single small
dep, no native build, and pulls the same `IMMDevice` graph the Sound
Control Panel reads from. On macOS / Linux this module's primary
function returns ``None`` cleanly so the sync-risk endpoint degrades
to a sample-rate-only check using existing sounddevice data.

PYCAW RUNS IN A CHILD PROCESS, NEVER IN THIS ONE (v2.25.1)
------------------------------------------------------------
Every confirmed STATUS_ACCESS_VIOLATION crash tracked across v2.23.2
and v2.25.0 implicates comtypes (pycaw's foundation); the Outlook/
pywin32 COM path has never appeared in a single one of the ten
captured crash dumps. This module is also called from `/audio/sync-
risk`, which the Record view polls continuously whenever it's open
and not actively recording (record-view.tsx's early-return only
covers `recording || conferenceRoomMode`) — by far the highest-
frequency, highest-exposure COM call in the app, and one that mints a
fresh batch of comtypes proxies (one per WASAPI endpoint) on every
single invocation.

`utils/com_worker.py`'s worker-thread `gc.collect()` fix addresses the
specific apartment-affinity/cyclic-GC mechanism behind the confirmed
crashes (see that module's docstring). This module goes one step
further as the primary containment for THIS specific call site:
`get_device_mix_format()` below never touches pycaw/comtypes in this
process at all. It spawns `scripts/get_mix_format.py` as a child
process (mirroring the precedent in
`services/recording_service._run_finalize_subprocess` /
`scripts/finalize_audio.py`) and only that child imports pycaw. If
comtypes crashes — via the mechanism above, or any other reason,
diagnosed or not — only the short-lived child dies; the backend
observes a non-zero exit and degrades to `None`, which every caller
already treats as a normal "can't tell" outcome.

`get_device_mix_format_inprocess()` (the actual pycaw/comtypes logic)
is still defined in this module because the child process imports and
calls it directly — but nothing in the long-running backend process
may call it. Callers in the backend must go through
`get_device_mix_format()`.

A settings-backed kill switch (`Settings.audio_mix_format_lookup_enabled`,
default True) lets this lookup be disabled entirely — the caller (the
`/audio/sync-risk` endpoint) checks it and passes `enabled=False` to
skip straight to `None` with no subprocess spawn at all. There is
deliberately NO "disable the subprocess but keep running pycaw in-
process" mode — that would silently reintroduce the exact crash this
module exists to prevent. If the subprocess route is ever unusable in
some environment, the correct degraded state is "no sync-risk
diagnostic," never "pycaw back in this process."
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from utils.com_worker import run_com
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Subprocess isolation ────────────────────────────────────────────

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "get_mix_format.py"

# /audio/sync-risk is polled continuously while the Record view is
# open (see module docstring) — keep the child's budget tight so a
# wedged child can't stack up behind repeated polls. Device
# enumeration + one GetMixFormat call normally completes in well under
# a second.
_SUBPROCESS_TIMEOUT_S = 10.0

# ── Result cache ─────────────────────────────────────────────────────
# The field log that motivated this whole fix showed the SAME lookup
# failing repeatedly for the SAME device ("No WASAPI endpoint matched
# 'Speakers (Realtek(R) Audio) [Loopback]' (output)") on every poll.
# Caching (including negative/None results) means fewer subprocess
# spawns and fewer COM calls overall — cheaper, and strictly fewer
# chances for anything in the chain to fault. Keyed by the exact
# (device_name, kind) pair the caller asked about; TTL matches
# core/audio_capture.py's own device-list cache (_DEVICE_CACHE_TTL),
# and is also cleared by the same invalidation hook (see
# invalidate_mix_format_cache() below and audio_capture.
# invalidate_device_cache()) so a device change clears both caches
# together.
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 60.0
_mix_format_cache: dict[tuple[str, str], tuple[float, Optional[dict]]] = {}
_CACHE_MISS = object()


def _cache_lookup(key: tuple[str, str]):
    with _CACHE_LOCK:
        entry = _mix_format_cache.get(key)
        if entry is None:
            return _CACHE_MISS
        ts, value = entry
        if time.time() - ts >= _CACHE_TTL_S:
            return _CACHE_MISS
        return value


def _cache_store(key: tuple[str, str], value: Optional[dict]) -> None:
    with _CACHE_LOCK:
        _mix_format_cache[key] = (time.time(), value)


def invalidate_mix_format_cache() -> None:
    """Drop every cached mix-format lookup (including negative
    results). Called from core.audio_capture.invalidate_device_cache()
    so a device-list change (unplug/replug, default-device switch)
    clears this cache too — a stale "no match" result should not
    outlive the device list that produced it."""
    with _CACHE_LOCK:
        _mix_format_cache.clear()


def _safe_import_pycaw():
    """Import pycaw lazily so a missing/broken install never breaks the
    backend's startup path. Returns the module or None."""
    if sys.platform != "win32":
        return None
    try:
        from pycaw import pycaw  # noqa: F401  (presence check)
        return pycaw
    except Exception as e:
        logger.info(
            f"pycaw unavailable ({e!r}); sync-risk falls back to "
            f"sample-rate-only comparison.")
        return None


def get_device_mix_format(
    device_name: str, kind: str, *, enabled: bool = True,
) -> Optional[dict]:
    """Return the shared-mode mix format Windows is using for the
    endpoint whose friendly name matches ``device_name`` — WITHOUT ever
    running pycaw/comtypes in this process. See the module docstring
    ("PYCAW RUNS IN A CHILD PROCESS, NEVER IN THIS ONE") for why.

    This is the ONLY entry point the backend process should call.
    `get_device_mix_format_inprocess()` below does the real pycaw work
    but must only ever run inside `scripts/get_mix_format.py`'s child
    process.

    Args:
        device_name: User-visible name as it appears in Sound Control
            Panel (e.g. "Microphone (AIRHUG 21)" or
            "Speakers (Realtek(R) Audio)"). Compared case-insensitively
            against ``IMMDevice.FriendlyName`` after stripping a leading
            "Microphone (" / "Speakers (" prefix that some host APIs
            inject (so callers can pass either form).
        kind: "input" or "output". Selects render vs. capture endpoints.
        enabled: The settings-backed kill switch
            (`Settings.audio_mix_format_lookup_enabled`). Callers pass
            the current setting value; when False this returns `None`
            immediately with no subprocess spawn and no cache entry —
            a way to turn the whole diagnostic off without any pycaw
            involvement whatsoever. Defaults to True for callers (e.g.
            tests) that don't wire up Settings.

    Returns:
        ``{"sample_rate": int, "bits_per_sample": int, "channels": int}``
        on success.

        ``None`` when the kill switch is off, when this isn't Windows,
        when pycaw isn't installed, when no matching endpoint is found,
        or when the child process fails for any reason (non-zero exit,
        timeout, unparsable output, native crash). Callers should
        degrade gracefully — the sync-risk endpoint reports an
        "unknown" level for the missing side rather than a
        false-positive warning.

        Results (including ``None``) are cached briefly per
        (device_name, kind) — see the module-level cache docstring.
    """
    if not enabled:
        return None
    if sys.platform != "win32":
        return None

    target = _canonical_device_name(device_name)
    if not target:
        return None

    cache_key = (device_name, (kind or "").lower())
    cached = _cache_lookup(cache_key)
    if cached is not _CACHE_MISS:
        return cached

    result = _get_device_mix_format_subprocess(device_name, kind)
    _cache_store(cache_key, result)
    return result


def _get_device_mix_format_subprocess(
    device_name: str, kind: str,
) -> Optional[dict]:
    """Spawn scripts/get_mix_format.py, parse its RESULT line, and
    degrade to None on ANY failure — non-zero exit, timeout, or
    unparsable stdout. Mirrors
    services/recording_service._run_finalize_subprocess's handling."""
    argv = [
        sys.executable, str(_SCRIPT_PATH),
        "--device", device_name, "--kind", kind,
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            f"get_mix_format subprocess timed out after "
            f"{_SUBPROCESS_TIMEOUT_S}s for {device_name!r} ({kind})")
        return None
    except OSError as e:
        logger.warning(f"could not spawn get_mix_format subprocess: {e!r}")
        return None

    if proc.stderr:
        tail = proc.stderr[-2000:]
        logger.info(f"[get-mix-format-subprocess] stderr:\n{tail}")

    if proc.returncode != 0:
        logger.warning(
            f"get_mix_format subprocess exited with code {proc.returncode} "
            f"for {device_name!r} ({kind}) — degrading to None")
        return None

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("RESULT "):
            continue
        raw = line[len("RESULT "):]
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                f"get_mix_format subprocess emitted unparsable RESULT "
                f"{raw!r} for {device_name!r} — degrading to None")
            return None
        if value is None:
            return None
        if not isinstance(value, dict):
            logger.warning(
                f"get_mix_format subprocess emitted non-dict RESULT "
                f"{value!r} for {device_name!r} — degrading to None")
            return None
        return value

    logger.warning(
        f"get_mix_format subprocess exited 0 but emitted no RESULT line "
        f"for {device_name!r} (stdout tail: {(proc.stdout or '')[-200:]!r})")
    return None


def get_device_mix_format_inprocess(
    device_name: str, kind: str,
) -> Optional[dict]:
    """The real pycaw/comtypes lookup. MUST ONLY be called from inside
    scripts/get_mix_format.py's child process — never from the
    long-running backend. See the module docstring. `get_device_mix_
    format()` above is the entry point every backend caller must use
    instead.
    """
    pycaw = _safe_import_pycaw()
    if pycaw is None:
        return None

    target = _canonical_device_name(device_name)
    if not target:
        return None

    # Everything below touches comtypes/pycaw COM proxies. This process
    # is a short-lived, single-purpose child (see module docstring) —
    # even so, still route through the process's single COM worker
    # thread (utils/com_worker.py) rather than whatever thread called
    # us, for the same apartment-affinity reasons documented there.
    # Only plain data (a dict or None) is returned; no comtypes/pycaw
    # object may cross back out of this function.
    def _query() -> Optional[dict]:
        try:
            from pycaw.pycaw import AudioUtilities  # type: ignore
        except Exception as e:
            logger.warning(f"pycaw import failed at call time: {e!r}")
            return None

        # AudioUtilities.GetAllDevices() walks render + capture endpoints
        # in one shot and returns objects exposing FriendlyName + the
        # device IMMDevice we can query for the mix format.
        try:
            all_devices = AudioUtilities.GetAllDevices()
        except Exception as e:
            logger.warning(f"WASAPI device enumeration failed: {e!r}")
            return None

        # We don't filter by flow direction here — pycaw's AudioDevice
        # doesn't reliably expose the EDataFlow, and a friendly-name
        # match against the same physical endpoint is unambiguous in
        # practice (mics aren't named "Speakers (...)" and vice versa).
        # If a future collision becomes an issue we can switch to
        # MMDeviceEnumerator's EnumAudioEndpoints with eRender / eCapture
        # explicitly.
        _ = (kind or "").lower()  # documents the param; not used for filter
        for dev in all_devices or []:
            try:
                name = getattr(dev, "FriendlyName", "") or ""
            except Exception:
                continue
            if _canonical_device_name(name) != target:
                continue
            # pycaw's AudioDevice carries an IMMDevice we can Activate
            # IAudioClient on. The mix format is its WAVEFORMATEX.
            try:
                from ctypes import POINTER, cast
                from comtypes import GUID
                try:
                    from pycaw.api.audioclient import IAudioClient
                    from pycaw.api.audioclient.depend.structures import WAVEFORMATEX  # noqa: F401
                except Exception:
                    # Older pycaw layout
                    from pycaw.pycaw import IAudioClient  # type: ignore
                IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
                client = dev._dev.Activate(IID_IAudioClient, 0x17, None)  # CLSCTX_ALL
                audio_client = cast(client, POINTER(IAudioClient))
                mix_format_ptr = audio_client.GetMixFormat()
                wf = mix_format_ptr.contents
                return {
                    "sample_rate": int(wf.nSamplesPerSec),
                    "bits_per_sample": int(wf.wBitsPerSample),
                    "channels": int(wf.nChannels),
                }
            except Exception as e:
                logger.warning(
                    f"GetMixFormat failed for {name!r}: {e!r}")
                return None

        logger.info(f"No WASAPI endpoint matched {device_name!r} ({kind})")
        return None

    try:
        return run_com(_query)
    except Exception as e:
        logger.warning(f"COM worker call failed for {device_name!r}: {e!r}")
        return None


def _canonical_device_name(name: str) -> str:
    """Normalize device names so we can match a sounddevice friendly
    name (often prefixed "Microphone (" or "Speakers (") against the
    raw WASAPI FriendlyName ("AIRHUG 21" / "Realtek(R) Audio")."""
    s = (name or "").strip().lower()
    for prefix in ("microphone (", "speakers ("):
        if s.startswith(prefix):
            s = s[len(prefix):]
            if s.endswith(")"):
                s = s[:-1]
            break
    # Some WASAPI names append " [Loopback]" — strip it so the same
    # underlying device matches whether the caller passed the loopback
    # alias or the playback alias.
    if s.endswith(" [loopback]"):
        s = s[: -len(" [loopback]")]
    return s.strip()


def compare_formats(mic: Optional[dict], loopback: Optional[dict]) -> dict:
    """Decide whether a mic / loopback pair carries a sync risk.

    Returns a dict shaped for the /audio/sync-risk endpoint:
        {
          "ok": bool,
          "level": "ok" | "warn" | "unknown",
          "reason": str | None,
          "mic_format": {...} | None,
          "loopback_format": {...} | None,
          "fix_hint": str | None,
        }

    Cases:
      - Either side None  → "unknown" (pycaw missing or device gone).
      - sample_rate differs → "warn" (most severe, biggest drift driver).
      - bits_per_sample differs → "warn" (the v2.10.5 field repro).
      - channels differs at the same other-fields → "ok" but include a
        soft note (different channel counts don't drift; mic mono +
        speakers stereo is normal and safe).
      - all match → "ok".
    """
    if not mic or not loopback:
        return {
            "ok": True,
            "level": "unknown",
            "reason": "Could not read the audio format from one or both "
                      "devices. The drift check is only available on "
                      "Windows with pycaw installed.",
            "mic_format": mic,
            "loopback_format": loopback,
            "fix_hint": None,
        }

    problems = []
    if mic["sample_rate"] != loopback["sample_rate"]:
        problems.append(
            f"sample rate (mic {mic['sample_rate']} Hz vs. "
            f"speakers {loopback['sample_rate']} Hz)")
    if mic["bits_per_sample"] != loopback["bits_per_sample"]:
        problems.append(
            f"bit depth (mic {mic['bits_per_sample']}-bit vs. "
            f"speakers {loopback['bits_per_sample']}-bit)")

    if not problems:
        return {
            "ok": True,
            "level": "ok",
            "reason": None,
            "mic_format": mic,
            "loopback_format": loopback,
            "fix_hint": None,
        }

    return {
        "ok": False,
        "level": "warn",
        "reason": (
            "Microphone and System Audio are configured at different "
            "default formats: " + ", ".join(problems) + ". The Windows "
            "audio engine resamples each side independently, which "
            "makes the mic and system-audio tracks drift apart over "
            "long recordings."),
        "mic_format": mic,
        "loopback_format": loopback,
        "fix_hint": (
            "Open Sound Control Panel → set BOTH devices to the same "
            "Default Format on the Advanced tab "
            f"(suggested: {mic['sample_rate']} Hz, "
            f"{min(mic['bits_per_sample'], loopback['bits_per_sample'])}-bit, "
            "2 channel)."),
    }
