"""
Pyannote speaker diarization — GPU accelerated.
"""

import asyncio
from typing import List, Optional
from utils.logger import get_logger

logger = get_logger(__name__)


class DiarizationEngine:

    def __init__(self, hf_token: str, max_speakers: int = 8):
        from pyannote.audio import Pipeline
        import torch
        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
        )
        # Pick the best available accelerator. Order: CUDA > MPS (Apple
        # Silicon) > CPU. Hardcoding any of them crashes on hosts that
        # don't have it (AssertionError: Torch not compiled with CUDA;
        # or "MPS backend not available").
        device = torch.device("cpu")
        device_label = "CPU"
        if torch.cuda.is_available():
            device = torch.device("cuda")
            device_label = "GPU (CUDA)"
        elif (getattr(torch.backends, "mps", None) is not None
              and torch.backends.mps.is_available()):
            # Pyannote works on MPS as of pyannote.audio 3.x; some
            # operations fall back to CPU automatically.
            device = torch.device("mps")
            device_label = "GPU (MPS, Apple Silicon)"
        logger.info(f"Loading pyannote diarization pipeline on {device_label}.")
        try:
            self._pipeline.to(device)
        except Exception as e:
            # If MPS rejects the move (rare — some pyannote layers don't
            # implement an MPS kernel), fall back to CPU rather than crash.
            if device.type != "cpu":
                logger.warning(
                    f"Pipeline.to({device}) failed ({e}); falling back to CPU.")
                self._pipeline.to(torch.device("cpu"))
        self._max_speakers = max_speakers
        logger.info("Diarization pipeline loaded.")

    async def diarize(self, audio_path: str) -> List[dict]:
        logger.info(f"Diarizing: {audio_path}")
        loop = asyncio.get_event_loop()
        try:
            diarization = await loop.run_in_executor(
                None,
                lambda: self._pipeline(
                    audio_path,
                    max_speakers=self._max_speakers,
                )
            )
        except Exception as e:
            raise RuntimeError(
                f"Diarization failed: {e}\n"
                "Check that the audio file is a valid 16kHz mono WAV."
            ) from e

        turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({
                "start":   turn.start,
                "end":     turn.end,
                "speaker": speaker,
            })
        logger.info(f"Diarization complete: {len(set(t['speaker'] for t in turns))} speakers detected.")
        return turns

    @staticmethod
    def assign_speakers(
        segments: List[dict],
        turns: List[dict],
    ) -> List[dict]:
        attributed = []
        for seg in segments:
            seg_mid = (seg["start"] + seg["end"]) / 2
            speaker = "SPEAKER_UNKNOWN"
            best_overlap = 0.0
            for turn in turns:
                overlap = min(seg["end"], turn["end"]) - max(seg["start"], turn["start"])
                if overlap > best_overlap:
                    best_overlap = overlap
                    speaker = turn["speaker"]
            attributed.append({**seg, "speaker_id": speaker})
        return attributed
