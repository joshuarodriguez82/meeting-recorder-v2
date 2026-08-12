# v2.23.2 — the backend crash, finally diagnosed from a real traceback

## Install (macOS)

> v2.23.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.23.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.23.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.23.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The backend crash

The backend has been dying with `STATUS_ACCESS_VIOLATION` (`0xC0000005`)
for months — mid-call, on Stop, and while sitting idle. One log recorded
**203 respawns**. Six different theories were proposed and every one was
wrong, because all of them were inferred from *when* the process died.

That reasoning was never going to work. v2.23.0 added `faulthandler`,
which finally wrote a real native traceback to `crash.log`, and it shows
why: the crash is triggered by **garbage collection**, so the code that
appears to be running at crash time is unrelated to the code at fault.
Every theory built on timing was chasing a coincidence.

Of the 8 crashes captured, 7 are byte-for-byte the same fault:

```
Current thread:
  Garbage-collecting
  File "...comtypes\_post_coinit\unknwn.py", line 420 in Release
  File "...comtypes\_post_coinit\unknwn.py", line 288 in __del__
  File "<a different innocent frame every time>"
```

`comtypes` releasing a Windows COM interface pointer whose apartment no
longer exists.

### Why the apartment was gone

Two subsystems used COM, and both ran on the shared `asyncio.to_thread`
thread pool:

- **pycaw** (audio device format inspection) creates COM proxies through
  comtypes, and never initialised or tore down an apartment explicitly —
  it inherited whatever pool thread it landed on.
- **Outlook calendar** (pywin32) called `CoInitialize()` on entry and
  tore the apartment down in a `finally` block on the way out.

Pool threads get reused. So an Outlook call would destroy the COM
apartment on a thread that still held live pycaw proxies from an earlier
task. Those proxies were now pointing into nothing. The next time the
garbage collector ran their `__del__`, it called `Release()` into a dead
apartment and Windows killed the process.

Nothing about that is deterministic, which is exactly why it looked
random, struck at unrelated moments, and survived six fixes.

### The fix

All COM work now runs on **one dedicated thread that owns a single
apartment for the entire life of the process**. It initialises COM once
and deliberately **never** tears it down — an apartment that is never
destroyed cannot be destroyed out from under a live object. Every COM
caller was rerouted onto it: audio device inspection, Outlook calendar
reads, Outlook follow-up drafts, and Windows startup-shortcut creation.
Results cross the thread boundary as plain data, never as COM objects.

Leaking the apartment for the process lifetime is the correct behaviour
here, not an oversight, and the code says so at length so it doesn't get
"fixed" back.

## Your Chrome extension should start working again

The extension posts to `127.0.0.1:17645`. The recorder is supposed to
hold that port, and only falls back to a random one when something else
has it.

That something else was **the recorder's own orphaned processes** — each
crash could leave a `pythonw.exe` behind still holding 17645, so the
replacement backend came up on a random port. The app finds its backend
dynamically so it kept working; the extension, which targets 17645
directly, could not. Fewer crashes means fewer orphans means the port
stays where the extension expects it.

If it's still failing right after updating, quit the app and end any
stray `pythonw.exe` in Task Manager once — that clears orphans left by
earlier crashes.

## How to tell whether this actually worked

`crash.log` (**Settings → Data & Diagnostics**) is append-only and
timestamped. Either new `Windows fatal exception` blocks stop appearing
after this update, or they don't. Two secondary signals: the backend
should hold port 17645 instead of falling back, and
`Backend exited unexpectedly: ExitStatus(3221225477)` should stop
appearing in `rust.log`.

This release deliberately contains **only** the COM fix, so if the
crashes stop there is no ambiguity about what stopped them.

## Honest scope

One of the 8 captured crashes had a different signature — the faulting
thread was inside PyAudio's constructor rather than comtypes. It is
plausible that it shares this root cause, since PortAudio's WASAPI
backend also uses COM and would fault the same way if its apartment were
torn down underneath it. That is a hypothesis, not a finding. If crashes
continue after this release, that is the next thread to pull.

Separately, a report of a recording that ran a full meeting but captured
only the first few minutes is **not** addressed here. If a crash caused
it, this fixes it. If it is an independent capture bug, it survives, and
it will be diagnosed from the `AUDIO_INTEGRITY` / `SYNC_INTEGRITY`
measurements already written on every session rather than guessed at.
