# v2.6.1 — Correct version string + working in-app updater

Supersedes **v2.6.0**, which was accidentally built from a pre-bump
commit: its app reported version `2.5.0`, so it nagged "update
available" on every launch and its Download button did nothing
(`window.open` is a no-op in the Tauri webview). v2.6.1 has the
correct embedded version and a working Download. All v2.6.0 features
are included.

## Install (macOS)

> v2.6.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.6.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.6.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.6.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.6.0

- **App reported the wrong version (2.5.0).** The v2.6.0 build was cut
  before the version bump landed, so it never matched the latest
  release and nagged on every launch. Embedded version is now correct.
- **In-app "Download" did nothing.** It used `window.open`, which is
  inert in the Tauri webview. Download now opens the correct installer
  for your OS (Windows `.exe`/`.msi`, macOS universal `.zip`) directly
  in your browser — the download starts immediately. (Still not a
  silent in-place updater; that needs code signing — separate work.)
- README's stale macOS `.dmg` instructions corrected to the real
  `.zip` flow; feature list refreshed.

## Everything from v2.6.0

Auto-record/auto-stop fixes, never-auto-record toggle, automatic
speaker naming, screenshots (capture + viewer + multi-monitor),
auto-process on by default, auto-refreshing Follow-Ups/Commitments/
Decisions, click-to-expand calendar meeting detail + one-click Join,
resolved Exchange attendee names, retention across client folders +
orphans, app-wide working external links, and calendar performance
fixes.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
