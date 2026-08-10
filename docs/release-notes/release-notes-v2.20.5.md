# v2.20.5 — the Sessions tab tells you why your count is what it is

> If sessions are missing, this release shows you which folder is short
> and why — on screen, without running anything.

## Install (macOS)

> v2.20.5 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.5_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.5_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.5_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## You can finally see what the app sees

The Sessions tab now opens with a line like:

> **74 session files found across 3 folders · 74 shown**

If those two numbers disagree — or a folder couldn't be read, or files
were skipped — it expands into a panel that names **every folder it
looked in**, how many sessions each holds, which ones failed, and the
exact reason anything was skipped.

There's a **Copy details** button that puts the whole thing on your
clipboard, so reporting a problem is one paste instead of a scavenger
hunt.

If zero sessions are showing, the panel opens automatically. An empty
Sessions tab looks like lost data; a list of the folders that were
searched, with counts, does not.

## Why this exists

The app has been able to produce this exact report since v2.20.0. It
was reachable only through an API endpoint that nothing in the
interface called — so when a user had 74 sessions on disk and saw 24,
the only way to find out why was to run diagnostic scripts by hand.

That is the same mistake as shipping the Session Archive setting with
no field to type it into. The information existed; there was no way to
look at it.

## Under the hood

- `scan_report()` now owns the "how many are visible" number instead of
  the endpoint recomputing it, so what's on screen can't drift from what
  the scan actually used.
- Every scanned folder carries an explicit reachable/unreachable flag —
  not only the ones that failed.
- 240 backend tests (2 new).
