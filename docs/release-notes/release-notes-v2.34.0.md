# v2.34.0 — auto-record works for extension-sourced meetings

## Install (macOS)

> v2.34.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.34.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.34.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.34.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## Auto-record could not see your calendar

If your meetings come from the Chrome extension rather than a local
Outlook profile, every row in Upcoming Meetings said **"Manual only"** —
even with the Auto-record toggle on. The toggle worked. It simply had
nothing to watch.

Auto-record polled the **local** calendar for its start trigger. The
Upcoming Meetings panel, by contrast, already merged local and
extension sources. Two components held different views of which
meetings exist: one displayed a meeting, the other never saw it.

Both now read the same merged, source-aware feed. Extension-sourced
meetings auto-record like any other, and there's a test that asserts
the wiring by identity, so a future change that quietly points one of
them back at a single source fails the build rather than shipping.

### All-day events still say "Manual only"

That one is deliberate. Out-of-office blocks, travel and birthdays are
excluded from auto-start regardless of where they came from. The label
now appears only where auto-record genuinely can't act, rather than for
a whole category of real meetings.

### Two tooltips were lying

The auto-record toggle claimed a Teams or Zoom link was required. It
isn't, and the code never checked for one. Corrected, along with the
"From Outlook Web" badge.

## Three things that would have broken this quietly

**A single odd meeting could have killed the feature outright.**
Comparing a timezone-aware timestamp against a naive one raises an
error inside the polling loop, which the loop treats as "this tick
failed" and moves on. One such meeting on your calendar would have
stopped auto-record firing for *every* meeting, permanently, with no
visible symptom. Every timestamp is now normalized before comparison,
and anything that still looks wrong is skipped individually with a log
line rather than taking the loop down.

**Stopping a recording would have immediately restarted it.** The
"already handled" ledger matched meetings on an exact subject-and-start
key. Extension events are re-imported wholesale on every capture and
their start times move by a minute or two, so the same meeting reads as
a brand-new one shortly after. Matching now tolerates that drift.

**Your per-meeting opt-out would have evaporated.** Extension events
carry no stable identifier, and Outlook Web relabels a changed invite
as "Updated! Weekly Sync" — so a blocked meeting would come back
unblocked after any edit. Opt-outs now key on a canonical form of the
subject that survives those relabels. Existing blocks keep working
unchanged.

## Tests

883 backend tests, up from 859. The 24 new ones were mutation-checked
rather than assumed: disabling the extension feed fails 9 of them,
removing timestamp normalization fails the mixed-timezone test, exact
key matching fails the drift test, and dropping canonical matching
fails both opt-out tests. Two existing tests were strengthened to
assert the local calendar was never *called*, since the shared feed now
degrades a failing local source to "extension only" and a silent
failure could otherwise pass.

## Known, unchanged

The "meeting starting soon" notification is still local-calendar only —
the same class of gap, in a different component, deliberately left for
its own change.

Under `calendar_source="outlook"` the extension feed is still merged in
for both display and auto-record, which doesn't match how that setting
is documented. Display and trigger agree with each other, which is the
property that matters here; changing it would remove rows from the
panel, so it's flagged rather than altered.
