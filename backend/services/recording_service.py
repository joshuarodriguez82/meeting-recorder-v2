"""
Orchestrates the full recording lifecycle.
"""

import asyncio
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np
import soundfile as sf

from config.settings import Settings
from core.audio_capture import AudioCapture
from core.diarization import DiarizationEngine
from core.transcription import TranscriptionEngine
from models.segment import Segment
from models.session import Session
from utils.audio_utils import finalize_recording_streaming
from utils.logger import get_logger

logger = get_logger(__name__)

SESSION_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

TARGET_SR = 16000


class RecordingService:

    def __init__(
        self,
        settings: Settings,
        transcription_engine: Optional[TranscriptionEngine] = None,
        diarization_engine: Optional[DiarizationEngine] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._settings = settings
        self._transcription = transcription_engine
        self._diarization = diarization_engine
        self._on_status = on_status or (lambda _: None)
        self._session: Optional[Session] = None
        self._capture: Optional[AudioCapture] = None
        self._wav_writer: Optional[sf.SoundFile] = None
        self._wav_temp_path: Optional[str] = None
        self._chunks_lock = threading.Lock()
        self._recording = False
        self._capture_sr = TARGET_SR
        self._chunk_count = 0
        self._session_log_handler: Optional[logging.FileHandler] = None

    @property
    def current_session(self) -> Optional[Session]:
        return self._session

    @property
    def is_recording(self) -> bool:
        return self._recording

    def set_session(self, session: Session) -> None:
        """Allow an externally created session (e.g. loaded file) to be processed."""
        self._session = session

    def set_engines(
        self,
        transcription_engine: TranscriptionEngine,
        diarization_engine: DiarizationEngine,
    ) -> None:
        """Attach AI engines after deferred model loading."""
        self._transcription = transcription_engine
        self._diarization = diarization_engine

    @property
    def can_process(self) -> bool:
        return self._transcription is not None and self._diarization is not None

    def start_recording(
        self,
        mic_device_index: Optional[int],
        output_device_index: Optional[int],
    ) -> Session:
        if self._recording:
            raise RuntimeError("A recording is already in progress.")

        session_id = uuid.uuid4().hex[:8].upper()
        self._session = Session(session_id=session_id)

        # Start per-session log file
        self._start_session_log(session_id)

        self._recording = True
        self._chunk_count = 0
        recordings_dir = Path(self._settings.recordings_dir)
        recordings_dir.mkdir(parents=True, exist_ok=True)
        self._loopback_temp_path = str(
            recordings_dir / f"_loopback_{session_id}.wav"
        ) if output_device_index is not None else None
        self._capture = AudioCapture(
            mic_device_index=mic_device_index,
            output_device_index=output_device_index,
            on_chunk=self._on_audio_chunk,
            loopback_wav_path=self._loopback_temp_path,
        )

        try:
            self._capture.start()
            if hasattr(self._capture, 'actual_sr'):
                self._capture_sr = self._capture.actual_sr
            else:
                self._capture_sr = TARGET_SR
        except Exception as e:
            self._recording = False
            self._capture = None
            raise RuntimeError(f"Failed to start audio capture: {e}") from e

        # Open a temp WAV file to stream audio to disk during recording
        try:
            recordings_dir = Path(self._settings.recordings_dir)
            recordings_dir.mkdir(parents=True, exist_ok=True)
            self._wav_temp_path = str(
                recordings_dir / f"_recording_{session_id}.wav"
            )
            self._wav_writer = sf.SoundFile(
                self._wav_temp_path,
                mode="w",
                samplerate=self._capture_sr,
                channels=1,
                subtype="FLOAT",
            )
        except Exception as e:
            self._recording = False
            self._capture.stop()
            self._capture = None
            raise RuntimeError(f"Failed to open recording file: {e}") from e

        self._on_status(f"Recording started — Session {session_id}")
        logger.info(f"Session {session_id} recording started.")
        return self._session

    def stop_recording(self) -> Optional[Session]:
        if not self._recording or not self._capture:
            return self._session

        self._recording = False
        self._capture.stop()
        self._capture = None

        # Close the streaming WAV file
        with self._chunks_lock:
            if self._wav_writer is not None:
                self._wav_writer.close()
                self._wav_writer = None

        if self._session and self._chunk_count > 0 and self._wav_temp_path:
            # Stream-merge mic + loopback into final WAV with bounded memory.
            # Earlier versions sf.read() both files fully into RAM before
            # mixing — a 36-minute 48kHz session allocates ~2-3 GB and can
            # trigger a native STATUS_ACCESS_VIOLATION on stop (lost session).
            loopback_path = getattr(self, '_loopback_temp_path', None)
            final_path = self._build_audio_path(self._session.session_id)
            try:
                duration_s, _ = finalize_recording_streaming(
                    mic_wav_path=self._wav_temp_path,
                    loopback_wav_path=loopback_path,
                    output_wav_path=final_path,
                    target_sr=TARGET_SR,
                )
                self._session.audio_path = final_path
                self._session.ended_at = datetime.now()
                self._on_status("Recording saved. Ready to process.")
                logger.info(
                    f"Audio saved to {final_path} ({duration_s:.1f}s)")
            except Exception as e:
                logger.exception("Failed to save audio")
                self._on_status(f"Error saving audio: {e}")
            finally:
                # Clean up temps whether or not merge succeeded; any failure
                # leaves them on disk for startup recovery to retry.
                if self._session.audio_path:
                    for temp in (self._wav_temp_path, loopback_path):
                        if temp and Path(temp).exists():
                            try:
                                Path(temp).unlink()
                            except OSError:
                                pass
        elif self._session and self._chunk_count == 0:
            logger.warning("Recording stopped with no audio chunks captured.")
            self._on_status("No audio was captured. Try again.")

        self._wav_temp_path = None
        self._stop_session_log()
        return self._session

    async def process_session(self) -> Session:
        if not self._session or not self._session.audio_path:
            raise RuntimeError("No recorded session to process.")
        if not self.can_process:
            raise RuntimeError(
                "AI models not loaded. Add API keys in File > Settings "
                "and restart the app to enable transcription and diarization.")

        self._on_status("__stage:transcribe:active__")
        raw_segments = await self._transcription.transcribe(self._session.audio_path)

        if not raw_segments:
            self._on_status("Transcription produced no output. Check audio quality.")
            return self._session

        self._on_status("__stage:transcribe:done____stage:diarize:active__")
        diarization_turns = await self._diarization.diarize(self._session.audio_path)

        self._on_status("__stage:diarize:done____stage:speakers:active__")
        attributed = DiarizationEngine.assign_speakers(raw_segments, diarization_turns)

        for raw in attributed:
            speaker = self._session.get_or_create_speaker(raw["speaker_id"])
            segment = Segment(
                speaker_id=speaker.speaker_id,
                start=raw["start"],
                end=raw["end"],
                text=raw["text"],
            )
            self._session.segments.append(segment)

        self._on_status("Processing complete.")
        logger.info(f"Session {self._session.session_id} processing complete.")
        return self._session

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        if not self._recording:
            return
        with self._chunks_lock:
            if self._wav_writer is not None:
                mono = chunk.mean(axis=0) if chunk.ndim > 1 else chunk
                self._wav_writer.write(mono)
                self._chunk_count += 1

    def _start_session_log(self, session_id: str) -> None:
        try:
            recordings_dir = Path(self._settings.recordings_dir)
            recordings_dir.mkdir(parents=True, exist_ok=True)
            log_path = recordings_dir / f"session_{session_id}.log"
            handler = logging.FileHandler(str(log_path), encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(SESSION_LOG_FMT))
            logging.getLogger().addHandler(handler)
            self._session_log_handler = handler
            logger.info(f"Session log started: {log_path}")
        except Exception as e:
            logger.warning(f"Could not create session log file: {e}")

    def _stop_session_log(self) -> None:
        if self._session_log_handler:
            logger.info("Session log closed.")
            logging.getLogger().removeHandler(self._session_log_handler)
            self._session_log_handler.close()
            self._session_log_handler = None

    def _build_audio_path(self, session_id: str) -> str:
        recordings_dir = Path(self._settings.recordings_dir)
        recordings_dir.mkdir(parents=True, exist_ok=True)
        return str(recordings_dir / f"session_{session_id}.wav")
