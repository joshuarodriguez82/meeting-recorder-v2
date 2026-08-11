# v2.21.1 — the live transcript reads like a conversation again

> v2.21.0 made the live transcript fast. It also chopped every sentence
> onto its own line. This puts each speaker's turn back together.

## Install (macOS)

> v2.21.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.21.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.21.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.21.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## One block per turn

Transcribing on speech boundaries means a lot of small pieces arrive.
Shown one per line, someone telling a thirty-second story became ten
stamped rows of sentence fragments — quick, but hard to read back.

Consecutive lines from the same speaker are now joined into a single
block, with their name and the time they *started* talking shown once
at the top. That start time is the one you want when scrubbing back to
find a moment.

Someone who holds the floor for a long time still gets broken into
paragraphs — a pause of more than twenty seconds starts a new block, so
a five-minute monologue doesn't become one unreadable wall.

Nothing about the recording changed. This is only how the live preview
is laid out; the transcript saved when you press Stop, and every
timestamp in it, is exactly as before.
