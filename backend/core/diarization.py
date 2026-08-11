"""
Pyannote speaker diarization — GPU accelerated.
"""

import asyncio
from typing import List, Optional, Tuple
from utils.logger import get_logger

logger = get_logger(__name__)


def _resolve_device(setting: str, torch_module) -> Tuple[object, str]:
    """Resolve the actual torch device for the pyannote pipeline.

    Field report 2026-08-11 (0xC0000005 after recording stop): the backend
    process died with STATUS_ACCESS_VIOLATION 3-4s after every recording
    stop, no Python traceback (native crash). Working hypothesis: during a
    recording, LiveTranscriber holds a faster-whisper (CTranslate2, its own
    bundled cuDNN) model resident on CUDA; on stop, auto-process loads this
    pyannote pipeline via PyTorch (a *different* bundled cuDNN) and moves it
    onto CUDA too — two CUDA/cuDNN runtimes alive in one process at once.
    `setting` (Settings.diarization_device) makes this device selectable so
    that hypothesis can be tested/worked around without guessing:

      "auto" (default) — untouched pre-2026-08-11 behavior: CUDA > MPS
             (Apple Silicon) > CPU. Hardcoding any one of these crashes on
             hosts that lack it (AssertionError: Torch not compiled with
             CUDA; or "MPS backend not available"), so we still probe.
      "cpu"  — force CPU, never probe CUDA/MPS at all. This is the
             workaround: it keeps pyannote off CUDA entirely so it can
             never collide with the whisper runtime.
      "cuda" — force CUDA if available; on a machine with no CUDA device
             this must fall back to CPU with a WARNING log rather than
             raise — forcing "cuda" is a deliberate choice to reproduce
             the crash for diagnosis, not a way to break the app on a
             GPU-less machine.

    Any value outside {"auto", "cpu", "cuda"} is treated as "auto" —
    Settings.from_env already normalizes stored config.env values, but
    this is a second line of defense for direct callers (tests, future
    code paths) that bypass Settings entirely.

    Returns (torch.device, human-readable label for the log line).
    """
    setting = (setting or "auto").strip().lower()
    if setting not in ("auto", "cpu", "cuda"):
        setting = "auto"

    if setting == "cpu":
        return torch_module.device("cpu"), "CPU"

    if setting == "cuda":
        if torch_module.cuda.is_available():
            return torch_module.device("cuda"), "GPU (CUDA)"
        logger.warning(
            "diarization_device=cuda but no CUDA device is available on "
            "this machine; falling back to CPU instead of crashing.")
        return torch_module.device("cpu"), "CPU"

    # "auto" — existing logic untouched. Order: CUDA > MPS > CPU.
    if torch_module.cuda.is_available():
        return torch_module.device("cuda"), "GPU (CUDA)"
    mps = getattr(torch_module.backends, "mps", None)
    if mps is not None and mps.is_available():
        # Pyannote works on MPS as of pyannote.audio 3.x; some
        # operations fall back to CPU automatically.
        return torch_module.device("mps"), "GPU (MPS, Apple Silicon)"
    return torch_module.device("cpu"), "CPU"


class DiarizationEngine:

    def __init__(
        self,
        hf_token: str,
        max_speakers: int = 8,
        diarization_device: str = "auto",
    ):
        from pyannote.audio import Pipeline
        import torch
        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
        )
        # Device is resolved here, at load time (not import time), so a
        # settings change takes effect on the next model load without
        # requiring a code change or process restart timing games.
        device, device_label = _resolve_device(diarization_device, torch)
        logger.info(f"Loading pyannote diarization pipeline on {device_label}.")
        # Greppable diagnostics line: both the requested setting and the
        # resolved device on one line, so `grep diarization_device` in
        # rust.log/backend logs shows exactly what took effect — this is
        # the evidence used to confirm the setting actually took effect
        # (field report 2026-08-11).
        logger.info(
            f"diarization_device setting={diarization_device!r} "
            f"resolved_device={device} ({device_label})")
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
