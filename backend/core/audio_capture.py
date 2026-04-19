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
                channels = min(2, int(dev_info["max_input_channels"]))
                self.actual_sr = native_sr
                logger.info(f"Mic device: {dev_info['name']}, sr={native_sr}, ch={channels}")
                mic_stream = sd.InputStream(
                    device=self._mic_idx,
                    channels=channels,
                    samplerate=native_sr,
                    blocksize=BLOCK_SIZE,
                    dtype="float32",
                    callback=self._mic_callback,
                )
                mic_stream.start()
                self._streams.append(mic_stream)
                logger.info(f"Mic stream started at {native_sr}Hz, {channels}ch")

            if self._out_idx is not None:
                try:
                    self._pa = pyaudio.PyAudio()
                    dev_info = self._pa.get_device_info_by_index(self._out_idx)
                    self._loopback_channels = int(dev_info["maxInputChannels"])
                    self._loopback_sr = int(dev_info["defaultSampleRate"])
                    logger.info(
                        f"System audio (WASAPI loopback): {dev_info['name']}, "
                        f"sr={self._loopback_sr}, ch={self._loopback_channels}")

                    self._pa_stream = self._pa.open(
                        format=pyaudio.paFloat32,
                        channels=self._loopback_channels,
                        rate=self._loopback_sr,
                        input=True,
                        input_device_index=self._out_idx,
                        frames_per_buffer=BLOCK_SIZE,
                    )
                    # Use a dedicated thread for blocking reads (more reliable than callbacks)
                    self._loopback_thread = threading.Thread(
                        target=self._loopback_reader, daemon=True)
                    self._loopback_thread.start()
                    logger.info(f"System audio stream started at {self._loopback_sr}Hz, {self._loopback_channels}ch")
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