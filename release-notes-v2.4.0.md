# v2.4.0 — Cross-device sync via cloud-synced recordings folder

Foundation work that lets you point the recordings folder at OneDrive
(or iCloud Drive, Dropbox, etc.) and have your sessions, clients, and
summary templates roam across devices automatically. No backend, no
login, no cloud bill — just file sync via the OS-level cloud client
you already have.

## What changed

### 1. Settings UI: pick the recordings folder

New "Recordings Folder" card at the top of Settings. Shows the current
path with a native folder picker (Tauri dialog). Set it to a
cloud-synced folder once on each device — same target — and you're
done. Save still uses the existing save button at the bottom of the
Settings page.

The app needs a restart for the new folder to fully take effect (the
backend re-instantiates services on save, but cached in-memory state
elsewhere can be stale until next launch).

### 2. `client_configs.json` and `summary_templates.json` moved into `RECORDINGS_DIR`

Previously these lived in `%LOCALAPPDATA%\MeetingRecorder\` on Windows
and `~/Library/Application Support/MeetingRecorder/` on macOS —
per-machine, not syncable. Now they live alongside the session JSONs
inside whatever folder `RECORDINGS_DIR` points at. Point that folder at
OneDrive and your client list + custom templates ride with your
sessions across machines.

### 3. Two migration paths so nothing breaks on upgrade

**First v2.4 launch** — `load_settings()` checks if
`RECORDINGS_DIR/<file>` exists and, if not, copies from the legacy
`USER_DATA_DIR/<file>` location. The old file is left in place as a
fallback in case you downgrade to v2.3.x.

**Changing folders mid-session** — `save_settings()` captures the
previous `recordings_dir` value before writing the new one and copies
the live clients/templates over if the new folder doesn't already
have its own copy. Again, copy not move — the old folder's copies are
untouched so revert is non-destructive.

### 4. Speaker profiles stay per-machine

`speaker_profiles.json` (the voice-fingerprint store powering
auto-rename of known speakers) **does not** sync. Voice fingerprints
are mic-hardware-dependent: a profile built against a Yeti USB mic
won't reliably match the same person captured through a laptop's
built-in array. Syncing them across devices with different mics would
produce false positives. You'll need to re-enroll on each machine.
(This may change in a future release if we add per-mic profile
variants.)

## Setting it up — typical PC primary + Mac travel flow

1. **On the PC** (main device, where your existing data lives): open
   Settings → Recordings Folder → Browse… → pick a folder under your
   OneDrive (e.g. `C:\Users\YOU\OneDrive\MeetingRecorder`). Save.
   Restart the app. v2.4 copies your clients/templates into the new
   folder; OneDrive syncs them to the cloud.
2. **On the Mac** (travel device): install v2.4, install the OneDrive
   client if not present and let it sync. Open Settings → Recordings
   Folder → Browse… → pick the same folder (`~/OneDrive/MeetingRecorder`
   on most Mac OneDrive layouts). Save. Restart. You should now see
   your PC's clients and any previously-recorded sessions.

For iCloud Drive instead of OneDrive, the path is
`~/Library/Mobile Documents/com~apple~CloudDocs/MeetingRecorder` on
Mac. Windows can mount iCloud via Apple's iCloud for Windows client
at `%USERPROFILE%\iCloudDrive\MeetingRecorder`.

## Caveats

- **No conflict resolution**: if you record on both devices in the
  same minute and they both write to OneDrive simultaneously, OneDrive
  will produce a conflict file. The session JSONs are tiny so this
  rarely happens in practice, but it's a possibility.
- **WAVs are big**: a 1h meeting at 16kHz mono is ~115 MB. If you
  record a lot, your cloud storage will fill up. OneDrive's "Files
  On-Demand" mostly handles this gracefully — old recordings stay
  offline until you click play.
- **No real-time sync**: OneDrive's sync interval can be minutes.
  Don't expect Mac to see a PC recording before OneDrive has uploaded
  it.

If any of those become real pain, the path forward is a small cloud
backend (Supabase + R2) — that's a v3.0 work item, not v2.4.

## Install (macOS)

> v2.4.0 ships **a single universal `.zip`** that runs on every Mac.
> On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.4.0_universal.zip`.
>
> v2.4.0 inherits v2.3.5's ad-hoc codesign with identifier-based DR,
> so calendar permissions granted on v2.3.5 should persist across the
> upgrade.
>
> **Path A — Finder:** double-click the `.zip`, drag the `.app` to
> `/Applications`, double-click, dismiss the "damaged" warning, then
> **System Settings → Privacy & Security → Open Anyway**.
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
> **Windows users**: download `Meeting.Recorder_2.4.0_x64-setup.exe` or
> `.msi` and double-click. No Gatekeeper-style hoops.

## Roadmap pointer

v3.0 plan, not started: a small Capacitor-wrapped Android app for
on-the-run mic-only recording, writing to the same OneDrive folder
via SAF. Sideload distribution, no Play Store. Will share the same
client list (because clients live in RECORDINGS_DIR now). iOS not
targeted in v3.0.
