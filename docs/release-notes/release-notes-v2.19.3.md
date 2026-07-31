# v2.19.3 — the watchdog stops killing healthy backends, and the Today tab stops losing your briefing

> **What this release fixes:**
>
> 1. **v2.19.2's watchdog was killing backends that were working fine.**
>    The 30-second "unreachable" threshold it shipped with sits *inside*
>    the window where the backend is loading its transcription models and
>    can't answer health checks. So the watchdog would kill a healthy
>    backend mid-startup, respawn it, and the replacement would get killed
>    the same way. That was my regression, introduced in v2.19.2. It's
>    fixed here.
> 2. **The Today tab no longer loses your briefing.** Clicking to another
>    tab and back could wipe the imported briefing and drop you on the
>    "import your day" empty state, as though you'd never imported one.

> **Not fixed in this release:** the backend is still hitting occasional
> native crashes (`STATUS_ACCESS_VIOLATION` / `0xC0000005`) inside the
> transcription libraries. This release stops the watchdog from making
> those worse, and makes recovery from them reliable — it does not stop
> them happening. That's still being tracked down.

## Install (macOS)

> v2.19.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.19.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.19.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.19.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## "Why does it keep restarting while the app is open?"

Because v2.19.2 taught the watchdog to check backend *health*, not just
whether the process existed — and then gave it a threshold far too short
for what the backend actually does at startup.

The log line that gave it away:

```
[20:31:05] Backend alive but unreachable for ~30s — killing it
```

That backend had been alive for 44 seconds. It wasn't wedged. It was
loading Whisper and the diarization model, which pins the Python
interpreter hard enough that the HTTP server can't answer `/health` — a
7-to-36-second window on a normal machine, longer on a cold cache or
while something else is competing for CPU. The 30-second threshold landed
squarely in the middle of it. The watchdog killed a perfectly good
backend, respawned it, and the fresh one started loading models and got
killed at the 30-second mark too.

Three changes, layered:

**60-second spawn grace.** For the first minute after a backend is
spawned, health failures don't count at all. Startup is expected to be
unresponsive; the watchdog no longer treats "still booting" as "broken."

**Threshold raised from 30 seconds to 2 minutes.** Past the grace period,
the backend must be continuously unreachable for two full minutes before
the watchdog concludes it's wedged. A backend busy with a long
transcription is busy, not dead.

**A recording in progress is never interrupted, full stop.** The frontend
now tells the Rust shell when recording starts and stops, and while that
flag is set the watchdog will not kill the backend for *any* health
reason — it logs that it's refusing to and waits. This covers
calendar-triggered auto-record and reopening the app mid-recording, not
just pressing the button yourself.

```
Backend unresponsive but a RECORDING IS ACTIVE — refusing to kill it
```

Crash recovery is unaffected: if the backend process genuinely dies, it's
respawned immediately, exactly as in v2.19.2. The change is entirely
about not killing one that hasn't.

## The Today tab keeps your briefing

Two separate bugs, both producing the same symptom — you'd import your
day, click to another tab, click back, and the briefing was gone.

**Switching tabs destroyed the view.** Tabs are rendered conditionally,
so leaving the Today tab unmounted the component and threw away its
state. Coming back re-mounted it empty and re-fetched from scratch, and
until that fetch landed you saw the empty state. The briefing is now held
in a module-level cache that survives unmount, so the tab renders with
your existing data instantly and refreshes behind it.

**A failed read looked identical to "no briefing yet."** If the briefing
file couldn't be read — locked by a sync client, mid-write, transient
backend error — the backend returned an empty result and the UI rendered
the same "import your day" empty state it shows when you genuinely
haven't imported one. Nothing distinguished "nothing there" from
"couldn't look."

The backend now returns a 503 when a briefing file exists but can't be
read, and the tab shows an explicit **"Couldn't load today's briefing"**
panel with a Retry button, auto-retrying every 5 seconds. It will never
again offer to import a day you already imported.

## Under the hood

- `RECORDING_ACTIVE` flag in the Rust shell, set via a new
  `set_recording_active` command from both the Record view and the global
  recording-status poll, so the flag is correct regardless of how the
  recording started.
- New `BriefingUnreadableError`; readers are strict (surface the failure),
  writers stay lenient so a corrupt briefing file can still be
  overwritten by a fresh import.
- 4 new watchdog unit tests, including one that walks the full 36-second
  worst-observed model-load window tick by tick and asserts the kill
  threshold is never reached.
- 4 new backend tests covering absent / unreadable / round-tripped /
  corrupt-but-overwritable briefing files.
