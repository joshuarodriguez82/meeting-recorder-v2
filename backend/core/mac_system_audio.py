"""
Recording "what the Mac is playing" without BlackHole.

WHY
---
On Windows every output device can be recorded directly (WASAPI
loopback). On macOS the app could only record a virtual loopback driver
(BlackHole), which meant routing all output through a Multi-Output
Device — and when Bluetooth headphones connect, macOS switches output
straight to them, BlackHole receives silence, and the other participants
are silently not recorded (field report 2026-09-23).

macOS 13+ can capture system audio natively through ScreenCaptureKit:
the audio every app plays, whatever it is played through — built-in
speakers, Bluetooth headphones, USB, HDMI. No driver, no routing. The
first use asks for the "Screen & System Audio Recording" permission.
Only audio is kept; video frames are configured to the minimum ScK
allows and discarded.

SHAPE
-----
The pure parts — availability, PCM decoding, the permission message —
are plain functions, tested on any OS. ``ScreenCaptureAudio`` is the
thin PyObjC shell around them and only runs on a Mac.
"""

from __future__ import annotations

import platform as _platform
import sys
import threading
from typing import Callable, Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)

#: Index the System Audio list uses for this source. PortAudio indices
#: are never negative, so it cannot collide with a real device; the UI
#: and auto-record select devices by NAME, which is stable.
SYSTEM_AUDIO_INDEX = -2
SYSTEM_AUDIO_NAME = "System audio — all apps (no BlackHole needed)"

SAMPLE_RATE = 48000
CHANNELS = 2

# kAudioFormatFlagIsNonInterleaved — ScreenCaptureKit delivers planar
# float32 (one block per channel), but the flag is honoured either way.
_NON_INTERLEAVED = 1 << 5

# SCStreamErrorUserDeclined, raised when the permission is off.
_USER_DECLINED = -3801

PERMISSION_MESSAGE = (
    "Meeting Recorder needs permission to record system audio. Open "
    "System Settings → Privacy & Security → Screen & System Audio "
    "Recording, turn on Meeting Recorder, then stop and restart the "
    "recording.")


def _mac_version(release: Optional[str] = None) -> tuple:
    text = release if release is not None else _platform.mac_ver()[0]
    parts = []
    for p in (text or "").split(".")[:2]:
        try:
            parts.append(int(p))
        except ValueError:
            return ()
    return tuple(parts)


def is_supported(platform: Optional[str] = None,
                 release: Optional[str] = None) -> bool:
    """macOS 13 or later with the ScreenCaptureKit bindings installed."""
    if (platform or sys.platform) != "darwin":
        return False
    ver = _mac_version(release)
    if not ver or ver < (13,):
        return False
    try:
        import ScreenCaptureKit  # noqa: F401
        import CoreMedia  # noqa: F401
    except Exception as e:  # noqa: BLE001
        logger.info(f"Native system audio unavailable (bindings): {e}")
        return False
    return True


def decode_float_pcm(raw: bytes, frames: int, channels: int,
                     non_interleaved: bool) -> np.ndarray:
    """One CMSampleBuffer's float32 PCM → mono float32.

    Planar data is laid out channel after channel; interleaved data is
    frame after frame. A buffer whose size doesn't match the stated
    shape is decoded as mono rather than guessed at — a garbled channel
    split would be worse than a slightly louder mono.
    """
    samples = np.frombuffer(raw, dtype=np.float32)
    channels = max(1, int(channels or 1))
    if channels == 1 or samples.size != frames * channels or frames <= 0:
        return samples.copy()
    if non_interleaved:
        return samples.reshape(channels, frames).mean(axis=0)
    return samples.reshape(frames, channels).mean(axis=1)


def describe_start_error(error: object) -> str:
    """A start failure in the user's terms. Permission is the common
    one, and the only one with a fix the user can make."""
    code = getattr(error, "code", None)
    try:
        code = code() if callable(code) else code
    except Exception:  # noqa: BLE001
        code = None
    text = str(error or "")
    if code == _USER_DECLINED or "declined" in text.lower() \
            or str(_USER_DECLINED) in text:
        return PERMISSION_MESSAGE
    return f"System audio could not be started: {text or 'unknown error'}"


class ScreenCaptureAudio:
    """System audio via ScreenCaptureKit. ``on_block(mono_float32)`` is
    called from a ScreenCaptureKit queue for every buffer; keep it fast.
    """

    def __init__(self, on_block: Callable[[np.ndarray], None]):
        self._on_block = on_block
        self._stream = None
        self._output = None
        self.samplerate = SAMPLE_RATE

    def start(self, timeout_s: float = 10.0) -> None:
        import CoreMedia
        import ScreenCaptureKit as SCK

        content_ready = threading.Event()
        found = {}

        def _content(content, error):
            found["content"], found["error"] = content, error
            content_ready.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(
            _content)
        if not content_ready.wait(timeout_s):
            raise RuntimeError("System audio could not be started: macOS "
                               "did not answer in time")
        if found.get("error") is not None or found.get("content") is None:
            raise RuntimeError(describe_start_error(found.get("error")))
        displays = found["content"].displays()
        if not displays:
            raise RuntimeError("System audio could not be started: no "
                               "display to attach the capture to")

        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setCapturesAudio_(True)
        cfg.setSampleRate_(SAMPLE_RATE)
        cfg.setChannelCount_(CHANNELS)
        # Our own playback (none today) must never feed back in.
        cfg.setExcludesCurrentProcessAudio_(True)
        # Video is mandatory in a stream; make it as cheap as allowed.
        cfg.setWidth_(2)
        cfg.setHeight_(2)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))

        filt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
            displays[0], [])

        self._output = _output_class().alloc().init()
        self._output.on_block = self._on_block
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            filt, cfg, None)
        ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            self._output, SCK.SCStreamOutputTypeAudio, None, None)
        if not ok:
            raise RuntimeError(describe_start_error(err))

        started = threading.Event()
        start_err = {}

        def _started(error):
            start_err["error"] = error
            started.set()

        stream.startCaptureWithCompletionHandler_(_started)
        if not started.wait(timeout_s):
            raise RuntimeError("System audio could not be started: macOS "
                               "did not answer in time")
        if start_err.get("error") is not None:
            raise RuntimeError(describe_start_error(start_err["error"]))
        self._stream = stream
        logger.info("System audio started (ScreenCaptureKit, "
                    f"{SAMPLE_RATE} Hz)")

    def stop(self, timeout_s: float = 3.0) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        done = threading.Event()
        stream.stopCaptureWithCompletionHandler_(lambda error: done.set())
        if not done.wait(timeout_s):
            logger.warning("System audio did not stop in time — abandoning")
        self._output = None


_OUTPUT_CLASS = None


def _output_class():
    """The SCStreamOutput receiver, defined once per process: the
    Objective-C runtime refuses to register a class name twice, so a
    class declared inside start() would fail on the second recording."""
    global _OUTPUT_CLASS
    if _OUTPUT_CLASS is not None:
        return _OUTPUT_CLASS
    import objc
    import CoreMedia
    import ScreenCaptureKit as SCK
    from Foundation import NSObject

    class MRSystemAudioOutput(NSObject, protocols=[
            objc.protocolNamed("SCStreamOutput")]):
        on_block = None

        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
            if kind != SCK.SCStreamOutputTypeAudio or self.on_block is None:
                return
            try:
                self.on_block(_sample_buffer_to_mono(CoreMedia, sbuf))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"System audio buffer skipped: {e}")

    _OUTPUT_CLASS = MRSystemAudioOutput
    return _OUTPUT_CLASS


def _sample_buffer_to_mono(CoreMedia, sbuf) -> np.ndarray:
    """Pull float32 PCM out of a CMSampleBuffer and downmix it."""
    frames = int(CoreMedia.CMSampleBufferGetNumSamples(sbuf))
    block = CoreMedia.CMSampleBufferGetDataBuffer(sbuf)
    length = int(CoreMedia.CMBlockBufferGetDataLength(block))
    result = CoreMedia.CMBlockBufferCopyDataBytes(block, 0, length, None)
    raw = result[1] if isinstance(result, tuple) else result
    channels, non_interleaved = CHANNELS, True
    try:
        fmt = CoreMedia.CMSampleBufferGetFormatDescription(sbuf)
        asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(
            fmt)
        asbd = asbd[0] if isinstance(asbd, (list, tuple)) else asbd
        channels = int(asbd.mChannelsPerFrame)
        non_interleaved = bool(int(asbd.mFormatFlags) & _NON_INTERLEAVED)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"System audio format unreadable, assuming planar: {e}")
    return decode_float_pcm(bytes(raw), frames, channels, non_interleaved)
