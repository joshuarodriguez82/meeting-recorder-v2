import asyncio
from pathlib import Path
from faster_whisper import WhisperModel
from utils.logger import get_logger
logger = get_logger(__name__)
class TranscriptionEngine:
    def __init__(self, model_name="base"):
        logger.info(f"Loading faster-whisper model: {model_name}")
        self._model = WhisperModel(model_name, device="cpu", compute_type="int8")
        logger.info("faster-whisper model loaded.")
    async def transcribe(self, audio_path):
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        loop = asyncio.get_event_loop()
        try:
            segments, info = await loop.run_in_executor(None, lambda: self._model.transcribe(audio_path, language="en", vad_filter=True))
            segment_list = await loop.run_in_executor(None, lambda: [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments if s.text.strip()])
        except Exception as e:
            raise RuntimeError(f"Transcription failed: {e}") from e
        return segment_list
