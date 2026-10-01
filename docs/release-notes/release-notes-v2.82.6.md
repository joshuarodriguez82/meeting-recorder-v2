# v2.82.6 — Mac records system audio without BlackHole

## Install (macOS)

> v2.82.6 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.6_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.6_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.6_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## System audio on a Mac, through any output

Until now a Mac could only record the other people on a call through
**BlackHole**, with all your sound routed through a Multi-Output Device.
That broke in the most common way possible: connect Bluetooth
headphones, macOS sends sound straight to them, BlackHole hears nothing
— and the other participants aren't recorded, with no error.

On **macOS 13 or later**, the System Audio list now starts with
**System audio — all apps (no BlackHole needed)**. It records whatever
your Mac plays, through any output: built-in speakers, AirPods and other
Bluetooth headphones, USB, HDMI. No BlackHole, no Multi-Output Device,
and switching headphones mid-meeting doesn't break it.

**The first recording asks for permission** to record system audio
(**Screen & System Audio Recording**). Allow it. If you miss the prompt,
the app tells you straight away that the other participants aren't
being recorded and exactly where to turn it on: **System Settings →
Privacy & Security → Screen & System Audio Recording → Meeting
Recorder**, then stop and restart the recording. Only audio is kept.

Because the app isn't signed yet, macOS may ask for this permission
again after an update.

BlackHole still works exactly as before, and is still the way on
macOS 12. If you already picked BlackHole, the app keeps it until you
choose the new option.

## A headset connected after launch now shows up

A Bluetooth headset connected after Meeting Recorder started never
appeared in the microphone list until you restarted the whole app. The
app now re-checks your audio devices when you come back to its window,
and keeps your chosen microphone selected even if the list reorders.
It never re-checks during a recording.
