# v2.12.0 — Read-side cloud-contention fix, subprocess-isolated finalize, ghost-session cleanup, auto-screenshot

> **What this release fixes, in priority order:**
>
> 1. **Read-side cloud-contention crashes.** v2.11.1 fixed the *finalize*
>    side; this fixes the *transcribe + diarize* side. The ML pipeline
>    now reads from a local-only working copy of the WAV instead of
>    directly from `recordings_dir`, so cloud sync filter driver
>    contention on Google Drive Stream / OneDrive can no longer crash
>    the backend mid-processing. Two backend segfaults on 2026-06-26
>    hit exactly this; v2.12.0 closes it.
> 2. **Subprocess-isolated finalize.** A native crash in scipy /
>    libsndfile during the WAV merge can no longer take the backend
>    down — finalize runs in a child process now.
> 3. **Ghost-session cleanup.** Stub session JSONs accumulate when the
>    backend crashes mid-recording (v2.11.1's JSON-first writes leave
>    them behind). Field repro 2026-06-26: 69 ghosts on one machine.
>    Stubs older than 14 days auto-purge at startup; younger ones get
>    a "Delete N ghost sessions" button in **Settings → Storage**.
> 4. **Auto-screenshot during recording.** New setting:
>    **Settings → Recording → Auto-screenshot during recording**.
>    Set to 3 minutes (or whatever) and the recorder captures a
>    screenshot on that cadence automatically — no more clicking the
>    button. Defaults to 0 (off) so existing users aren't surprised.

This is the structural defense pass for the recording pipeline. After
v2.12.0 a native crash in the ML stack, the WAV merge, OR cloud sync
contention during processing can no longer kill the backend OR lose a
session. The session row always survives; recovery is always one
click.

## Install (macOS)

> v2.12.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.12.0_universal.zip`.
>
> Still unsigned for Gatekeeper purposes. First launch needs the
> Gatekeeper bypass — pick whichever path you prefer:
>
> **Path A — System Settings (no Terminal):** double-click the `.zip`
> in Finder (Archive Utility auto-extracts to `Meeting Recorder.app`),
> drag the `.app` to `/Applications`, double-click, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_2.12.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.12.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Read-side cloud-contention fix (the headline)

Field repro 2026-06-26: a v2.11.1 install on Google Drive Stream
crashed the backend twice in one day. v2.11.1 had moved the *finalize*
intermediate temp off the cloud volume — that fix held; finalize
completed in 4 seconds. But the SUBSEQUENT auto-process step (read
the saved WAV → transcribe → diarize) crashed both times. Backend
exited with `STATUS_ACCESS_VIOLATION 0xC0000005`. Same shape as the
v2.11.1 finalize crash, just on the READ side now: faster-whisper /
pyannote reading from Google Drive Stream's local cache hit the
same intermittent cloud-sync-filter-driver contention.

The fix: at the start of `process_session`, stream-copy the WAV
ONCE from `recordings_dir` (cloud-side) to
`%TEMP%/meeting_recorder_processing/<session_id>.wav` (local-only),
then route transcribe + diarize + speaker-embedding extraction
through the local copy. The cloud volume is touched exactly once;
the heavy ML pipeline never sees it. Cleanup is automatic in
`finally`. A failed re-process re-copies; disk doesn't grow.

The same backend.log that showed today's crashes:

```
[ERROR] process_full: transcribe/diarize failed
  File "services/recording_service.py", line 727, in process_session
    await asyncio.to_thread(_ensure_audio_available, session.audio_path)
  File "services/recording_service.py", line 74, in _ensure_audio_available
    raise RuntimeError(
        "The audio file for this recording is missing — it may have "
        "been moved, deleted, or not yet synced down from the cloud.")
```

After v2.12.0, the same flow copies the WAV to a local-only path
before that error path is even reachable. Re-processing the same
session is a no-op copy because the helper is idempotent.

### 2. Subprocess-isolated WAV finalize

The recording stop path used to call `finalize_recording_streaming`
inline — reading two big WAVs, resampling one with scipy, streaming a
mixed PCM_16 output. Every step is a C extension. A native crash in
any of them used to take the whole Python backend down with
`STATUS_ACCESS_VIOLATION` (the 2026-06-15 8B88C1C3 case). v2.11.1
fixed the *specific* trigger we observed but the single-point-of-
failure shape remained.

v2.12.0 moves the merge into a child process. The recording service
spawns `backend/scripts/finalize_audio.py` with the same Python the
backend runs on (same numpy/scipy/soundfile build — no version
skew), passes argv for input paths + sample rate + offset, and
parses one machine-readable result line from the child's stdout
when it exits cleanly:

```
RESULT duration_s=1937.5 loopback_mixed=true
```

Exit codes are typed so the parent can distinguish three failure
classes for clear diagnostics:

| Exit code | Meaning | Parent action |
|---|---|---|
| `0` | Success | Parse stdout, stamp session, delete temps |
| `1` | Python-level error (missing input, empty WAV, …) | Log message, mark session "Error saving audio," leave temps |
| `2` | Argparse / wrapper misuse | Same as 1, surfaces the misuse |
| Other / negative / `0xC0000005` | Native crash | Log exit code + stderr tail, leave temps, **backend stays alive** |

Stderr from the child is mirrored into backend.log so diagnostics
are visible at the same level they used to land at.

### 3. Temp cleanup gate fixed

v2.11.1 introduced a JSON-stub-at-start-of-recording write so a
crashed finalize wouldn't lose the session row. That change pre-set
`session.audio_path` to the planned final location, which had a
side effect: the temp-cleanup logic in `stop_recording` keyed on
"audio_path is truthy" to mean "merge succeeded — delete the temp
WAVs." After v2.11.1 that gate was always true. So a failed merge
would (in some edge cases that didn't fire in practice yet) have
deleted the temps you need for recovery.

v2.12.0 gates temp cleanup on the actual final WAV file existing on
disk at the planned path, not just the field being set. The
subprocess-finalize crash path explicitly relies on this — when the
child segfaults, the parent's `except` arrives in `finally` with no
file at `session.audio_path` and the temps are preserved.

### 4. Recovery loop tightens for v2.12 sessions

A subprocess crash now produces *exactly* the orphan-temp shape
that v2.11.1's recovery flow was designed to handle: a session JSON
with the planned `audio_path` populated but no WAV at that path,
plus `_recording_<id>.wav` / `_loopback_<id>.wav` in
`%TEMP%\meeting_recorder_capture\`. On next launch the recovery
service finds the orphans, merges them (out-of-process again, via
the same script), and stamps the existing session JSON with the
final fields. The user sees "Recovery available" for ~3 seconds,
clicks it, the merged WAV lands, processing kicks off.

### 5. Ghost-session cleanup

v2.11.1's "JSON-first write" change was designed to ensure a backend
crash mid-recording / mid-finalize never lost the Sessions-list row
— the stub gets written to disk on Start, so even a complete
process death leaves a recoverable entry. Working as intended, but
over time the stubs accumulate. Field repro 2026-06-26: **69
session JSONs with no WAV on disk** on one machine. Each one shows
up in the Sessions list, fails to process when clicked, and confuses
the user.

v2.12.0 adds two complementary cleanups:

- **Auto-purge at backend startup.** Stubs older than 14 days that
  STILL have no WAV at their `audio_path` get deleted (JSON +
  sidecars) automatically. The 14-day window is well past any
  plausible "OneDrive is still syncing this back from another
  machine" recovery scenario. Younger stubs are kept so the
  recover-from-temps path can still find them.
- **Manual button in Settings → Storage.** New endpoint
  `GET/DELETE /ghost-sessions` returns / removes the list. An
  amber banner appears in Settings → Storage when ghosts exist:
  "X session(s) with no audio file — Delete X ghost session(s)."
  Defense in depth: the delete handler re-checks each row's
  `audio_path` at delete time and skips any that materialized
  (Drive synced back) between the scan and the click.

### 6. Auto-screenshot during recording

Field gap 2026-06-26: a 28-minute meeting captured 1 screenshot
because screenshots are manual (Screenshot button on the Record
view). Users expecting "the app screenshots periodically" got one
per click and didn't realize. v2.12.0 adds a settings field —
**Settings → Recording → Auto-screenshot during recording** — that
takes a minutes interval (0 = off, default; recommended 3). When
set > 0 and a recording is active, the Record view fires
`capture_screenshot` on a `setInterval` of that cadence against
the primary monitor; captures are best-effort and silent on
failure (locked screen, revoked permission), so the screen
flickering or a toast every 3 min doesn't interrupt the meeting.
Captures attach to the session's `screenshots[]` and feed the
summarizer the same way manual ones do.

## Bundle changes

`backend/scripts/` is now in the release-bundle allowlist
(`zip-bundle.py`) so `finalize_audio.py` ships with the installed
app. Without this entry the script wouldn't be present in the
release `.zip` and the recording service would crash spawning a
non-existent path. Same packaging trap that almost bit Fable's
router-split review — caught and fixed before the release builds.

## Tests

Four new tests pin the subprocess contract end-to-end:

- `test_subprocess_merges_mic_only` — happy path, mic only, parses
  the RESULT line, verifies output format.
- `test_subprocess_merges_mic_and_loopback` — full merge with a
  wallclock-anchored offset; proves argv passes through correctly.
- `test_subprocess_exits_1_on_missing_mic` — Python-level error path
  exits with code 1, distinguishable from a native crash.
- `test_subprocess_exits_2_on_bad_offset` — argparse misuse exits 2,
  distinguishable from finalize failure.

Total backend test count: 29 (was 25 in v2.11.1). All run on every
PR.

## Known not yet patched

- **`RecordingService` decomposition** — the 1,300-line god-object
  still owns capture + finalize + session state. v2.12.0 isolates
  the finalize subprocess but the inside-Python state machine
  remains a single class. Tracked for a later release; not blocking.
- **macOS Bluetooth headset drift detection** — v2.10.6's
  `audio-format-mismatch` banner is Windows-only. The macOS
  CoreAudio HAL-property equivalent for Bluetooth playback-clock
  drift over long calls is a separate integration.
- **Subprocess timeout** — `subprocess.run` has no timeout because
  long recordings legitimately take minutes to merge. A future
  version will add a generous timeout (10× duration?) so a
  genuinely-stuck child doesn't keep the parent's stop path hung
  forever.
