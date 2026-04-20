import asyncio
from pathlib import Path

from faster_whisper import WhisperModel

from utils.logger import get_logger

logger = get_logger(__name__)


def _pick_device() -> tuple[str, str]:
    """
    Prefer CUDA when both a CUDA-enabled torch and a CUDA-capable GPU are
    present. Fall back to CPU otherwise. `torch.cuda.is_available()`
    returns False on the default CPU-only torch wheel shipped with the
    installer, so CPU-only machines (or users who haven't opted into CUDA
    torch via the GPU toggle) get the exact same init path as before —
    no regression, no GPU probe cost.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return ("cuda", "float16")
    except Exception:
        # torch missing / CPU-only wheel on a machine without CUDA —
        # fall through to CPU path.
        pass
    return ("cpu", "int8")


class TranscriptionEngine:
    def __init__(self, model_name="base"):
        device, compute_type = _pick_device()
        logger.info(
            f"Loading faster-whisper model: {model_name} "
            f"(device={device}, compute_type={compute_type})"
        )
        try:
            self._model = WhisperModel(
                model_name, device=device, compute_type=compute_type,
            )
        except Exception as e:
            # Most common GPU failure mode: CUDA-enabled torch is
            # installed but ctranslate2's cuDNN DLL isn't on PATH, so
            # WhisperModel.__init__ raises. Rather than crash the whole
            # backend, degrade gracefully to CPU int8 (what every prior
            # release shipped anyway).
            if device != "cpu":
                logger.warning(
                    f"faster-whisper failed to init on {device} ({e}); "
                    f"falling back to CPU int8"
                )
                self._model = WhisperModel(
                    model_name, device="cpu", compute_type="int8")
                device, compute_type = "cpu", "int8"
            else:
                raise
        logger.info(
            f"faster-whisper model loaded on {device} ({compute_type})")
        self._device = device
        self._compute_type = compute_type

    @property
    def device(self) -> str:
        return self._device

    async def transcribe(self, audio_path):
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        loop = asyncio.get_event_loop()
        try:
            segments, info = await loop.run_in_executor(
                None,
                lambda: self._model.transcribe(
                    audio_path, language="en", vad_filter=True),
            )
            segment_list = await loop.run_in_executor(
                None,
                lambda: [
                    {"start": s.start, "end": s.end, "text": s.text.strip()}
                    for s in segments if s.text.strip()
                ],
            )
        except Exception as e:
            raise RuntimeError(f"Transcription failed: {e}") from e
        return segment_list
