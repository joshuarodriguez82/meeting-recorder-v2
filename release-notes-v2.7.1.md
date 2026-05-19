# v2.7.1 — Update-notification fix

Patch release. Stops the in-app update prompt from appearing before the
new build's installers are actually published. All v2.7.0 features are
included.

## Install (macOS)

> v2.7.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.7.0

- **"Update available" appeared before the installer was downloadable.**
  The GitHub release object exists the instant a version tag is pushed,
  but the Windows/macOS installers upload ~10–20 min later when the
  build finishes. The app prompted as soon as the tag existed, so
  "Download" could land on a release/build page with no file yet. The
  update check now only surfaces a prompt once an installer asset for
  your OS is actually attached to the release; otherwise it stays quiet
  and catches it on a later launch.

## Everything from v2.7.0

The **Engagements** layer (per-client register + hand-editable Excel
export), the **Python-3.12 installer bootstrap** fix (clean machines
install without a developer toolchain), and **calendar auto-record for
every timed meeting** (auto-stop ends it per Settings).

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
- The in-app updater still opens the installer download in your browser
  for you to run; it does not auto-launch the installer (that needs a
  signed in-place updater — tracked separately).
