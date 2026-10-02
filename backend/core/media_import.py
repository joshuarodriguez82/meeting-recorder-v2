"""
Turning a video (or any non-WAV recording) into audio the pipeline reads.

WHY
---
Importing a meeting someone else recorded — a Teams or Zoom ``.mp4`` —
used to copy the file in as-is. Transcription coped, because Whisper
decodes containers itself. Nothing after it did: processing copies the
recording to ``processing/<id>.wav`` whatever it really is, and the
diarizer and voice fingerprinting read that through libsndfile, which
cannot open an MP4. The import "worked" and processing then failed at
the speaker stage, so a video had to be converted by hand before the
app could use it.

So an import now extracts the sound track once, up front, to the same
kind of file a recording produces: a WAV every stage can read. The
decoder is PyAV, which faster-whisper already depends on — nothing new
to install, and no ffmpeg on PATH required.

16 kHz mono is what Whisper, pyannote and the speaker encoder all
resample to anyway; keeping the source's 48 kHz stereo would only make
a two-hour video's audio a 1.3 GB file instead of a 230 MB one.

Decoding streams frame by frame and writes as it goes, so a long video
never has to fit in memory.
"""

from __future__ import annotations

import datetime as _dt
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

#: Containers people are actually handed: Teams / Zoom / Meet / Webex
#: downloads, phone recordings, screen recorders.
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".wmv")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus",
              ".wma")
SUPPORTED_EXTS = AUDIO_EXTS + VIDEO_EXTS

TARGET_RATE = 16000


class NoAudioTrack(ValueError):
    """The file opened but has nothing to transcribe."""


@dataclass
class ExtractResult:
    duration_s: float
    #: When the source says it was recorded, if it says (MP4/MOV
    #: ``creation_time``). Local, naive — like Session.started_at.
    recorded_at: Optional[_dt.datetime] = None


def is_supported(path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def needs_extraction(path) -> bool:
    """Everything but WAV is converted, so every later stage sees one
    format. (FLAC and MP3 happen to be readable by libsndfile on some
    builds; relying on which build is installed is how this broke.)"""
    return Path(path).suffix.lower() != ".wav"


def parse_creation_time(value) -> Optional[_dt.datetime]:
    """A container's ``creation_time`` as a local naive datetime, or
    None. MP4 stores UTC; an unset field is often the 1904/1970 epoch,
    which is not a meeting date."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        when = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.year < 2000:
        return None
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when


def extract_audio(src, dst, *, rate: int = TARGET_RATE,
                  progress: Optional[Callable[[float], None]] = None,
                  av_module=None) -> ExtractResult:
    """Decode the first audio stream of ``src`` to a mono 16-bit WAV at
    ``dst``. Written to a temporary name and renamed, so a failed or
    interrupted extraction never leaves a truncated WAV that looks like
    a real recording.

    Raises NoAudioTrack for a file with no sound, ValueError for one
    that can't be decoded at all."""
    av = av_module
    if av is None:
        try:
            import av  # type: ignore[no-redef]  # ships with faster-whisper
        except ImportError as e:  # pragma: no cover - always present in the app
            raise ValueError(
                "The video decoder isn't installed, so audio can't be "
                "extracted from this file.") from e

    src, dst = Path(src), Path(dst)
    part = dst.with_name(dst.name + ".part")
    samples = 0
    recorded_at = None
    try:
        try:
            container = av.open(str(src))
        except Exception as e:  # noqa: BLE001 - av raises many types
            raise ValueError(
                f"Couldn't open {src.name} as audio or video: {e}") from e
        with container:
            if not container.streams.audio:
                raise NoAudioTrack(
                    f"{src.name} has no sound track, so there is nothing "
                    f"to transcribe.")
            stream = container.streams.audio[0]
            recorded_at = parse_creation_time(
                (container.metadata or {}).get("creation_time"))
            total_s = float(container.duration / 1_000_000) \
                if container.duration else 0.0
            resampler = av.AudioResampler(format="s16", layout="mono",
                                          rate=rate)
            with wave.open(str(part), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)

                def _write(frames) -> None:
                    nonlocal samples
                    for f in frames or ():
                        data = f.to_ndarray().tobytes()
                        out.writeframes(data)
                        samples += len(data) // 2

                frames = container.decode(stream)
                while True:
                    # Only the DECODE is guarded: a write error (disk
                    # full) must fail the import, not be mistaken for a
                    # damaged file and leave a truncated meeting.
                    try:
                        frame = next(frames)
                        converted = resampler.resample(frame)
                    except StopIteration:
                        break
                    except Exception as e:  # noqa: BLE001
                        if not samples:
                            raise ValueError(
                                f"Couldn't decode the sound track of "
                                f"{src.name}: {e}") from e
                        # A download cut short at the end still holds
                        # a usable meeting; keep what decoded.
                        break
                    _write(converted)
                    if progress and total_s > 0 and frame.time is not None:
                        progress(min(1.0, float(frame.time) / total_s))
                _write(resampler.resample(None))
        if not samples:
            raise NoAudioTrack(
                f"{src.name}'s sound track is empty, so there is nothing "
                f"to transcribe.")
        os.replace(part, dst)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    if progress:
        progress(1.0)
    return ExtractResult(duration_s=samples / float(rate),
                         recorded_at=recorded_at)


def wav_duration(path) -> float:
    """Length of a WAV in seconds, 0.0 when unreadable."""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else 0.0
    except (wave.Error, OSError, EOFError):
        pass
    try:
        import soundfile as sf
        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001
        return 0.0
