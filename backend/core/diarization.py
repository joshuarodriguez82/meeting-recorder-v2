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
        logger.info("Loading pyannote diarization pipeline on GPU...")
        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
        )
        self._pipeline.to(torch.device("cuda"))
        self._max_speakers = max_speakers
        logger.info("Diarization pipeline loaded on GPU.")

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
