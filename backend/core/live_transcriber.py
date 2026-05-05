"""
Live transcription pipeline (dual-stream).

The recording service feeds two parallel sources into us:

  AudioCapture mic chunks      → recording_service → LiveTranscriber.push_audio
  AudioCapture loopback chunks → recording_service → LiveTranscriber.push_loopback
                                                              │
                                            buffered into two independent
                                            non-overlapping 15s windows
                                                              │
                                                              ▼
                                  one worker thread drains whichever
                                  buffer has a full window first and
                                  runs faster-whisper on it
                                                              │
                                                              ▼
                                  segments tagged speaker="you" | "them"
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

Caveats / known limitations:

- 15-second non-overlapping windows per source. Boundary words
  occasionally split between two windows. Cosmetic.

- Per-source window timestamps advance by audio duration, not
  wallclock — so if loopback has a long quiet stretch (no system audio)
  its segment timestamps will drift behind wallclock until audio
  resumes. The UI displays segments in arrival order, so this is mostly
  invisible to the user, and the canonical post-stop transcript at
  /sessions/{id}/process uses the real audio timing.

- Model load is the caller's responsibility. If the engine isn't ready
  when push_audio fires, audio queues up but nothing transcribes until
  the engine arrives via the engine_provider closure.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Callable, List, Optional

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)


WINDOW_SECONDS = 15.0
TARGET_SR = 16000
# Min remaining audio at stop time before we bother running Whisper on
# the tail. Sub-second tails are typically silence + a fade-out and
# produce empty segments.
MIN_TAIL_SECONDS = 1.0

# Speaker labels published on each segment. The frontend uses them to
# render a "You" / "Them" badge. Stable string constants so the JSON
# contract with the frontend is explicit.
SPEAKER_YOU = "you"
SPEAKER_THEM = "them"


class _SourceBuffer:
    """One independent windowed audio buffer, keyed by speaker label.

    Owns its own ring of numpy chunks, its own window-start counter,
    and its own lock. Doesn't run a worker — the parent LiveTranscriber
    drives a single shared worker that alternates between sources.
    """

    def __init__(self, label: str, samplerate: int):
        self.label = label
        self.sr = samplerate
        self.window_samples = int(samplerate * WINDOW_SECONDS)
        self._chunks: List[np.ndarray] = []
        self._chunk_samples = 0
        self._lock = threading.Lock()
        # Global timestamp where the next window starts, in seconds since
        # recording start. Whisper hands back window-relative timings;
        # we add this to get absolute timing on every published segment.
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

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, samplerate: int) -> None:
        """Reset state and spawn the worker. Idempotent if already running."""
        if self._running:
            return
        self._sr = samplerate
        self._mic = _SourceBuffer(SPEAKER_YOU, samplerate)
        self._loopback = _SourceBuffer(SPEAKER_THEM, samplerate)
        self._running = True
        self._worker = threading.Thread(
            target=self._run, daemon=True, name="live-transcriber",
        )
        self._worker.start()
        logger.info(
            f"LiveTranscriber started — dual-stream, window={WINDOW_SECONDS}s, "
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

    # ── Worker internals ─────────────────────────────────────────────

    def _publish(self, segment: dict) -> None:
        """Best-effort fan-out to every consumer. A slow client gets its
        new segments dropped (queue.Full) rather than backpressuring
        the worker — the canonical transcript at /process catches them."""
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
        """Run faster-whisper on one window of audio from one source.
        Returns the count of non-empty segments published."""
        engine = self._engine_provider()
        if engine is None:
            # Engine not loaded yet — drop the window. Better to show
            # silence than a sudden wall of buffered audio appearing
            # later. The /process pass produces the canonical transcript.
            logger.debug(
                f"Live window dropped [{source.label}] — engine not ready")
            return 0
        try:
            segments_iter, _ = engine._model.transcribe(
                audio, language="en", vad_filter=True,
            )
        except Exception as e:
            logger.exception(
                f"Live transcribe call raised [{source.label}]: {e}")
            return 0
        published = 0
        for s in segments_iter:
            text = (s.text or "").strip()
            if not text:
                continue
            self._publish({
                "start": float(s.start) + window_start,
                "end": float(s.end) + window_start,
                "text": text,
                "speaker": source.label,
            })
            published += 1
        return published

    def _try_drain_one(self, source: _SourceBuffer) -> bool:
        """If `source` has a full window ready, transcribe and publish
        it. Returns True if work was done, False otherwise."""
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
