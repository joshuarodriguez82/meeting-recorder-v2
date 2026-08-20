# v2.50.0 — the 1.11 regression, and the store rule that amplified it

## Install (macOS)

> v2.50.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.50.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.50.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.50.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.12.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.12.0.

## What 1.11 broke, precisely

Extension 1.10 found join links by waiting for a clicked event's pane
to render its **text**, then reading it. That worked — Webex and Zoom
links appeared.

1.11 added two more "the pane has rendered" signals so Teams panes
(mostly a button, little text) would count: **any new anchor anywhere
on the page**, and **any change in the page-wide count of name-shaped
labels**. On a real calendar those are the wrong questions to ask.
Outlook Web is a live application whose DOM churns constantly, clicked
or not — so the check fired on the first 150ms poll, extraction ran
against a pane that had not loaded, and every meeting came back empty.
The links 1.10 had been finding disappeared.

**Then the store made it worse.** A capture whose events are fine but
whose detail is empty still won the merge against the previously
enriched stored events. One capture with a broken detail pass erased
every join link the user already had. "No links at all now" was this
exact mechanism.

None of the 130 extension tests caught it, because every fake page in
the suite was silent until clicked. The two new regression tests model
the churn, and both fail against the shipped 1.11.

## The fix, in two rules

**A render signal must be specific to what we came for.** Not any
anchor — a new **join-shaped** anchor. Not a label-count change — a
name-shaped label that was **not present before the click**, compared
by membership, so a page re-rendering the same labels moves nothing.

**A signal starts extraction only after the page settles.** Once
signalled, polling continues until the text length holds still for two
consecutive polls, so extraction reads the loaded pane rather than its
first painted fragment.

Two collateral fixes in the same pass:

- **Attendees subtract the pre-click labels.** The page-wide name scan
  also matches interface chrome ("New event", "Next week") and, when a
  pane fails to close, the previous meeting's invitees. Anything
  visible before the click is not an invitee of this meeting; set
  subtraction removes all of it in one move, and a regression test
  pins that one pane's attendees cannot bleed into the next meeting.
- **The body is a real diff or nothing.** The old fallback stored the
  entire calendar's visible text as the meeting "agenda" when no diff
  anchor was found. URL and email scans still search the whole page —
  that is what found the pasted Webex links — but the body field is
  held to a genuine diff, because there "the whole page" is worse than
  nothing.

## The store: enrichment ratchets up, never down

The grid labels a capture starts from never carry attendees, a body,
or a Teams join link — detail is best-effort, per capture. So an empty
detail field on a fresh event means "not fetched this time", not "the
invite has none", and deleting stored data because of it is
indefensible.

A fresh event that **carries** a detail field still wins — an invite
really can change its link, and the capture that saw it is newer truth.
A fresh event whose detail field is **empty** now inherits the stored
value. The carry-forward is keyed on the exact meeting, and a test pins
that one meeting's join link can never migrate to a different meeting —
sending you into the wrong call is worse than no link.

This closes the amplifier permanently: even if a future extension
regression empties the detail pass again, it can no longer destroy
what previous captures earned.

## Also in this release

The v2.49.0 finalize-message fix (merged immediately before this): the
Sessions banner no longer claims echo cancellation is running when the
setting is off — it says "merging the audio tracks; usually a few
seconds", which is what is actually happening.

## Tests

1284 backend tests, up from 1281, and 132 extension tests, up from 130.

Every new test was verified to fail against the code it guards:

- **Churn**: an unrelated anchor appears the instant the click lands;
  the pane's Webex URL arrives 500ms later. 1.11 extracted at the first
  poll and missed it. The test also asserts the whole-page text never
  becomes the meeting body.
- **Bleed**: Escape fails to close pane A; meeting B must not inherit
  A's invitees.
- **Ratchet**: a bare capture cannot erase stored detail; fresh detail
  still beats stored detail; detail never jumps between different
  meetings.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
