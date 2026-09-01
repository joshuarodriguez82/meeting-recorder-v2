# v2.79.0 — meetings reach your folders on their own, and the status bar tells you the truth

## Install (macOS)

> v2.79.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.79.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.79.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.79.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## A meeting can no longer go missing from its folder

This is the one to install for.

If a copy to your Designated Folder failed — a cloud mount briefly busy,
a sync client mid-refresh — the app retried three times over about two
and a half minutes and then **gave up permanently**. Nothing tried
again. The meeting sat on your local disk, complete, and never reached
the folder until you happened to press Sync now or restart the app.
Someone recorded a meeting and found it still missing two hours later.

The app now re-checks recent meetings against their folders **every two
minutes** and re-queues anything missing. The normal path still copies
within seconds of processing finishing; this is the ceiling on how long
a failure can go unnoticed. Two minutes, not forever.

**If you have a meeting stuck right now**, it will be picked up within
two minutes of launching this build. You don't need to do anything.

## The status bar is a real activity panel now

The old one showed this to a user:

> Transcription completeIdentifying s…

Two labels welded into one word and then cut off mid-sentence. That was
not a display glitch — the app genuinely built that string, and the
sidebar had one truncated line to show it in.

Both halves are fixed, and the second half is the interesting one. The
old strip could tell you *something is happening*. It could not tell you
which step, how many were left, whether it worked, or what happened
while you were looking at another tab — because each message simply
overwrote the last.

Now:

- **The current step is named** and wraps instead of truncating.
- **A progress bar and "Step 2 of 3"**, so "is it nearly done?" has an
  answer.
- **Click it** for the full stage list — transcribing, identifying
  speakers, assigning speakers — each showing done, running, or failed.
- **A history**, so work that finished while you were elsewhere is
  still there to read, with times.
- **A failure looks like a failure.** A run that stopped stays stopped
  on screen instead of quietly resolving into a checkmark.

## Rename and delete projects

v2.78.0 shipped the ability to rename, merge and delete clients, but the
project half only existed in the backend. It's on screen now, in the
same place and the same shape as the client one — including "Merge" on
the button when renaming onto a project that already exists.

There were also **two different Rename buttons** on the Clients tab. The
older one only retagged meetings and left the client's folder settings
and indexed documents behind under the old name, which is the exact
stranded state the merge feature exists to repair. It's gone; there is
one rename now, on the complete implementation.

## Under the hood

Automatic indexing now stands aside while exports are pending. The two
were reading and writing the same cloud-mounted folder — the Knowledge
Folder card offers "Same as Designated Folder" as a one-click option, so
on a common setup they are literally the same directory. Deferring an
index sweep costs nothing; delaying a meeting you're waiting for does.

That deference expires after thirty minutes, so one export that can
never succeed can't switch indexing off for good.
