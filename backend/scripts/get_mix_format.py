"""
Subprocess-isolated WASAPI mix-format lookup for
core.audio_format_inspector.get_device_mix_format().

WHY THIS EXISTS
---------------
pycaw (built on comtypes) is the ONLY confirmed source of the
STATUS_ACCESS_VIOLATION crashes tracked across v2.23.2 / v2.25.0 (see
utils/com_worker.py's module docstring for the full apartment-affinity
/ cyclic-GC diagnosis). Every one of the ten captured crash dumps
implicates comtypes; the Outlook/pywin32 COM path has never appeared
in one. `get_device_mix_format()` is called from `/audio/sync-risk`,
which the Record view polls continuously while idle (record-view.tsx
only skips the call while an actual recording is in progress) — so it
runs far more often, on a far larger fleet, than any other COM path in
this app, and it enumerates every WASAPI endpoint (minting a fresh
batch of COM proxies) on every single call.

`utils/com_worker.py`'s worker-thread `gc.collect()` fix (v2.25.1)
closes the specific mechanism behind the confirmed crashes. This
script is defence in depth on top of that: pycaw simply never runs
inside the long-lived backend process again. If comtypes crashes here
— for any reason, including ones we haven't diagnosed yet — only this
short-lived child process dies. The parent (get_device_mix_format in
core/audio_format_inspector.py) observes a non-zero/garbage/absent
result and degrades to `None`, which the caller already treats as a
normal "can't tell, unknown" outcome (see compare_formats()).

Follows the same subprocess-isolation shape as scripts/finalize_audio.py.

PROTOCOL
--------
stdout: ONE line on success, machine-parseable:
    RESULT <json>
  <json> is a JSON object {"sample_rate": int, "bits_per_sample": int,
  "channels": int} on a match, or the literal `null` when no matching
  endpoint was found / the format couldn't be read — that is a NORMAL
  outcome, not an error, and still exits 0.
stderr: human log lines (mirrored into backend.log by the parent).

Exit codes:
    0 — lookup ran to completion; see the RESULT line (value may be
        `null`)
    1 — expected failure before a RESULT could be produced (bad args,
        import failure)
    2 — argparse error
    other (negative, 0xC0000005, …) — native crash. The parent treats
        this identically to any other non-zero exit: log and degrade
        to None. This is exactly the outcome this script exists to
        make safe.

The child resolves its imports the same way scripts/finalize_audio.py
does: BACKEND_ROOT is added to sys.path so `from core.xxx import ...`
works regardless of the spawning cwd.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _emit(line: str) -> None:
    """Write the result line to stdout, flushed immediately so the
    parent can read it even if the process is killed right after."""
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Look up a WASAPI endpoint's shared-mode mix format.",
    )
    parser.add_argument("--device", required=True, help="friendly device name")
    parser.add_argument("--kind", required=True, help="'input' or 'output'")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse already printed its usage error to stderr.
        return 2

    # Deferred import: keeps a misuse (bad args) exiting fast without
    # paying for pycaw/comtypes import cost, and confines any import-
    # time failure inside this process rather than the parent.
    try:
        from core.audio_format_inspector import (
            get_device_mix_format_inprocess,
        )
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    try:
        result = get_device_mix_format_inprocess(args.device, args.kind)
    except Exception:
        # Anything unexpected at the Python level (still distinct from
        # a native crash, which exits with no traceback at all).
        traceback.print_exc(file=sys.stderr)
        return 1

    _emit("RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
