# v2.31.0 — three hardening fixes, one of which echo cancellation made urgent

## Install (macOS)

> v2.31.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.31.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.31.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.31.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

This release is preventive. Nothing here fixes a bug you reported — it
closes three gaps found by code audit and confirmed absent from the
source, one of which echo cancellation turned from theoretical into
live.

## Finalization can no longer starve a meeting that's still recording

Stopping a recording spawns a finalize job. Nothing serialized those
jobs, and nothing stopped one from competing with live capture.

That barely mattered when finalize took 3–15 seconds. With echo
cancellation it takes **192–278 seconds** — real numbers from a single
morning: 278.5s, 144.8s, 191.9s. That's a window roughly forty times
wider in which a back-to-back meeting, or a bulk re-process, can run a
multi-threaded resampler and echo-cancellation pass across every core
while a meeting is still being recorded. Dropped frames in a call
that's still happening is the worst possible time to lose audio.

Two changes:

**Only one finalize runs at a time.** Both spawn sites — stopping a
recording, and the startup recovery sweep — now pass through a single
gate. A second job waits its turn instead of competing.

**Finalize runs at below-normal CPU priority.** Live capture always
outranks post-processing at the OS scheduler level. If the priority
hint can't be applied on some platform, finalize still runs — a
hardening measure must never become a reason a recording fails to
finalize.

Because stopping a recording already waits for finalize, queueing
behind another job makes that wait longer. So a queued session now says
so: **"waiting behind another finalize job (queued for 1m 20s)"**,
distinct from one that's actively running. A wait you can see is a wait;
a wait you can't is a hang.

## Memory is released after transcription and diarization

Nothing in the backend released PyTorch's caching allocator after model
work — `empty_cache` appeared zero times in the entire codebase. Whisper,
diarization, and speaker embeddings each leave cached allocations
behind, and across five or six back-to-back calls in a working day that
accumulates. On a 16 GB laptop running two-hour sessions, that ends in
an out-of-memory crash.

Cleanup now runs after transcription, diarization, speaker-centroid
extraction, and document embedding — once per recording or per batch.

Deliberately **not** on the live paths: per-utterance speaker tracking
and per-query search embedding. Running a garbage collection on every
utterance would trade a slow leak for constant latency in the live
transcript. The cleanup is also a silent no-op when PyTorch isn't
installed, so CPU-only installs are unaffected.

## A killed app can no longer leave the backend holding your audio device

On Windows, ending the app from Task Manager could leave the Python
backend running — holding the microphone, the database lock, and port
17645. The next launch then fails because the port is taken.

There was already a watchdog: the backend polls whether its parent
process is alive and exits when it isn't. That stays, and it's the
better path — it stops an active recording cleanly first, so you get a
finished recording rather than an orphaned temp file.

The gap it can't cover is **PID recycling**. The watchdog checks that
*a* process with the parent's ID exists, not that it's the *same*
process. If Windows reassigns that ID to something unrelated, the
watchdog sees "alive" forever and never fires.

The Python sidecar is now assigned to a Windows **Job Object** with
kill-on-close, so when the app's process handle goes away the kernel
terminates the backend unconditionally — no polling, no PID ambiguity.
If the Job Object can't be created it's logged and the app starts
normally, with the existing watchdog as the fallback.

Non-Windows behaviour is unchanged.

## Tests

741 backend tests, up from 725. The 16 new ones cover: two finalizes
cannot run concurrently, the queued callback fires exactly once, the
gate releases on exception, the priority flag is correct per platform
and a failure to apply it doesn't block finalize, a full two-session
concurrency proof through `stop_recording`, and the memory cleanup
being a genuine no-op with PyTorch absent or broken.

## One honest limitation

The Windows Job Object was cross-compiled and lint-checked for the
Windows target, but no Windows machine executed it — that isn't
possible from the build environment. Whether kill-on-close actually
fires on a Task Manager kill is unverified until it runs on a real
machine. The cross-compile did catch one genuine bug before release, so
it wasn't merely assumed to work.
