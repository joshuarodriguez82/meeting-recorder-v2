# v2.47.0 — three bugs that hid each other

## Install (macOS)

> v2.47.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.47.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.47.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.47.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.10.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.10.0.

## Why six releases did not fix attendees and agendas

Three separate defects, and each one concealed the next.

## 1. The diagnostics were recorded, then deleted 30ms later

v2.44.0 added counters that say which of four detail-capture failures
is happening, and v2.44.0's tests passed. In the field the counters
were always empty.

On every calendar POST the backend runs, in order:

1. `record_extension_version` — writes to the calendar store
2. `record_capture_diag` — writes the counters to the same store
3. `replace_all` — **rebuilt that file from scratch**, carrying forward
   only a hand-written list of keys

`last_capture_diag` was not on the list. So the counters were written
and destroyed, milliseconds apart, on the same request — every time.

The evidence sat in plain sight and read as the store working: a bundle
exported minutes after three captures said
`extension_capture_diag: {}` while `extension_last_seen_version` in the
**same file** said `1.9.0`. The version survived because it happened to
be on the list.

The v2.44.0 tests passed because they wrote and read the counters with
no `replace_all` between them. The one sequence production always runs
was the one sequence never tested.

**The fix is structural, not another entry on the list.** The store
write now inherits the existing file and overwrites only the keys it
owns, so a key it has never heard of survives by default. Preserving
something stale is a visible wrong value; deleting it is invisible, and
this module has now paid for that difference twice.

## 2. The screen-reading pass was looking at the wrong week

v2.45.0 added a second mechanism: click an event, read what renders.
It ran **after** both weeks were scanned — which is after the capture
has navigated to next week, with no navigation back.

That mechanism finds a meeting by its tile and only considers meetings
inside 72 hours: every one of which is a tile in the *current* week.
So it searched for tiles that were no longer on screen, matched
nothing, counted everything skipped, and returned having opened
nothing.

The timing said so plainly: a whole capture completed in about 21
seconds. A pass that actually opened ~20 events cannot finish in under
a minute. **The second mechanism had never once clicked an event** —
and defect 1 was deleting the counter that would have said so.

Detail is now collected per week, while that week is still rendered.
Both mechanisms run there, cheapest first, so the click pass only pays
for what the response reader could not already answer.

## 3. The store could never let go of a stale meeting

A guard stops a bad capture from wiping good data: if a capture returns
fewer events than the store already holds, merge instead of replace.
Correct, and on its own it had no way out.

Meetings move, get cancelled, get declined — so an *honest* capture
legitimately returns fewer events than an accumulated store. Once the
store held more than any correct capture returns, every capture was
"fewer", every capture merged, and nothing ever left. The same warning
appeared three times in one afternoon: *returned fewer events (36) than
the store already holds (43)*.

On screen that was two copies of one recurring training block, both
marked **LIVE**. The series instance had been moved — an "Exception to
recurring event" shifting it by half an hour — so the capture carried
the new time while the merge kept resurrecting the old one.

A prior event is now dropped when the fresh capture holds the same
subject, on the same day, at a different time. That is a reschedule,
and the capture is the newer truth.

**Deliberately narrow.** A prior event on a day the capture said
nothing about is still protected — that is the partial-capture case the
guard exists for. And the merge-versus-replace decision still weighs
the *original* count: judging it on the filtered list would let two
dropped ghosts make a partial capture look complete and delete real
meetings on days it never covered. A test caught exactly that during
development, so the decision and the filtering were separated.

## Tests

1276 backend tests, up from 1269, and 123 extension tests, up from 118.

Every new test was verified to **fail** against the shipped code and
pass against the fix. Given that three releases shipped with tests that
passed while the feature did nothing, a test that cannot fail is worse
than no test — it reads as coverage.

- The store tests drive the **real production call order**
  (`record_extension_version` → `record_capture_diag` → `replace_all`),
  which is what the previous tests skipped, plus a key the store has
  never heard of surviving a write.
- The extension tests cover the click pass being handed the right
  events, an event beyond the window not being opened, one already
  answered by the response reader not being re-opened, a throwing
  clicker being non-fatal — and the **ordering itself**, since running
  the pass after the week navigation is the whole bug.
- The store tests cover a moved meeting leaving no ghost, a day the
  capture never covered staying protected, an unchanged meeting not
  being mistaken for a move, and one day's move not disturbing the same
  series on another day.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 3 / 0 new, personal-data 0.

## What to check

Expand a **Teams** meeting on the Record tab. Zoom has worked since
v2.41.0 and proves nothing new; Teams is the case that has failed every
time.

Captures now take up to about 90 seconds instead of ~20, because the
click pass finally does something. It runs in a background tab and will
not disturb your own Outlook window.

If it is still empty, the diagnostics bundle will now actually carry
`extension_capture_diag` — and `domDetailOpened`, `domDetailNoTile` and
`responsesSeen` separate the remaining possibilities without another
round of guessing.
