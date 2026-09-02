import asyncio
from pathlib import Path

from faster_whisper import WhisperModel

from core import decode_options
from utils.logger import get_logger
from utils.ml_memory import cleanup_ml_memory

logger = get_logger(__name__)


def _word_spans(segment) -> list:
    """`[{start, end, word}, …]` for a faster-whisper segment, or [].

    Defensive on purpose: `word_timestamps` is requested, but a model
    build that ignores it, or an older library, returns segments with no
    `words` attribute at all. Returning [] means the caller falls back
    to whole-segment speaker attribution rather than raising over a
    missing field.
    """
    words = getattr(segment, "words", None) or []
    out = []
    for w in words:
        try:
            out.append({
                "start": float(w.start),
                "end": float(w.end),
                "word": str(w.word),
            })
        except (AttributeError, TypeError, ValueError):
            continue
    return out


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

    async def transcribe(self, audio_path, initial_prompt: str = "",
                         language: str = "en"):
        """Transcribe an audio file.

        `initial_prompt` biases the decoder toward domain vocabulary
        (built from TerminologyService). Empty string → identical
        behavior to before, so users who clear their glossary lose
        nothing.

        `language` is an ISO code, or "auto" to detect. It used to be
        hardcoded "en" here AND in the live path: non-English speech was
        decoded as English, which Whisper does without erroring — it
        emits fluent, confident, wrong text, and every summary and
        embedding downstream is built on it.

        Decode options come from core.decode_options so this path and
        the live one cannot drift; see that module for why each one is
        set. Word timestamps are requested here (batch only) because
        speaker attribution needs them to split a segment at a hand-off.
        """
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        loop = asyncio.get_event_loop()
        opts = decode_options.build(
            language=language, initial_prompt=initial_prompt, live=False)
        try:
            segments, info = await loop.run_in_executor(
                None,
                lambda: self._model.transcribe(audio_path, **opts),
            )
            segment_list = await loop.run_in_executor(
                None,
                lambda: [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text.strip(),
                        # Carried for assign_speakers, which uses word
                        # boundaries to split a segment that spans a
                        # speaker hand-off. Absent on any model or
                        # version that did not return them, and that
                        # path degrades to whole-segment attribution.
                        "words": _word_spans(s),
                    }
                    for s in segments if s.text.strip()
                ],
            )
        except Exception as e:
            raise RuntimeError(f"Transcription failed: {e}") from e
        finally:
            # SESSION-BOUNDARY cleanup, not per-chunk: this transcribe()
            # runs once per whole recording (the post-stop batch pass —
            # see recording_service.process_session), never inside the
            # live-transcription hot path (core/live_transcriber.py
            # calls engine._model.transcribe() directly and never
            # touches this method), so one gc.collect() +
            # torch cache release here costs nothing latency-sensitive.
            cleanup_ml_memory()
        return segment_list
