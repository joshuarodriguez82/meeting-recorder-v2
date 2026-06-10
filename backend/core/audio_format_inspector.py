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
"""

from __future__ import annotations

import sys
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


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
    device_name: str, kind: str,
) -> Optional[dict]:
    """Return the shared-mode mix format Windows is using for the
    endpoint whose friendly name matches ``device_name``.

    Args:
        device_name: User-visible name as it appears in Sound Control
            Panel (e.g. "Microphone (AIRHUG 21)" or
            "Speakers (Realtek(R) Audio)"). Compared case-insensitively
            against ``IMMDevice.FriendlyName`` after stripping a leading
            "Microphone (" / "Speakers (" prefix that some host APIs
            inject (so callers can pass either form).
        kind: "input" or "output". Selects render vs. capture endpoints.

    Returns:
        ``{"sample_rate": int, "bits_per_sample": int, "channels": int}``
        on success.

        ``None`` when pycaw isn't installed, when this isn't Windows,
        when no matching endpoint is found, or when the underlying COM
        call fails. Callers should degrade gracefully — the sync-risk
        endpoint reports an "unknown" level for the missing side rather
        than a false-positive warning.
    """
    pycaw = _safe_import_pycaw()
    if pycaw is None:
        return None

    target = _canonical_device_name(device_name)
    if not target:
        return None

    try:
        from pycaw.pycaw import AudioUtilities  # type: ignore
    except Exception as e:
        logger.warning(f"pycaw import failed at call time: {e!r}")
        return None

    # AudioUtilities.GetAllDevices() walks render + capture endpoints in
    # one shot and returns objects exposing FriendlyName + the device
    # IMMDevice we can query for the mix format.
    try:
        all_devices = AudioUtilities.GetAllDevices()
    except Exception as e:
        logger.warning(f"WASAPI device enumeration failed: {e!r}")
        return None

    # We don't filter by flow direction here — pycaw's AudioDevice
    # doesn't reliably expose the EDataFlow, and a friendly-name match
    # against the same physical endpoint is unambiguous in practice
    # (mics aren't named "Speakers (...)" and vice versa). If a future
    # collision becomes an issue we can switch to MMDeviceEnumerator's
    # EnumAudioEndpoints with eRender / eCapture explicitly.
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
