import threading
from typing import Callable, List, Optional
import numpy as np
import sounddevice as sd
import pyaudiowpatch as pyaudio
from utils.logger import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
BLOCK_SIZE = 1024


def _get_wasapi_host_api_index() -> Optional[int]:
    """Find the Windows WASAPI host API index for sounddevice deduplication."""
    try:
        for i, api in enumerate(sd.query_hostapis()):
            if "WASAPI" in api.get("name", ""):
                return i
    except Exception:
        pass
    return None


def list_input_devices() -> List[dict]:
    devices = []
    wasapi_idx = _get_wasapi_host_api_index()
    seen_names = set()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            if wasapi_idx is not None and dev.get("hostapi") != wasapi_idx:
                continue
            name = dev["name"]
            if name in seen_names:
                continue
            seen_names.add(name)
            devices.append({
                "index": idx,
                "name": name,
                "max_input_channels": dev["max_input_channels"],
                "default_samplerate": dev["default_samplerate"],
            })
    if not devices:
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append({
                    "index": idx,
                    "name": dev["name"],
                    "max_input_channels": dev["max_input_channels"],
                    "default_samplerate": dev["default_samplerate"],
                })
    return devices


def list_output_devices() -> List[dict]:
    """List WASAPI loopback devices for system audio capture via pyaudiowpatch."""
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

                # Try multiple configurations — some drivers reject
                # our preferred blocksize or sample rate combination.
                attempts = [
                    # Most compatible first: let driver pick blocksize + high latency
                    dict(samplerate=native_sr, blocksize=0, latency="high"),
                    # Try low latency with default blocksize
                    dict(samplerate=native_sr, blocksize=0, latency="low"),
                    # Try our BLOCK_SIZE
                    dict(samplerate=native_sr, blocksize=BLOCK_SIZE, latency="high"),
                    # Try 16kHz fallback (widely supported, matches target)
                    dict(samplerate=16000, blocksize=0, latency="high"),
                    # Try 44100 fallback
                    dict(samplerate=44100, blocksize=0, latency="high"),
                ]

                mic_stream = None
                last_err = None
                for i, cfg in enumerate(attempts):
                    try:
                        logger.info(f"  Mic attempt {i+1}: {cfg}")
                        mic_stream = sd.InputStream(
                            device=self._mic_idx,
                            channels=channels,
                            samplerate=cfg["samplerate"],
                            blocksize=cfg["blocksize"],
                            latency=cfg["latency"],
                            dtype="float32",
                            callback=self._mic_callback,
                        )
                        mic_stream.start()
                        self.actual_sr = cfg["samplerate"]
                        logger.info(
                            f"  ✓ Mic stream opened: sr={cfg['samplerate']}Hz "
                            f"ch={channels} latency={cfg['latency']} "
                            f"blocksize={cfg['blocksize']}")
                        break
                    except Exception as e:
                        last_err = e
                        logger.warning(f"  ✗ Attempt {i+1} failed: {e}")
                        continue

                if mic_stream is None:
                    raise RuntimeError(
                        f"All mic configurations failed. Last error: {last_err}. "
                        f"The device may be in use by another app, disconnected, "
                        f"or driver may need a restart. Try selecting a different mic.")

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
                            logger.info(f"  ✓ Loopback opened with buffer={buf}")
                            break
                        except Exception as e:
                            last_err = e
                            logger.warning(f"  ✗ Loopback buffer={buf} failed: {e}")
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
        # Stop mic/sounddevice streams first
        self._close_all_streams()
        # Then stop loopback
        if hasattr(self, '_loopback_thread') and self._loopback_thread is not None:
            self._loopback_thread.join(timeout=1.0)
            self._loopback_thread = None
        if self._pa_stream is not None:
            try:
                self._pa_stream.stop_stream()
                self._pa_stream.close()
            except Exception as e:
                logger.warning(f"Error closing loopback stream: {e}")
            self._pa_stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
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