# v2.56.0 — your next meeting gets served first, and the empty state tells the truth

## Install (macOS)

> v2.56.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.56.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.56.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.56.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.15.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.15.0.

## The 22:07 capture, finally readable — and what it said

The first capture with full manual-path diagnostics settled it. The
pipeline held end to end for the first time: **11 meetings gained
invite bodies and they stored.** Real progress — invisible, because the
meeting the user expands every time was not among the 11, and the
counters say exactly why: 23 meetings queued for clicking, 11 opened,
then **12 consecutive misses**.

Two structural causes, both fixed:

**The past week was eating the click budget.** The click list had an
upper time bound only, so every finished meeting still sitting in the
current week's view queued for detail nobody would ever read — ahead of
tomorrow morning's call. Finished meetings now drop out after a short
grace, and the list is ordered **soonest first**, so whatever budget or
breakage cuts a run short cuts it at the meetings that matter least.
The meeting you will expand next is, by definition, the soonest one —
it is now served first, not last.

**One stuck pane cascaded into misses for everything after it.** When
an event's pane fails to close, the grid leaves the accessibility tree
and every later tile "does not exist" — the exact 11-then-12 signature
in the field counters. A missing tile now triggers recovery: Escape,
settle, re-scan. The regression test uses a sticky pane that swallows
the first Escape, and the shipped code loses the second meeting to it;
the fix recovers it.

## The empty state stops lying

"(No description on this invite.)" was shown for every cause — pane
never opened, capture never ran, budget exhausted, and the one honest
case. For six releases those were indistinguishable, for you and for
diagnosis.

Every captured meeting now carries its click-pass outcome, and the
empty state names it:

- *No description on this invite (the capture opened it and found
  none)* — the honest zero.
- *Not captured yet — the last capture couldn't locate this meeting on
  the calendar grid* — retried next capture.
- *Not captured yet — the last capture ran out of time before reaching
  this meeting* — retried next capture.
- *No description captured yet — details fill in when the extension
  next captures this meeting* — nothing has reported for it yet.

The outcome field was added to the import door, the store and the UI in
the same commit — the constructor that silently deleted late-added
fields hid invite bodies for six releases, and this field refuses to
repeat that history (round-trip test included).

## Portal binding moved to the Clients tab

Binding a project to a portal opportunity is configuration, and
configuration lives with the client setup — not in the Engagements
view, which is where the register is read.
The full bind/re-bind/unbind controls now sit in **Clients**, next to
Rename Project, when a project is selected; Engagements keeps only the
**Sync to portal** action for an already-bound project.

## Tests

1300 backend tests, up from 1299, and 139 extension tests, up from 136.
All three new extension tests were verified to **fail against the
shipped build**: finished meetings excluded and soonest-first ordering,
the sticky-pane cascade recovery, and every wanted meeting resolving to
a named outcome.

Security scanning run against the baselines before merge: bandit 184
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
