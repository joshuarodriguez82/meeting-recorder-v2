# v2.49.0 — the app said echo cancellation was running when it wasn't

## Install (macOS)

> v2.49.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.49.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.49.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.49.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.11.0**.

## Echo cancellation was off, and off is what was happening

A session showed:

> Finalizing (running for 14s) — echo cancellation can take several
> minutes.

Echo cancellation was switched off. The event log for that same session
recorded `aec_requested: false`, and the setting was being honoured
correctly — the finalize step that ran was the WAV merge, which takes
seconds.

**The banner named echo cancellation unconditionally.** It said that
sentence on every finalize, regardless of the setting, because the text
was not behind any check.

Two costs, and the second is the one that matters. It read as a setting
being ignored. And "several minutes" is true of echo cancellation and
false of a merge that takes seconds — so a perfectly normal 12-second
finalize looked like something stuck.

Describing work that is not happening is the same defect as reporting a
result that was never established. It just points the other way.

The banner now says what is running: *"merging the audio tracks;
usually a few seconds"* when echo cancellation is off, and names echo
cancellation only when it is genuinely part of that finalize.

## The message describes the finalize you are waiting on

The server-side message was already conditional — but it checked the
**current setting** rather than what that finalize started with. Toggle
echo cancellation while one is running and the message describes the
*next* run instead of the one you are watching.

Whether echo cancellation is part of a finalize is now recorded on the
session before it starts, and every message reads that. The live
setting remains the fallback for sessions recorded before the field
existed.

The button tooltip in the session dialog dropped the claim entirely
rather than branching: it is a terse hover on a disabled button, and
what the user needs there is that it will work shortly, not which step
is running. Saying nothing is honest; naming a step that may not be
running is not.

## Tests

1281 backend tests, up from 1276.

**Only one of the four message tests fails against the shipped build**,
because the backend text was already conditional — that one covers the
toggle-mid-finalize case found while fixing this.

The defect actually reported lived entirely in the React banner, and
this project has no frontend test harness, which would have left the
one reported bug as the one uncovered thing. So it gets a **source-level
guard**: a test that reads `sessions-view.tsx` and asserts the echo
cancellation claim sits inside a check on `finalize_aec_requested`.
Crude, and verified to fail against the shipped file. Same pattern as
the extension suite's check that a registered content-script file
exists — a cheap guard beats an untested behaviour a later edit can
silently revert.

## Session index

Two index tests caught the new field being added to the session summary
but not to the index's projection, which would have made the cached and
freshly-scanned paths disagree. The field is now in both, and the index
schema version is bumped so existing rows rebuild once — without that,
an untouched session file never re-parses and would serve a summary
missing the field forever, defaulting the banner to "not running"
regardless of the truth.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, personal-data 0.
