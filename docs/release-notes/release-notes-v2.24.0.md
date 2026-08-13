# v2.24.0 — you can now see that it's actually recording

## Install (macOS)

> v2.24.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.24.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.24.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.24.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Live capture meters

A recording once ran a full meeting and captured only the first few
minutes. Nothing on screen said so. The timer counted up the whole time,
and the loss was only discovered the next day.

The advice at the time was "watch the live transcript — if it freezes
while people are talking, capture has died." That was a workaround for a
missing instrument, so this release adds the instrument.

While recording you now get **live level meters for the microphone and
for system audio**, each with an explicit state:

- **Flowing** — audio is arriving and has real level
- **Silent** — audio is arriving, but it's quiet
- **Not receiving audio** — nothing is arriving at all

That third state is the one that matters. It is the failure that used to
be invisible.

The meters are driven by RMS the recorder was already computing for its
silence watchdog — the number existed, it was just being thrown away
after each comparison. Levels are mapped on a dB curve rather than raw
linear RMS, anchored to thresholds already in the code, so quiet-but-real
speech sits near the middle of the meter instead of pinned near zero
where you'd never notice it move.

### Silence is not failure

A muted mic is normal. A call where only the far end is talking is
normal. Neither raises a warning.

A warning appears **only** when a stream stops delivering audio data
entirely, and only after it has been dead long enough to be real rather
than a hiccup. When that happens you get a prominent banner — not a
toast that can vanish while you're mid-meeting — telling you capture may
have stopped and that you should stop and restart the recording.

This distinction is deliberate and is the most heavily tested part of the
change. A warning that fires on ordinary quiet would train you to ignore
the one that matters.

## Pre-flight readiness check

Before recording, a compact checklist replaces having to infer readiness
from dropdowns:

- Microphone selected and present
- System audio available
- Backend online
- Calendar connected

Each row says what's wrong and what to do about it. Calendar is
informational only — it being unavailable never blocks recording, and is
never shown as an error.

## Under the hood

While a recording is running, the status poller speeds up so the meters
move in something close to real time, then drops back to its idle rate
when you stop. Every view still reads from the single shared poller
introduced in v2.23.1 — this release does not add a second one.

## Still being verified

The `0xC0000005` backend crash fix shipped in v2.23.2 and is being
judged on evidence, not assertion: `crash.log` is append-only, so either
new `Windows fatal exception` entries stop appearing or they don't. This
release deliberately does not touch that code path.
