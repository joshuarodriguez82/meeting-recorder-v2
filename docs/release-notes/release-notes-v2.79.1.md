# v2.79.1 — a failed meeting no longer shows a green checkmark

## Install (macOS)

> v2.79.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.79.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.79.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.79.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## What this fixes

**v2.79.0 could show a completed meeting that had actually failed.**

Yesterday's release added the activity panel, with a red state for a
run that stops part-way. The panel was built correctly. Nothing ever
told it a run had failed.

So when transcription or speaker identification fell over — a bad audio
file, a GPU that ran out of memory, a cloud folder that vanished
mid-read — the panel finished the run, ticked every stage green, and
said "Processing complete." The failure went to the log file and
nowhere else. Auto-processing after a recording stops was worse: it
retries, gives up quietly, and you were told nothing at all.

Now a failed run stops where it failed, marks that stage red, keeps the
later stages unstarted, and shows the reason.

This only affects what you were *told*. No meeting was lost by it — the
recording and the audio were always safe on disk. But you may have been
told a meeting processed when it did not, so if a summary or transcript
looks empty or missing for a meeting from the last day, re-process it.

## Under the hood

The irony was not lost on us. v2.79.0 exists because work that did not
happen was rendering as work that did, and it reintroduced exactly that
one layer up. There are now seven tests on the failure paths, four of
which fail against yesterday's build.
