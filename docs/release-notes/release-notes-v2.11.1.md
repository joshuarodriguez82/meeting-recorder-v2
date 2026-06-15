# v2.11.1 — Hardened finalize: meetings survive a backend segfault

> **Install this if you are on v2.11.0 or earlier and you record long
> meetings into a cloud-synced folder (OneDrive, Google Drive Stream,
> iCloud, Dropbox).** v2.11.0 had a latent native-code segfault in
> the WAV finalize step that could kill the backend mid-stop and
> erase the entire session from the Sessions list with no trace. v2.11.1
> closes the segfault AND ensures a future crash anywhere in the stop
> path leaves a recoverable session row instead of a vanished meeting.

Three structural fixes to the recording pipeline + one quality-of-life
recovery fix.

1. **Critical** — the loopback-resample temp file used during finalize
   was being written to your `recordings_dir`. When that's on Google
   Drive Stream / OneDrive / iCloud, the 100+ MB temp write contends
   with the cloud sync filter driver and crashes libsndfile at native
   level on long recordings. Field repro on 2026-06-15: a 58-minute
   call segfaulted the backend mid-stop and disappeared from the
   Sessions list. The temp now goes to `%TEMP%\meeting_recorder_capture\`
   — same local-only dir the streaming-capture WAVs moved to in
   v2.10.5/v2.10.6, same reasoning, same fix shape.
2. **Defense-in-depth** — session JSON is now written to disk at
   *start* of recording and again before finalize, not only after
   finalize completes. If finalize segfaults / OOMs / the laptop loses
   power, the session row still exists in your list with audio_path
   pointing at the expected location and the temp WAVs preserved on
   disk for one-click recovery on next launch.
3. **Recovery now scans the local capture dir too.** Since v2.10.5 the
   streaming-capture temp WAVs live in `%TEMP%\meeting_recorder_capture\`,
   but `recover_orphans()` was still only scanning `recordings_dir`.
   Any backend crash during recording was leaving orphans in a location
   the recovery service never looked at — they sat there forever and
   the user lost the meeting. Recovery now scans both locations.
4. **UTF-8 BOM tolerance** on session JSONs. Recovery scripts written
   in PowerShell often emit a BOM (PowerShell's `Set-Content
   -Encoding UTF8` does so by default), and the previous loader
   raised JSONDecodeError on the BOM byte — sessions silently
   disappeared from the list. `SessionService.load` and
   `read_text_hydrated` now decode with `utf-8-sig` (a strict
   superset; non-BOM files still load identically).

## Install (macOS)

> v2.11.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.11.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.11.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.11.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's new

### 1. Loopback resample temp moved off the cloud-synced volume

`finalize_recording_streaming` runs a two-pass merge: pass 1
resamples the loopback WAV to 16 kHz into an intermediate temp file,
pass 2 streams mic + intermediate together into the final WAV. The
intermediate landed in `out_path.parent` — same directory as the
final session WAV, which on a user with `RECORDINGS_DIR=G:\My Drive\…`
put a 100+ MB temp write directly on the Google Drive Stream mount.

Long recordings amplify the problem because each `sf.write` of a 10-
second resampled block has to land on a volume the cloud filter
driver is busy reading. On the 2026-06-15 8B88C1C3 field repro the
contention pushed scipy/sf into a state that segfaulted the entire
Python process (Windows `STATUS_ACCESS_VIOLATION` / `0xC0000005`).
The recording_service couldn't catch it because the crash was below
Python — no traceback, no error log, the backend just exited and the
session JSON never got written.

Fix: the intermediate file now lands in `%TEMP%\meeting_recorder_capture\`
via a new `_scratch_temp_dir()` helper. Same dir as the streaming-
capture temps. The cloud filter driver never sees it.

### 2. Session JSON written at recording START, not finalize END

Old flow:

```
start_recording      → in-memory Session object created
… recording …
stop_recording       → finalize_recording_streaming (CRASHES HERE)
… (never reached) …
session_svc.save     → JSON would have been written here
```

A crash in `finalize_recording_streaming` meant the JSON never landed
on disk → the meeting silently vanished from the list, even though
the temp WAVs were still sitting in `%TEMP%`. Users perceived this as
"my recording is gone" which is the worst possible experience this
app could offer.

New flow:

```
start_recording      → in-memory Session object created
                       + minimal stub JSON written to disk RIGHT NOW
                         (session_id + started_at + planned audio_path)
… recording …
stop_recording       → stub JSON re-saved with current state (no audio yet)
                       finalize_recording_streaming
                       on success: full JSON saved with ended_at +
                                   durations + sync-integrity fields
                       on crash:   JSON from start/pre-finalize remains;
                                   recovery on next launch finds the
                                   stub + temp WAVs and merges
```

The stub is atomic (temp file + rename, same pattern
`SessionService.save` already uses), so a crash mid-write can't
produce a half-written JSON.

### 3. Recovery scans both `recordings_dir` and `%TEMP%` capture dir

v2.10.5 moved active-capture temps from `recordings_dir` to
`%TEMP%\meeting_recorder_capture\` to keep cloud sync from
stalling the audio thread. `recover_orphans()` kept scanning only
`recordings_dir` though — which made the new layout invisible to
recovery. A backend crash during recording would leave temp WAVs in
the capture dir, recovery would never find them, and the user
would have to manually copy the temps back into `recordings_dir`
(or run a recovery script) to get the meeting back.

Recovery now scans both locations and de-duplicates on session_id.
On any future backend crash, restarting the app will pick up the
orphan temps automatically and produce a "Recovered Session <id>"
row in the list with one click to process.

### 4. UTF-8 BOM tolerance on session JSON

The recovery script we used today wrote the session JSON via
PowerShell's `Set-Content -Encoding UTF8`, which prepends a UTF-8
BOM. The previous `json.load(open(..., encoding='utf-8'))` raised
`json.JSONDecodeError: Unexpected UTF-8 BOM` and the session row
silently disappeared from the Sessions list. Both `SessionService.load`
and `read_text_hydrated` now decode with `utf-8-sig` — a strict
superset of `utf-8` that strips a leading BOM if present.

## Tests

Three new tests cover the data-loss surface this release closes:

- `test_recovery_finds_orphans_in_local_capture_dir` — proves
  recovery picks up temps from `%TEMP%\meeting_recorder_capture\`.
- `test_recovery_preserves_user_metadata_from_stub_json` — proves
  recovery merges with an existing stub JSON instead of clobbering
  user-set `display_name`, `client`, `project`, `notes`.
- `test_session_json_with_utf8_bom_loads` — proves a BOM at the
  start of a session JSON no longer hides the session from the list.

Total backend test count: 25 (was 22 in v2.11.0). Run on every PR.

## Known not yet patched

- **In-process WAV finalize** — the segfault root cause is now
  prevented for the cloud-sync volume case (the by-far most common
  trigger). A truly defensive design would run finalize in a
  subprocess so a native crash from any future cause (a buggy
  scipy release, a malformed input WAV) can't kill the backend.
  Tracked for v2.12; significant refactor.
- **Cloud-sync hydration of recovered audio** — after recovery
  copies the WAV to `recordings_dir` on a Google Drive Stream /
  OneDrive volume, the file is "uploaded" from the local cache.
  If you switch machines before the upload completes, the recovered
  session on the second device shows as a cloud placeholder until
  Drive finishes syncing. Same constraint as any other file in the
  recordings dir; not a regression.
