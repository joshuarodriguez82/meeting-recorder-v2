# v2.30.3 — "the audio file is missing" was the app talking about a file it was still writing

## Install (macOS)

> v2.30.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.30.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.30.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.30.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## The incident

A recording was stopped at 10:51:10. Thirty-six seconds later, pressing
**Process** produced:

> Internal Server Error: The audio file for this recording is missing —
> it may have been moved, deleted, or not yet synced down from the cloud.

Moved, deleted, not synced from the cloud. All three were false. The
file simply **did not exist yet** — echo cancellation was still writing
it, and finished at 10:54:22, 192 seconds after Stop. The recording was
complete and intact the whole time.

Nothing in the app said to wait. The audio player read `0:00 / 0:00`,
every AI action button was enabled, and the only feedback on pressing
one was a red error announcing data loss that hadn't happened.

## Finalize is now a state the app can see

Sessions carry an explicit finalize state — running (with a start
time), succeeded, or failed with a reason — written to disk *before*
the finalize subprocess starts, so anything reading the session
mid-finalize sees it.

Every endpoint that opens the audio file — process, summarize, action
items, decisions, requirements, follow-up drafts, and playback — now
answers three different questions differently:

- **Still finalizing** → "This recording is still being finalized
  (running for 2m 14s). Echo cancellation is enabled, which adds a few
  minutes. This is normal — no data has been lost." Not an error, and
  no longer logged as one.
- **Finalize failed** → says so, with the recorded reason.
- **Genuinely missing, nothing in flight** → the original message, now
  reserved for when it's actually true.

## The UI waits with you

While a recording is finalizing, the session shows a processing state
with elapsed time and a note that echo cancellation takes a few
minutes. The AI action buttons are disabled with a tooltip explaining
why, instead of inviting a click that can only fail. The Sessions list
shows a matching indicator, and the view picks up completion on its own
— no manual refresh, no guessing whether it's done.

You can still navigate away and come back. Nothing blocks.

## A crashed finalize no longer strands a session

If the backend is killed mid-finalize, a session would previously be
left marked "finalizing" forever. The startup recovery scan now
resolves those: cleared when the audio is recovered or already
complete, marked failed with a reason when the raw capture genuinely
can't be salvaged. A second sweep covers the narrow race where finalize
finished and cleaned up its temp files but died before recording that
on disk — leaving no orphan behind to find.

## The bug behind the bug

This is the fourth time this codebase has shipped the same mistake:
**a thing you couldn't read rendering as a thing that isn't there.**

- Knowledge Folder documents rendering as "Untitled" meetings
- Extension posts that carried no version rendering as "never posted"
- AEC decisions written to a stream nobody captured, reading as no
  decision
- And now a file still being written, reading as a file that was
  deleted

Each time, the code had exactly one branch where it needed three:
present, absent, and *not yet knowable*. The fix in each case is the
same shape — make the third state explicit and say it out loud.

## Note on the earlier warnings

The same field log shows `AUDIO_INTEGRITY: deficit=14%` with
`mic_gap=196.6s` against `finalize done in 191.9s` — the "missing"
audio again equalling finalize time almost exactly. That is the
measurement bug fixed in **v2.30.2**, independently confirmed for a
third time here. If you are updating from v2.30.1, both fixes land
together.

## Tests

725 backend tests, up from 704. The 21 new ones cover: process during
finalize returns 409 rather than 500, process after a failed finalize
reports the failure, genuinely-missing audio still reports missing, and
a session left "finalizing" by a killed backend is recovered on startup
rather than stuck.
