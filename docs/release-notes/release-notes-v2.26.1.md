# v2.26.1 — three things that told you the wrong story

## Install (macOS)

> v2.26.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.26.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.26.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.26.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

Three fixes, all the same underlying mistake: **stale state rendered as
current state.**

## The sidebar spinner kept spinning after processing finished

The spinner ran for a long time after work was done — sometimes
indefinitely.

The backend stores the last status message it emitted, and never clears
it. The sidebar was treating *"a status message exists"* as *"work is in
progress."* So `"Processing complete."` — a message that announces
completion — kept the spinner turning until something else happened to
overwrite it.

The backend log confirmed it: during one of these spells the process was
serving nothing but routine status polls. No transcription, no
speaker identification, no processing of any kind.

The fix isn't to pattern-match the word "complete", which would break the
moment a new message is added. The backend now reports whether pipeline
work is genuinely running, and the spinner follows that. You'll still see
"Processing complete." as text — it just stops spinning, and clears
itself a few seconds later.

Two details worth recording:

- It's a **counter**, not a flag, because overlapping work is real here,
  not hypothetical: a background auto-process and a manual re-process of
  a different session can run at once. A flag would let whichever
  finished first switch the spinner off while the other was still going.
- Every increment is released in a `finally`. If an exception could leave
  the counter stuck, this exact bug would come back in a form that's
  harder to see — so there's a test that processing which *raises* still
  releases it.

## "The backend crashed at least once" nagged forever

The warning in Settings was driven purely by `crash.log` existing. That
file is append-only and never deleted, so once the backend had ever
crashed, the banner was permanent — still warning about crashes that were
diagnosed and fixed.

It now reports **when**, not merely **ever**. Recent crashes still warn;
older history shows as a neutral note with the date. "Show crash log" and
"Copy crash log" remain available either way, and the log itself is never
deleted or truncated — the history is what made the crash diagnosable in
the first place.

One subtlety: `crash.log` gets a header line on *every* backend start,
crash or not, so counting headers would have reported a crash every time
the app launched. Only a header followed by an actual fault dump counts.

## The Settings bars overlaid the content behind them

In Settings, the tab bar at the top and the Save bar at the bottom let
page content show through and around them.

Two causes. The bars are sticky inside a scrolling area that has its own
top and bottom padding, and a sticky element can't be positioned outside
its container — so the Save bar parked 64px above the true bottom edge
and the tab bar 24px below the true top, leaving bands where content kept
scrolling in full view. On top of that, both bars were 95% opaque with a
blur, so content was faintly visible *through* them as well.

Both bars now reach the real edges of the scrolling area and are fully
opaque. The Save bar also gained a stacking order it never had, which
could have let a card paint over the Save button.

Every other scrolling panel in the app was checked for the same pattern.
The detail panels in Follow-Ups, Commitments and Decisions use a
different, correct arrangement and were left alone.
