"""
Subprocess-isolated WAV finalize for the recording stop path.

WHY THIS EXISTS
---------------
``finalize_recording_streaming`` reads two large WAVs (mic + loopback),
resamples one of them with scipy, and streams a mixed PCM_16 output —
all through libsndfile + numpy/scipy C extensions. A native crash in
any of those (a malformed input, an OOM, a buggy scipy point release,
or the cloud-sync-filter-driver contention that fired on the
2026-06-15 8B88C1C3 segfault) historically took the WHOLE Python
backend down. Even after v2.11.1 moved the resample temp off the
cloud volume, the broader class of native-code crash in this hot path
remained a single-point-of-failure for the entire backend.

This script is the same finalize call wrapped in its own process. The
recording service spawns it via ``subprocess.run`` instead of calling
``finalize_recording_streaming`` inline. If the child segfaults the
parent observes a non-zero exit code, logs the cause, leaves the temp
WAVs on disk, marks the session ``merge_failed`` — and **stays alive**.
Recovery on the next launch (or a manual "Recover audio" click)
re-runs the finalize from the preserved temps.

PROTOCOL
--------
stdout: on success, machine-parseable lines:
    AEC_RESULT <json>                                  (only when
        --echo-cancellation was passed — see utils.audio_utils.
        finalize_recording_streaming's `aec_result` out-param. The
        parent (recording_service._run_finalize_subprocess) reads this
        to persist the AEC decision on the session record. Its absence
        despite --echo-cancellation being passed is itself meaningful:
        the parent must record that as "no decision came back", never
        silently treat it as "AEC wasn't requested" or "AEC rejected.")
    RESULT duration_s=<float> loopback_mixed=<bool>
stderr: human log lines (mirrored into backend.log by the parent)

Everything the child logs via utils.logger.get_logger (including the
"Offline AEC decision: ..." line in audio_utils.py) goes to the child's
STDOUT, not stderr — get_logger's handler is a StreamHandler(sys.
stdout). The parent mirrors both streams into backend.log, but only
stdout's AEC_RESULT line is authoritative/structured; free-form log
lines are for human debugging only.

Exit codes:
    0  — success; the merged WAV is at --output
    1  — expected failure (input file missing, empty, etc.) — Python
         raised RuntimeError; the merge did not produce output
    2  — argparse / wrapper error before finalize started
    other (negative, 0xC0000005, …) — native crash. Parent treats
         this identically to exit 1 from the user's perspective but
         logs the exact code for diagnosis.

The child resolves its imports the same way the long-running backend
does: ``BACKEND_ROOT`` is added to ``sys.path`` so ``from utils
.audio_utils import ...`` works whether the script is invoked as
``python -m scripts.finalize_audio`` or ``python <abs path>``.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# Make `utils`, `core`, etc. importable when this is invoked by an
# absolute path. The parent (recording_service) spawns us with the
# same Python that runs the backend, but `cwd` may not be `backend/`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _emit(line: str) -> None:
    """Write a result line to stdout, flushed immediately so the parent
    can read it even if Python is killed right after."""
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream-merge mic + loopback into a final session WAV.",
    )
    parser.add_argument("--mic", required=True, help="path to mic temp WAV")
    parser.add_argument(
        "--loopback", default="",
        help="path to loopback temp WAV (empty string = mic-only merge)",
    )
    parser.add_argument(
        "--output", required=True,
        help="path to write the merged session WAV",
    )
    parser.add_argument(
        "--target-sr", type=int, default=16000,
        help="output sample rate (default 16000, matches whisper/pyannote)",
    )
    parser.add_argument(
        "--offset", type=str, default="",
        help=("loopback start offset in seconds (e.g. 0.116). Empty "
              "string means no wallclock anchor — finalize falls back "
              "to right-aligning loopback against mic length."),
    )
    parser.add_argument(
        "--echo-cancellation", action="store_true",
        help=("Run offline AEC on the mic channel (using loopback as "
              "reference) before mixing. Default off — see "
              "Settings.echo_cancellation_enabled / utils/aec.py."),
    )
    args = parser.parse_args(argv)

    try:
        offset: float | None
        if args.offset.strip() == "":
            offset = None
        else:
            offset = float(args.offset)
    except ValueError as e:
        print(f"finalize_audio: invalid --offset {args.offset!r}: {e}",
              file=sys.stderr, flush=True)
        return 2

    loopback_path = args.loopback or None

    # Import is deferred until after argparse so a misuse exits 2 fast
    # without paying for the (heavy) scipy / soundfile module init.
    try:
        from utils.audio_utils import finalize_recording_streaming
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2

    # Filled in-place by finalize_recording_streaming when --echo-
    # cancellation is set (see its `aec_result` param). If AEC's own
    # try/except swallows something unexpected before it can update
    # this, it simply stays empty and no AEC_RESULT line is emitted —
    # the parent then correctly treats that as "requested but no
    # decision came back" rather than inventing a false one.
    aec_result: dict = {}

    try:
        duration_s, loopback_mixed = finalize_recording_streaming(
            mic_wav_path=args.mic,
            loopback_wav_path=loopback_path,
            output_wav_path=args.output,
            target_sr=args.target_sr,
            loopback_start_offset_s=offset,
            echo_cancellation_enabled=args.echo_cancellation,
            aec_result=aec_result if args.echo_cancellation else None,
        )
    except RuntimeError as e:
        # Expected, raisable error (missing mic, empty WAV, etc.) —
        # exit 1 so the parent distinguishes this from a native crash.
        print(f"finalize_audio: RuntimeError: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception:
        # Anything else is unexpected at the Python level (still
        # different from a native crash, which exits with no traceback
        # at all). Surface the trace so backend.log captures it.
        traceback.print_exc(file=sys.stderr)
        return 1

    if args.echo_cancellation and aec_result:
        _emit(f"AEC_RESULT {json.dumps(aec_result)}")
    _emit(
        f"RESULT duration_s={duration_s:.6f} "
        f"loopback_mixed={'true' if loopback_mixed else 'false'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
