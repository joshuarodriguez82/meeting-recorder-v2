# v2.7.4 — Auto-record actually records, and you can see it's running

Patch release. Fixes a cluster of bugs around auto-record that all came
from "auto-record exercised paths the manual flow always covered." All
v2.7.3 features are included.

## Install (macOS)

> v2.7.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.4_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.4_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.4_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.7.3

- **Auto-record was recording silence.** The calendar auto-recorder
  built its start request with no mic / loopback device indices, so
  the capture began with nothing selected and the WAV was empty. Auto-
  record now uses the mic + loopback you last picked manually
  (persisted **by name**, so a USB re-plug or reboot doesn't break it)
  and, if you've never run a manual recording, surfaces a clear
  "Auto-record skipped — no microphone configured" notification
  instead of silently recording nothing.
- **No visible cue when auto-record fired.** Auto-record could start
  while you were on any other tab with zero indication, so you'd
  click manual Start and collide with the in-progress recording. Now
  there's a persistent **● Recording…** badge in the sidebar (visible
  on every tab) with the meeting subject + live elapsed timer, plus a
  native OS notification + in-app toast the moment auto-record kicks
  off ("Auto-recording started: *<subject>*").
- **Start button disappeared after Stop until you switched tabs.** The
  Record-view button was gated on "no recently-finished session
  showing," so after a stop the just-finished session pane hid Start
  until the tab unmounted. The gate is gone; click Start any time to
  begin a fresh recording.

## Everything from v2.7.3 and earlier

The PowerShell-window-in-screenshots fix, the one-click Windows
in-app update (downloads + launches the installer; falls back to the
browser on failure), the gated update prompt (no dead-end before
installers publish), the **Engagements** layer (per-client register +
hand-editable Excel export), the **Python-3.12 installer bootstrap**
fix, and **calendar auto-record for every timed meeting**.

## Notes

- The first time after upgrading: run a manual recording once. That
  registers your mic + loopback by name so calendar auto-record knows
  what to use the next time it fires. Without that one-time step the
  auto-record skip notification will tell you what's missing.
- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
