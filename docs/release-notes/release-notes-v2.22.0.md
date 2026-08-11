# v2.22.0 — a cleaner, lighter interface

> A visual refresh. Nothing about how the app works has changed — same
> features, same shortcuts, same data.

## Install (macOS)

> v2.22.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.22.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.22.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.22.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's different

**Depth instead of lines.** The app sits on a soft gray backdrop, and
content floats on white cards with rounded corners and a gentle shadow.
Sections are separated by space and elevation rather than hairline
rules.

**A calmer sidebar.** Larger icons, more room between items, and the
current page marked by a soft rounded highlight instead of a bar
running to the edges. Settings and the Usage Guide are now grouped as
support links at the bottom.

**Softer controls.** Buttons have rounded squircle corners. Search and
filter boxes are full pills with a light gray fill instead of white
boxes with dark outlines.

**Sessions read as a list of things.** Each meeting is its own card
with room to breathe, rather than rows packed against dividers. The
title leads; dates, durations and tags step back into muted gray. Every
action button and tooltip is unchanged — just spaced out.

**Warnings became badges.** The yellow and blue notices are now soft
rounded pills instead of full-width bordered boxes. Deliberately still
obvious — those notices are what tell you a folder couldn't be read or
a recording's audio drifted, and quiet is not the same as invisible.

**New typeface.** Body text is now Figtree, a rounder, friendlier sans
serif. Timestamps and IDs stay in the monospaced face so columns line
up.

## Dark mode

Fully supported, as before. Every colour was defined for both themes
together.

## Nothing else changed

No features added or removed. Your recordings, transcripts, clients and
settings are untouched. If something looks wrong rather than merely
different, that's a bug worth reporting — visual work benefits from
real eyes on real screens.
