# v2.7.2 — Screenshot window fix + one-click update (Windows)

Patch release. Stops a PowerShell window from flashing into screenshots,
and makes the in-app updater download and launch the installer directly
on Windows instead of opening a browser. All v2.7.1 / v2.7.0 features
are included.

## Install (macOS)

> v2.7.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.7.1

- **A PowerShell window flashed into screenshots.** The Windows
  screen-capture command was the one process in the app spawned
  without the hidden-window flag, so a blank console popped up over a
  live meeting and was captured into the screenshot itself. It's now
  hidden like every other shelled command.
- **In-app update now downloads and launches the installer (Windows).**
  Clicking Download no longer opens a browser tab — the app fetches
  `Meeting.Recorder_X.Y.Z_x64-setup.exe` to your temp folder and starts
  it (Windows will show the usual UAC prompt). If anything fails it
  falls back to the old browser download, so updating can't get worse.
  macOS still opens the `.zip` in the browser (unsigned `.zip` can't
  auto-install under Gatekeeper).

## Everything from v2.7.1 and v2.7.0

Update prompt now waits until installers are actually published; the
**Engagements** layer (per-client register + hand-editable Excel
export); the **Python-3.12 installer bootstrap** fix; and **calendar
auto-record for every timed meeting** (auto-stop ends it per Settings).

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
- Windows one-click update launches the NSIS installer; close the app
  when the installer asks so it can replace the running build.
