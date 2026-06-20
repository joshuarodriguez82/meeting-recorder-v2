# v2.12.0 — Subprocess-isolated finalize: a native crash here can't take the backend down

> **What this fixes that v2.11.1 didn't.** v2.11.1 fixed the *specific*
> native crash we observed on 2026-06-15 (the loopback resample temp
> file contending with Google Drive Stream's filter driver). v2.12.0
> closes the broader *class* of native crashes in the same code path:
> the WAV merge now runs in a child process, so a crash in scipy /
> libsndfile / numpy can no longer kill the backend. The session row
> stays, the temp WAVs stay, and recovery is one click.

This is structural defense-in-depth. The same merge logic runs;
where it runs is different. On a healthy machine you won't notice
v2.12.0 — finalize completes in 2-3 seconds for a 20-min recording
like before. On a machine where finalize would have crashed in
v2.11.1, you now get a clear "Recovery available" row instead of a
silent backend respawn.

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

### 1. Subprocess-isolated WAV finalize

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

### 2. Temp cleanup gate fixed

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

### 3. Recovery loop tightens for v2.12 sessions

A subprocess crash now produces *exactly* the orphan-temp shape
that v2.11.1's recovery flow was designed to handle: a session JSON
with the planned `audio_path` populated but no WAV at that path,
plus `_recording_<id>.wav` / `_loopback_<id>.wav` in
`%TEMP%\meeting_recorder_capture\`. On next launch the recovery
service finds the orphans, merges them (out-of-process again, via
the same script), and stamps the existing session JSON with the
final fields. The user sees "Recovery available" for ~3 seconds,
clicks it, the merged WAV lands, processing kicks off.

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
