# v2.20.1 — set the shared archive folder from Settings

> v2.20.0 added a shared session archive so a Mac and a PC can see one
> library, but left it reachable only by hand-editing a config file.
> This adds the Settings card it should have shipped with.

## Install (macOS)

> v2.20.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Session Archive, in Settings

**Settings → Session Archive.** Browse to a folder that syncs to both
machines (iCloud, OneDrive, Drive), save, done. Each machine keeps
recording to its own local disk; the synced folder carries the session
files so both see one library. Audio is never copied there.

Set it to the same folder on your Mac and your PC and they merge.

The card shows **"N in shared archive · M local · P pending"** so you
can tell at a glance whether it's actually working, plus a **Sync now**
button. If the folder isn't reachable — sync client offline, drive
unplugged — it says so instead of quietly reporting success.

Changing the folder takes effect immediately; no restart, and existing
sessions start copying in the background right away.

Anyone already using the `SESSION_ARCHIVE_DIR` environment variable is
unaffected — the setting takes precedence, the variable still works.

## Also fixed

Toggling the live co-pilot mid-recording saved settings without the new
archive field, which would have silently blanked your archive folder
every time you flipped it. Caught before it could bite.

## Under the hood

- Pending-detection extracted into a shared module used by both the
  background reconciler and the status readout, so the two can't drift
  on what "pending" means.
- New `GET /sessions/archive-status` and `POST /sessions/archive/sync`.
- 199 backend tests (22 new), including one asserting an unreachable
  archive folder reads as **all pending** rather than all present — a
  disconnected sync mount must never report success.
