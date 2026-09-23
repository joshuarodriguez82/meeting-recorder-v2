# v2.82.4 — Back-to-back meetings no longer lose each other's audio

## Install (macOS)

> v2.82.4 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.4_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.4_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.4_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## Back-to-back meetings could lose each other's recordings

**This is the most important fix in this release.** Three back-to-back
meetings recorded on one machine, with auto-record on, came out as:

- **the first** — saved;
- **the second** — lost, with the error *"sequence item 2: expected str
  instance, NoneType found"*;
- **the third** — 29 minutes long, lost with **no error at all**. The
  app reported the stop as complete in a tenth of a second.

When a meeting stops, its audio is merged into the final recording in
the background, and a merge waits its turn if the previous meeting's is
still running — a 25-minute meeting took 82 seconds. So that the next
meeting can start straight away, a stop hands the recorder back
immediately and does its merge afterwards.

The problem was *where* each recording kept track of its own files: in
one shared place on the recorder, which the next meeting's start
overwrote and the previous meeting's cleanup erased. So while the second
meeting waited its turn, the third meeting started and the first
meeting's cleanup ran — and by the time the second meeting's merge
began, the pointer to its own microphone file was gone. The first
meeting's cleanup also erased the third meeting's pointer while it was
still recording, which is why the third meeting's stop found nothing to
save and said nothing.

With slightly different timing, the second meeting would have been
saved **with the third meeting's microphone audio** — silently. The
tests for this release reproduce that case too.

Each stop now takes its own copy of everything that belongs to its
recording — microphone file, system-audio file, recorder, session log,
settings — before it hands the recorder back, and uses only that copy
from then on. Cleanup only clears something if it still belongs to the
meeting being cleaned up. A merge that is somehow started without a
microphone file now says exactly that, instead of a type error.

Ten tests drive the real stop path through the exact sequence from the
field, deterministically. Eight fail against v2.82.3, and each of the
four parts of the fix is independently covered — reverting any one of
them turns tests red.

### If you lost a meeting this way

The audio is very likely still on your machine. A failed or skipped
merge leaves the raw recording files in Meeting Recorder's temporary
capture folder, and the app looks for them every time it starts:
**restart Meeting Recorder** and check Sessions for the missing
meetings. On Windows, don't clear `%TEMP%\meeting_recorder_capture\`
before you've done that.

## A meeting recorded with nobody else's voice in it

On Windows, a whole meeting was captured with **only the microphone**.
The far end — everyone else on the call — was missing. The log said so
once and the recording continued:

```
[FAIL] Loopback buffer=0    failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=1024 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=4096 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
[FAIL] Loopback buffer=2048 failed: … 'AUDCLNT_E_UNSUPPORTED_FORMAT'
System audio capture unavailable. Mic only.
```

Four attempts, one error, four times. That error is Windows saying
*"this combination of sample rate and channel count is not what this
speaker is using."* The app's response was to try the same rate and the
same channel count again with a different buffer size — which cannot
answer that question. Two of the four attempts were not even different
from each other.

System audio now steps through **rates and channel counts**, not just
buffer sizes: the speaker's own rate and channels first (unchanged, so
a machine that works today takes exactly the same path), then stereo
and mono, then the other standard rate. A device the app cannot read at
all still gets a full ladder instead of one guess.

When the stream opens at a rate other than the one first requested, the
recording is now written at **that** rate. Previously the file would
have carried the wrong sample rate in its header, which plays back at
the wrong speed and drifts out of sync with the microphone track.

The log line names the format it tried instead of only the buffer size,
so four failures now tell you what was actually refused.

### The same mistake, one layer over

v2.80.3 fixed exactly this for the **microphone**: a ladder that varied
sample rate, block size and latency, and never varied the one parameter
that was wrong. System audio never got that treatment. It has it now,
and the decision lives in its own module with 18 tests — five of which
fail against the previous behaviour.

## Still being looked at

Two problems from the same report are **not** fixed here:

- **Nothing tells you while it's happening.** Both the missing system
  audio and the lost recordings were written to the log and shown to
  nobody. A live warning on the recording screen — while the meeting can
  still be saved — is next.
- **Voice fingerprinting silently unavailable** on one install because
  the installed speech library no longer matches what the app expects.
  That is an environment mismatch rather than a code defect, and it
  needs a proper startup check rather than a log line nobody reads.
