# v2.30.1 — the date was in the label the whole time

## Install (macOS)

> v2.30.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.30.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.30.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.30.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension too

Needs extension **1.3.3**. Settings → Templates & Integrations →
**Install / Update extension files**, then `chrome://extensions` →
**Reload** on the Meeting Recorder card. Confirm it reads 1.3.3.

## The scan found the meetings; the parser threw away their dates

v2.30.0 fixed the scan's depth limit, and it worked — candidate
discovery went from 0 to 133 on a real week view. The capture still
produced zero meetings, and the new honest diagnostics said exactly
why:

```
0 events (found 257 candidates, none produced an event
 (208 not meeting-shaped, 47 unresolved date/time, 2 all-day))
```

**47 unresolved date/time.** Not unreadable times — unresolved dates,
on labels that each carry a fully-qualified date in their own text:

```
Homeserve, 8:30 AM to 9:00 AM, Friday, August 14, 2026, ...
```

Outlook Web writes the date **after** the time range. The parser's
regex only ever captured a date atom immediately *preceding* a time
atom, so it read `8:30 AM` and `9:00 AM` perfectly and saw no date at
all. It then fell back to the calendar column's date — and Outlook
Web's week grid publishes no `role="columnheader"` elements to resolve
one from. Every meeting was discarded while holding its own date.

Date resolution now runs in four tiers: a date captured beside the
time, then a date anywhere else in the same label, then the column
date, then give up. The label's own date outranks any ancestor guess,
because the label describes exactly one event.

The search deliberately looks *after* the time range first. Searching
the whole label first would let a month name inside a subject
("August Planning Review, 9:00 AM to 10:00 AM, Monday, August 10,
2026") outrank the event's real date — there's a test for that.

### Why the test suite never caught this

Every existing parser fixture supplied a `columnDateIso`. Real Outlook
Web supplies none. The suite was green on 688 backend and 35 extension
tests while the field capture returned zero, because the fixtures
handed the parser the one input production never had.

The regression tests added here use the **verbatim labels from the
failing machine** with `columnDateIso: null`, and they fail against the
previous build:

```
not ok - FIELD: date after the time range resolves with NO column date
not ok - FIELD: a month name in the SUBJECT loses to the real date
```

A fixture that supplies what production can't is a test of the fixture.
Realistic labels alone weren't enough — the missing column date was the
part that mattered.

## Honest note

This is the fourth release aimed at this bug. The first three each
fixed something real — a truncated scan, a silent alarm, missing
logging — but none of them fixed the capture, and two were built on
causes asserted before they were measured. What changed is that
v2.30.0's diagnostics finally reported a specific, countable failure
(`47 unresolved date/time`) instead of a catch-all that named the wrong
cause. The fix follows from that number and from your own labels, and
it is pinned by tests that fail without it.
