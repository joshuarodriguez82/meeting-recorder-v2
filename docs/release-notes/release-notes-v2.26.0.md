# v2.26.0 — screenshots you can see, echo cancellation, and much faster session loading

## Install (macOS)

> v2.26.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.26.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.26.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.26.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The crash appears to be fixed

v2.25.1 moved the audio device inspection out of the backend process.
Since installing it, a full recorded meeting — including the Stop, and
sitting on the Record tab afterwards — completed with **no backend
restart at all**. Every one of those steps previously killed it.

The evidence is unambiguous: every backend start appends a
`faulthandler enabled` line to `crash.log`, and no new line appeared
across the entire session. Same process throughout.

One meeting is not one month, but that is the exact scenario that used to
reproduce it every time.

## See your screenshots as you take them

Screenshots taken during a recording now appear as **thumbnails in the
recording panel**, immediately, from both the manual button and the
automatic timer. Click one to view it larger. The destination folder is
shown right there with a copy button, so it's no longer a hunt through
the folder structure afterwards.

### A data-loss bug this uncovered

Screenshots were only written into the session's record when you pressed
**Stop**. So every time the backend crashed mid-meeting — which it has
been doing — the screenshots taken during that meeting were **orphaned**:
the image files were written to disk, but nothing connected them to the
meeting they belonged to.

Screenshots are now saved to the session the moment they're captured.
The write is best-effort and can never interrupt a recording.

**Screenshots from previously crashed sessions are still on disk**, in
`MeetingRecordings\screenshots\session_<id>\`. They're recoverable by
hand, and a tool to relink them automatically can be added on request.

## Echo cancellation for speaker users (experimental, off by default)

If you record with speakers rather than a headset, unmuting lets the
far-end voices come back out of your speakers and into your microphone.
They then get transcribed a second time and attributed to **you** —
producing duplicate lines under your name and confusing speaker
identification, since your microphone channel contains someone else's
voice.

The recorder captures your mic and the system audio as two separate
tracks, which means the system-audio track is an exact reference for what
your speakers played. That's precisely what an echo canceller needs. The
canceller now runs during finalization, before the two tracks are mixed —
offline, where there's no real-time pressure and no risk to the audio
thread.

On the test fixture it removes about **18.5 dB** of echo — roughly 85%
quieter — while your own voice comes through essentially intact.

**It is off by default.** Turn it on in **Settings → Echo cancellation**
and compare one meeting against a recent one. If your own transcript
lines stop echoing the caller, it's working.

Safety, because this touches recorded audio:

- The cleaned signal is written to a **separate temporary file**. Your
  original microphone recording is never overwritten.
- The result is checked before use, and **rejected** if it looks wrong —
  invalid numbers, wrong length, no measurable improvement, or an
  implausibly large reduction that suggests it removed real speech.
  Every check defaults to rejecting.
- Any failure falls back to the original microphone audio, untouched.
- Output length is forced to match exactly what the normal path would
  produce.

The most important test isn't whether it removes echo — it's whether
**your own voice survives**. A canceller that cleaned up the echo by also
deleting you would be worse than the problem.

## Session loading is roughly 8× faster

Every session was stored as a JSON file, and listing sessions re-read and
re-parsed **every one of them on every request**. With a large library
that's slow, and it was doing it repeatedly across several threads at
once.

Session metadata is now kept in a local SQLite index. On a 200-session
library, listing dropped from about 40 ms to under 5 ms per call.

**The JSON files remain the only source of truth.** The index is a
disposable cache built from them: delete it and it rebuilds, and if it's
ever missing, corrupt, or unreadable, the app falls back to scanning the
files directly — slower, but always correct.

The behaviours protecting your library are preserved and tested,
including the one that has bitten repeatedly: **a session file that can't
be read is reported as unreadable, never silently dropped.** A cloud file
that hasn't downloaded yet must never look like a session that doesn't
exist. The strongest test asserts the indexed results are *identical* to
a direct scan of the same folders.

If sessions ever look wrong, **Settings → session index** turns it off
and restores the old direct-scan behaviour immediately.

## Dependency scanning in CI

`pip-audit`, `npm audit` and `cargo audit` now run on pull requests,
pushes, and weekly. Non-blocking by design and not wired into the release
pipeline — the pinned ML dependencies carry advisories that can't be
acted on without a migration project, and a gate that's permanently red
gets ignored.
