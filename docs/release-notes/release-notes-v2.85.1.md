# v2.85.1 — Imported meetings show up on top, and aren't processed twice

## Install (macOS)

> v2.85.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.85.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.85.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.85.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## A meeting you just imported is on top of the list

An imported meeting is dated by when it happened, not when you
imported it — so a newest-first list could put it days or weeks down,
where it looked like the import never arrived. A meeting imported in
the last day now stays at the top of **Sessions** with a **Just
imported** label, still showing its real meeting date.

## No second run while one is going

Right after an import (or a recording), processing runs in the
background, but the meeting window still offered **Process** — and
pressing it transcribed the whole meeting a second time. The window
now shows **Processing in the background…** and refreshes itself when
it finishes; the Sessions list shows a **Processing…** chip for the
same time; and a second run for the same meeting is refused.

