# v2.3.4 — Hotfix: live transcript pane + sessions list crash

Two unrelated regressions bundled into one release so you only have to
install once. **Supersedes v2.3.3** (which was never tagged).

## Fix 1: Live transcript pane on every v2.3.x build

`src/components/live-transcript-panel.tsx` hardcoded the SSE backend
URL to `http://127.0.0.1:17645`, but the backend port has been
OS-picked at app startup since the dynamic-port migration (see
`lib.rs::pick_free_port`). Every other API call routes through
`api.getBaseUrl()` which resolves the dynamic port via the
`get_backend_port` Tauri command. The live-transcript SSE EventSource
was the lone survivor of the old fixed-port era.

Result: the live preview pane sat on "Connecting to the backend…" for
the entire recording on v2.3.0 / v2.3.1 / v2.3.2. Recording itself was
fine — the post-stop `/process` transcript route uses `getBaseUrl()`
correctly and produced normal transcripts throughout.

**Fix:**
- `src/lib/api.ts` — exported `getBaseUrl` on the `api` object so
  non-`request` callers (the SSE EventSource here, future websockets,
  etc.) can resolve the dynamic port without duplicating the resolution
  logic.
- `src/components/live-transcript-panel.tsx` — `connect()` is now async
  and awaits `api.getBaseUrl()` before constructing the EventSource
  URL. Checks the `cancelled` flag both before AND after the await so a
  rapid unmount during port resolution can't open an orphan connection.

## Fix 2: Sessions list crashed when a commitments sidecar existed

`backend/services/session_service.py::list_sessions()` globs
`session_*.json` and called `data.get("session_id")` on each — but the
glob also matched commitments / item-status sidecar files
(`session_<id>.commitments.json`, `session_<id>.item_status.json`),
whose JSON root is a list, not a dict. The `.get(...)` call raised
`AttributeError` which wasn't caught, 500'd the `/sessions` endpoint,
and left the UI showing "No sessions yet" even though sessions existed
on disk.

**Fix:**
- Skip any glob hit whose stem contains a dot (canonical session IDs
  are dotless hex; a dot is a sidecar suffix).
- Add an `isinstance(data, dict)` guard after `json.load` so any future
  non-dict file that slips past the dotted-stem skip gets logged and
  skipped instead of crashing the listing.

## Install (macOS)

> v2.3.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.3.4_universal.zip`.
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
> `Meeting.Recorder_2.3.4_x64-setup.exe` or `.msi` and double-click.

## What's in the release (unchanged from v2.3.0)

- **Conference room mode** — toggle in the Record view; forces
  mic-only capture and replaces `SPEAKER_YOU` with generic labels.
- **Offline AEC validator** — `python -m backend.scripts.measure_aec`
  plus the `KEEP_AUDIO_TEMPS=1` env var to preserve per-session WAVs.

## Note on the v2.3.x line

- **v2.3.0** — feature drop; Mac build failed (Rust binding errors).
- **v2.3.1** — Rust binding fixes; Mac build still failed in DMG bundler.
- **v2.3.2** — switched Mac packaging from DMG to ditto-zipped `.app`;
  Mac shipped successfully. Live transcript pane silently broken since
  v2.3.0; sessions list could 500 if a commitments sidecar existed.
- **v2.3.3** — live-transcript fix; never tagged.
- **v2.3.4** — bundles v2.3.3's live-transcript fix with the sessions-
  list-glob fix. **Use this one.**

## Post-install one-liner (if you had to rename the commitments sidecar)

If you renamed a `session_<id>.commitments.json` to `.bak` to unblock
the Sessions list on v2.3.2, you can now rename it back — the new
glob skips it correctly without renaming:

```ps1
# Windows
Rename-Item "C:\meeting_recorder\recordings\session_<id>.commitments.json.bak" `
    "session_<id>.commitments.json"
```

```sh
# Mac
mv ~/Library/Application\ Support/MeetingRecorder/recordings/session_<id>.commitments.json.bak \
   ~/Library/Application\ Support/MeetingRecorder/recordings/session_<id>.commitments.json
```
