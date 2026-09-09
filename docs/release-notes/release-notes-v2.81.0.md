# v2.81.0 — "Ready to record" now means the microphone was actually opened

## Install (macOS)

> v2.81.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.81.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.81.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.81.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## The green light was never checked

**"Ready to record" meant a microphone was selected in the dropdown.**
Nothing had tried to open it. So it sat there green over a mic whose
driver refused every attempt, and the first anyone found out was
pressing Start with a meeting already underway.

Now the app opens the device, releases it, and tells you what happened
— when you pick a mic and whenever the selection changes. Green means
it was opened. Red means it was not, and says what the audio driver
said when it refused.

The check runs the **same sequence Start Recording runs**, so what it
proves is what recording will actually do. A check with its own
separate logic would be worse than none — it would be confidently
wrong.

It also tells you two things that used to be invisible:

- **"Opened in mono"** — the recording works, but speaker separation
  has one channel instead of two.
- **"Opened through another audio subsystem"** — Windows exposes one
  microphone several times over, and if the entry you picked refuses,
  the app uses another. That still records; you just deserve to know it
  is not the one you chose.

While a recording is running, nothing is probed — the microphone is
open and working, and reaching into the audio system underneath a live
capture is exactly what caused a crash loop in an earlier release.

## Why this took two releases

v2.80.3 fixed the underlying failure: the app was asking fallback
devices for the wrong number of audio channels. This release fixes the
part that made it painful — being told everything was fine right up
until it wasn't.
