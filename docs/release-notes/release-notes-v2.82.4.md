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
fail against the previous behaviour — plus three that drive the real
Windows open against a simulated speaker that refuses its own format.

## You're told at once when the other participants aren't being recorded

When system audio couldn't be opened, the app waited for 45 seconds of
"silence" and then showed, on the Record tab only: *"No system audio for
45 seconds — capture may have stopped. Consider stopping and restarting
the recording."* All three parts of that were wrong for this failure:

- **It waited.** The app knew at the first second that system audio had
  never started.
- **The advice didn't work.** When Windows refuses a speaker's format, a
  restart asks the same question and gets the same answer.
- **Nobody was looking.** During an auto-recorded meeting you're in the
  meeting, not on the Record tab.

Now:

- **At once, from any tab.** On the first status check after the
  recording starts (about two seconds), you get a system notification
  and an in-app alert that stays until you dismiss it: **"Other
  participants aren't being recorded"**, with an **Open Record** button.
  The red recording strip in the sidebar, which is visible from every
  tab, changes to **"Only your mic is recording"**.
- **What to actually do.** The message names the cause and the fix for
  it: a refused audio format (on Windows: Sound settings → the speaker's
  Properties → Advanced → a 2 channel, 48000 Hz Default Format, then
  restart the recording), a speaker that was unplugged or switched, or
  another app holding exclusive control of it. A cause the app doesn't
  recognise shows the underlying error rather than a guess.
- **Once, not every two seconds.** One notification per problem per
  recording. If the microphone then fails as well, that's a new problem
  and you're told about that too.
- **Afterwards, on the session.** A meeting recorded without the other
  participants now carries a red note in Sessions: *"Other participants
  were not recorded — … Only your microphone was captured, so the
  transcript, summary and action items cover your side of the
  conversation only."* Previously it carried nothing, and a one-sided
  transcript looked exactly like a meeting in which one person talked.

A recording set up without system audio on purpose — an in-room
meeting, or no speaker selected — is not a failure and raises none of
this. Neither is a quiet far end: "system audio stopped" still waits 45
seconds, because participants who aren't talking are normal.

The tests drive the real start → status → stop path with only the sound
card simulated. The status payload the app's own tests use was captured
from the real endpoint, and the backend suite checks on every run that
the endpoint still sends that shape.

## Still being looked at

- **The summary isn't told that only one side was recorded.** The red
  note on the session is the signal for now; the summary and action
  items themselves are still written as if the transcript were the whole
  meeting.
- **Voice fingerprinting silently unavailable** on one install because
  the installed speech library no longer matches what the app expects.
  That is an environment mismatch rather than a code defect, and it
  needs a proper startup check rather than a log line nobody reads.
