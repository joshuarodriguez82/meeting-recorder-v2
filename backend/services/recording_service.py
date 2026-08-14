"""
Orchestrates the full recording lifecycle.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
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


def _local_capture_dir() -> Path:
    """Local-only scratch directory for the WAV streams that the capture
    thread writes to during an active recording.

    ``recordings_dir`` is often pointed at OneDrive / iCloud / Dropbox
    for cross-device sync, but writing the live capture through a cloud
    filter driver causes contention severe enough to drop audio
    wholesale on long meetings. Use the OS temp dir (``%TEMP%`` on
    Windows, ``/tmp`` family elsewhere) — guaranteed local, fast, and
    cleaned up by the OS even if we miss a deletion on crash.
    """
    d = Path(tempfile.gettempdir()) / "meeting_recorder_capture"
    d.mkdir(parents=True, exist_ok=True)
    return d

logger = get_logger(__name__)

SESSION_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Windows file attributes signalling a cloud-on-demand placeholder whose
# bytes aren't local yet (OneDrive Files-On-Demand, etc.).
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000


def _ensure_audio_available(path: str) -> None:
    """Guarantee the WAV is readable from local disk before the
    transcribe/diarize pipeline opens it.

    Recordings dirs that live in OneDrive (the common case) get older
    files evicted to cloud-only placeholders: the file reports its full
    size but the content isn't on disk. pyannote's soundfile reader then
    raises an opaque 'invalid audio file' error mid-pipeline. Reading the
    bytes through once forces the cloud provider to hydrate the file
    locally.

    Raises RuntimeError with a clear, user-facing message when the file
    is missing, empty, or unreadable — far better than the raw pyannote
    docstring the user would otherwise see.
    """
    p = Path(path)
    if not p.exists():
        raise RuntimeError(
            "The audio file for this recording is missing — it may have "
            "been moved, deleted, or not yet synced down from the cloud.")
    try:
        st = p.stat()
    except OSError as e:
        raise RuntimeError(
            f"The audio file couldn't be accessed: {e}") from e
    if st.st_size < 1024:
        raise RuntimeError(
            "The audio file is empty or truncated — this recording didn't "
            "capture usable audio and can't be processed.")

    # Only pay the full-read cost when the file looks like a cloud
    # placeholder; a normal local file skips straight through.
    attrs = getattr(st, "st_file_attributes", 0) or 0
    is_placeholder = bool(attrs & (
        _FILE_ATTRIBUTE_OFFLINE
        | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    ))
    if not is_placeholder:
        return

    logger.info(
        f"Audio file is a cloud placeholder ({p.name}); hydrating "
        f"{st.st_size/1024/1024:.1f} MB to local disk before processing.")
    try:
        with open(p, "rb") as f:
            while True:
                chunk = f.read(8 * 1024 * 1024)
                if not chunk:
                    break
    except OSError as e:
        raise RuntimeError(
            "The audio file couldn't be loaded — it may still be syncing "
            "from OneDrive. Wait a moment and process the session again."
        ) from e
    logger.info(f"Audio file hydrated: {p.name}")


# The local-copy + purge helpers (formerly defined here) moved to
# ``utils.audio_utils`` so they can be tested in isolation without
# pulling in ``config.settings`` (which has a 3.11/3.12 f-string
# compatibility bump pending). Re-exported under the old private names
# so any existing reference inside this module keeps working.
from utils.audio_utils import (
    copy_audio_to_local_for_processing as _copy_audio_to_local_for_processing,
    purge_processing_temp as _purge_processing_temp,
    _processing_temp_dir,
)

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

# ── Live capture-confidence meters (mic_level / system_level) ──────────
#
# RMS → 0..1 mapping for the frontend meter bars. Raw linear RMS reads
# terribly on a meter: normal conversational speech (roughly -30 to
# -20 dBFS in this app's float32 capture) would sit at 3-10% width and
# look broken/dead even when capture is perfectly healthy, because
# human hearing (and speech level) is logarithmic. We convert to a
# dB-ish scale and clamp to a fixed window, so:
#
#   LEVEL_FLOOR_DB (-60 dBFS) → 0.0   (at/below the noise floor)
#   LEVEL_CEIL_DB  (-12 dBFS) → 1.0   (loud / approaching clipping)
#
# Anchor points against the constants already used elsewhere in this
# file, so the meter's "this counts as real audio" reading lines up
# with what the watchdog considers real audio:
#   SILENCE_RMS_FLOOR  (-50 dBFS, "someone is talking" floor) → ~0.17
#   DUCK_LEVEL_THRESHOLD (-34 dBFS, "quiet speech band")      → ~0.54
# i.e. quiet-but-real speech lands right around the middle of the
# meter, not pinned near zero — the failure mode this feature exists
# to prevent is a meter so compressed toward zero that "flowing" and
# "dead" look identical at a glance.
LEVEL_FLOOR_DB = -60.0
LEVEL_CEIL_DB = -12.0

def _rms_to_level(rms: float) -> float:
    """Map a linear-amplitude RMS value to a 0.0-1.0 meter width using
    the dB window documented above. Never raises — feeds directly into
    a UI meter, so a bad input degrades to 0.0 rather than crashing the
    status endpoint."""
    try:
        if rms <= 0.0 or not np.isfinite(rms):
            return 0.0
        db = 20.0 * np.log10(max(rms, 1e-9))
        level = (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)
        return float(min(1.0, max(0.0, level)))
    except Exception:
        return 0.0


# Stream-state thresholds (per mic / system-audio stream independently).
#
#   flowing — chunks are arriving AND a recent one cleared the "real
#             audio" floor (SILENCE_RMS_FLOOR for mic, DUCK_LEVEL_
#             THRESHOLD for system — the same floors the existing
#             dead-air watchdog already uses for each stream).
#   silent  — chunks are arriving but nothing has cleared that floor
#             recently. NORMAL: a muted mic, or a meeting where only
#             the far end is talking. Must never produce a warning.
#   dead    — no chunks have arrived at all recently. This is the
#             failure mode this feature exists to catch: the recording
#             is "running" but the capture path silently broke.
#
# STREAM_DEAD_S is intentionally short (chunks land at ~10 Hz per the
# capture-stall detector above) so the meter itself flips to "dead"
# quickly — the meter's job is fast, honest feedback. CAPTURE_WARNING_
# DEAD_S is deliberately longer: it gates the actionable banner text,
# so a single dropped chunk or a brief device hiccup doesn't cry wolf
# and train the user to ignore the banner when it matters. 45s mirrors
# "the next morning" horror story this feature exists to prevent, while
# still surfacing well within the meeting instead of after it.
STREAM_DEAD_S = 3.0
STREAM_FLOWING_RECENCY_S = 2.0
CAPTURE_WARNING_DEAD_S = 45.0
CAPTURE_WARNING_GRACE_S = 5.0  # spin-up grace, mirrors capture_stalled above


def _stream_state(
    last_chunk_at: Optional[datetime],
    last_audio_at: Optional[datetime],
    now: datetime,
) -> str:
    """Classify one stream (mic or system) as flowing/silent/dead from
    its two tracked timestamps. Pure function — no I/O, easy to unit
    test with injected timestamps instead of real sleeps."""
    if last_chunk_at is None:
        return "dead"
    if (now - last_chunk_at).total_seconds() >= STREAM_DEAD_S:
        return "dead"
    if (last_audio_at is not None
            and (now - last_audio_at).total_seconds() <= STREAM_FLOWING_RECENCY_S):
        return "flowing"
    return "silent"


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
        # Domain glossary used to bias transcription (Whisper
        # initial_prompt) and to correct known mis-hears afterward.
        # Optional + settable so server.py can wire it without changing
        # the constructor signature; None → transcription behaves exactly
        # as before.
        self.terminology = None
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
        # Mirrors _loopback_level_ema above but for the mic stream — same
        # EMA smoothing approach (DUCK_SMOOTHING blend), fed from the RMS
        # _on_audio_chunk already computes for the silence-floor check
        # (no new RMS math on the audio path). Surfaced on /recording/
        # status as mic_level so the frontend can render a live meter —
        # see the "capture confidence" feature this whole block supports.
        self._mic_level_ema: float = 0.0
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
        # Per-stream capture-confidence bookkeeping (mic side reuses
        # _last_chunk_at above as "last chunk received at all"; loopback
        # gets its own since it wasn't previously tracked independently).
        # The "_audio_at" timestamps below are narrower than "_chunk_at":
        # only chunks that clear that stream's real-audio floor bump
        # them, so flowing vs. silent can be told apart from chunks vs.
        # no-chunks. See _stream_state() near the top of this module.
        self._last_mic_audio_at: Optional[datetime] = None
        self._last_loopback_chunk_at: Optional[datetime] = None
        self._last_loopback_audio_at: Optional[datetime] = None

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
        """Attach a captured screenshot to the active session's
        in-memory list, which stays authoritative for the running
        recording. The full session (including this list) is written
        to the session JSON on stop/process as always; server.py's
        /recording/screenshot handler additionally mirrors it to disk
        right away, best-effort, so a mid-recording crash doesn't
        orphan a screenshot that was already taken. Returns False if
        there's no session or the file doesn't exist."""
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

    def _build_live_speaker_tracker(self):
        """Best-effort construction of the live "them"-speaker splitter.

        Field report 2026-08-10 (Zoom notetaker parity): splits the
        loopback stream's single "them" bucket into distinct
        Speaker N labels (or a known SpeakerProfile's real name) inside
        the live preview, using the same ECAPA encoder the canonical
        post-stop pipeline already depends on
        (core/speaker_embeddings.py).

        Progressive enhancement, never a hard dependency: if
        speechbrain/torch aren't importable (is_available() False —
        same probe core/speaker_embeddings.py's own callers use), we
        return None and LiveTranscriber degrades to its pre-existing
        behavior — every loopback segment stays plain "them" with no
        speaker_label field. Any import/construction failure is
        swallowed the same way live transcription itself degrades
        (see the try/except around this call in start_recording).

        Kill switch: `live_speaker_split_enabled` (default True). When
        the user turns it off we return None here, which is the same
        code path as "speechbrain isn't installed" — every loopback
        segment keeps the plain "them" label and NO embedding work
        happens at all (LiveTranscriber only calls assign() when a
        tracker is wired in). Field report 2026-08-11: the live splitter
        over-split one voice into nine labels, so there has to be a way
        to switch the whole feature off without downgrading the app.
        """
        if not bool(getattr(
                self._settings, "live_speaker_split_enabled", True)):
            logger.info("Live speaker splitting disabled by user setting; "
                        "loopback segments stay labelled 'them'.")
            return None
        try:
            from core.speaker_embeddings import embed_utterance, is_available
            if not is_available():
                return None
            from core.live_speakers import (
                LiveSpeakerTracker, PROFILE_NAME_THRESHOLD)

            profile_service = self._profile_service

            def _profile_lookup(embedding):
                if profile_service is None:
                    return None
                # Ask the profile store with the LIVE naming bar, not
                # its own post-stop default (0.75). Field report
                # 2026-08-11: at 0.75 a female speaker was labelled
                # "CALEB JOHNSON" mid-call. LiveSpeakerTracker re-checks
                # the returned similarity against the same constant —
                # belt and braces, because a wrong name is the worst
                # failure mode this feature has.
                match = profile_service.find_match(
                    embedding, threshold=PROFILE_NAME_THRESHOLD)
                if match is None:
                    return None
                profile, similarity = match
                return profile.display_name, similarity

            return LiveSpeakerTracker(
                embed_fn=embed_utterance,
                profile_lookup=_profile_lookup,
            )
        except Exception as e:
            logger.warning(f"Live speaker tracker unavailable: {e}")
            return None

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
        # Stamp the planned final audio path NOW so the JSON stub we
        # write next has a recovery target even if finalize never runs.
        # The file itself doesn't exist yet — it will after stop. See
        # _write_session_stub() for the survives-crash rationale.
        self._session.audio_path = self._build_audio_path(session_id)
        self._write_session_stub(self._session)

        # Start per-session log file
        self._start_session_log(session_id)

        self._recording = True
        self._chunk_count = 0
        self._last_chunk_at = None
        self._loopback_level_ema = 0.0
        self._mic_level_ema = 0.0
        self._last_mic_audio_at = None
        self._last_loopback_chunk_at = None
        self._last_loopback_audio_at = None
        recordings_dir = Path(self._settings.recordings_dir)
        recordings_dir.mkdir(parents=True, exist_ok=True)
        # Active-capture temp WAVs go to a LOCAL-only dir, never under
        # recordings_dir. When recordings_dir is on OneDrive / iCloud /
        # Dropbox, the cloud sync engine's filter driver fights the
        # capture thread for file-lock + write bandwidth: every audio
        # sample queues, the ring buffer overflows, samples are dropped
        # silently, and the result is exactly the field repro on the
        # SimpliSafe 2h25m call — "you got 28 min of audio in a 149-min
        # recording — about 121 min appears to be missing", with
        # mic↔loopback drift of multiple thousand seconds. The final
        # merged WAV still writes to recordings_dir on stop (one-shot
        # write, OneDrive can absorb that fine).
        capture_dir = _local_capture_dir()
        self._loopback_temp_path = str(
            capture_dir / f"_loopback_{session_id}.wav"
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

        # Open a temp WAV file to stream audio to disk during recording.
        # MUST be the local-only capture dir, NOT recordings_dir — see
        # the longer comment at the loopback-temp assignment above for
        # the OneDrive contention story.
        try:
            capture_dir = _local_capture_dir()
            self._wav_temp_path = str(
                capture_dir / f"_recording_{session_id}.wav"
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
                        speaker_tracker=self._build_live_speaker_tracker(),
                    )
                # In conference room mode the mic is capturing multiple
                # in-room people, not "you" specifically — pass the room
                # label so live segments render with a neutral badge
                # rather than "You".
                self._live_transcriber.start(
                    LIVE_SR, conference_room_mode=conference_room_mode,
                    vad_enabled=bool(
                        getattr(self._settings, "live_vad_enabled", True)),
                )
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

        # DATA-LOSS FIX (field repro 2026-06-15): snapshot `self._session`
        # into a LOCAL variable at the top of stop_recording. The prior
        # implementation read `self._session` ~25 times across the stop
        # path (audio_path assignment, ended_at, sync-integrity stamping,
        # final-path computation). If a concurrent processing path called
        # `set_session(other)` while stop_recording was running (the
        # exact race that fired on 2026-06-15 when NLX's auto-process
        # called set_session(NLX) mid-Ricoh-recording), every later
        # `self._session` read would resolve to the WRONG session — and
        # `_build_audio_path(self._session.session_id)` then wrote
        # Ricoh's audio to NLX's WAV path, while `self._session.ended_at
        # = ...` stamped NLX's JSON with Ricoh's stop time. Both halves
        # of the visible bug came from this single shared-mutable-state
        # read. Capture `_session` exactly once here; the local survives
        # any concurrent reassignment.
        session = self._session

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
        # Grab read-only capture telemetry before we tear the capture down,
        # for the sync-integrity report computed after finalize.
        try:
            capture_stats = (
                self._capture.get_capture_stats() if self._capture else None)
        except Exception as e:
            logger.warning(f"[stop] get_capture_stats failed: {e}")
            capture_stats = None
        logger.info("[stop] capture.stop() …")
        t = _t.monotonic()
        try:
            self._capture.stop()
        except Exception as e:
            logger.exception(f"[stop] capture.stop raised: {e}")
        logger.info(f"[stop] capture.stop done in {_t.monotonic()-t:.1f}s")
        self._capture = None

        # TIMING FIX: stamp ended_at HERE — the moment capture actually
        # stops — not after finalize returns. Finalize (WAV merge,
        # optional AEC, resample) is a subprocess spawned further down
        # and can legitimately take minutes on a long meeting (278s
        # observed with AEC on); the old code stamped ended_at AFTER
        # that subprocess returned, so `expected_s` (ended_at -
        # started_at, used by both AUDIO_INTEGRITY and SYNC_INTEGRITY
        # below) silently included the entire finalize duration. Field
        # proof: two AEC-enabled stops showed mic_gap tracking finalize
        # duration to within 0.5s (278.5s finalize -> 278.9s gap; 144.8s
        # -> 145.2s) — no audio was lost, finalize time was just being
        # counted as capture time. Finalize's own cost is now recorded
        # separately in session.finalize_duration_s once it completes.
        if session:
            session.ended_at = datetime.now()

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

        if session and self._chunk_count > 0 and self._wav_temp_path:
            # Stream-merge mic + loopback into final WAV with bounded memory.
            # Earlier versions sf.read() both files fully into RAM before
            # mixing — a 36-minute 48kHz session allocates ~2-3 GB and can
            # trigger a native STATUS_ACCESS_VIOLATION on stop (lost session).
            loopback_path = getattr(self, '_loopback_temp_path', None)
            final_path = self._build_audio_path(session.session_id)
            # Re-write the stub session JSON with started_at + planned
            # audio_path BEFORE finalize. If finalize segfaults the
            # process (the 8B88C1C3 case), the JSON is already on disk
            # and the session appears in the list with the temp WAVs
            # ready for recovery, instead of the meeting vanishing.
            self._write_session_stub(session)
            logger.info(
                f"[stop] finalize_recording_streaming → {final_path} …")
            t = _t.monotonic()
            echo_cancellation_requested = bool(
                getattr(self._settings, "echo_cancellation_enabled", False))
            try:
                # SUBPROCESS-ISOLATED FINALIZE (v2.12): the WAV merge
                # runs in a child process so a native crash here can't
                # take the backend down. The child reports duration +
                # loopback-mixed status via stdout; the parent parses
                # it and proceeds as before on success. On crash /
                # non-zero exit the helper raises RuntimeError, the
                # except block below stamps the session "Error saving
                # audio" and we LEAVE the temp WAVs on disk for the
                # next-launch recovery flow to pick up.
                duration_s, _, aec_outcome = self._run_finalize_subprocess(
                    mic_wav_path=self._wav_temp_path,
                    loopback_wav_path=loopback_path,
                    output_wav_path=final_path,
                    target_sr=TARGET_SR,
                    loopback_start_offset_s=loopback_start_offset_s,
                    echo_cancellation_enabled=echo_cancellation_requested,
                )
                session.audio_path = final_path
                # NOTE: ended_at is stamped earlier, right after
                # capture.stop() — see the [stop] capture.stop() block
                # above. Finalize's own cost lives here instead, kept
                # separate so it never gets folded into the capture
                # window (see Session.finalize_duration_s).
                finalize_elapsed_s = _t.monotonic() - t
                session.finalize_duration_s = float(finalize_elapsed_s)

                # AEC OUTCOME — persist distinctly for all three cases:
                # not requested / requested+decided / requested+no
                # decision. The last case (child crashed or otherwise
                # didn't emit AEC_RESULT despite the flag being on) must
                # never be indistinguishable from "AEC rejected" or "AEC
                # off" — that exact "unreadable outcome renders as clean"
                # failure mode has bitten this repo before (document
                # hits as "Untitled" sessions; extension posts as "never
                # posted").
                if not echo_cancellation_requested:
                    session.aec_outcome = {"requested": False}
                elif aec_outcome is not None:
                    session.aec_outcome = aec_outcome
                else:
                    session.aec_outcome = {
                        "requested": True,
                        "accepted": None,
                        "reason": "no_decision_returned",
                        "erle_db": None,
                        "residual_delay_ms": None,
                    }
                    logger.warning(
                        f"[finalize-subprocess] AEC was requested for "
                        f"session {session.session_id} but no AEC_RESULT "
                        f"came back from the finalize subprocess — "
                        f"recording outcome as unknown, not as rejected.")

                self._on_status("Recording saved. Ready to process.")
                logger.info(
                    f"Audio saved to {final_path} ({duration_s:.1f}s)")
                logger.info(
                    f"[stop] finalize done in {finalize_elapsed_s:.1f}s")

                # AUDIO INTEGRITY CHECK — compare the actual WAV
                # duration (returned by finalize_recording_streaming) to
                # the TRUE capture window (ended_at - started_at, where
                # ended_at is now stamped at capture.stop(), NOT after
                # finalize — see the [stop] capture.stop() block above).
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
                # is the failure mode we're guarding against. Because
                # expected_s no longer includes finalize time, a slow
                # (e.g. AEC-enabled) finalize can no longer trip this by
                # itself — it used to: a 278s finalize inflated
                # expected_s enough to fire a false 14%-deficit CRITICAL
                # here even though zero audio was lost.
                session.audio_actual_duration_s = float(duration_s)
                expected_s = (
                    session.ended_at - session.started_at
                ).total_seconds() if session.started_at else 0.0
                session.audio_expected_duration_s = float(expected_s)
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
                        # Deliberately NOT "about N min appears to be
                        # missing" as a bare claim — that phrasing was
                        # flatly wrong and alarming when what it actually
                        # measured (pre-fix) was finalize time, not lost
                        # audio. This check now compares against the true
                        # capture window only, and says so, while still
                        # calling out a genuine shortfall plainly.
                        msg = (
                            f"The saved audio ({actual_min:.0f} min) is "
                            f"shorter than the {expected_min:.0f}-min "
                            f"capture window — about {lost_min:.0f} min "
                            f"may not have been captured (a dropped "
                            f"device, contention, or write failure during "
                            f"recording). This does not include the "
                            f"{finalize_elapsed_s / 60.0:.1f}-min "
                            f"finalize/processing step afterward, which "
                            f"is tracked separately and is never counted "
                            f"as missing audio."
                        )
                        session.audio_integrity_warning = msg
                        logger.critical(
                            f"AUDIO_INTEGRITY: session "
                            f"{session.session_id}: actual="
                            f"{duration_s:.1f}s expected={expected_s:.1f}s "
                            f"deficit={deficit_ratio*100:.0f}% "
                            f"finalize_s={finalize_elapsed_s:.1f}")
                    elif duration_s > expected_s * 1.10:
                        # Actual longer than expected — also suspicious
                        # (a stale wav got concatenated maybe). Log but
                        # don't warn user; this case is rarer.
                        logger.warning(
                            f"AUDIO_INTEGRITY: session "
                            f"{session.session_id} wav is longer "
                            f"than wall-clock — actual={duration_s:.1f}s "
                            f"expected={expected_s:.1f}s (possible stale "
                            f"file concat) finalize_s={finalize_elapsed_s:.1f}")

                # SYNC-INTEGRITY (read-only measurement). Compares each
                # stream's delivered sample count to wall-clock elapsed: a
                # stream that delivered fewer samples than real time fell
                # behind (dropped frames or clock drift), and divergence
                # between the two streams is what knocks diarization out of
                # alignment. We only MEASURE + log/flag — no audio is
                # altered. This is the "measure first" data that tells us
                # whether the heavier timestamp-anchoring correction is
                # actually warranted, and whether echo (if any) is physical
                # bleed vs. a sync artifact.
                #
                # `window` here is the true capture window (ended_at is
                # stamped at capture.stop(), before finalize spawns) —
                # `finalize_s` is logged alongside it so a slow finalize
                # is always legible as its own number, never folded into
                # (and mistaken for) the capture window or mic_gap.
                try:
                    if capture_stats and expected_s > 30.0:
                        msr = max(1, int(capture_stats.get("mic_sr") or 1))
                        lsr = max(1, int(capture_stats.get("loopback_sr") or 1))
                        mic_secs = capture_stats.get("mic_samples", 0) / msr
                        lb_samples = capture_stats.get("loopback_samples", 0)
                        lb_secs = (lb_samples / lsr) if lb_samples else None
                        mic_gap = expected_s - mic_secs
                        mic_ovf = capture_stats.get("mic_overflows", 0)
                        lb_ovf = capture_stats.get("loopback_overflows", 0)
                        offset = loopback_start_offset_s or 0.0
                        drift = (abs(mic_secs - (lb_secs + offset))
                                 if lb_secs is not None else None)
                        logger.info(
                            f"SYNC_INTEGRITY: session {session.session_id} "
                            f"mic={mic_secs:.1f}s "
                            f"lb={('%.1fs' % lb_secs) if lb_secs is not None else 'n/a'} "
                            f"window={expected_s:.1f}s mic_gap={mic_gap:.1f}s "
                            f"drift={('%.1fs' % drift) if drift is not None else 'n/a'} "
                            f"overflows(mic/lb)={mic_ovf}/{lb_ovf} "
                            f"finalize_s={finalize_elapsed_s:.1f}")
                        bits = []
                        if mic_gap > max(2.0, expected_s * 0.02):
                            bits.append(
                                f"mic captured {mic_secs/60:.0f} min of a "
                                f"{expected_s/60:.0f}-min window "
                                f"(~{mic_gap:.0f}s behind real time)")
                        if drift is not None and drift > 2.0:
                            bits.append(
                                f"mic and system-audio tracks drifted "
                                f"~{drift:.0f}s apart")
                        if mic_ovf + lb_ovf > 0:
                            bits.append(
                                f"{mic_ovf + lb_ovf} buffer overflow(s) during capture")
                        if bits:
                            session.sync_warning = (
                                "Sync integrity: " + "; ".join(bits) + ".")
                            logger.warning(
                                f"SYNC_INTEGRITY WARNING: session "
                                f"{session.session_id}: "
                                f"{session.sync_warning}")
                except Exception as e:
                    logger.warning(f"sync-integrity measurement failed: {e}")
            except Exception as e:
                finalize_elapsed_s = _t.monotonic() - t
                logger.exception(
                    f"[stop] finalize failed after {finalize_elapsed_s:.1f}s")
                self._on_status(f"Error saving audio: {e}")
                session.finalize_duration_s = float(finalize_elapsed_s)
                if echo_cancellation_requested:
                    # The whole finalize subprocess failed/crashed before
                    # any AEC_RESULT could come back — record that
                    # distinctly from both "not requested" and "AEC
                    # rejected the mic": neither is true here.
                    session.aec_outcome = {
                        "requested": True,
                        "accepted": None,
                        "reason": "finalize_subprocess_failed",
                        "erle_db": None,
                        "residual_delay_ms": None,
                    }
                else:
                    session.aec_outcome = {"requested": False}
            finally:
                # Clean up temps ONLY when the merge actually produced a
                # final WAV at the expected path. Pre-v2.11.1 we
                # gated on ``session.audio_path`` being truthy, but
                # v2.11.1's "JSON-first" change pre-sets audio_path at
                # start_recording so the post-crash recovery flow has
                # a target — that means truthy-audio_path now means
                # "planned location," not "merge succeeded." Gating on
                # actual file existence at the final path correctly
                # distinguishes the two, AND it preserves temps on the
                # v2.12 subprocess-finalize crash path (parent raised
                # RuntimeError → except branch logged the error → we
                # arrive here with no file at session.audio_path →
                # temps STAY, recovery on next launch picks them up).
                # KEEP_AUDIO_TEMPS=1 preserves them either way for
                # offline AEC validation via measure_aec.py.
                keep_temps = os.environ.get("KEEP_AUDIO_TEMPS") == "1"
                merge_succeeded = bool(
                    session.audio_path and Path(session.audio_path).exists()
                )
                if merge_succeeded and not keep_temps:
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
                elif not merge_succeeded:
                    logger.warning(
                        f"[stop] merge did not produce {session.audio_path} "
                        f"— preserving temps for recovery: "
                        f"{self._wav_temp_path} + {loopback_path}")
        elif session and self._chunk_count == 0:
            logger.warning("Recording stopped with no audio chunks captured.")
            self._on_status("No audio was captured. Try again.")

        self._wav_temp_path = None
        self._stop_session_log()
        logger.info(
            f"[stop] complete in {_t.monotonic()-stop_t0:.1f}s")
        return session

    async def process_session(self, session: Optional[Session] = None) -> Session:
        # DATA-LOSS FIX (field repro 2026-06-15): every reference inside
        # this method used to read `self._session`. If a NEW recording
        # started while this process_session() was awaiting transcribe /
        # diarize, start_recording would do `self._session = Session(new)`
        # and the in-flight pipeline would silently switch to operating
        # on the WRONG session — the new recording's session object got
        # the previous meeting's transcript, and the original meeting's
        # WAV got overwritten by the new meeting's audio when stop_
        # recording later finalized through the now-aliased reference.
        # Field repro on 2026-06-15: NLX Training (56105413) had its
        # session_56105413.wav overwritten by the next meeting's 32-min
        # audio, and the Ricoh session ended up with NLX's transcript +
        # summary attached. Ricoh's session log carries the smoking gun:
        #   [stop] finalize_recording_streaming → ...\session_56105413.wav
        # written from inside Ricoh's stop, because Ricoh's `_session`
        # had been reassigned to NLX by the auto-process path that ran
        # while Ricoh was still recording.
        #
        # The fix is to never read `self._session` here — the caller
        # passes the exact session to process. The optional default
        # keeps backwards compatibility for any direct call site, but
        # both call sites in server.py now pass the session explicitly.
        if session is None:
            session = self._session
        if not session or not session.audio_path:
            raise RuntimeError("No recorded session to process.")
        if not self.can_process:
            raise RuntimeError(
                "AI models not loaded. Add API keys in File > Settings "
                "and restart the app to enable transcription and diarization.")

        # READ-SIDE CLOUD-CONTENTION FIX (v2.12.0, field repro 2026-06-26):
        # Stream-copy the WAV to a LOCAL-only scratch path before any ML
        # read touches it. ``_ensure_audio_available`` (kept for callers
        # that don't need the full copy) does a single hydration read;
        # that wasn't enough — two backend segfaults today crashed in
        # the read side after hydration "succeeded," because the cloud
        # sync filter driver intermittently returns ENOENT or partial
        # reads while it negotiates the file's state. Working from a
        # local copy makes the entire ML pipeline immune to that
        # contention class. The local copy is purged in the ``finally``
        # below so disk doesn't grow over time.
        local_audio_path = await asyncio.to_thread(
            _copy_audio_to_local_for_processing,
            session.audio_path, session.session_id,
        )
        try:
            self._on_status("__stage:transcribe:active__")
            # Bias Whisper toward the user's domain vocabulary when a glossary
            # is configured. Best-effort — any failure building the prompt
            # falls back to plain transcription.
            initial_prompt = ""
            if self.terminology is not None:
                try:
                    initial_prompt = self.terminology.build_initial_prompt()
                except Exception as e:
                    logger.warning(f"terminology initial_prompt build failed: {e}")
            raw_segments = await self._transcription.transcribe(
                local_audio_path, initial_prompt=initial_prompt)

            if not raw_segments:
                self._on_status("Transcription produced no output. Check audio quality.")
                return session

            # Post-transcription correction of known mis-hears (Genesys,
            # CCaaS, UCCX, MEDDIC, ...) so the canonical spelling propagates
            # into every downstream extraction. Best-effort per segment.
            if self.terminology is not None:
                try:
                    for raw in raw_segments:
                        raw["text"] = self.terminology.apply_corrections(
                            raw.get("text", ""))
                except Exception as e:
                    logger.warning(f"terminology correction pass failed: {e}")

            self._on_status("__stage:transcribe:done____stage:diarize:active__")
            diarization_turns = await self._diarization.diarize(local_audio_path)

            self._on_status("__stage:diarize:done____stage:speakers:active__")
            attributed = DiarizationEngine.assign_speakers(raw_segments, diarization_turns)

            # REPLACE, don't append. process_session must be idempotent: re-
            # transcribing a session (recovery, re-process) has to produce a
            # transcript for THIS audio only, not concatenate onto whatever
            # was there before. Appending is what let two meetings merge into
            # one session (the wrong transcript got tacked onto the right one,
            # then summarized as a blend). Clear segments + speakers first so
            # the result reflects exactly this diarization pass.
            session.segments = []
            session.speakers = {}
            for raw in attributed:
                speaker = session.get_or_create_speaker(raw["speaker_id"])
                segment = Segment(
                    speaker_id=speaker.speaker_id,
                    start=raw["start"],
                    end=raw["end"],
                    text=raw["text"],
                )
                session.segments.append(segment)

            # Speaker fingerprinting: compute per-speaker centroid embeddings
            # from the diarization turns, then look up matches in the
            # persistent profile store. Best-effort — any failure here is
            # logged and the session is still saved with bare SPEAKER_XX
            # labels (i.e. v1 behaviour). Reads through ``local_audio_path``
            # so it gets the same cloud-contention immunity.
            try:
                self._fingerprint_speakers(
                    diarization_turns, session,
                    audio_path_override=local_audio_path,
                )
            except Exception as e:
                logger.exception(f"Speaker fingerprinting failed: {e}")

            self._on_status("Processing complete.")
            logger.info(f"Session {session.session_id} processing complete.")
            return session
        finally:
            # Always clean up the local working copy. A future re-process
            # re-copies from the canonical (cloud-side) audio_path, so
            # leaving the scratch on disk would just bloat %TEMP%.
            await asyncio.to_thread(
                _purge_processing_temp, session.session_id)

    def _fingerprint_speakers(
        self, diarization_turns: List[dict],
        session: Optional[Session] = None,
        audio_path_override: Optional[str] = None,
    ) -> None:
        """Extract per-speaker centroids from the audio + diarization
        turns, attach them to each Speaker object, and apply any matches
        from the persistent profile store as auto-renames awaiting user
        confirmation.

        Called from process_session() after segments + speakers are
        populated. Takes the session as an explicit parameter — same
        data-loss-bug rationale as process_session above; never read
        self._session here, that race is exactly how the 2026-06-15
        NLX/Ricoh meeting overwrite happened. Safe to call when
        self._profile_service is None (skips matching, still computes +
        stores embeddings so a future session can match them later if
        profiling gets enabled).

        ``audio_path_override`` lets the caller pin the actual file
        path the centroid extractor reads from — process_session passes
        the local-scratch working copy here so fingerprinting reads
        through the same cloud-contention-immune path as transcribe +
        diarize."""
        if session is None:
            session = self._session
        if not session or not session.audio_path:
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

        audio_for_centroids = audio_path_override or session.audio_path
        centroids = extract_speaker_centroids(
            audio_for_centroids, turns_by_speaker,
        )
        if not centroids:
            return

        for speaker_id, centroid in centroids.items():
            speaker = session.speakers.get(speaker_id)
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

    # ── Capture-confidence meters ───────────────────────────────────
    #
    # Cheap, pure read of state that _on_audio_chunk / _on_loopback_chunk
    # already maintain — no locks beyond a plain attribute read (these
    # are simple float/datetime assignments from a single producer
    # thread each; torn reads would at worst show one stale tick, never
    # a crash). Server.py's /recording/status calls this on every poll,
    # same pattern as watchdog_tick().
    #
    # IMPORTANT: silence is not failure. A muted mic, or a meeting where
    # only the far end is talking, is the "silent" state and must NEVER
    # produce capture_warning — only genuine absence of chunks (the
    # stream stopped arriving entirely) does. Getting this wrong trains
    # the user to ignore the warning that actually matters.
    def get_capture_levels(self) -> dict:
        """Return the live mic/system capture-confidence snapshot:

            {
              "mic_level": float,             # 0.0-1.0 meter width
              "system_level": float,          # 0.0-1.0 meter width
              "mic_state": "flowing"|"silent"|"dead",
              "system_state": "flowing"|"silent"|"dead"|None,
              "capture_warning": str | None,
            }

        system_state/system_level are None/0.0 when this recording has
        no system-audio device configured at all (self._loopback_temp_path
        is None) — that's a deliberate choice not to record, not a
        capture failure, so it must read as "not applicable", never as
        "dead".
        """
        if not self._recording:
            return {
                "mic_level": 0.0,
                "system_level": 0.0,
                "mic_state": "dead",
                "system_state": None,
                "capture_warning": None,
            }

        now = datetime.now()
        system_configured = getattr(self, "_loopback_temp_path", None) is not None

        mic_state = _stream_state(self._last_chunk_at, self._last_mic_audio_at, now)
        mic_level = _rms_to_level(self._mic_level_ema)

        if system_configured:
            system_state = _stream_state(
                self._last_loopback_chunk_at, self._last_loopback_audio_at, now)
            system_level = _rms_to_level(self._loopback_level_ema)
        else:
            system_state = None
            system_level = 0.0

        # capture_warning: only for a stream that is genuinely dead (no
        # chunks at all) for longer than the actionable threshold, past
        # the post-start grace period. Deliberately independent of
        # mic_state/system_state's shorter STREAM_DEAD_S so the meter
        # itself is responsive while the banner stays conservative.
        capture_warning: Optional[str] = None
        started_at = self._session.started_at if self._session else None
        elapsed_s = (now - started_at).total_seconds() if started_at else 0.0
        if elapsed_s > CAPTURE_WARNING_GRACE_S:
            mic_dead_s = (
                (now - self._last_chunk_at).total_seconds()
                if self._last_chunk_at else elapsed_s
            )
            if mic_dead_s >= CAPTURE_WARNING_DEAD_S:
                capture_warning = (
                    f"No microphone audio for {int(mic_dead_s)} seconds — "
                    f"capture may have stopped. Consider stopping and "
                    f"restarting the recording.")
            elif system_configured:
                sys_dead_s = (
                    (now - self._last_loopback_chunk_at).total_seconds()
                    if self._last_loopback_chunk_at else elapsed_s
                )
                if sys_dead_s >= CAPTURE_WARNING_DEAD_S:
                    capture_warning = (
                        f"No system audio for {int(sys_dead_s)} seconds — "
                        f"capture may have stopped. Consider stopping and "
                        f"restarting the recording.")

        return {
            "mic_level": mic_level,
            "system_level": system_level,
            "mic_state": mic_state,
            "system_state": system_state,
            "capture_warning": capture_warning,
        }

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
            now = datetime.now()
            if rms > SILENCE_RMS_FLOOR:
                self._last_speech_at = now
                self._last_mic_audio_at = now
            # Capture-confidence meter: same EMA smoothing as the
            # existing loopback level (_loopback_level_ema), fed from
            # the RMS we already computed above — no extra RMS math on
            # the audio path. Kept inside this same try/except so a
            # bad chunk degrades the meter, never the recording.
            self._mic_level_ema = (
                DUCK_SMOOTHING * rms
                + (1.0 - DUCK_SMOOTHING) * self._mic_level_ema
            )
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
        # Capture-confidence bookkeeping: this chunk arrived at all,
        # regardless of its level — mirrors _last_chunk_at on the mic
        # side. Outside the try/except below so a downstream RMS bug
        # can never make the system stream look "dead" when it isn't.
        self._last_loopback_chunk_at = datetime.now()
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
            # counts actual far-end speech. Same floor used by
            # _stream_state() to decide flowing vs. silent for the
            # system-audio meter.
            if chunk_rms > DUCK_LEVEL_THRESHOLD:
                now = datetime.now()
                self._last_speech_at = now
                self._last_loopback_audio_at = now
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
            # The session log writes to a LOCAL-only temp dir during the
            # capture, then gets copied to recordings_dir on stop. v2.10.5
            # moved the WAV temps off cloud-synced recordings_dir to fix
            # the catastrophic drift bug; we missed this FileHandler.
            #
            # The handler is attached to the ROOT logger at DEBUG level,
            # so every logger.info / logger.debug from ANY thread —
            # including the sounddevice audio-capture callback — writes
            # synchronously through this handler. When recordings_dir is
            # on OneDrive / Google Drive / iCloud, each write blocks on
            # the cloud sync driver (50–500 ms vs ~50 μs to local NTFS).
            # The audio ring buffer overflows under the resulting micro-
            # stalls and samples are dropped on whichever capture thread
            # logged. Field repro on 2.10.5: 55-min Demo Discussion
            # Emily session, mic↔loopback drift ~1215 s (≈22 s per min)
            # — the WAVs were on local temp, but the session log was
            # still on G:\My Drive\Recordings\.
            #
            # Capture path goes to %TEMP%\meeting_recorder_capture\ ;
            # _stop_session_log copies the finished log to
            # recordings_dir as a one-shot write that the cloud sync can
            # absorb without contention.
            capture_dir = _local_capture_dir()
            self._session_log_temp = str(
                capture_dir / f"session_{session_id}.log")
            self._session_log_final = str(
                Path(self._settings.recordings_dir).resolve()
                / f"session_{session_id}.log")
            handler = logging.FileHandler(
                self._session_log_temp, encoding="utf-8")
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(SESSION_LOG_FMT))
            logging.getLogger().addHandler(handler)
            self._session_log_handler = handler
            logger.info(
                f"Session log started: {self._session_log_temp} "
                f"(will copy to {self._session_log_final} on stop)")
        except Exception as e:
            logger.warning(f"Could not create session log file: {e}")

    def _stop_session_log(self) -> None:
        if self._session_log_handler:
            logger.info("Session log closed.")
            logging.getLogger().removeHandler(self._session_log_handler)
            self._session_log_handler.close()
            self._session_log_handler = None
            # Copy the local-only temp log into recordings_dir so it
            # rides cloud sync alongside the WAV and JSON. One-shot
            # copy, no contention with an active capture thread.
            src = getattr(self, "_session_log_temp", None)
            dst = getattr(self, "_session_log_final", None)
            if src and dst:
                try:
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    import shutil
                    shutil.copy2(src, dst)
                    try:
                        Path(src).unlink()
                    except OSError:
                        pass
                    logger.info(f"Session log copied to {dst}")
                except Exception as e:
                    # Non-fatal — the log still exists in the temp dir
                    # for local debugging; user-visible session content
                    # (WAV, JSON) is unaffected.
                    logger.warning(
                        f"Could not copy session log to {dst}: {e}")
            self._session_log_temp = None
            self._session_log_final = None

    def _build_audio_path(self, session_id: str) -> str:
        recordings_dir = Path(self._settings.recordings_dir)
        recordings_dir.mkdir(parents=True, exist_ok=True)
        return str(recordings_dir / f"session_{session_id}.wav")

    def _build_session_json_path(self, session_id: str) -> Path:
        recordings_dir = Path(self._settings.recordings_dir)
        recordings_dir.mkdir(parents=True, exist_ok=True)
        return recordings_dir / f"session_{session_id}.json"

    def _write_session_stub(self, session: "Session") -> None:
        """Atomically write the session's current state to its session
        JSON.

        SURVIVES-CRASH DESIGN (v2.11.1, field repro 2026-06-15): historically
        the session JSON was only written AFTER ``finalize_recording_streaming``
        completed in ``stop_recording``. A native segfault in scipy /
        libsndfile during finalize (the 8B88C1C3 case) killed the backend
        before the JSON was ever written — the meeting silently vanished
        from the Sessions list as if it had never been recorded, even
        though the temp WAVs were sitting on disk.

        We now write the JSON twice per recording:

        1. On ``start_recording`` — minimal stub with ``session_id``,
           ``started_at``, and the planned ``audio_path``. ``ended_at``
           stays ``None``. If anything crashes mid-recording the user
           still sees a "Recording in progress (recovery available)"
           row pointing at the right temp WAVs.
        2. On ``stop_recording`` — re-saved with ``ended_at``,
           ``audio_actual_duration_s``, sync-integrity fields, etc.
           AFTER finalize completes. If finalize itself segfaults the
           stub from step 1 stays on disk.

        Uses the same atomic temp-file + rename pattern as SessionService
        (a half-written JSON would be worse than no JSON). Failures are
        logged but not raised — capture must not be blocked by a JSON-
        write hiccup."""
        try:
            json_path = self._build_session_json_path(session.session_id)
            data = json.dumps(session.to_dict(), indent=2, ensure_ascii=False)
            fd, tmp_path = tempfile.mkstemp(
                dir=json_path.parent, suffix=".json.tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, json_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(
                f"Could not write session stub for "
                f"{session.session_id}: {e}")

    @staticmethod
    def _run_finalize_subprocess(
        mic_wav_path: str,
        loopback_wav_path: Optional[str],
        output_wav_path: str,
        target_sr: int,
        loopback_start_offset_s: Optional[float],
        echo_cancellation_enabled: bool = False,
    ) -> tuple[float, bool, Optional[dict]]:
        """Run the WAV merge in a subprocess instead of in-process.

        SURVIVES-NATIVE-CRASH DESIGN (v2.12): finalize reads two big
        WAVs, resamples one with scipy, and streams a mixed PCM_16
        output — every step is a C extension. A native crash in any
        of them used to take the entire Python backend down (the
        2026-06-15 8B88C1C3 STATUS_ACCESS_VIOLATION). v2.11.1 fixed
        the SPECIFIC cause we observed (Google Drive contention on
        the intermediate temp), but the broader single-point-of-
        failure remained.

        Running finalize as a child process makes any future native
        crash here ISOLATED: the parent observes a non-zero exit
        code, logs the cause, leaves the temp WAVs on disk, raises
        ``RuntimeError`` — and the backend keeps running. The session
        stub written by ``_write_session_stub`` ensures the row stays
        in the Sessions list with one-click recovery.

        Spawned with the same Python that runs the parent backend
        (``sys.executable``), so we get exactly the same numpy /
        scipy / soundfile / cffi build — no version skew.

        Returns ``(duration_s, loopback_mixed, aec_outcome)`` on success.
        ``aec_outcome`` is the dict parsed from the child's ``AEC_RESULT``
        stdout line, or ``None`` if the child never emitted one (either
        AEC wasn't requested, or it was requested but no decision came
        back — the caller must tell those two apart using the
        ``echo_cancellation_enabled`` flag it passed in, exactly like the
        "document hits as Untitled" / "extension posts as never posted"
        failure mode this codebase has hit before: an unreadable outcome
        must never quietly render as a clean one).

        Both the child's stdout and stderr are mirrored into
        backend.log at INFO level (bounded to the tail 4000 chars each)
        — this is the ONLY way the child's log lines (including
        audio_utils.py's "Offline AEC decision: ..." line, which uses
        the same get_logger() that always logs to the child's STDOUT)
        ever reach backend.log at all; earlier versions only mirrored
        stderr, so every log line the child emitted via logger.info(...)
        was silently dropped — a field pull for AEC decisions came back
        with nothing despite two AEC-enabled finalizes provably running.

        Raises ``RuntimeError`` on any failure, including subprocess
        crash — the message includes the exit code (negative numbers
        on POSIX = signal; large positive on Windows = NTSTATUS like
        0xC0000005 for STATUS_ACCESS_VIOLATION) and a tail of stderr.
        """
        script_path = (
            Path(__file__).resolve().parents[1] / "scripts" / "finalize_audio.py"
        )
        argv = [
            sys.executable,
            str(script_path),
            "--mic", mic_wav_path,
            "--loopback", loopback_wav_path or "",
            "--output", output_wav_path,
            "--target-sr", str(target_sr),
            "--offset",
            "" if loopback_start_offset_s is None
                else f"{loopback_start_offset_s:.6f}",
        ]
        if echo_cancellation_enabled:
            argv.append("--echo-cancellation")
        logger.info(
            f"[finalize-subprocess] spawn {' '.join(argv[1:])}"
        )
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                # No timeout — long meetings legitimately take minutes
                # to merge. The watchdog has its own per-step
                # instrumentation; a hung subprocess would surface
                # via the [stop] timing log.
            )
        except OSError as e:
            raise RuntimeError(
                f"could not spawn finalize subprocess: {e}"
            ) from e

        if proc.stderr:
            # Mirror child stderr into backend.log so its diagnostics
            # are visible at the same level the inline call's logs
            # used to land at. Trim to keep huge tracebacks bounded.
            tail = proc.stderr[-4000:]
            logger.info(f"[finalize-subprocess] stderr:\n{tail}")

        if proc.stdout:
            # Mirror child stdout too. utils.logger.get_logger's handler
            # is a StreamHandler(sys.stdout), so EVERY logger.info(...)
            # call the child makes (including audio_utils.py's "Offline
            # AEC decision: ..." line) lands here, not in stderr — before
            # this, only stderr was mirrored, so that entire log stream
            # was silently discarded past the single machine-parsed
            # RESULT line. Trimmed the same way stderr is.
            tail = proc.stdout[-4000:]
            logger.info(f"[finalize-subprocess] stdout:\n{tail}")

        if proc.returncode != 0:
            raise RuntimeError(
                f"finalize subprocess exited with code {proc.returncode} "
                f"(stderr tail: {proc.stderr[-400:]!r})"
            )

        # Parse "RESULT duration_s=X loopback_mixed=Y" and the optional
        # "AEC_RESULT <json>" line from stdout. Scan every line (not
        # break-on-first) so AEC_RESULT can appear before or after
        # RESULT.
        duration_s = 0.0
        loopback_mixed = False
        aec_outcome: Optional[dict] = None
        result_found = False
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("RESULT "):
                kv = dict(
                    part.split("=", 1) for part in line.split()[1:]
                    if "=" in part
                )
                try:
                    duration_s = float(kv.get("duration_s", "0"))
                except ValueError:
                    duration_s = 0.0
                loopback_mixed = kv.get("loopback_mixed", "false") == "true"
                result_found = True
            elif line.startswith("AEC_RESULT "):
                try:
                    aec_outcome = json.loads(line[len("AEC_RESULT "):])
                except (ValueError, TypeError) as e:
                    # Malformed JSON is exactly the "unreadable outcome"
                    # case — must not silently look like "no AEC" or
                    # "AEC accepted". Leave aec_outcome as None; the
                    # caller (which knows whether AEC was requested)
                    # is responsible for recording that distinctly.
                    logger.warning(
                        f"[finalize-subprocess] could not parse "
                        f"AEC_RESULT line ({e}): {line[:200]!r}")
                    aec_outcome = None

        if not result_found:
            raise RuntimeError(
                f"finalize subprocess exited 0 but emitted no RESULT line "
                f"(stdout tail: {proc.stdout[-200:]!r})"
            )

        return duration_s, loopback_mixed, aec_outcome
