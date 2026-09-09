# v2.81.1 — Meetings sync to the Designated Folder the moment they finish processing

## Install (macOS)

> v2.81.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.81.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.81.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.81.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## Processing a meeting now queues its export

**A meeting that finished processing did not tell the exporter
anything.** Every artifact was produced — transcript, summary, action
items, decisions, requirements — the session was saved, and nothing
asked for it to be copied to the Designated Folder. The files got there
eventually, when a periodic reconciliation sweep next noticed the gap.
That is the difference between synced *now* and synced *within a couple
of minutes*, and "within a couple of minutes" is what was being seen.

The export is queued the instant processing completes, on the same path
a normal recording takes: stop → auto-process → export.

### How this happened

Five places asked for an export — tagging a meeting to a client, tagging
a back catalogue, the individual transcribe endpoint, each individual
extractor, and the individual summarize endpoint. The one-shot pipeline
that runs after a recording stops was not among them.

That pipeline exists because five separate extractors each doing *load →
set one field → save the whole session* clobbered one another; the last
writer won and silently nulled the rest. Consolidating them into one
pass with one save fixed that — and the export request that lived on
each of the five individual endpoints did not come along.

### Reprocessing re-queues too

Reprocessing a meeting whose inputs have not changed skips the language
model calls, because regenerating byte-identical text is pure cost.
Skipping those calls used to skip the export as well.

But unchanged artifacts are not the same as *delivered* artifacts. An
export that exhausted its retries against an offline folder, or a client
tag added after the fact, both leave a meeting owing files it already
has — and reprocessing is exactly what someone does when a meeting looks
wrong in the folder. That path now queues the export too. It rewrites a
handful of small text files and never the audio, so asking twice costs
nothing.

### The safety net stays

Reconciliation still runs, still sweeps recent meetings, and still
catches anything the fast path misses — a folder that was offline, a
copy that failed all three retries, an app that was closed mid-export.
It is the backstop. It was never supposed to be the delivery mechanism.

### Tests

Seven tests now cover this, and they fail against the previous build —
the fast-path enqueue, that it happens exactly once rather than once per
artifact, that it happens *after* the save (the exporter re-reads the
session from disk, so queueing earlier would copy the pre-save
contents), that a run producing no transcript and a run that failed
outright are both left alone, that a queueing problem cannot turn a
completed meeting into a failed one, and that a skipped reprocess still
queues.
