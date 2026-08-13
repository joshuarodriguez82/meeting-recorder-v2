# v2.25.1 — the crashing code no longer runs in the backend process

## Install (macOS)

> v2.25.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.25.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.25.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.25.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What was still crashing, and why v2.23.2 didn't fix it

v2.23.2 moved all COM work onto a dedicated thread that creates a COM
apartment once and never tears it down. The theory was that tearing the
apartment down on shared pool threads was invalidating live COM pointers.

The crash continued, roughly every 90 seconds. The theory was wrong — or
rather, incomplete in a way that mattered.

COM pointers created in a single-threaded apartment may only be released
**from that same thread**. Keeping the apartment alive forever doesn't
help if the release happens somewhere else. And it did: comtypes objects
sit in reference cycles, so they aren't freed when the worker finishes
its job. They wait in Python's garbage collector and are destroyed later,
on whatever thread happens to trigger a collection.

In the two most recent crashes those threads were parsing JSON — once in
session listing, once in retention cleanup. Neither has anything to do
with audio. That's why every earlier theory based on *when* the crash
happened was chasing a coincidence: the timing belongs to the garbage
collector, not to the faulty code.

## Narrowing it to one function

Across all ten captured crashes the fault is in **comtypes**, never in
pywin32. That distinction matters: comtypes is what pycaw uses, and pycaw
has exactly one caller here — the audio device format check behind the
sync-risk warning. The Outlook calendar, which uses pywin32, was never
involved.

So a diagnostic that displays an optional warning was taking down the
recorder.

## The fix

**pycaw no longer runs in the backend process at all.** The device format
check now runs in a short-lived child process, the same approach already
used for audio finalization — a native crash in a child cannot take the
backend down with it. If that child crashes, times out, or returns
anything unexpected, the check simply reports "unknown" and the app
carries on.

There is deliberately **no fallback that runs pycaw in-process**. A silent
fallback would reintroduce this crash while appearing fixed. If the
subprocess route can't be used, the diagnostic is skipped entirely.
Losing an optional audio-sync warning costs nothing; crashing the backend
costs a meeting.

Two supporting changes:

- Results are now **cached** per device (including negative results), so
  the check runs far less often. The logs showed the same failing lookup
  repeating continuously while the Record tab sat open.
- The COM worker thread now **collects cyclic garbage itself** after every
  job, inside its own apartment, so any COM object created through it is
  destroyed on the thread that owns it rather than drifting into a
  collection on some unrelated thread later. This protects paths not yet
  identified.

A setting also lets the whole lookup be switched off.

## Being honest about confidence

This is the second fix aimed at this crash, and the first one failed.

What's different: the previous fix was built on a theory about apartment
lifetime that the crash data later contradicted. This one removes the
implicated code from the process entirely rather than trying to make it
safe in place — a weaker claim that doesn't depend on reasoning about COM
threading being correct.

The verdict comes from `crash.log` (**Settings → Data & Diagnostics**),
which is append-only and timestamped. Either new
`Windows fatal exception` entries stop appearing, or they don't.
