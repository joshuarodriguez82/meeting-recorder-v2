"""
Audio capture: mic + system audio (loopback).

Two platform implementations behind one interface:

- Windows: WASAPI loopback via pyaudiowpatch. The user's chosen system
  audio device is opened as an "input" by the WASAPI loopback feature,
  which gives a real-time copy of whatever the OS is playing.

- macOS: requires the user to install BlackHole (`brew install blackhole-2ch`)
  or another virtual loopback driver. The OS has no first-party loopback
  API for non-screen-recording contexts, so we list any input device whose
  name suggests it's a loopback (BlackHole / Loopback / Soundflower /
  AggregateDevice) and open it as an ordinary sounddevice InputStream.
  No pyaudiowpatch dependency on this path.

Linux is untested but should follow the macOS path through PulseAudio /
PipeWire (loopback exposed as a normal capture source).
"""

import os
import sys
import threading
import time
from typing import Callable, List, Optional
import numpy as np
import sounddevice as sd

from utils.logger import get_logger

logger = get_logger(__name__)

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# pyaudiowpatch is Windows-only; importing it on Mac/Linux will ImportError.
# We swallow that and switch to the sounddevice-based loopback path.
pyaudio = None  # type: ignore
if IS_WINDOWS:
    try:
        import pyaudiowpatch as _pyaudio  # type: ignore
        pyaudio = _pyaudio
    except ImportError as e:
        logger.warning(f"pyaudiowpatch not available on Windows: {e}. "
                       "System audio loopback will be unavailable.")

SAMPLE_RATE = 16000
BLOCK_SIZE = 1024

# sd.query_devices() can take 1-3s on systems with a lot of audio hardware
# (Bluetooth stack enumeration is expensive). Cache the friendly list for
# a minute so UI refreshes are instant and we don't pay that cost repeatedly.
_DEVICE_CACHE_LOCK = threading.Lock()
_DEVICE_CACHE_TTL = 60
_input_cache: Optional[tuple[float, List[dict]]] = None
_output_cache: Optional[tuple[float, List[dict]]] = None

# Substrings that identify virtual loopback devices on macOS. The user
# installs at least one of these (BlackHole is the recommended free
# option) and picks it from the System Audio dropdown to capture audio
# the other meeting participants are playing.
_MAC_LOOPBACK_NAME_HINTS = (
    "blackhole",
    "loopback",
    "soundflower",
    "vb-cable",
    "aggregate",   # user-created Aggregate Device
    "multi-output", # not technically input but shows what user routed
)


def _get_wasapi_host_api_index() -> Optional[int]:
    """Find the Windows WASAPI host API index for sounddevice deduplication."""
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api.get("name", ""):
                return i
    except Exception:
        pass
    return None


def _clean_device_name(raw: str) -> Optional[str]:
    """
    Normalize and filter device names. Returns None for junk entries we
    never want to show (raw Windows registry paths, empty names, etc.).
    """
    if not raw:
        return None
    name = raw.strip()
    if not name:
        return None
    # WDM-KS entries look like: "Input (@System32\drivers\bthhfenum.sys,#2;%1 Hands-Free%0 ;(Jabra Elite 85t))"
    # These are raw PnP path strings — friendly names live in WASAPI/MME.
    if "@System32\\" in name or "@system32\\" in name:
        return None
    if name.startswith("Input (") and "\\" in name:
        return None
    if name.startswith("Output (") and "\\" in name:
        return None
    if name in ("Input ()", "Output ()"):
        return None
    return name


def list_input_devices() -> List[dict]:
    """
    One clean entry per physical mic. Cached for 60s.

    Windows: dedupes across WASAPI/MME/DirectSound, prefers WASAPI.
    macOS: walks Core Audio host devices and skips loopback drivers
    (those go in list_output_devices instead so the UI distinguishes
    "what mic captures me" from "what captures the other side").
    """
    global _input_cache
    with _DEVICE_CACHE_LOCK:
        if _input_cache is not None:
            ts, val = _input_cache
            if time.time() - ts < _DEVICE_CACHE_TTL:
                return val

    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []

    if IS_WINDOWS:
        ALLOWED_APIS = ("WASAPI", "MME", "DirectSound")

        def api_rank(api_name: str) -> int:
            if "WASAPI" in api_name: return 0
            if "MME" in api_name: return 1
            if "DirectSound" in api_name: return 2
            return 99

        # Keep the best-ranked entry per cleaned device name
        best: dict[str, tuple[int, int, dict]] = {}
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            api_idx = dev.get("hostapi", -1)
            api_name = hostapis[api_idx].get("name", "") if 0 <= api_idx < len(hostapis) else ""
            if not any(tag in api_name for tag in ALLOWED_APIS):
                continue
            clean = _clean_device_name(dev.get("name", ""))
            if not clean:
                continue
            rank = api_rank(api_name)
            if clean not in best or rank < best[clean][0]:
                best[clean] = (rank, idx, dev)

        devices = [
            {
                "index": idx,
                "name": name,
                "max_input_channels": dev["max_input_channels"],
                "default_samplerate": dev["default_samplerate"],
            }
            for name, (_, idx, dev) in best.items()
        ]
    else:
        # macOS / Linux: Core Audio (Mac) or ALSA/PulseAudio (Linux) names
        # are already user-friendly. Skip loopback drivers — those go in
        # the System Audio dropdown so we don't show BlackHole twice.
        devices = []
        seen = set()
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            name = (dev.get("name") or "").strip()
            if not name:
                continue
            low = name.lower()
            if any(hint in low for hint in _MAC_LOOPBACK_NAME_HINTS):
                continue
            if name in seen:
                continue
            seen.add(name)
            devices.append({
                "index": idx,
                "name": name,
                "max_input_channels": dev["max_input_channels"],
                "default_samplerate": dev["default_samplerate"],
            })

    devices.sort(key=lambda d: d["name"].lower())
    with _DEVICE_CACHE_LOCK:
        _input_cache = (time.time(), devices)
    return devices


def invalidate_device_cache():
    global _input_cache, _output_cache
    with _DEVICE_CACHE_LOCK:
        _input_cache = None
        _output_cache = None


def _find_device_alternatives(primary_idx: int) -> List[int]:
    """
    Given a device index, return other indices that refer to the SAME
    physical device via other host APIs — MME / DirectSound / WDM-KS —
    ranked from most-to-least compatible. Used as fallbacks when the
    primary (WASAPI) entry refuses to open. Windows-only behaviour;
    on macOS / Linux every device only appears under one host API so
    this returns [].
    """
    if not IS_WINDOWS:
        return []
    try:
        hostapis = sd.query_hostapis()
        primary = sd.query_devices(primary_idx)
    except Exception:
        return []
    name = primary.get("name", "")
    if not name:
        return []

    def api_rank(api_name: str) -> int:
        if "MME" in api_name: return 0
        if "DirectSound" in api_name: return 1
        if "WASAPI" in api_name: return 2
        if "WDM-KS" in api_name: return 3
        return 4

    alternatives = []
    for idx, dev in enumerate(sd.query_devices()):
        if idx == primary_idx:
            continue
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if dev.get("name") != name:
            continue
        api_idx = dev.get("hostapi", -1)
        api_name = ""
        if 0 <= api_idx < len(hostapis):
            api_name = hostapis[api_idx].get("name", "") or ""
        alternatives.append((api_rank(api_name), idx))
    alternatives.sort(key=lambda x: x[0])
    return [idx for _, idx in alternatives]


def list_output_devices() -> List[dict]:
    """
    List loopback / system-audio devices. Cached.

    Windows: WASAPI loopback via pyaudiowpatch — every output device
    auto-exposes a loopback "input" with `isLoopbackDevice` = True.

    macOS / Linux: enumerate sounddevice inputs whose name suggests they
    are virtual loopback drivers (BlackHole etc.). The user must install
    one — see MAC_SETUP.md. If none are present the list is empty and
    the UI degrades to mic-only capture.
    """
    global _output_cache
    with _DEVICE_CACHE_LOCK:
        if _output_cache is not None:
            ts, val = _output_cache
            if time.time() - ts < _DEVICE_CACHE_TTL:
                return val

    devices: list[dict] = []

    if IS_WINDOWS and pyaudio is not None:
        try:
            p = pyaudio.PyAudio()
            wasapi_info = None
            for i in range(p.get_host_api_count()):
                api = p.get_host_api_info_by_index(i)
                if api["name"] == "Windows WASAPI":
                    wasapi_info = api
                    break

            if wasapi_info:
                for i in range(wasapi_info["deviceCount"]):
                    dev = p.get_device_info_by_host_api_device_index(
                        wasapi_info["index"], i)
                    if dev.get("isLoopbackDevice", False):
                        devices.append({
                            "index": dev["index"],
                            "name": dev["name"],
                            "channels": int(dev["maxInputChannels"]),
                            "default_samplerate": dev["defaultSampleRate"],
                        })
            p.terminate()
        except Exception as e:
            logger.warning(f"Could not enumerate loopback devices: {e}")
    else:
        # macOS / Linux: list any input device whose name matches a known
        # virtual-loopback driver. We use sounddevice indices directly so
        # AudioCapture can open them as ordinary InputStreams below.
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                name = (dev.get("name") or "").strip()
                if not name:
                    continue
                low = name.lower()
                if not any(hint in low for hint in _MAC_LOOPBACK_NAME_HINTS):
                    continue
                devices.append({
                    "index": idx,
                    "name": name,
                    "channels": int(dev["max_input_channels"]),
                    "default_samplerate": dev["default_samplerate"],
                })
        except Exception as e:
            logger.warning(f"Could not enumerate loopback devices: {e}")

    with _DEVICE_CACHE_LOCK:
        _output_cache = (time.time(), devices)
    return devices


class AudioCapture:

    def __init__(
        self,
        mic_device_index: Optional[int],
        output_device_index: Optional[int],
        on_chunk: Callable[[np.ndarray], None],
        loopback_wav_path: Optional[str] = None,
    ):
        self._mic_idx = mic_device_index
        self._out_idx = output_device_index
        self._on_chunk = on_chunk
        self._streams: List[sd.InputStream] = []
        self._lock = threading.Lock()
        self._running = False
        self._chunk_count = 0
        self.actual_sr = SAMPLE_RATE
        # Windows-only PyAudio handle; unused on Mac (we use sounddevice
        # for both mic and loopback there).
        self._pa = None
        self._pa_stream = None
        self._loopback_wav_path = loopback_wav_path
        self._loopback_writer: Optional[object] = None
        # macOS path uses a sounddevice InputStream for loopback. Tracked
        # separately from the mic stream so we can stop them independently.
        self._loopback_sd_stream: Optional[sd.InputStream] = None
        self._loopback_thread: Optional[threading.Thread] = None
        self._loopback_sr: int = SAMPLE_RATE
        self._loopback_channels: int = 1

    def start(self) -> None:
        self._running = True
        self._chunk_count = 0
        logger.info(f"Starting capture: mic={self._mic_idx}, output={self._out_idx}")

        try:
            if self._mic_idx is not None:
                dev_info = sd.query_devices(self._mic_idx)
                native_sr = int(dev_info["default_samplerate"])
                max_ch = int(dev_info["max_input_channels"])
                channels = min(2, max_ch)
                self.actual_sr = native_sr
                api_name = sd.query_hostapis(dev_info["hostapi"])["name"]
                logger.info(
                    f"Mic device: [{self._mic_idx}] {dev_info['name']} | "
                    f"api={api_name} ch={channels}/{max_ch} sr={native_sr}")

                attempts = [
                    dict(samplerate=native_sr, blocksize=0, latency="high"),
                    dict(samplerate=native_sr, blocksize=0, latency="low"),
                    dict(samplerate=native_sr, blocksize=BLOCK_SIZE, latency="high"),
                    dict(samplerate=48000, blocksize=0, latency="high"),
                    dict(samplerate=44100, blocksize=0, latency="high"),
                    dict(samplerate=16000, blocksize=0, latency="high"),
                ]
                seen_cfgs = set()
                unique_attempts = []
                for cfg in attempts:
                    key = (cfg["samplerate"], cfg["blocksize"], cfg["latency"])
                    if key not in seen_cfgs:
                        seen_cfgs.add(key)
                        unique_attempts.append(cfg)

                # Try the user-selected device first; if every config fails,
                # fall back to the SAME physical mic on other host APIs
                # (Windows only — _find_device_alternatives returns [] on Mac).
                device_candidates = [self._mic_idx] + _find_device_alternatives(self._mic_idx)

                mic_stream = None
                mic_started = False
                last_err = None
                for dev_idx in device_candidates:
                    try:
                        dev_info_alt = sd.query_devices(dev_idx)
                        api_name_alt = sd.query_hostapis(dev_info_alt["hostapi"])["name"]
                        logger.info(
                            f" Trying device [{dev_idx}] '{dev_info_alt['name']}' via {api_name_alt}")
                    except Exception:
                        api_name_alt = "?"

                    for i, cfg in enumerate(unique_attempts):
                        candidate = None
                        try:
                            logger.info(f"  Mic attempt {i+1}: {cfg}")
                            candidate = sd.InputStream(
                                device=dev_idx,
                                channels=channels,
                                samplerate=cfg["samplerate"],
                                blocksize=cfg["blocksize"],
                                latency=cfg["latency"],
                                dtype="float32",
                                callback=self._mic_callback,
                            )
                            candidate.start()
                            mic_stream = candidate
                            mic_started = True
                            self.actual_sr = cfg["samplerate"]
                            self._mic_idx = dev_idx
                            logger.info(
                                f"  [OK] Mic stream opened: sr={cfg['samplerate']}Hz "
                                f"ch={channels} latency={cfg['latency']} "
                                f"blocksize={cfg['blocksize']} api={api_name_alt}")
                            break
                        except Exception as e:
                            last_err = e
                            logger.warning(f"  [FAIL] Attempt {i+1} failed: {e}")
                            if candidate is not None:
                                try:
                                    candidate.close()
                                except Exception:
                                    pass
                            continue
                    if mic_started:
                        break

                if not mic_started:
                    if IS_MACOS:
                        hint = (
                            "On macOS this usually means the app hasn't been "
                            "granted microphone access. Open System Settings → "
                            "Privacy & Security → Microphone and toggle "
                            "Meeting Recorder on. Then quit and relaunch.")
                    else:
                        hint = (
                            "The device may be in use by another app (Teams, "
                            "Zoom, Windows Camera), disconnected, or the driver "
                            "may need a restart. Try closing other apps or "
                            "picking a different mic.")
                    raise RuntimeError(
                        f"All mic configurations failed. Last error: {last_err}. "
                        f"{hint}")

                self._streams.append(mic_stream)

            if self._out_idx is not None:
                if IS_WINDOWS and pyaudio is not None:
                    self._start_loopback_windows()
                else:
                    self._start_loopback_macos()

        except Exception:
            self._close_all_streams()
            self._running = False
            raise

    def _start_loopback_windows(self) -> None:
        """WASAPI loopback path (pyaudiowpatch)."""
        try:
            self._pa = pyaudio.PyAudio()
            dev_info = self._pa.get_device_info_by_index(self._out_idx)
            self._loopback_channels = int(dev_info["maxInputChannels"])
            self._loopback_sr = int(dev_info["defaultSampleRate"])
            logger.info(
                f"Loopback device (WASAPI): [{self._out_idx}] {dev_info['name']} "
                f"ch={self._loopback_channels} sr={self._loopback_sr}")

            buffer_attempts = [0, 1024, 4096, 2048]
            opened = False
            last_err = None
            for buf in buffer_attempts:
                try:
                    logger.info(f"  Loopback attempt buffer={buf}")
                    self._pa_stream = self._pa.open(
                        format=pyaudio.paFloat32,
                        channels=self._loopback_channels,
                        rate=self._loopback_sr,
                        input=True,
                        input_device_index=self._out_idx,
                        frames_per_buffer=buf if buf else 1024,
                    )
                    opened = True
                    logger.info(f"  [OK] Loopback opened with buffer={buf}")
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"  [FAIL] Loopback buffer={buf} failed: {e}")
                    continue
            if not opened:
                raise last_err or RuntimeError("No working loopback config")

            self._loopback_thread = threading.Thread(
                target=self._loopback_reader_pa, daemon=True)
            self._loopback_thread.start()
            logger.info("System audio stream started (WASAPI)")
        except Exception as e:
            logger.warning(f"System audio capture unavailable: {e}. Mic only.")
            self._out_idx = None
            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None

    def _start_loopback_macos(self) -> None:
        """
        BlackHole / virtual-loopback path. The "loopback" device is just an
        ordinary sounddevice input here — whatever the OS routes into it
        (which the user configures in Audio MIDI Setup or via System
        Settings → Sound → Output) appears as audio frames.
        """
        try:
            dev_info = sd.query_devices(self._out_idx)
            self._loopback_channels = min(2, int(dev_info["max_input_channels"]))
            self._loopback_sr = int(dev_info["default_samplerate"])
            logger.info(
                f"Loopback device (sounddevice): [{self._out_idx}] {dev_info['name']} "
                f"ch={self._loopback_channels} sr={self._loopback_sr}")

            self._loopback_sd_stream = sd.InputStream(
                device=self._out_idx,
                channels=self._loopback_channels,
                samplerate=self._loopback_sr,
                blocksize=0,
                latency="high",
                dtype="float32",
                callback=self._loopback_callback_sd,
            )
            self._loopback_sd_stream.start()
            # Open the WAV writer in a thread so the callback path stays
            # tight. The same callback writes frames into the writer.
            self._loopback_thread = threading.Thread(
                target=self._loopback_writer_sd, daemon=True)
            self._loopback_thread.start()
            logger.info("System audio stream started (sounddevice loopback)")
        except Exception as e:
            logger.warning(
                f"System audio capture unavailable on this device: {e}. "
                f"Mic only. (On macOS, install BlackHole and pick it from "
                f"the System Audio dropdown — see MAC_SETUP.md.)")
            self._out_idx = None
            if self._loopback_sd_stream is not None:
                try:
                    self._loopback_sd_stream.close()
                except Exception:
                    pass
                self._loopback_sd_stream = None

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._out_buffer = None

        def _run_with_timeout(fn, timeout_s, label):
            done = threading.Event()
            def _wrap():
                try:
                    fn()
                finally:
                    done.set()
            t = threading.Thread(target=_wrap, daemon=True)
            t.start()
            if not done.wait(timeout_s):
                logger.warning(f"{label} did not finish in {timeout_s}s — abandoning")
                return False
            return True

        # Stop sounddevice mic stream (and the sd loopback if we used it).
        _run_with_timeout(self._close_all_streams, 3.0, "close_all_streams")

        # Mac path: also stop the dedicated loopback sd.InputStream.
        if self._loopback_sd_stream is not None:
            sd_stream = self._loopback_sd_stream
            self._loopback_sd_stream = None
            def _stop_sd():
                try:
                    sd_stream.stop()
                    sd_stream.close()
                except Exception as e:
                    logger.warning(f"Error closing sd loopback stream: {e}")
            _run_with_timeout(_stop_sd, 2.0, "loopback_sd.stop")

        # Wake the loopback writer thread out of its queue wait.
        try:
            self._loopback_q_putter(None)
        except Exception:
            pass

        if self._loopback_thread is not None:
            self._loopback_thread.join(timeout=2.0)
            self._loopback_thread = None

        # Close pyaudio loopback stream — Windows path.
        if self._pa_stream is not None:
            pa_stream = self._pa_stream
            self._pa_stream = None
            def _stop_pa():
                try:
                    pa_stream.stop_stream()
                except Exception as e:
                    logger.warning(f"Error stopping loopback stream: {e}")
            _run_with_timeout(_stop_pa, 2.0, "pa_stream.stop_stream")
            def _close_pa():
                try:
                    pa_stream.close()
                except Exception as e:
                    logger.warning(f"Error closing loopback stream: {e}")
            _run_with_timeout(_close_pa, 2.0, "pa_stream.close")

        if self._pa is not None:
            pa = self._pa
            self._pa = None
            _run_with_timeout(
                lambda: pa.terminate(), 2.0, "pa.terminate"
            )
        logger.info(f"Audio capture stopped. Total chunks captured: {self._chunk_count}")

    def _close_all_streams(self) -> None:
        for stream in self._streams:
            try:
                stream.stop()
                stream.close()
            except Exception as e:
                logger.warning(f"Error closing stream: {e}")
        self._streams.clear()

    def _mic_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        try:
            if status:
                logger.warning(f"Mic stream status: {status}")
            chunk = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0].copy()
            self._chunk_count += 1
            if self._chunk_count % 100 == 0:
                logger.info(f"Mic chunks received: {self._chunk_count}")
            self._safe_invoke(chunk)
        except Exception as e:
            logger.error(f"Error in mic callback: {e}")

    def _loopback_reader_pa(self):
        """Blocking-read loop for WASAPI loopback (Windows pyaudiowpatch path)."""
        import soundfile as sf
        logged_first = False
        try:
            writer = sf.SoundFile(
                self._loopback_wav_path, mode="w",
                samplerate=self._loopback_sr, channels=1, subtype="FLOAT")
        except Exception as e:
            logger.error(f"Could not open loopback WAV: {e}")
            return

        while self._running:
            try:
                if self._pa_stream is None or not self._pa_stream.is_active():
                    break
                in_data = self._pa_stream.read(BLOCK_SIZE, exception_on_overflow=False)
                audio = np.frombuffer(in_data, dtype=np.float32)
                if audio.size > BLOCK_SIZE:
                    channels = audio.size // BLOCK_SIZE
                    audio = audio.reshape(BLOCK_SIZE, channels).mean(axis=1)
                writer.write(audio)
                if not logged_first:
                    logged_first = True
                    logger.info("Loopback audio flowing")
            except OSError:
                break
            except Exception as e:
                if self._running:
                    logger.error(f"Loopback read error: {e}")
                break

        writer.close()
        logger.info(f"Loopback WAV closed: {self._loopback_wav_path}")

    # ── macOS / sounddevice loopback path ──────────────────────────────
    #
    # We can't write soundfile from inside a sounddevice callback (it does
    # disk I/O which would underrun the audio thread). Instead the callback
    # pushes blocks into a queue; a worker thread drains them and writes.
    def _loopback_callback_sd(self, indata: np.ndarray, frames: int, time_, status) -> None:
        try:
            if status:
                logger.warning(f"Loopback stream status: {status}")
            block = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
            self._loopback_q_putter(np.copy(block))
        except Exception as e:
            logger.error(f"Error in loopback callback: {e}")

    # Lazy-initialised queue. We don't import queue.Queue until first use
    # so the Windows path doesn't pay the import cost.
    _loopback_queue = None  # type: ignore[assignment]

    def _loopback_q_putter(self, item):
        if self._loopback_queue is None:
            import queue
            self._loopback_queue = queue.Queue(maxsize=1024)
        # Drop oldest on overflow rather than block the audio callback.
        try:
            self._loopback_queue.put_nowait(item)
        except Exception:
            try:
                _ = self._loopback_queue.get_nowait()
                self._loopback_queue.put_nowait(item)
            except Exception:
                pass

    def _loopback_writer_sd(self):
        """Drain the loopback queue and write frames to a WAV on disk."""
        import soundfile as sf
        if self._loopback_queue is None:
            import queue
            self._loopback_queue = queue.Queue(maxsize=1024)
        try:
            writer = sf.SoundFile(
                self._loopback_wav_path, mode="w",
                samplerate=self._loopback_sr, channels=1, subtype="FLOAT")
        except Exception as e:
            logger.error(f"Could not open loopback WAV: {e}")
            return
        logged_first = False
        try:
            while self._running:
                try:
                    block = self._loopback_queue.get(timeout=0.5)
                except Exception:
                    continue
                if block is None:
                    break
                writer.write(block)
                if not logged_first:
                    logged_first = True
                    logger.info("Loopback audio flowing (sounddevice)")
        finally:
            try:
                writer.close()
            except Exception:
                pass
            logger.info(f"Loopback WAV closed: {self._loopback_wav_path}")

    def _safe_invoke(self, chunk: np.ndarray) -> None:
        try:
            self._on_chunk(chunk)
        except Exception as e:
            logger.error(f"Error in on_chunk callback: {e}")
