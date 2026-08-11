# v2.21.3 — a switch to stop the backend crashing when a call ends

> If the app says **"Reconnecting to backend"** right after you stop
> recording — and your meetings sometimes come out only half-processed
> — this release gives you the switch that should stop it.

## Install (macOS)

> v2.21.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.21.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.21.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.21.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What was happening

The backend was dying a few seconds after every recording stopped —
consistently, on a stopwatch:

```
Recording stopped   22:44:48
Backend died        22:44:52
```

Two things follow from that, and they turn out to be the same problem:

**"Reconnecting to backend" right after a call.** The app notices,
restarts it, and carries on — which is why it looked like a hiccup
rather than a crash.

**Meetings that only got part of the way through.** After a recording,
the app runs transcript → summary → action items → decisions →
requirements. The crash was landing in the middle of that chain, so a
meeting kept whatever had finished and lost the rest. That's the reason
some sessions show every icon in the list and others show three.

Not two bugs. One.

## The switch

**Settings → Recording & Co-Pilot → Speaker identification device.**

The likely cause is two pieces of AI using your graphics card at the
same time — transcription is still holding it when speaker
identification starts, and they use incompatible versions of the same
graphics library. The second one in wins or crashes; here it crashed.

Setting this to **CPU** keeps speaker identification off the graphics
card entirely, so the two can't collide. Speaker identification gets
slower; nothing else changes.

- **Auto** (default) — exactly as before, uses your GPU
- **CPU** — slower, avoids the collision
- **GPU** — force it, falls back to CPU rather than failing if there's
  no GPU

**Try CPU if you're seeing this.** Record a short call, stop it, and
watch whether the backend survives. That result tells us whether the
diagnosis is right.

## Being straight with you

This is a workaround and a test, not a proven cure. The evidence points
firmly at a graphics-library conflict, but the switch exists so it can
be confirmed rather than assumed.

The real fix is to run the AI work in a separate process, so a crash of
this kind costs that work and not your whole backend. That's worth
building once this is confirmed, rather than guessing at it first.

## Your recordings were never at risk

Audio is written to disk continuously while you record, not saved up
until the end. And the app already recovers interrupted recordings on
the next start — that's why a session that crashed still came back with
its audio intact. What was being lost was the processing afterwards,
which you could always re-run with **Process**.
