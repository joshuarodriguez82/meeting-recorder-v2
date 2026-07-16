# v2.19.0 — text-only network exports (audio stays local, always)

> **What this release changes:**
>
> Every path that copies a session to a network / cloud folder now
> writes **only the derived text artifacts** — transcript, summary,
> action items, decisions, and requirements. The **raw session WAV and
> the session JSON never leave local disk.** That covers per-client
> Designated Folders (Clients view) and the Cloud Mirror root
> (Settings). The WAV was the artifact that stalled Google Drive in
> every incident, and it isn't what teammates read from a shared
> drive — a `.txt` transcript is.
>
> No settings to change. Existing Cloud Mirror + Designated Folder
> configs keep working, but only text goes across the network from
> now on. Recording, finalize, and process paths are still 100% local.

## Install (macOS)

> v2.19.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.19.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.19.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.19.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The rule, in one paragraph

- **Local disk** holds the canonical session: `session_<id>.json` +
  `session_<id>.wav` under your recordings folder.
- **Network folder** (Cloud Mirror or client Designated Folder) holds
  only the human-readable outputs — transcript, summary, action items,
  decisions, requirements — dropped in per-client subfolders.
- Nothing changes when a session finishes recording. Text arrives as
  each processing step completes (transcribe → summary → extractions),
  each writing its own artifact to the folder in the background with
  retries.
- Manual "Export" button follows the same rule (text only).

## Why this is the safe default

Every Drive-stall incident this month traced to the same thing: the app
tried to write a multi-hundred-MB WAV directly onto Google Drive Stream
mid-recording or mid-finalize. Text artifacts are KB-sized; they can't
cause the same class of freeze. Removing the audio copy from the
network path leaves nothing left to stall on.

You still get:

- **Every session on your local disk** — full audio, full JSON, full
  history, always reachable from the app.
- **Every processed artifact on the shared folder** — teammates and
  other machines read summaries, action items, decisions from the
  network folder without the app installed.
- **Retention** still cleans up old audio (from your local recordings
  dir); text artifacts on the network folder are tiny and keep forever.

## If you want the audio somewhere else

Point Google Drive for desktop's **"Mirror files"** mode at the same
folder your recordings live in — it uploads the WAV in Drive's own
background sync (which is what it's designed for), completely separate
from the app. That's the supported way to get audio playback across
machines without paying for it on the recording thread.

## Under the hood

- The background export worker's `_do_export_session` ignores its
  `copy_audio` flag and always calls `export_all(copy_audio=False,
  strict=False)`. `strict=True` was there to force retries on WAV-copy
  failures; with no WAV copy, no retry needed for that class.
- Enqueues at `stop_recording` and `import_from_file` are removed —
  there's no text to write at those points and the WAV never goes.
- Retention's `_client_export_dirs()` no longer enumerates Cloud Mirror
  subfolders (there are no orphan WAVs to sweep there anymore); it still
  covers explicit Designated Folders that pre-date v2.19.
- 3 new tests pin the invariant: `export_all(copy_audio=False)` never
  writes a WAV, empty sessions bail without touching the mount, the
  local source WAV is byte-identical after export. Full suite: 108
  passing.
