# v2.3.3 — Hotfix: live transcript panel

One-line bug: `live-transcript-panel.tsx` hardcoded the backend URL
to `http://127.0.0.1:17645`, but the backend port has been picked
dynamically by the OS since the move away from a fixed port (the
Rust shell binds `127.0.0.1:0`, gets an ephemeral port, and threads
it to Python via `MEETING_RECORDER_PORT`).

Every other API call in the app routes through `api.getBaseUrl()`
which resolves the dynamic port via the `get_backend_port` Tauri
command. The live-transcript SSE EventSource was the one survivor
that never got migrated. It happened to work on pre-v2.3.x builds
because Python sometimes landed on 17645 by chance, and definitely
worked on the older fixed-port builds. On v2.3.x the chance of the
OS-picked ephemeral port being 17645 is effectively zero, so the
live preview pane has been silently broken since v2.3.0.

Recording itself was always fine — the post-stop transcript is
produced by `/process` which routes through `getBaseUrl()` correctly.
Only the live preview pane that streams 15-second-window text while
you record was affected.

## Fix

- `src/lib/api.ts` — exported `getBaseUrl` on the `api` object so
  non-`request` callers (the SSE EventSource here, future websockets,
  etc.) can resolve the dynamic port without each maintaining their
  own copy of the port-resolution logic.
- `src/components/live-transcript-panel.tsx` — dropped the hardcoded
  `const BACKEND = "http://127.0.0.1:17645"`; the connect routine now
  awaits `api.getBaseUrl()` before opening the EventSource. The
  `cancelled` guard is checked twice (once before the await, once
  after) so a rapid unmount during the resolve doesn't open an
  orphaned connection.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.3.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.3_universal.zip`.
>
> The build is **unsigned** — first launch needs the Gatekeeper bypass.
>
> **Path A — Finder:** double-click the `.zip` in Finder (Archive
> Utility auto-extracts to `Meeting Recorder.app`). Drag the `.app`
> to `/Applications`. Double-click, dismiss the "damaged" warning,
> then **System Settings → Privacy & Security → Open Anyway**,
> double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_*_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> `Meeting.Recorder_2.3.3_x64-setup.exe` or `.msi` and double-click.

## What's in the release (unchanged from v2.3.0)

- **Conference room mode** — toggle in the Record view; forces
  mic-only capture and replaces `SPEAKER_YOU` with generic labels.
- **Offline AEC validator** — `python -m backend.scripts.measure_aec`
  plus the `KEEP_AUDIO_TEMPS=1` env var to preserve per-session WAVs.

## Affected users

Anyone on **v2.3.0 / v2.3.1 / v2.3.2** whose live preview pane stayed
on "Connecting to the backend…" (or showed no segments) during a
recording was hitting this bug. The recording itself was captured
correctly — running it through Stop → Process produces a normal
transcript. v2.3.3 just restores the live preview.
