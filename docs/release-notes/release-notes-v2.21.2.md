# v2.21.2 — paragraphs break on a real pause, not a long silence

> Follow-up to v2.21.1. Same idea, better tuned. **Install this instead
> of v2.21.1** — it supersedes it.

## Install (macOS)

> v2.21.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.21.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.21.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.21.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## A new line when someone finishes a thought

v2.21.1 joined a speaker's lines into one block, but only started a new
block after twenty seconds of silence — long enough that separate
points ran together into one slab.

A pause of **three seconds** now starts a new line. That's someone
finishing a thought before moving to the next, rather than drawing
breath mid-sentence. Everything shorter stays joined, so a flowing
sentence stays whole.

## Still as fast as they're talking

Worth being explicit, because it's the thing worth protecting: grouping
costs nothing in speed.

Each piece of text still appears the moment it's transcribed — about a
second after it's said. It simply flows into the block already on
screen instead of starting a new stamped row. You watch the paragraph
grow as the person speaks.

The speaker's name and the time they started talking stay pinned at the
top of the block.

## Nothing else changed

This is only how the live preview is laid out. The recording, the
transcript saved when you press Stop, and every timestamp in it are
exactly as before.

> If three seconds still feels wrong once you've read a few real
> transcripts — too chopped, or too run-together — it's a one-line
> change. The number that matters is worth getting right by using it,
> not by guessing.
