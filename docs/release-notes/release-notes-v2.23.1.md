# v2.23.1 — one caller stays one speaker, and the recording indicator stops lying

## Install (macOS)

> v2.23.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.23.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.23.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.23.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## One person no longer becomes nine speakers

v2.21.0 added live speaker separation, and on a real two-person call it
labelled a single caller SPEAKER 1, 2, 3, 4, 5, 6, 7 and 9. That was a
regression introduced by that release, and this fixes it.

The cause was one number doing two jobs. A single similarity threshold
of `0.75` decided both *"is this the same person as before?"* and
*"should I create a brand new speaker?"* — they were the same decision,
just the `if` and the `else`. Live audio arrives as short clips carved
out by voice-activity detection, and the same voice scores below `0.75`
all the time on those: a two-word interjection, a turn away from the
mic, a cough over the first syllable. Every one of those dips fell into
the `else` branch and minted a new speaker.

Those two questions now have their own thresholds, because getting them
wrong costs very different amounts. Merging two people is a small,
legible error. Splitting one person nine ways destroys the transcript.
So matching is now permissive (`0.55`), while **creating** a speaker has
to clear a much higher bar: the clip must be at least 2.5 seconds long
*and* score below `0.40` against everyone already known. A short clip
can never create a speaker, no matter how odd it looks. There is also a
slight bias toward whoever spoke last, which is the right prior in a
conversation.

One further change that isn't obvious but mattered more than the
thresholds: when a clip is short or ambiguous, it's now assigned to the
best-matching speaker **without** folding it into that speaker's voice
profile. Learning from samples we weren't confident about was dragging
profiles toward the wrong voice — a single interjection from the second
speaker contaminated the first speaker's profile enough that the second
person could never earn a label of their own.

## Names are only shown when they're actually certain

The live transcript labelled a woman speaking as **CALEB JOHNSON**.

Live labels were reusing the same `0.75` match threshold as offline
processing, which is tuned for long, clean, whole-meeting audio — far
too loose for a two-second live clip. A named guess is worse than no
guess: `SPEAKER 2` is obviously provisional and you read it as such,
but a real name reads as a fact, and it ends up in summaries and
follow-up emails.

Live name matching now requires `0.88` similarity, enforced both when
querying the profile store and re-checked on the result. Below that bar
you get a neutral `SPEAKER N` label instead. You will see fewer names
during a call; the ones you do see will be right. Offline processing
after the meeting is unchanged and still does the thorough job.

## Turning it off

**Settings → "Separate speakers in the live transcript"** now switches
the whole feature off. With it off the live transcript groups everything
from the far end as "them", exactly as it did before v2.21.0, and skips
loading the speaker-embedding model entirely.

## The "Recording…" indicator can no longer get stuck

Pressing Stop while the backend was reconnecting left the sidebar
counting upward — "Recording… 20:10" — while the Record tab at the same
moment showed the idle **Start Recording** form. Two parts of the same
window disagreeing about whether your meeting was being captured.

Four separate things each polled the server on their own schedule and
each kept a private copy of the answer: the sidebar badge, the Record
view, the "Right Now" card on Today, and a flag inside the app shell
that suppresses the close-while-recording warning. Nothing ever
reconciled them, so any request that failed — like a Stop pressed
during a restart — left one copy stale permanently.

There is now a single poller that every view subscribes to, and each
successful poll pushes the server's answer down into the app shell. The
indicator is derived from the server's state rather than remembered
locally, so a failed Stop self-corrects on the next poll instead of
persisting until restart. When the backend is unreachable the last known
status is held rather than cleared, so a genuine in-progress recording
never disappears from the UI just because a poll failed.

## Still open

The **truncated-audio** report — a recording that ran the full meeting
but only captured the first few minutes — is **not** fixed in this
release. It's being diagnosed from capture logs rather than guessed at,
and shipping a speculative fix for it would risk making recording worse
rather than better. The `AUDIO_INTEGRITY` and `SYNC_INTEGRITY`
measurements already recorded on every session are what that diagnosis
is being built from.

Meanwhile, the live transcript is a reliable capture canary: while text
keeps appearing, audio is reaching the recorder. If it freezes for more
than a minute while people are still talking, stop and restart the
recording.
