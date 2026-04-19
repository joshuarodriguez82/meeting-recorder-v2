import threading
import time
from typing import Callable, List, Optional
import numpy as np
import sounddevice as sd
import pyaudiowpatch as pyaudio
from utils.logger import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
BLOCK_SIZE = 1024

# sd.query_devices() can take 1-3s on systems with a lot of audio hardware
# (Bluetooth stack enumeration is expensive). Cache the friendly list for
# a minute so UI refreshes are instant and we don't pay that cost repeatedly.
_DEVICE_CACHE_LOCK = threading.Lock()
_DEVICE_CACHE_TTL = 60
_input_cache: Optional[tuple[float, List[dict]]] = None
_output_cache: Optional[tuple[float, List[dict]]] = None


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
    # "Input ()" with nothing in parens — orphan virtual device
    if name in ("Input ()", "Output ()"):
        return None
    # If the device has a friendly form in parens at the end like
    # "Headset (Jabra Elite 85t)", the part in the last parens is the
    # user-friendly label — but we only keep the whole string if it's
    # human-readable.
    return name


def list_input_devices() -> List[dict]:
    """
    One clean entry per physical mic. Cached for 60s.
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

    # Only these three host APIs ever appear in the dropdown.
    ALLOWED_APIS = ("WASAPI", "MME", "DirectSound")

    def api_rank(api_name: str) -> int:
        if "WASAPI" in api_name: return 0
        if "MME" in api_name: return 1
        if "DirectSound" in api_name: return 2
        return 99  # filtered out below

    # Keep the best-ranked entry per cleaned device name
    best: dict[str, tuple[int, int, dict]] = {}
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        api_idx = dev.get("hostapi", -1)
        api_name = hostapis[api_idx].get("name", "") if 0 <= api_idx < len(hostapis) else ""
        if not any(tag in api_name for tag in ALLOWED_APIS):
            continue  # skip WDM-KS and anything exotic
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
    primary (WASAPI) entry refuses to open.
    """
    try:
        hostapis = sd.query_hostapis()
        primary = sd.query_devices(primary_idx)
    except Exception:
        return []
    name = primary.get("name", "")
    if not name:
        return []

    def api_rank(api_name: str) -> int:
        if "MME" in api_name: return 0         # most forgiving
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
    """List WASAPI loopback devices for system audio capture via pyaudiowpatch. Cached."""
    global _output_cache
    with _DEVICE_CACHE_LOCK:
        if _output_cache is not None:
            ts, val = _output_cache
            if time.time() - ts < _DEVICE_CACHE_TTL:
                return val
    devices = []
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
        self._pa: Optional[pyaudio.PyAudio] = None
        self._pa_stream = None
        self._loopback_wav_path = loopback_wav_path
        self._loopback_writer: Optional[object] = None

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

                # Try multiple configurations. Order matters: start with the
                # device's native sample rate (the most likely to work), then
                # variations. ASCII-only log markers (cp1252 terminals choke
                # on unicode like ✓/✗).
                attempts = [
                    dict(samplerate=native_sr, blocksize=0, latency="high"),
                    dict(samplerate=native_sr, blocksize=0, latency="low"),
                    dict(samplerate=native_sr, blocksize=BLOCK_SIZE, latency="high"),
                    dict(samplerate=48000, blocksize=0, latency="high"),
                    dict(samplerate=44100, blocksize=0, latency="high"),
                    dict(samplerate=16000, blocksize=0, latency="high"),
                ]
                # Dedup while preserving order
                seen_cfgs = set()
                unique_attempts = []
                for cfg in attempts:
                    key = (cfg["samplerate"], cfg["blocksize"], cfg["latency"])
                    if key not in seen_cfgs:
                        seen_cfgs.add(key)
                        unique_attempts.append(cfg)

                # Try the user-selected device first; if every config fails,
                # fall back to the SAME physical mic on other host APIs
                # (MME / DirectSound). This rescues drivers that refuse
                # WASAPI but open cleanly under MME.
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
                            # Update in case we fell back to an alternate host API
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
                    raise RuntimeError(
                        f"All mic configurations failed across every host API. "
                        f"Last error: {last_err}. The device may be in use by "
                        f"another app (Teams, Zoom, Windows Camera), disconnected, "
                        f"or the driver may need a restart. Try closing other "
                        f"apps or picking a different mic.")

                self._streams.append(mic_stream)

            if self._out_idx is not None:
                try:
                    self._pa = pyaudio.PyAudio()
                    dev_info = self._pa.get_device_info_by_index(self._out_idx)
                    self._loopback_channels = int(dev_info["maxInputChannels"])
                    self._loopback_sr = int(dev_info["defaultSampleRate"])
                    logger.info(
                        f"Loopback device: [{self._out_idx}] {dev_info['name']} "
                        f"ch={self._loopback_channels} sr={self._loopback_sr}")

                    # Try different buffer sizes — some drivers are picky
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

                    # Use a dedicated thread for blocking reads
                    self._loopback_thread = threading.Thread(
                        target=self._loopback_reader, daemon=True)
                    self._loopback_thread.start()
                    logger.info(f"System audio stream started")
                except Exception as e:
                    logger.warning(f"System audio capture unavailable: {e}. Mic only.")
                    self._out_idx = None
                    if self._pa:
                        self._pa.terminate()
                        self._pa = None

        except Exception as e:
            self._close_all_streams()
            self._running = False
            raise

    def stop(self) -> None:
        self._running = False
        # Clear output buffer so mic callback doesn't merge stale loopback data
        with self._lock:
            self._out_buffer = None

        def _run_with_timeout(fn, timeout_s, label):
            """Run fn() in a daemon thread, return whether it finished in time."""
            done = threading.Event()
            err: List[Exception] = []
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

        # Stop sounddevice streams (mic) with a hard cap so a bad driver
        # can't wedge the stop request.
        _run_with_timeout(self._close_all_streams, 3.0, "close_all_streams")

        # Join loopback reader thread
        if hasattr(self, '_loopback_thread') and self._loopback_thread is not None:
            self._loopback_thread.join(timeout=2.0)
            self._loopback_thread = None

        # Close pyaudio loopback stream — stop_stream() is the real WASAPI
        # hang risk, so time-box it and fall through to close() regardless.
        if self._pa_stream is not None:
            pa_stream = self._pa_stream
            self._pa_stream = None  # prevent reader thread from touching it
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

    def _loopback_reader(self):
        """Blocking-read loop for WASAPI loopback — writes to separate WAV file."""
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
                break  # Stream closed during shutdown — normal
            except Exception as e:
                if self._running:
                    logger.error(f"Loopback read error: {e}")
                break

        writer.close()
        logger.info(f"Loopback WAV closed: {self._loopback_wav_path}")

    def _safe_invoke(self, chunk: np.ndarray) -> None:
        try:
            self._on_chunk(chunk)
        except Exception as e:
            logger.error(f"Error in on_chunk callback: {e}")