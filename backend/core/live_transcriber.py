"""
Live transcription pipeline (dual-stream).

The recording service feeds two parallel sources into us:

  AudioCapture mic chunks      → recording_service → LiveTranscriber.push_audio
  AudioCapture loopback chunks → recording_service → LiveTranscriber.push_loopback
                                                              │
                                        buffered into two independent
                                        per-source buffers, chunked on
                                        SPEECH BOUNDARIES (VAD) — or, as
                                        a fallback, fixed non-overlapping
                                        15s windows
                                                              │
                                                              ▼
                                  one worker thread drains whichever
                                  buffer has a chunk ready first and
                                  runs faster-whisper on it
                                                              │
                                                              ▼
                                  segments tagged speaker="you" | "them",
                                  loopback segments additionally tagged
                                  speaker_label="Speaker N" | a known name
                                  fan out to every consumer queue
                                  (SSE clients drain via subscribe())

Why dual-stream instead of mixed:

  Whisper struggles with overlapping speakers and is biased toward the
  louder channel. The mic is typically much hotter than the OS loopback,
  so when we mixed them with a sum the loopback (everyone else on the
  call) got buried under the user's own voice and was effectively
  ignored. Transcribing the two sources independently fixes that AND
  gives us free speaker attribution for the live preview ("you" vs
  "them") with no diarization work.

Why one worker (not two):

  faster-whisper / CTranslate2 isn't documented thread-safe across
  concurrent transcribe() calls on the same model. A single worker that
  alternates between sources serializes inference for free without
  doubling memory by loading the model twice.

Why VAD-driven chunking (field report 2026-08-10, Zoom notetaker
parity):

  Fixed 15s windows meant a sentence spoken right after a window
  boundary sat in the buffer for up to 15 more seconds before its
  window filled and got transcribed — the live preview felt nothing
  like Zoom's notetaker, which shows text within a couple of seconds.
  core/vad.py's energy+zero-crossing detector finds utterance
  boundaries in each source's buffer; we flush a chunk as soon as we
  see a real pause after real speech, instead of waiting for a wall-
  clock timer. WINDOW_SECONDS / the fixed-window path stays in place
  as a fallback: `live_vad_enabled=False` (a Settings toggle) or a
  runtime VAD failure both fall back to it per source, so the live
  preview degrades to its old (slower but reliable) behavior rather
  than going silent. See `_SourceBuffer.try_vad_chunk` and
  `LiveTranscriber._try_drain_one`.

Why live speaker splitting happens on the SAME worker, after transcribe
returns (not a separate thread/queue):

  Embedding one short utterance with ECAPA is ~tens of milliseconds —
  cheap relative to the Whisper call that just ran on the same audio.
  Doing it inline on the worker thread, immediately after
  `_model.transcribe()` returns, keeps the single-worker /
  single-in-flight-model-call invariant simple: at any instant at most
  one of {faster-whisper, ECAPA} is running, on the same thread, so
  there's never a question of whether two model calls can overlap.
  A separate embedding queue would add a second thread whose failure
  modes (queue backlog, an embed racing a `reset()` on start()) would
  need their own reasoning for a win that isn't there — the inline cost
  is already small enough not to visibly delay the next chunk.

Caveats / known limitations:

- Fixed-window fallback still uses 15-second non-overlapping windows
  per source; boundary words occasionally split between two windows.
  Cosmetic, and only relevant when VAD is off or has failed.

- Per-source window timestamps advance by audio duration, not
  wallclock — so if loopback has a long quiet stretch (no system audio)
  its segment timestamps will drift behind wallclock until audio
  resumes. The UI displays segments in arrival order, so this is mostly
  invisible to the user, and the canonical post-stop transcript at
  /sessions/{id}/process uses the real audio timing.

- Model load is the caller's responsibility. If the engine isn't ready
  when push_audio fires, audio queues up but nothing transcribes until
  the engine arrives via the engine_provider closure.

- Live speaker labels ("Speaker 1", "Speaker 2", ...) are assigned per
  TRANSCRIBED CHUNK, not per individual Whisper sub-segment within that
  chunk. In VAD mode a chunk is usually one utterance from one speaker,
  so this is normally correct; on the fixed-window fallback (or a VAD
  chunk that happens to catch a fast back-and-forth) a chunk can contain
  more than one speaker's turns and they'll share a label until the
  next chunk. This is a live-preview approximation — the canonical
  post-stop transcript's pyannote diarization pass is unaffected and
  produces the authoritative per-turn speakers.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Callable, Deque, List, NamedTuple, Optional

import numpy as np

from core.vad import find_utterances
from core import decode_options
from utils.logger import get_logger

logger = get_logger(__name__)


WINDOW_SECONDS = 15.0
TARGET_SR = 16000
# Min remaining audio at stop time before we bother running Whisper on
# the tail. Sub-second tails are typically silence + a fade-out and
# produce empty segments.
MIN_TAIL_SECONDS = 1.0

# ── VAD-driven chunking (field report 2026-08-10) ───────────────────
#
# Trailing silence required after detected speech before we consider an
# utterance "closed" and safe to flush. Long enough that a natural
# breath mid-sentence doesn't trigger a premature cut; short enough that
# the user isn't waiting long after they actually stop talking.
VAD_TRAILING_SILENCE_S = 0.4
# Minimum total speech accumulated in the buffer before we'll flush
# early on trailing silence. Below this we keep buffering even after a
# pause — avoids firing off a Whisper call for a single "yeah".
VAD_MIN_SPEECH_S = 0.6
# Hard ceiling: force a flush regardless of VAD state once a source's
# buffer reaches this many seconds, so a continuous talker who never
# pauses long enough to close an utterance still gets published
# incrementally instead of waiting indefinitely.
VAD_HARD_CEILING_S = 8.0
# Audio kept before the first detected utterance in a flushed chunk, so
# the first phoneme of a fast talker isn't clipped by the VAD boundary.
VAD_PRE_ROLL_S = 0.2

# Speaker labels published on each segment. The frontend uses them to
# render a "You" / "Them" / "Room" badge. Stable string constants so
# the JSON contract with the frontend is explicit.
SPEAKER_YOU = "you"
SPEAKER_THEM = "them"
# Conference-room recordings: mic captures multiple in-room speakers,
# no system-audio loopback. We use a single "room" label for the
# whole live preview; the post-stop pyannote pass splits the audio
# into proper SPEAKER_00/01/… for the canonical transcript.
SPEAKER_ROOM = "room"


class VadChunk(NamedTuple):
    """One VAD-driven chunk ready to hand to Whisper.

    `start_offset_s` / `consumed_s` are both relative to the source's
    `next_window_start` AT THE TIME try_vad_chunk() WAS CALLED (i.e.
    before the caller advances it) — see `_try_drain_one` for how the
    caller turns these into an absolute segment timestamp and the next
    `next_window_start`.
    """
    audio: np.ndarray
    # Seconds from the old next_window_start to `audio`'s first sample
    # (i.e. how much leading silence before the pre-roll point was
    # discarded, NOT retained in the buffer).
    start_offset_s: float
    # Seconds to advance next_window_start by — covers the discarded
    # leading silence AND the emitted audio, i.e. everything removed
    # from the buffer this call.
    consumed_s: float


class _SourceBuffer:
    """One independent audio buffer, keyed by speaker label.

    Owns its own ring of numpy chunks, its own window-start counter,
    and its own lock. Doesn't run a worker — the parent LiveTranscriber
    drives a single shared worker that alternates between sources.

    Supports two chunking strategies, selected by `vad_enabled`:
      - VAD mode (default): `try_vad_chunk()` flushes on a detected
        speech boundary or the hard ceiling. See module docstring.
      - Fixed-window mode (fallback): `drain_window()` flushes exactly
        WINDOW_SECONDS of audio at a time, unconditionally — the
        original v1 behavior, used when VAD is disabled by settings or
        fails at runtime.
    `vad_enabled` is mutable so a runtime VAD failure can downgrade a
    single source to the fixed-window path for the rest of the
    recording without touching the other source or restarting anything.
    """

    def __init__(self, label: str, samplerate: int, vad_enabled: bool = True):
        self.label = label
        self.sr = samplerate
        self.vad_enabled = vad_enabled
        self.window_samples = int(samplerate * WINDOW_SECONDS)
        self._chunks: List[np.ndarray] = []
        self._chunk_samples = 0
        self._lock = threading.Lock()
        # Global timestamp where the next window/chunk starts, in
        # seconds since recording start. Whisper hands back
        # window-relative timings; we add this to get absolute timing
        # on every published segment.
        self.next_window_start: float = 0.0
        # Whether any audio has ever been pushed. Used at tail-flush time
        # so a never-used loopback buffer (mic-only sessions) doesn't
        # generate a spurious tail flush.
        self._received_audio = False
        self._push_count = 0

    def push(self, chunk: np.ndarray) -> None:
        if chunk.ndim > 1:
            chunk = chunk.mean(axis=1)
        block = np.ascontiguousarray(chunk, dtype=np.float32)
        with self._lock:
            self._chunks.append(block)
            self._chunk_samples += len(block)
            self._received_audio = True
            self._push_count += 1
            if self._push_count % 200 == 0:
                logger.info(
                    f"Live [{self.label}] chunks pushed: {self._push_count}, "
                    f"buffer={self._chunk_samples} samples")

    def drain_window(self) -> Optional[np.ndarray]:
        """Return the next full window if one's ready, else None."""
        with self._lock:
            if self._chunk_samples < self.window_samples:
                return None
            audio = (np.concatenate(self._chunks)
                     if self._chunks else np.zeros(0, dtype=np.float32))
            window = audio[: self.window_samples].copy()
            tail = audio[self.window_samples:]
            self._chunks = [tail] if len(tail) else []
            self._chunk_samples = len(tail)
        return window

    def try_vad_chunk(self) -> Optional[VadChunk]:
        """VAD-driven chunk emission. Returns a ready-to-transcribe
        chunk, or None if nothing is ready yet (still waiting for
        trailing silence, or the buffer is empty/pure silence).

        Never emits a chunk that is pure silence — if the buffer has no
        detected speech at all and has grown past the hard ceiling, we
        drop it (a muted participant's dead air must not grow this
        buffer forever) rather than hand Whisper a clip with nothing in
        it, which tends to hallucinate text on silence.
        """
        with self._lock:
            if self._chunk_samples == 0:
                return None
            audio = (np.concatenate(self._chunks)
                     if len(self._chunks) > 1 else self._chunks[0])
            n = len(audio)

            utterances = find_utterances(audio, self.sr)

            ceiling_samples = int(self.sr * VAD_HARD_CEILING_S)
            if not utterances:
                if n >= ceiling_samples:
                    logger.debug(
                        f"Live [{self.label}] dropped {n/self.sr:.1f}s "
                        f"of pure silence at the VAD hard ceiling")
                    self._chunks = []
                    self._chunk_samples = 0
                return None

            pre_roll_samples = int(self.sr * VAD_PRE_ROLL_S)
            trailing_samples = int(self.sr * VAD_TRAILING_SILENCE_S)
            min_speech_samples = int(self.sr * VAD_MIN_SPEECH_S)

            first_start = utterances[0][0]
            last_end = utterances[-1][1]
            total_speech = sum(e - s for s, e in utterances)
            trailing_available = n - last_end

            closed = (trailing_available >= trailing_samples
                      and total_speech >= min_speech_samples)
            force_flush = n >= ceiling_samples

            if not closed and not force_flush:
                return None

            chunk_start = max(0, first_start - pre_roll_samples)
            if closed:
                chunk_end = min(n, last_end + trailing_samples)
            else:
                # Hard-ceiling flush of a continuous talker who never
                # paused long enough to close an utterance naturally —
                # take everything buffered so far.
                chunk_end = n

            chunk = audio[chunk_start:chunk_end].copy()
            tail = audio[chunk_end:]
            self._chunks = [tail] if len(tail) else []
            self._chunk_samples = len(tail)

        return VadChunk(
            audio=chunk,
            start_offset_s=chunk_start / self.sr,
            consumed_s=chunk_end / self.sr,
        )

    def drain_tail(self) -> Optional[np.ndarray]:
        """Return whatever's left, or None if too short to be worth
        transcribing or if this source never received audio."""
        with self._lock:
            if not self._received_audio:
                return None
            if self._chunk_samples < int(self.sr * MIN_TAIL_SECONDS):
                self._chunks = []
                self._chunk_samples = 0
                return None
            audio = (np.concatenate(self._chunks)
                     if self._chunks else np.zeros(0, dtype=np.float32))
            self._chunks = []
            self._chunk_samples = 0
        return audio

    def clear(self) -> None:
        with self._lock:
            self._chunks = []
            self._chunk_samples = 0
        self.next_window_start = 0.0
        self._received_audio = False
        self._push_count = 0


class LiveTranscriber:
    """Streams mic + loopback audio into two independent windowed
    transcription pipelines, fanning the segments out to subscribers."""

    def __init__(
        self,
        engine_provider: Callable[[], object],
        samplerate: int = TARGET_SR,
        speaker_tracker: Optional[object] = None,
    ):
        # Indirection so we can construct LiveTranscriber before models
        # have loaded. engine_provider() returns the current
        # TranscriptionEngine or None — checked on every window.
        self._engine_provider = engine_provider
        self._sr = samplerate
        self._mic = _SourceBuffer(SPEAKER_YOU, samplerate)
        self._loopback = _SourceBuffer(SPEAKER_THEM, samplerate)
        self._consumers: List[queue.Queue] = []
        self._consumers_lock = threading.Lock()
        self._running = False
        self._worker: Optional[threading.Thread] = None
        # Rolling history of every published segment, kept so the live
        # co-pilot tick endpoint can read the last N minutes of transcript
        # without subscribing its own SSE queue. Bounded so a marathon
        # meeting can't grow this unboundedly — a typical 15s window
        # produces 1-4 segments, so 2000 holds ~2-3h of dense talking.
        self._history: Deque[dict] = deque(maxlen=2000)
        self._history_lock = threading.Lock()
        # Optional core.live_speakers.LiveSpeakerTracker — splits the
        # loopback "them" bucket into "Speaker N" / known display names.
        # None (the default) means the feature degrades to v1 behavior:
        # every loopback segment stays plain "them" with no
        # speaker_label field, and nothing about that path changes.
        # Typed as `object` here (not LiveSpeakerTracker) to avoid this
        # module importing core.live_speakers — and transitively
        # core.speaker_embeddings' lazy torch/speechbrain probe — at
        # import time; only `assign()` is ever called on it, duck-typed.
        self._speaker_tracker = speaker_tracker
        # Decode settings for the CURRENT recording. Set in start(),
        # not here: this object is constructed once and reused for every
        # subsequent recording (see RecordingService), so reading
        # Settings at construction would pin the first recording's
        # language and glossary for the life of the process.
        self._language: str = "en"
        self._initial_prompt: str = ""

    @property
    def is_running(self) -> bool:
        return self._running

    def start(
        self,
        samplerate: int,
        conference_room_mode: bool = False,
        vad_enabled: bool = True,
        language: str = "en",
        initial_prompt: str = "",
    ) -> None:
        """Reset state and spawn the worker. Idempotent if already running.

        conference_room_mode flips the mic source's label from "you" to
        "room" so the live transcript UI doesn't tag every line with
        "You" when the mic is actually capturing multiple in-room
        speakers. The loopback buffer is still created so the existing
        on_loopback_chunk plumbing has somewhere to write — but no
        loopback chunks ever arrive in this mode (RecordingService
        passes output_device_index=None upstream), so the buffer stays
        empty and the loopback transcription pipeline is effectively
        a no-op.

        vad_enabled selects speech-boundary chunking (default) vs the
        legacy fixed-15s-window path — see the module docstring. Wired
        from Settings.live_vad_enabled by recording_service.py.

        language ("auto" or an ISO code) and initial_prompt (the
        glossary bias) are read per recording rather than at
        construction, because this object outlives any one meeting.
        Before they existed the live path was hardcoded to English and
        got no glossary at all, so it mis-heard the very terms the
        glossary exists to correct while the batch transcript got them
        right — two transcripts of one meeting, disagreeing.
        """
        self._language = language or "en"
        self._initial_prompt = initial_prompt or ""
        if self._running:
            return
        self._sr = samplerate
        mic_label = SPEAKER_ROOM if conference_room_mode else SPEAKER_YOU
        self._mic = _SourceBuffer(mic_label, samplerate, vad_enabled=vad_enabled)
        self._loopback = _SourceBuffer(
            SPEAKER_THEM, samplerate, vad_enabled=vad_enabled)
        with self._history_lock:
            self._history.clear()
        if self._speaker_tracker is not None:
            # Fresh meeting, fresh speakers — "Speaker 1" from a
            # previous recording must not bleed into this one. Known
            # SpeakerProfile matches are unaffected (re-consulted live).
            try:
                self._speaker_tracker.reset()
            except Exception as e:
                logger.warning(f"Live speaker tracker reset failed: {e}")
        self._running = True
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="live-transcriber",
        )
        self._worker.start()
        logger.info(
            f"LiveTranscriber started — dual-stream, vad_enabled="
            f"{vad_enabled}, window={WINDOW_SECONDS}s (fallback), "
            f"sr={samplerate}Hz, samples_per_window={self._mic.window_samples}")

    def stop(self) -> None:
        """Stop accepting new audio and signal SSE clients with a None
        sentinel so they close their stream.

        We DO NOT block waiting for the worker thread to finish. The
        worker's `finally` block runs a tail flush — Whisper on the
        residual audio in both buffers — which can take 5-15 seconds
        on CPU. Blocking the recording-stop flow on that means the
        frontend's POST /recording/stop sees its fetch time out (Tauri's
        invoke timeout is shorter than indefinite browser fetch),
        showing "Failed to fetch" even though the file saves cleanly.
        Worse, the rest of stop_recording (close WAV, finalize merge,
        save session JSON) is delayed by the same amount.

        The tail flush is best-effort UI polish — the canonical
        post-stop transcript at /sessions/{id}/process produces the
        authoritative segments from the WAV file, so live tail segments
        that never get published are not a data-loss event. Letting the
        worker finish its Whisper call in the background and exit on
        its own is the right call.
        """
        if not self._running:
            return
        self._running = False
        # Drop the worker reference so a follow-up start() can spawn a
        # new one even if the previous tail-flush is still grinding.
        # The thread is daemon, so it can't block process exit.
        self._worker = None
        with self._consumers_lock:
            for q in self._consumers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._consumers.clear()
        logger.info(
            "LiveTranscriber stop signaled — worker will tail-flush in "
            "background and exit on its own")

    def push_audio(self, chunk: np.ndarray) -> None:
        """Append mic audio (mono float32 at self._sr) to the You buffer."""
        if not self._running:
            return
        self._mic.push(chunk)

    def push_loopback(self, chunk: np.ndarray) -> None:
        """Append system-audio loopback (mono float32 at self._sr) to the
        Them buffer. Independent of the mic buffer — different timing,
        different transcription pass."""
        if not self._running:
            return
        self._loopback.push(chunk)

    def subscribe(self, max_pending: int = 256) -> queue.Queue:
        """Return a Queue that will receive published segment dicts.

        A None value on the queue means the recording stopped — drain
        and close. SSE handler should call unsubscribe(q) on disconnect.
        """
        q: queue.Queue = queue.Queue(maxsize=max_pending)
        with self._consumers_lock:
            self._consumers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._consumers_lock:
            try:
                self._consumers.remove(q)
            except ValueError:
                pass

    def all_segments(self) -> List[dict]:
        """Snapshot of every segment published during the current
        recording (subject to the rolling history cap of 2000 entries).
        Used by the live-transcript rehydrate endpoint so the UI panel
        can repopulate after the user navigates away from the Record
        tab and back — without this, the panel resets to empty and
        new SSE events start fresh from whatever's next."""
        with self._history_lock:
            return list(self._history)

    def recent_segments(self, last_seconds: float = 600.0) -> List[dict]:
        """Snapshot of published segments whose `end` falls within the
        last `last_seconds` of the recording's timeline. The co-pilot
        tick endpoint calls this every ~45s to feed the LLM a rolling
        window — independent of the SSE fan-out (which is fire-and-forget
        and may have dropped chunks on slow consumers)."""
        with self._history_lock:
            snapshot = list(self._history)
        if not snapshot:
            return []
        cutoff = snapshot[-1].get("end", 0.0) - last_seconds
        return [s for s in snapshot if s.get("end", 0.0) >= cutoff]

    # ── Worker internals ─────────────────────────────────────────────

    def _publish(self, segment: dict) -> None:
        """Record into the rolling history, then fan out to every
        consumer. History is the source of truth for the co-pilot tick;
        fan-out is best-effort — a slow SSE client gets its new segments
        dropped (queue.Full) rather than backpressuring the worker."""
        with self._history_lock:
            self._history.append(segment)
        with self._consumers_lock:
            consumers = list(self._consumers)
        for q in consumers:
            try:
                q.put_nowait(segment)
            except queue.Full:
                logger.debug("Live segment dropped — consumer too slow")

    def _transcribe_window(
        self, source: _SourceBuffer, audio: np.ndarray, window_start: float,
    ) -> int:
        """Run faster-whisper on one window/chunk of audio from one
        source. Returns the count of non-empty segments published.

        For the loopback source, when a speaker tracker is wired in, we
        also assign ONE speaker_label for the whole chunk (see the
        module docstring's "live speaker labels" caveat for why it's
        per-chunk, not per Whisper sub-segment) — computed AFTER
        transcribe() returns, inline on this same worker thread. See
        the module docstring's "Why live speaker splitting happens on
        the same worker" section for why this doesn't need its own
        thread/queue.
        """
        engine = self._engine_provider()
        if engine is None:
            # Engine not loaded yet — drop the window. Better to show
            # silence than a sudden wall of buffered audio appearing
            # later. The /process pass produces the canonical transcript.
            logger.debug(
                f"Live window dropped [{source.label}] — engine not ready")
            return 0
        try:
            # Decode options come from core.decode_options, the same
            # place the batch pass gets them (core/transcription.py).
            # Two hand-built argument lists is how this path ended up
            # hardcoded to English AND without the glossary bias, so the
            # live transcript mis-heard the exact product and customer
            # terms the glossary exists to fix — while the transcript
            # written to disk got them right. `live=True` keeps the
            # cheaper beam and skips word timestamps; nothing else
            # differs between the two.
            segments_iter, _ = engine._model.transcribe(
                audio,
                **decode_options.build(
                    language=self._language,
                    initial_prompt=self._initial_prompt,
                    live=True,
                ),
            )
        except Exception as e:
            logger.exception(
                f"Live transcribe call raised [{source.label}]: {e}")
            return 0

        speaker_label: Optional[str] = None
        if (source is self._loopback and self._speaker_tracker is not None
                and len(audio) > 0):
            try:
                speaker_label = self._speaker_tracker.assign(audio, source.sr)
            except Exception as e:
                # Never let a speaker-ID failure take down the live
                # transcript — degrade to the plain "them" label for
                # this chunk only, exactly as if no tracker were wired.
                logger.debug(f"Live speaker assignment failed: {e}")
                speaker_label = None

        published = 0
        for s in segments_iter:
            text = (s.text or "").strip()
            if not text:
                continue
            segment = {
                "start": float(s.start) + window_start,
                "end": float(s.end) + window_start,
                "text": text,
                "speaker": source.label,
            }
            if speaker_label:
                segment["speaker_label"] = speaker_label
            self._publish(segment)
            published += 1
        return published

    def _try_drain_one(self, source: _SourceBuffer) -> bool:
        """If `source` has a chunk ready (VAD boundary, or a full fixed
        window in fallback mode), transcribe and publish it. Returns
        True if work was done, False otherwise."""
        if source.vad_enabled:
            try:
                result = source.try_vad_chunk()
            except Exception as e:
                # VAD itself failed (should be rare — find_utterances
                # doesn't raise on well-formed numpy input, but a
                # corrupt buffer state is cheaper to degrade past than
                # to debug live). Downgrade this source to the fixed-
                # window fallback for the rest of the recording rather
                # than let a VAD bug silence the live preview entirely
                # (field report 2026-08-10: Zoom notetaker parity work
                # must never make the live transcript LESS reliable
                # than it was before).
                logger.warning(
                    f"Live VAD chunking failed [{source.label}]: {e} — "
                    f"falling back to the fixed {WINDOW_SECONDS:.0f}s "
                    f"window path for the rest of this recording")
                source.vad_enabled = False
                result = None
            if result is not None:
                start = source.next_window_start + result.start_offset_s
                source.next_window_start += result.consumed_s
                t0 = time.time()
                count = self._transcribe_window(source, result.audio, start)
                elapsed = time.time() - t0
                logger.info(
                    f"Live VAD chunk [{source.label}] @ {start:.1f}s "
                    f"({len(result.audio)/source.sr:.2f}s audio) → "
                    f"{count} segments in {elapsed:.1f}s")
                return True
            if source.vad_enabled:
                # Nothing ready yet (still waiting on trailing silence
                # or more audio) — not a failure, just not our turn.
                return False
            # Else: VAD just failed above and downgraded this source —
            # fall through to the fixed-window path below on this same
            # call so we don't wait an extra loop iteration.

        window = source.drain_window()
        if window is None:
            return False
        start = source.next_window_start
        source.next_window_start += WINDOW_SECONDS
        t0 = time.time()
        count = self._transcribe_window(source, window, start)
        elapsed = time.time() - t0
        logger.info(
            f"Live window [{source.label}] @ {start:.1f}s → {count} "
            f"segments in {elapsed:.1f}s")
        return True

    def _run(self) -> None:
        """Single worker loop. Drains whichever source has a full window
        ready (mic first, then loopback) so a hot mic doesn't starve the
        loopback or vice versa. On stop() this loop exits and we run a
        tail flush against both buffers."""
        try:
            while self._running:
                worked = False
                # Mic first, then loopback — but if both have full
                # windows we'll come back around immediately and process
                # the other on the next iteration without sleeping.
                if self._try_drain_one(self._mic):
                    worked = True
                if self._try_drain_one(self._loopback):
                    worked = True
                if not worked:
                    # Half a second is short enough to feel responsive,
                    # long enough to avoid a busy-wait spin.
                    time.sleep(0.5)
        finally:
            for source in (self._mic, self._loopback):
                tail = source.drain_tail()
                if tail is None:
                    continue
                if source.vad_enabled:
                    # Same "never transcribe pure silence" rule as the
                    # live chunking path — a tail that's entirely quiet
                    # (e.g. the user stopped recording mid-pause) would
                    # otherwise hand Whisper a silent clip and risk a
                    # hallucinated line landing in the live preview's
                    # last moment. Best-effort: if VAD itself raises on
                    # this one-off tail clip, just transcribe it anyway
                    # rather than lose the tail over a detector bug.
                    try:
                        if not find_utterances(tail, source.sr):
                            logger.info(
                                f"Live tail [{source.label}] — pure "
                                f"silence, skipping "
                                f"({len(tail)/source.sr:.1f}s)")
                            continue
                    except Exception as e:
                        logger.debug(
                            f"Tail VAD check failed [{source.label}], "
                            f"transcribing anyway: {e}")
                start = source.next_window_start
                logger.info(
                    f"Live tail [{source.label}] @ {start:.1f}s "
                    f"({len(tail)/source.sr:.1f}s of audio)")
                try:
                    self._transcribe_window(source, tail, start)
                except Exception as e:
                    logger.exception(
                        f"Tail transcribe failed [{source.label}]: {e}")


def serialize_segment_sse(segment: dict) -> str:
    """Format a segment dict as a Server-Sent Events `data:` line."""
    return f"data: {json.dumps(segment)}\n\n"
