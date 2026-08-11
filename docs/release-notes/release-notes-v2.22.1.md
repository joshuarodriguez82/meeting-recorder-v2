# v2.22.1 — polish pass on the new look

> Follow-up to v2.22.0, from real screenshots on a real screen.

## Install (macOS)

> v2.22.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.22.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.22.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.22.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## See at a glance how far a meeting got

Each session shows six stages — audio, transcript, summary, action
items, decisions, requirements — always in the same order and the same
place. Finished stages are solid; ones that haven't run are faded but
still there.

Before, only the finished ones appeared, so a meeting that stopped
halfway just looked like it had fewer buttons. Now it's obvious at a
glance which meetings completed and which didn't — and which step they
stopped at.

The cluster is quieter too: no more circular chips, so it reads as
progress rather than a row of buttons.

## Collapse the sidebar

A toggle at the bottom of the sidebar shrinks it to icons only, with
names on hover. Useful on a laptop screen. Your choice is remembered.

The recording controls stay usable when collapsed — Stop is still
there.

## Fixes from the redesign

**Empty boxes no longer look disabled.** The new pill inputs had no
visible edge, so an empty Meeting Name field looked switched off next
to the dropdowns. They now show a soft edge at rest, and genuinely
disabled fields still look clearly different.

**Lists no longer look cut off at the top.** The header was slightly
see-through, so scrolled cards showed through it and looked clipped.
It's solid now, with room at the bottom of the list and beside the
scrollbar.

**Delete is out of the way.** It appears when you hover a session
rather than sitting on every row — still reachable by keyboard.

**Tighter spacing.** Less empty space above the Today briefing, and the
Record page fits more without scrolling.

**Each tab starts at the top.** Scrolling down one tab no longer leaves
the next tab you open scrolled to the same spot — every view opens at
its beginning, as it should.

## Unchanged

Every action, tooltip and keyboard shortcut works as before. Dark mode
is untouched. No recordings, transcripts or settings are affected.
