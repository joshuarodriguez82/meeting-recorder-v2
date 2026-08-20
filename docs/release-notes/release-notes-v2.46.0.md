# v2.46.0 — the crash loop, and why the app could trap you in it

## Install (macOS)

> v2.46.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.46.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.46.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.46.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.9.0**.

## The crash

Windows fatal exception: access violation. The backend died about
fifteen seconds after every start, the supervisor respawned it, and it
died again — sixteen cycles in four minutes with the app unusable
throughout.

`faulthandler` caught two threads inside PortAudio simultaneously:

```
Thread A   _prewarm_audio → list_output_devices → PyAudio.terminate()
Thread B   auto-record starting a recording     → PyAudio.__init__()
```

`PyAudio()` calls `Pa_Initialize()` and `.terminate()` calls
`Pa_Terminate()`. Both mutate a single process-global state in C and
must not run concurrently. When they do, the heap is corrupted — and
because that is a C-level fault there is no Python exception to catch:
the interpreter is killed outright. `0xC0000005` on most spawns,
`0xC0000374` on others, which is the same corruption surfacing at a
different allocation.

**Why it was a loop rather than an occasional glitch.** The user had a
meeting already *in progress*. Auto-record fires for an in-progress
meeting immediately, so a recording start landed within milliseconds of
boot — exactly when the audio pre-warm runs. Every restart recreated
the same conditions, so every restart hit the same crash.

Every PortAudio entry point in the app now runs under one process-wide
lock, held across the whole init → use → terminate span. Guarding
construction alone would not be enough: one thread can still terminate
while another is mid-enumeration, which is the same fault.

## The part that made it much worse

**Nothing stopped the retry, and the off-switch was behind the thing
that was broken.** Settings are served *by* the backend, so the toggle
that would have disabled auto-record was unreachable precisely because
the backend would not stay up. Unbounded retry plus an unreachable
off-switch is a trap, not resilience.

Respawning forever is still correct for a *transient* failure — an
antivirus lock, a moment of disk pressure. What was missing was
noticing when the failure is not transient. Three backends dying before
they finish starting is not a flake; it is a reproducible crash on a
startup path, and spawning the same thing again cannot produce a
different result.

**Safe mode** now engages after three such deaths. The backend restarts
with auto-record and the audio pre-warm both disabled — the two startup
paths that can crash it — so the app comes up and Settings is reachable.

Your setting is not touched. Safe mode does not write
`auto_record_enabled`, so nothing is turned off behind your back; a
normal restart leaves safe mode and restores exactly what you had.

## The log path was wrong on the one screen that needed it

The "Backend didn't start" screen told users to check
`%APPDATA%\MeetingRecorder\backend.log`. The log is written to
`%LOCALAPPDATA%`. Different folders. Every other screen in the app had
it right — just the one that appears when you are already stuck sent
you to an empty directory.

## Tests

1269 backend tests, up from 1264.

A C-level access violation cannot be reproduced in a test — it kills
the interpreter, so there is nothing to assert on. What the five new
tests pin is the invariant that prevents it: **at most one thread
inside PortAudio at any instant**, driven through the real locking
helpers against a fake that reports overlap.

Both regression tests were verified to **fail** with the lock removed —
eight threads inside PortAudio at once — and pass with it. A test that
passes either way would have been worse than none, since it reads as
coverage.

The rest cover teardown still running when the body raises (a leaked
init strands the refcount and makes the *next* caller's terminate the
one that corrupts), terminate never raising on stop paths, and the lock
being reentrant — the capture path can legitimately enumerate while
already holding it, and a plain lock would trade the crash for a hang.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, personal-data 0.
