"""
Pyannote speaker diarization — GPU accelerated.
"""

import asyncio
from typing import List, Optional, Tuple
from utils.logger import get_logger
from utils.ml_memory import cleanup_ml_memory

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
        # `hf_token` is passed through here rather than being accepted
        # and ignored, which is what this did until 2026-09-02. The
        # gated-model download worked only because Settings.from_env
        # calls load_dotenv(override=True), which happens to export
        # HF_TOKEN into the process environment, which huggingface_hub
        # happens to read. The secrets layer has already changed its
        # mind once about whether config.env keeps the token in
        # plaintext; if it changes again, diarization stops downloading
        # on fresh installs with nothing pointing at why.
        self._pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=(hf_token or None),
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

    def _run_pipeline(self, audio_path: str):
        """One pyannote pass. Split out so the retry below runs exactly
        the same call rather than a near-copy of it."""
        return self._pipeline(audio_path, max_speakers=self._max_speakers)

    def _current_device_type(self) -> str:
        """Best-effort read of where the pipeline currently is.

        pyannote exposes `.device`; a version that does not is treated
        as non-CPU so the retry still gets a chance — the cost of a
        wasted CPU retry is seconds, the cost of skipping a needed one
        is the user's whole meeting.
        """
        device = getattr(self._pipeline, "device", None)
        return str(getattr(device, "type", "") or "").lower()

    def _retry_on_cpu(self, error: Exception) -> bool:
        """Move the pipeline to CPU for one more attempt.

        Returns False when there is no point trying: already on CPU (the
        fallback itself failed, so the audio is the problem, and a
        second identical attempt is a slower route to the same error) or
        the move itself fails.
        """
        if self._current_device_type() == "cpu":
            logger.error(
                f"Diarization failed on CPU; not retrying: {error}")
            return False
        try:
            import torch
            logger.warning(
                f"Diarization failed on "
                f"{self._current_device_type() or 'the accelerator'} "
                f"({error}); retrying once on CPU. Set "
                f"diarization_device=cpu in Settings to skip the "
                f"failing attempt entirely.")
            self._pipeline.to(torch.device("cpu"))
            return True
        except Exception as move_error:  # noqa: BLE001
            logger.error(
                f"Could not move the diarization pipeline to CPU "
                f"({move_error}); the original failure stands.")
            return False

    async def diarize(
        self,
        audio_path: str,
        channel_attribution: Optional[dict] = None,
    ) -> List[dict]:
        """Diarize `audio_path` into speaker turns.

        `channel_attribution` is the parsed
        ``session_<ID>.channel_attribution.json`` sidecar written during
        finalize (see core/channel_attribution.py) — the record of which
        physical DEVICE captured each moment of the recording. When it
        is present and trustworthy, the spans the mic confidently owns
        are reassigned to the user outright, so pyannote's clustering
        is only responsible for telling far-end speakers apart from each
        other and can never hand the far end's words to the user.

        Passing None — or a sidecar that stands itself down (mic-only
        session, conference-room mode, a speakerphone recording where
        far-end audio bleeds into the mic, or any session recorded
        before the sidecar existed) — leaves the pyannote turns exactly
        as they are: pure voice-similarity diarization, i.e. the
        behaviour every caller had before this parameter existed.
        """
        logger.info(f"Diarizing: {audio_path}")
        loop = asyncio.get_event_loop()
        try:
            try:
                diarization = await loop.run_in_executor(
                    None, self._run_pipeline, audio_path)
            except Exception as first_error:
                # RETRY ON CPU, ONCE (2026-09-02). __init__ already
                # falls back when `pipeline.to(device)` rejects the
                # model at LOAD. It could not help with a device that
                # accepts the model and then fails while RUNNING it —
                # the documented MPS failure mode (some pyannote layers
                # have no MPS kernel and only find out when they
                # execute), and what a CUDA card does when it runs out
                # of memory on a long meeting.
                #
                # Before this, that killed the whole processing run and
                # the app's answer was a Settings dropdown asking the
                # user to switch to CPU by hand, after the crash, in a
                # panel served by the process that crashed.
                if not self._retry_on_cpu(first_error):
                    raise
                diarization = await loop.run_in_executor(
                    None, self._run_pipeline, audio_path)
        except Exception as e:
            raise RuntimeError(
                f"Diarization failed: {e}\n"
                "Check that the audio file is a valid 16kHz mono WAV."
            ) from e
        finally:
            # SESSION-BOUNDARY cleanup: one diarization pass per whole
            # recording (see recording_service.process_session), not a
            # hot loop — safe to pay a gc.collect() + torch cache
            # release here.
            cleanup_ml_memory()

        turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({
                "start":   turn.start,
                "end":     turn.end,
                "speaker": speaker,
            })
        logger.info(f"Diarization complete: {len(set(t['speaker'] for t in turns))} speakers detected.")
        return self.apply_channel_attribution(turns, channel_attribution)

    @staticmethod
    def apply_channel_attribution(
        turns: List[dict],
        channel_attribution: Optional[dict],
    ) -> List[dict]:
        """Constrain voice-clustered turns with the channel timeline.

        Split out from ``diarize`` so it is reachable (and testable)
        without loading pyannote, and so a re-process path that already
        has turns in hand can apply the same constraint.

        Never raises: any failure here logs and returns the turns
        untouched, which is exactly today's pure-voice behaviour. The
        import is deferred for the same reason the pyannote import is —
        so merely importing this module stays cheap and cannot fail on
        a backend that never diarizes.
        """
        if not channel_attribution:
            return turns
        try:
            from core.channel_attribution import constrain_turns_to_owner
            constrained, stats = constrain_turns_to_owner(
                turns, channel_attribution)
        except Exception as e:
            logger.exception(
                f"Channel-aware attribution failed ({e}); keeping the "
                f"voice-only diarization result unchanged")
            return turns
        if not stats.get("applied"):
            logger.info(
                "Channel attribution not applied (%s) — speaker "
                "attribution falls back to voice-only clustering",
                stats.get("reason"))
            return turns
        logger.info(
            "Channel attribution applied: %.1fs assigned to the user "
            "(%s) by capture device; %d turns → %d (%d split at a "
            "channel boundary); timeline confidence %s",
            stats.get("owner_seconds", 0.0), stats.get("owner_label"),
            stats.get("turns_in", 0), stats.get("turns_out", 0),
            stats.get("split_turns", 0), stats.get("overall_confidence"))
        return constrained

    @staticmethod
    def assign_speakers(
        segments: List[dict],
        turns: List[dict],
    ) -> List[dict]:
        """Attribute each transcript segment to a diarization speaker.

        Whisper's segments and pyannote's turns have different
        boundaries, and a segment several seconds long routinely spans a
        hand-off:

            "...so we'll take that away. Actually, hold on."

        Giving that whole segment to whoever overlapped it most — which
        is all this did until 2026-09-02 — puts one person's words in
        another's mouth. That is not cosmetic here: the transcript is
        the input to the summary, the action items and the commitments,
        so the wrong person gets the follow-up.

        With word timestamps (requested by core/decode_options.py on the
        batch pass) a spanning segment is SPLIT at the word where the
        speaker changes. Without them — every session recorded before
        that change, and any model build that ignores the request — this
        falls back to whole-segment max-overlap, i.e. exactly the
        previous behaviour. That fallback is the common case for
        existing libraries and has to stay silent and safe.
        """
        attributed: List[dict] = []
        for seg in segments:
            pieces = DiarizationEngine._split_segment_by_speaker(seg, turns)
            attributed.extend(pieces)
        return attributed

    @staticmethod
    def _speaker_at(t: float, turns: List[dict]) -> str:
        """Who is speaking at instant `t`.

        Ties break on turn order so re-processing one session twice
        cannot produce two different transcripts.
        """
        for turn in turns:
            if turn["start"] <= t < turn["end"]:
                return turn["speaker"]
        return "SPEAKER_UNKNOWN"

    @staticmethod
    def _dominant_speaker(seg: dict, turns: List[dict]) -> str:
        """Max-overlap attribution for the whole segment — the original
        rule, kept for segments with no usable word timings."""
        speaker = "SPEAKER_UNKNOWN"
        best_overlap = 0.0
        for turn in turns:
            overlap = (min(seg["end"], turn["end"])
                       - max(seg["start"], turn["start"]))
            if overlap > best_overlap:
                best_overlap = overlap
                speaker = turn["speaker"]
        return speaker

    @staticmethod
    def _usable_words(seg: dict) -> List[dict]:
        """Word entries with timings we can act on, in time order.

        A malformed entry is dropped rather than fatal: a bad word list
        must degrade this segment to whole-segment attribution, never
        raise into the processing pipeline. If ANY word is unusable the
        whole list is rejected — splitting on a partial list would drop
        the text of the words that were skipped.
        """
        words = seg.get("words") or []
        out: List[dict] = []
        for w in words:
            try:
                start = float(w["start"])
                end = float(w["end"])
                text = str(w["word"])
            except (KeyError, TypeError, ValueError):
                return []
            out.append({"start": start, "end": end, "word": text})
        return out

    @staticmethod
    def _split_segment_by_speaker(
        seg: dict,
        turns: List[dict],
    ) -> List[dict]:
        """One segment in, one or more attributed segments out.

        Returns a single segment — the previous behaviour — whenever
        there are no usable words, or when every word belongs to the
        same speaker. Splitting has to be rare: a rule that fragmented
        ordinary speech into per-word lines would be worse than the bug
        it replaces.
        """
        words = DiarizationEngine._usable_words(seg)
        if not words:
            return [{**seg,
                     "speaker_id": DiarizationEngine._dominant_speaker(
                         seg, turns)}]

        # A word's speaker is whoever holds its MIDPOINT. Using the
        # start would hand a word that begins in the tail of the
        # previous turn to the wrong person.
        runs: List[dict] = []
        for word in words:
            mid = (word["start"] + word["end"]) / 2.0
            who = DiarizationEngine._speaker_at(mid, turns)
            if runs and runs[-1]["speaker_id"] == who:
                runs[-1]["words"].append(word)
            else:
                runs.append({"speaker_id": who, "words": [word]})

        # A word in a gap between turns lands on SPEAKER_UNKNOWN. Rather
        # than emit an orphan line for it, attach it to the run beside
        # it — losing or isolating text is worse than attributing it to
        # the nearest speaker.
        runs = DiarizationEngine._absorb_unknown_runs(runs)

        if len(runs) == 1:
            return [{**seg, "speaker_id": runs[0]["speaker_id"]}]

        pieces: List[dict] = []
        for run in runs:
            run_words = run["words"]
            pieces.append({
                **seg,
                "start": run_words[0]["start"],
                "end": run_words[-1]["end"],
                "text": "".join(w["word"] for w in run_words).strip(),
                "words": run_words,
                "speaker_id": run["speaker_id"],
            })
        return pieces

    @staticmethod
    def _absorb_unknown_runs(runs: List[dict]) -> List[dict]:
        """Fold SPEAKER_UNKNOWN runs into a neighbour and re-merge.

        Only when there is a neighbour to fold into: a segment that is
        entirely outside every turn stays unknown, which is the honest
        answer and what the no-words path returns too.
        """
        if all(r["speaker_id"] == "SPEAKER_UNKNOWN" for r in runs):
            return runs
        merged: List[dict] = []
        for i, run in enumerate(runs):
            if run["speaker_id"] == "SPEAKER_UNKNOWN":
                # Prefer the run before (the speaker was still talking);
                # fall back to the one after for a leading gap.
                target = merged[-1] if merged else None
                if target is None:
                    nxt = next((r for r in runs[i + 1:]
                                if r["speaker_id"] != "SPEAKER_UNKNOWN"), None)
                    if nxt is not None:
                        nxt["words"] = run["words"] + nxt["words"]
                        continue
                else:
                    target["words"].extend(run["words"])
                    continue
            if merged and merged[-1]["speaker_id"] == run["speaker_id"]:
                merged[-1]["words"].extend(run["words"])
            else:
                merged.append(run)
        return merged
