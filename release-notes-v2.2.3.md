# v2.2.3 — Auto-stop watchdog

Catches the "I forgot the recording was still running for hours" failure
mode. Three independent triggers share the same plumbing — warnings show
inline + fire native OS notifications; auto-stops actually end the
recording and run the normal post-stop processing pipeline.

> ## ⚠️ macOS install — READ THIS FIRST
>
> v2.2.3 ships **a single universal `.dmg`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.2.3_universal.dmg`.
>
> The build is **unsigned** — first launch needs the Gatekeeper
> bypass. Pick whichever is easier:
>
> **Path A — System Settings:** double-click the app, dismiss the
> "damaged" warning, then **System Settings → Privacy & Security →
> Open Anyway**, double-click again, click Open.
>
> **Path B — Terminal:**
> ```sh
> xattr -cr ~/Downloads/Meeting*.dmg
> open ~/Downloads/Meeting*.dmg
> # drag to Applications, then:
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users** — none of this Gatekeeper stuff applies. Download
> the `.msi` or `.exe` and double-click.

## What's new

### Auto-stop watchdog

Three independent triggers, configurable in **Settings → Auto-stop**:

| Trigger | What it watches | Default |
|---|---|---|
| **Silence (dead air)** | Mic + loopback RMS below threshold for N min | Warn at 5 min, auto-stop opt-in |
| **Meeting overrun** | Wall clock past the calendar event's scheduled end + N min | Warn at 5 min after end, auto-stop opt-in |
| **Hard cap** | Total recording duration | Always-on safety net at 4 hours; auto-stops |

Warnings render as an amber banner under the recording bar AND fire a
native OS notification (once per warning code per recording — no spam).
Auto-stops use the same code path as your Stop button, so the audio
file finalises cleanly and the post-stop processing chain runs as
normal (transcribe → summarize → action items, depending on your
auto-process setting).

### Calendar-aware overrun detection

When you start a recording from an Upcoming Meetings tile (the **Use**
button on a calendar entry), the meeting's scheduled end time threads
through to the backend's watchdog. That's how the meeting-overrun
trigger knows when to start nagging. Ad-hoc recordings (no calendar
context) skip this trigger entirely — only the silence + hard-cap
ones apply.

### How the silence detector works

The audio-chunk callback computes RMS energy on every block. If the
sample exceeds an empirically-calibrated threshold (≈ -50 dBFS, which
maps to amplitude > 0.003 in float space), we mark "speech detected"
at the current timestamp. The watchdog reads that timestamp every
second on the same poll the UI uses for duration; if more than N
minutes have passed since the last speech-level chunk, the warning
fires. Both mic and loopback streams contribute — if either is hot,
the room isn't dead.

## Config storage

Five new lines in `config.env`:

```
SILENCE_WARN_MIN=5
SILENCE_STOP_MIN=0
OVERRUN_WARN_MIN=5
OVERRUN_STOP_MIN=0
HARD_CAP_HOURS=4
```

Existing installs migrate to the defaults automatically.

## No feature changes from v2.2.2

The semantic-index auto-build, universal2 Mac DMG, and dependency
self-heal from v2.2.2 are unchanged. v2.2.3 only adds the auto-stop
layer.
