"""
Orchestrates the full recording lifecycle.
"""

import asyncio
import logging
import os
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
from core.live_transcriber import LiveTranscriber, TARGET_SR as LIVE_SR
from core.transcription import TranscriptionEngine
from models.segment import Segment
from models.session import Session
from services.speaker_profile_service import SpeakerProfileService
from utils.audio_utils import finalize_recording_streaming
from utils.logger import get_logger

logger = get_logger(__name__)

SESSION_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

TARGET_SR = 16000

# Mic duck gate (live transcription only). When far-end audio (loopback)
# is louder than DUCK_LEVEL_THRESHOLD, the user's mic is almost
# certainly picking up speaker bleed — at typical desktop speaker
# distances, anything the OS is rendering at moderate volume couples
# back into a webcam/desk mic. Multiply the mic chunk going into the
# LiveTranscriber by DUCK_ATTENUATION so the live "You" transcript
# doesn't get polluted with mangled echoes of what the loopback channel
# already transcribes cleanly. The smoothing keeps brief silences mid-
# sentence (a speaker's natural breathing pauses) from reopening the
# gate prematurely. Tuned conservatively — too-aggressive ducking would
# clip your own voice when you talk over someone.
#
# Disk-write path is NOT ducked. The full-fidelity mic WAV is preserved
# for the canonical post-stop transcript, where Whisper's VAD + the
# stereo merge handle echo more carefully than this naive gate ever
# could.
DUCK_LEVEL_THRESHOLD = 0.02   # ~ -34 dBFS RMS — quiet speech band
DUCK_ATTENUATION = 0.15
DUCK_SMOOTHING = 0.4          # EMA blend of new-chunk RMS into running

# Dead-air watchdog silence floor. A chunk whose RMS clears this counts
# as "someone is talking" and resets the silence timer. ~ -50 dBFS in
# float space. Both the mic AND the loopback (far-end participants)
# feed this — if the user mutes their own mic but the other side keeps
# talking, the room is NOT dead and the watchdog must not auto-stop.
SILENCE_RMS_FLOOR = 0.003


def _resample_for_live(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample a mono float32 chunk for the live transcriber.

    Most mics report 48 kHz natively; faster-whisper expects 16 kHz.
    Per-chunk resample produces tiny artifacts at chunk boundaries, but
    the artifacts are imperceptible at speech-recognition fidelity and
    we'd lose more from a lock-coupled queue than we gain from a longer
    resample window. The canonical /process pass at stop-time uses
    full-window resample for the persisted transcript.
    """
    if src_sr == dst_sr:
        return audio.astype(np.float32, copy=False)
    # scipy.signal.resample_poly is the same routine utils/audio_utils
    # uses for the final merge — keep them consistent so live + final
    # transcripts have identical timing math.
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(src_sr, dst_sr)
    up = dst_sr // g
    down = src_sr // g
    out = resample_poly(audio.astype(np.float64, copy=False), up, down)
    return out.astype(np.float32)


class RecordingService:

    def __init__(
        self,
        settings: Settings,
        transcription_engine: Optional[TranscriptionEngine] = None,
        diarization_engine: Optional[DiarizationEngine] = None,
        profile_service: Optional[SpeakerProfileService] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self._settings = settings
        self._transcription = transcription_engine
        self._diarization = diarization_engine
        # SpeakerProfileService is optional — when None we keep the v1
        # behaviour (no fingerprinting; speakers are session-local
        # SPEAKER_XX labels until manually renamed). Server.py wires it
        # up by default.
        self._profile_service = profile_service
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
        # Live transcription (#1 from the roadmap). Constructed lazily
        # on first start_recording so we don't pay the import cost at
        # backend startup. The provider closure lets the LiveTranscriber
        # see the current TranscriptionEngine even if it loads after
        # recording starts (fresh installs, no models warmed yet).
        self._live_transcriber: Optional[LiveTranscriber] = None
        # Rolling RMS of the most recent system-audio chunks. Used by
        # the mic duck gate (see _on_audio_chunk) — when far-end audio
        # is loud (other meeting participants talking), the user's mic
        # is also picking up bleed from their speakers, which shows up
        # as garbled "you said" segments mixed in with the real
        # transcript. We attenuate the mic copy that goes into live
        # transcription whenever loopback is hot. The mic WAV on disk
        # is untouched — full fidelity preserved for the canonical
        # post-stop transcript / processing.
        self._loopback_level_ema: float = 0.0
        # Auto-stop watchdog state. The audio callback updates
        # _last_speech_at whenever a chunk's RMS clears the silence
        # threshold; the watchdog periodically reads that timestamp to
        # decide whether the room has gone quiet. _meeting_scheduled_end
        # is the calendar-derived end time when start_recording was
        # called from a calendar tile; None for ad-hoc recordings.
        # _watchdog_warnings is the live set of warning dicts surfaced
        # via /recording/status to the frontend.
        self._last_speech_at: Optional[datetime] = None
        self._meeting_scheduled_end: Optional[datetime] = None
        self._watchdog_warnings: List[dict] = []
        self._watchdog_lock = threading.Lock()
        # Rate-limit the per-tick watchdog diagnostic log so it doesn't
        # drown out the rest of backend.log (status polls fire at 1 Hz).
        self._watchdog_last_log_at: Optional[datetime] = None
        # Wall-clock time of the most recent audio chunk write. Used by
        # the capture-stall detector in watchdog_tick — if no chunks
        # arrive for >30s during an active recording, something has
        # silently broken the capture path (device unplugged, OS audio
        # session killed, OneDrive locking the file). The user should
        # know IMMEDIATELY rather than discovering 30 min of silence
        # after the meeting.
        self._last_chunk_at: Optional[datetime] = None

    @property
    def current_session(self) -> Optional[Session]:
        return self._session

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def live_transcriber(self) -> Optional[LiveTranscriber]:
        """Exposed so the SSE endpoint in server.py can subscribe to
        the active recording's segment stream."""
        return self._live_transcriber

    def screenshot_dir(self) -> Optional[Path]:
        """Per-session screenshot folder, created on demand. None when
        no session is active. Lives under recordings_dir so screenshots
        are retained/cleaned up alongside the rest of the meeting's
        artifacts and can be reused later like any other file."""
        if self._session is None:
            return None
        d = (Path(self._settings.recordings_dir) / "screenshots"
             / f"session_{self._session.session_id}")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_screenshot(self, path: str) -> bool:
        """Attach a captured screenshot to the active session. The path
        is persisted with the session JSON on stop/process. Returns
        False if there's no session or the file doesn't exist."""
        if self._session is None:
            return False
        if not path or not Path(path).is_file():
            return False
        if path not in self._session.screenshots:
            self._session.screenshots.append(path)
        return True

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
        scheduled_end: Optional[datetime] = None,
        conference_room_mode: bool = False,
    ) -> Session:
        if self._recording:
            raise RuntimeError("A recording is already in progress.")
        # Reset watchdog state at the start of every recording. We seed
        # last_speech_at to "now" so the silence timer doesn't fire
        # immediately on a freshly-started recording before any audio
        # has come through.
        self._last_speech_at = datetime.now()
        self._meeting_scheduled_end = scheduled_end
        # Reset the diagnostic-log rate limiter too so the FIRST tick of
        # this recording always emits a line — otherwise a stop+restart
        # within 60s suppresses the very log entry you'd need to debug
        # an early auto-stop on the new recording.
        self._watchdog_last_log_at = None
        with self._watchdog_lock:
            self._watchdog_warnings = []

        session_id = uuid.uuid4().hex[:8].upper()
        self._session = Session(session_id=session_id)

        # Start per-session log file
        self._start_session_log(session_id)

        self._recording = True
        self._chunk_count = 0
        self._last_chunk_at = None
        self._loopback_level_ema = 0.0
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
            # Tee loopback into the live transcriber so other meeting
            # participants' audio appears in the live preview alongside
            # the user's voice. None when output_device_index is None
            # (no system-audio capture configured).
            on_loopback_chunk=(self._on_loopback_chunk
                               if output_device_index is not None else None),
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

        # Spin up live transcription only if the user hasn't disabled it
        # in Settings. When disabled we skip the LiveTranscriber thread
        # AND the per-chunk resampling in _on_audio_chunk, saving CPU on
        # long calls. The canonical post-stop transcript runs regardless.
        live_enabled = bool(
            getattr(self._settings, "live_transcription_enabled", True))
        try:
            if not live_enabled:
                logger.info("Live transcription disabled by user setting; "
                            "skipping LiveTranscriber spawn.")
                self._live_transcriber = None
            else:
                if self._live_transcriber is None:
                    self._live_transcriber = LiveTranscriber(
                        engine_provider=lambda: self._transcription,
                    )
                # In conference room mode the mic is capturing multiple
                # in-room people, not "you" specifically — pass the room
                # label so live segments render with a neutral badge
                # rather than "You".
                self._live_transcriber.start(
                    LIVE_SR, conference_room_mode=conference_room_mode)
        except Exception as e:
            # Live transcription failure is never fatal — recording must
            # still proceed even if streaming text doesn't.
            logger.warning(f"Live transcription unavailable: {e}")
            self._live_transcriber = None

        self._on_status(f"Recording started — Session {session_id}")
        logger.info(f"Session {session_id} recording started.")
        return self._session

    def stop_recording(self) -> Optional[Session]:
        if not self._recording or not self._capture:
            return self._session

        # Step-by-step instrumentation so a hung or crashed stop has a
        # clear timeline pointing at the offending step. Without these,
        # the log just trails off mid-stop and you can't tell whether
        # capture, the live transcriber, the WAV merge, or session save
        # was the one that hung. Each step logs entry + elapsed.
        import time as _t
        stop_t0 = _t.monotonic()
        logger.info("[stop] begin")

        self._recording = False
        # Grab the per-stream wallclock anchors before tearing the capture
        # down. If both arrived (mic always does; loopback only when system
        # audio is captured) the difference is the real cross-stream start
        # offset and gets passed to the merge step. None means: fall back to
        # the legacy right-aligned heuristic.
        mic_start = getattr(self._capture, "mic_start_monotonic", None)
        lb_start = getattr(self._capture, "loopback_start_monotonic", None)
        if mic_start is not None and lb_start is not None:
            loopback_start_offset_s = max(0.0, lb_start - mic_start)
        else:
            loopback_start_offset_s = None
        logger.info("[stop] capture.stop() …")
        t = _t.monotonic()
        try:
            self._capture.stop()
        except Exception as e:
            logger.exception(f"[stop] capture.stop raised: {e}")
        logger.info(f"[stop] capture.stop done in {_t.monotonic()-t:.1f}s")
        self._capture = None

        # Stop the live transcriber FIRST (before joining the audio
        # threads) so its tail flush + None sentinel reach SSE clients
        # while they're still listening. The 10-second join inside
        # stop() covers the worst case where Whisper is mid-window.
        if self._live_transcriber is not None:
            logger.info("[stop] live_transcriber.stop() …")
            t = _t.monotonic()
            try:
                self._live_transcriber.stop()
            except Exception as e:
                logger.warning(f"[stop] live transcriber raised: {e}")
            logger.info(
                f"[stop] live_transcriber.stop done in {_t.monotonic()-t:.1f}s")

        # Close the streaming WAV file
        logger.info("[stop] close mic WAV writer …")
        t = _t.monotonic()
        with self._chunks_lock:
            if self._wav_writer is not None:
                try:
                    self._wav_writer.close()
                except Exception as e:
                    logger.exception(f"[stop] mic WAV close raised: {e}")
                self._wav_writer = None
        logger.info(f"[stop] close mic WAV done in {_t.monotonic()-t:.1f}s")

        if self._session and self._chunk_count > 0 and self._wav_temp_path:
            # Stream-merge mic + loopback into final WAV with bounded memory.
            # Earlier versions sf.read() both files fully into RAM before
            # mixing — a 36-minute 48kHz session allocates ~2-3 GB and can
            # trigger a native STATUS_ACCESS_VIOLATION on stop (lost session).
            loopback_path = getattr(self, '_loopback_temp_path', None)
            final_path = self._build_audio_path(self._session.session_id)
            logger.info(
                f"[stop] finalize_recording_streaming → {final_path} …")
            t = _t.monotonic()
            try:
                duration_s, _ = finalize_recording_streaming(
                    mic_wav_path=self._wav_temp_path,
                    loopback_wav_path=loopback_path,
                    output_wav_path=final_path,
                    target_sr=TARGET_SR,
                    loopback_start_offset_s=loopback_start_offset_s,
                )
                self._session.audio_path = final_path
                self._session.ended_at = datetime.now()
                self._on_status("Recording saved. Ready to process.")
                logger.info(
                    f"Audio saved to {final_path} ({duration_s:.1f}s)")
                logger.info(
                    f"[stop] finalize done in {_t.monotonic()-t:.1f}s")

                # AUDIO INTEGRITY CHECK — compare the actual WAV
                # duration (returned by finalize_recording_streaming)
                # to the wall-clock duration the recording was running.
                # Silent partial-audio loss has happened in v2.9.0 and
                # earlier (multiple processes contending for the same
                # mic, OneDrive truncating mid-write, etc.) — without
                # this check, a recording with the last 30 minutes of
                # a 1-hour meeting saves with metadata claiming 1 hour
                # and the user only discovers the loss when listening
                # back. Now we tag the session so the UI can warn.
                #
                # Tolerance is 10% — small differences are normal
                # (final-write buffering, sample-rate rounding); 30+%
                # is the failure mode we're guarding against.
                self._session.audio_actual_duration_s = float(duration_s)
                expected_s = (
                    self._session.ended_at - self._session.started_at
                ).total_seconds() if self._session.started_at else 0.0
                self._session.audio_expected_duration_s = float(expected_s)
                if expected_s > 30.0:
                    # Only check when the recording was substantial
                    # (>30s); short test recordings have too much
                    # noise in the buffer math to be reliable.
                    deficit_ratio = (
                        (expected_s - duration_s) / expected_s
                        if expected_s > 0 else 0.0
                    )
                    if deficit_ratio > 0.10:
                        actual_min = duration_s / 60.0
                        expected_min = expected_s / 60.0
                        lost_min = (expected_s - duration_s) / 60.0
                        msg = (
                            f"Audio is shorter than the recording window. "
                            f"You got {actual_min:.0f} min of audio in a "
                            f"{expected_min:.0f}-min recording — about "
                            f"{lost_min:.0f} min appears to be missing."
                        )
                        self._session.audio_integrity_warning = msg
                        logger.critical(
                            f"AUDIO_INTEGRITY: session "
                            f"{self._session.session_id}: actual="
                            f"{duration_s:.1f}s expected={expected_s:.1f}s "
                            f"deficit={deficit_ratio*100:.0f}%")
                    elif duration_s > expected_s * 1.10:
                        # Actual longer than expected — also suspicious
                        # (a stale wav got concatenated maybe). Log but
                        # don't warn user; this case is rarer.
                        logger.warning(
                            f"AUDIO_INTEGRITY: session "
                            f"{self._session.session_id} wav is longer "
                            f"than wall-clock — actual={duration_s:.1f}s "
                            f"expected={expected_s:.1f}s (possible stale "
                            f"file concat).")
            except Exception as e:
                logger.exception(
                    f"[stop] finalize failed after {_t.monotonic()-t:.1f}s")
                self._on_status(f"Error saving audio: {e}")
            finally:
                # Clean up temps whether or not merge succeeded; any failure
                # leaves them on disk for startup recovery to retry.
                # KEEP_AUDIO_TEMPS=1 preserves them for offline AEC validation
                # via backend/scripts/measure_aec.py.
                keep_temps = os.environ.get("KEEP_AUDIO_TEMPS") == "1"
                if self._session.audio_path and not keep_temps:
                    for temp in (self._wav_temp_path, loopback_path):
                        if temp and Path(temp).exists():
                            try:
                                Path(temp).unlink()
                            except OSError:
                                pass
                elif keep_temps:
                    logger.info(
                        "[stop] KEEP_AUDIO_TEMPS=1 set — preserving "
                        f"{self._wav_temp_path} + {loopback_path}")
        elif self._session and self._chunk_count == 0:
            logger.warning("Recording stopped with no audio chunks captured.")
            self._on_status("No audio was captured. Try again.")

        self._wav_temp_path = None
        self._stop_session_log()
        logger.info(
            f"[stop] complete in {_t.monotonic()-stop_t0:.1f}s")
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

        # Speaker fingerprinting: compute per-speaker centroid embeddings
        # from the diarization turns, then look up matches in the
        # persistent profile store. Best-effort — any failure here is
        # logged and the session is still saved with bare SPEAKER_XX
        # labels (i.e. v1 behaviour).
        try:
            self._fingerprint_speakers(diarization_turns)
        except Exception as e:
            logger.exception(f"Speaker fingerprinting failed: {e}")

        self._on_status("Processing complete.")
        logger.info(f"Session {self._session.session_id} processing complete.")
        return self._session

    def _fingerprint_speakers(self, diarization_turns: List[dict]) -> None:
        """Extract per-speaker centroids from the audio + diarization
        turns, attach them to each Speaker object, and apply any matches
        from the persistent profile store as auto-renames awaiting user
        confirmation.

        Called from process_session() after segments + speakers are
        populated. Safe to call when self._profile_service is None
        (skips matching, still computes + stores embeddings so a future
        session can match them later if profiling gets enabled)."""
        if not self._session or not self._session.audio_path:
            return

        # Lazy import — avoids paying the speechbrain import cost on
        # backends that never run processing (e.g. settings-only restarts).
        from core.speaker_embeddings import (
            extract_speaker_centroids, is_available,
        )
        if not is_available():
            return

        # Group diarization turns by speaker label.
        turns_by_speaker: dict[str, list[tuple[float, float]]] = {}
        for turn in diarization_turns:
            label = turn.get("speaker") or turn.get("speaker_id")
            if not label:
                continue
            turns_by_speaker.setdefault(label, []).append(
                (float(turn["start"]), float(turn["end"]))
            )

        centroids = extract_speaker_centroids(
            self._session.audio_path, turns_by_speaker,
        )
        if not centroids:
            return

        for speaker_id, centroid in centroids.items():
            speaker = self._session.speakers.get(speaker_id)
            if speaker is None:
                continue
            speaker.embedding = [float(x) for x in centroid.tolist()]

            # Profile lookup. The threshold is intentionally on the
            # service side (configurable centrally) rather than per-call
            # so the same cutoff applies to UI hints later.
            if self._profile_service is None:
                continue
            match = self._profile_service.find_match(centroid)
            if match is None:
                continue
            profile, similarity = match
            speaker.profile_id = profile.profile_id
            speaker.match_confidence = similarity
            speaker.match_confirmed = False  # user confirms in UI
            # Set the display name optimistically — the UI shows a
            # "(87%) confirm?" badge so the user knows it's an auto-match.
            speaker.display_name = profile.display_name
            logger.info(
                f"Auto-matched {speaker_id} → {profile.display_name} "
                f"(similarity={similarity:.2f})")

    # ── Auto-stop watchdog ──────────────────────────────────────────
    #
    # Caller (server.py /recording/status handler) invokes
    # watchdog_tick() on every poll. We compute the current set of
    # active warnings against settings and return them. The server is
    # responsible for actually stopping the recording (via
    # stop_recording) when watchdog_tick reports an `auto_stopped` code
    # — the watchdog itself stays in a pure-evaluation role so the
    # logic is easy to test and doesn't fight the asyncio event loop
    # over who owns the recording lifecycle.

    def get_warnings(self) -> List[dict]:
        """Read-only accessor for the latest warning set. Cheap; no
        recomputation. Server.py exposes this through /recording/status
        so the frontend renders banners + native notifications."""
        with self._watchdog_lock:
            return [dict(w) for w in self._watchdog_warnings]

    def watchdog_tick(self) -> dict:
        """Re-evaluate every watchdog condition against the current
        recording state + Settings, update the cached warning list,
        and return a decision dict:

            {"warnings": [...], "should_auto_stop": bool, "reason": str}

        Caller uses should_auto_stop to decide whether to invoke
        stop_recording. Returns an empty / no-op dict when no recording
        is active.
        """
        if not self._recording:
            with self._watchdog_lock:
                self._watchdog_warnings = []
            return {"warnings": [], "should_auto_stop": False, "reason": ""}

        s = self._settings
        now = datetime.now()
        warnings: List[dict] = []
        should_stop = False
        stop_reason = ""

        # Hard cap — user-configurable safety net. Default 4 hours.
        # 0 = disabled (but the absolute cap below still applies).
        hard_cap_h = max(0, getattr(s, "hard_cap_hours", 0))
        # We need the recording start time. Fall back to session.started_at
        # since RecordingService doesn't track it independently.
        started_at = (self._session.started_at if self._session else None)
        if started_at and hard_cap_h > 0:
            elapsed_h = (now - started_at).total_seconds() / 3600.0
            if elapsed_h >= hard_cap_h:
                should_stop = True
                stop_reason = "hard_cap"
                warnings.append({
                    "code": "hard_cap_hit",
                    "message": (
                        f"Recording auto-stopped at the {hard_cap_h}-hour "
                        f"hard cap. Change in Settings → Workflow."),
                    "since_seconds": int(elapsed_h * 3600),
                })

        # ABSOLUTE CAP — independent of user settings, cannot be
        # disabled. After an incident where an orphan backend recorded
        # 4h17m of audio across multiple meetings, we enforce a system-
        # wide ceiling that triggers regardless of what the user has
        # configured. 6 hours is generous (longest legit meeting we've
        # seen is ~3.5 hours) but tight enough that any "the recording
        # ran all day" scenario hits this wall.
        ABSOLUTE_CAP_HOURS = 6
        if started_at and not should_stop:
            elapsed_h = (now - started_at).total_seconds() / 3600.0
            if elapsed_h >= ABSOLUTE_CAP_HOURS:
                should_stop = True
                stop_reason = "absolute_cap"
                warnings.append({
                    "code": "absolute_cap_hit",
                    "message": (
                        f"Recording stopped at the {ABSOLUTE_CAP_HOURS}-hour "
                        f"absolute system maximum. This cannot be disabled. "
                        f"If you genuinely need longer recordings, please "
                        f"contact the developer — this limit exists to "
                        f"prevent runaway capture in failure scenarios."),
                    "since_seconds": int(elapsed_h * 3600),
                })
                logger.critical(
                    f"ABSOLUTE_CAP fired at {elapsed_h:.2f}h — "
                    f"stopping recording regardless of user settings")

        # Capture-stall detector: how long since ANY audio chunk reached
        # the WAV writer? Distinct from dead-air (which is "no SPEECH
        # detected"). This catches the failure mode where the capture
        # device went away, the WAV file got locked, OneDrive flipped
        # the file cloud-only, or any other silent break in the path.
        # Chunks arrive at ~10 Hz during normal capture; 30s with no
        # chunks at all means something is wrong.
        if self._last_chunk_at is not None and started_at:
            chunk_silence_s = int(
                (now - self._last_chunk_at).total_seconds())
            elapsed_recording_s = (now - started_at).total_seconds()
            # Grace period for the first ~5s after start_recording —
            # capture spin-up can take a moment.
            if elapsed_recording_s > 5 and chunk_silence_s >= 30:
                warnings.append({
                    "code": "capture_stalled",
                    "message": (
                        f"No audio captured for {chunk_silence_s}s. "
                        f"Recording is RUNNING but data is NOT reaching "
                        f"the file. Check your microphone, then stop and "
                        f"restart the recording."),
                    "since_seconds": chunk_silence_s,
                })
                logger.critical(
                    f"CAPTURE_STALLED: no audio chunks for "
                    f"{chunk_silence_s}s during active recording")

        # Dead-air: how long since the last chunk above the silence floor.
        if self._last_speech_at:
            silence_s = int((now - self._last_speech_at).total_seconds())
        else:
            silence_s = 0

        silence_warn_min = max(0, getattr(s, "silence_warn_min", 0))
        silence_stop_min = max(0, getattr(s, "silence_stop_min", 0))

        if (silence_stop_min > 0 and not should_stop
                and silence_s >= silence_stop_min * 60):
            should_stop = True
            stop_reason = "dead_air"
            warnings.append({
                "code": "dead_air_stop",
                "message": (
                    f"Recording auto-stopped — no speech detected for "
                    f"{silence_stop_min} minute"
                    f"{'s' if silence_stop_min != 1 else ''}."),
                "since_seconds": silence_s,
            })
        elif silence_warn_min > 0 and silence_s >= silence_warn_min * 60:
            warnings.append({
                "code": "dead_air",
                "message": (
                    f"No speech detected for {silence_s // 60} minute"
                    f"{'s' if silence_s // 60 != 1 else ''}. "
                    f"Did the meeting end?"),
                "since_seconds": silence_s,
            })

        # Meeting overrun (only meaningful when we have a scheduled end).
        if self._meeting_scheduled_end is not None:
            overrun_s = int((now - self._meeting_scheduled_end).total_seconds())
            if overrun_s > 0:
                overrun_warn_min = max(0, getattr(s, "overrun_warn_min", 0))
                overrun_stop_min = max(0, getattr(s, "overrun_stop_min", 0))
                if (overrun_stop_min > 0 and not should_stop
                        and overrun_s >= overrun_stop_min * 60):
                    should_stop = True
                    stop_reason = "meeting_overrun"
                    warnings.append({
                        "code": "meeting_overrun_stop",
                        "message": (
                            f"Recording auto-stopped — your scheduled "
                            f"meeting ended {overrun_s // 60} minute"
                            f"{'s' if overrun_s // 60 != 1 else ''} ago."),
                        "since_seconds": overrun_s,
                    })
                elif overrun_warn_min > 0 and overrun_s >= overrun_warn_min * 60:
                    warnings.append({
                        "code": "meeting_overrun",
                        "message": (
                            f"Your scheduled meeting ended "
                            f"{overrun_s // 60} minute"
                            f"{'s' if overrun_s // 60 != 1 else ''} ago. "
                            f"Still recording."),
                        "since_seconds": overrun_s,
                    })

        with self._watchdog_lock:
            self._watchdog_warnings = warnings

        # Diagnostic log — rate-limited to once a minute. Lets us
        # reconstruct why the watchdog did or didn't fire when a user
        # reports "it never auto-stopped". Format is greppable.
        log_due = (
            self._watchdog_last_log_at is None
            or (now - self._watchdog_last_log_at).total_seconds() >= 60
        )
        if log_due:
            self._watchdog_last_log_at = now
            last_speech_age = (
                int((now - self._last_speech_at).total_seconds())
                if self._last_speech_at else None
            )
            logger.info(
                f"watchdog: silence_s={silence_s} "
                f"last_speech_age={last_speech_age} "
                f"loopback_ema={self._loopback_level_ema:.4f} "
                f"silence_warn_min={silence_warn_min} "
                f"silence_stop_min={silence_stop_min} "
                f"should_stop={should_stop} reason={stop_reason!r}")

        return {
            "warnings": warnings,
            "should_auto_stop": should_stop,
            "reason": stop_reason,
        }

    def _on_audio_chunk(self, chunk: np.ndarray) -> None:
        if not self._recording:
            return
        mono = chunk.mean(axis=0) if chunk.ndim > 1 else chunk
        with self._chunks_lock:
            if self._wav_writer is not None:
                # Disk write keeps the original capture sample rate so the
                # final merge / Whisper-on-stop pipeline sees full-fidelity
                # audio. Live transcription gets a 16 kHz copy below since
                # faster-whisper's input is hardcoded to 16 kHz.
                self._wav_writer.write(mono)
                self._chunk_count += 1
                self._last_chunk_at = datetime.now()
        # Auto-stop watchdog input: track the most recent moment we heard
        # speech-level audio on the MIC. The loopback path
        # (_on_loopback_chunk) feeds the same _last_speech_at so the
        # far-end participants keep the room "alive" even when the user
        # mutes their own mic. Calibrated for the typical mic noise
        # floor — too aggressive a threshold treats background-fan noise
        # as speech and the watchdog never fires; too loose treats
        # whispered speech as silence and the watchdog fires while the
        # user is still talking.
        try:
            rms = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
            if rms > SILENCE_RMS_FLOOR:
                self._last_speech_at = datetime.now()
        except Exception:
            # Don't let RMS math kill the audio path
            pass
        # Live transcription path. We do this OUTSIDE the chunks_lock
        # because resampling is CPU work and we don't want to delay the
        # disk-writer thread waiting on it. push_audio is internally
        # thread-safe via its own buffer lock.
        live = self._live_transcriber
        if live is not None and live.is_running:
            try:
                # Mic duck: if far-end audio is loud right now, the
                # user's mic is bleeding speaker output and the live
                # "You" transcript would pollute with echo of what
                # the "Them" transcript already captures cleanly.
                # Attenuate before pushing.
                if self._loopback_level_ema > DUCK_LEVEL_THRESHOLD:
                    mic_for_live = mono * DUCK_ATTENUATION
                else:
                    mic_for_live = mono
                live.push_audio(
                    _resample_for_live(
                        mic_for_live, self._capture_sr, LIVE_SR))
            except Exception as e:
                # Never let live transcription kill the recording. A
                # bad chunk just means a glitch in the live preview.
                logger.debug(f"Live push_audio failed: {e}")

    def _on_loopback_chunk(self, chunk: np.ndarray) -> None:
        """Loopback (system audio) chunks routed into the live
        transcriber. Same resample-to-16k step as mic. AudioCapture's
        loopback sample rate is on the capture instance once start()
        runs; we read it lazily here because at __init__ time the
        capture hasn't opened the loopback stream yet."""
        if not self._recording:
            return
        try:
            mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
            # Update the rolling level estimate that the mic-duck gate
            # in _on_audio_chunk reads. RMS over the chunk smoothed with
            # an exponential moving average so brief silences within
            # speech don't release the gate.
            chunk_rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
            self._loopback_level_ema = (
                DUCK_SMOOTHING * chunk_rms
                + (1.0 - DUCK_SMOOTHING) * self._loopback_level_ema
            )
            # Dead-air watchdog: far-end audio counts as "the room is
            # alive". Without this, muting your own mic during a meeting
            # makes the silence timer run even though the other people
            # are still talking — the recorder would auto-stop mid-call.
            # This runs whether or not live transcription is enabled.
            #
            # Loopback uses DUCK_LEVEL_THRESHOLD (not SILENCE_RMS_FLOOR
            # like the mic does) because the system audio stream
            # typically carries low-level codec hiss / keepalive noise
            # even after the meeting ends. The lower mic floor is right
            # for whispered speech; the higher loopback floor only
            # counts actual far-end speech.
            if chunk_rms > DUCK_LEVEL_THRESHOLD:
                self._last_speech_at = datetime.now()
        except Exception:
            # Never let RMS math kill the audio path.
            pass

        # Live-transcription tee. Independent of the watchdog update
        # above so disabling the live preview doesn't re-introduce the
        # mute-triggers-auto-stop bug.
        live = self._live_transcriber
        if live is None or not live.is_running:
            return
        capture = self._capture
        if capture is None:
            return
        loopback_sr = capture.loopback_sr or LIVE_SR
        try:
            mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
            live.push_loopback(_resample_for_live(mono, loopback_sr, LIVE_SR))
        except Exception as e:
            logger.debug(f"Live push_loopback failed: {e}")

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
