# v2.82.0 — Merge a speaker the diarizer split in two, and sync the moment processing finishes

## Install (macOS)

> v2.82.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## One person, two speakers

**The diarizer hands out a second label for the same participant partway
through a meeting** — an echo, a headset swap, someone unmuting into a
different audio path. Because naming happens per label, the half that
matched a saved profile gets the name and the other half stays
`SPEAKER_03`. The transcript then reads as a named person talking to a
stranger who is the same person:

```
[12:04 → 12:09]  Jane Doe:    ...one of the first things
[12:09 → 12:14]  SPEAKER_03:  and then after that you'd
[12:14 → 12:20]  Jane Doe:    determine where it goes
```

And there was no way to fix it. Settings → Known Speakers has a merge,
but that merges entries in the **global** roster — it never touched a
session's transcript, so merging there left the meeting reading exactly
as before.

### Merge them yourself

In a meeting's **Speakers** tab, tick two or more speakers and choose
**Merge into one person**. Every segment is rewritten, the extra label
disappears, and the merged speaker keeps whichever half had a real name
— in either direction, so it does not matter which one you tick first.

The export is re-queued at the same time, so the copy in the Designated
Folder stops disagreeing with the app.

The summary, action items and decisions are **not** regenerated
automatically. They are language-model output, they cost money, and a
merge is usually one of several corrections made in a sitting.
Reprocess when the names are right.

### It fixes the obvious ones for you

Two cases need no judgement, and now happen during processing without
asking:

- both labels matched the **same known-speaker profile**
- both labels ended up with the **same name**

In both, the app had already decided they were one person and then
rendered them as two.

### It asks about the rest

Two labels that merely *sound* alike get a suggestion at the top of the
Speakers tab — "Jane Doe and SPEAKER_03 sound like the same person ·
91% voice match" — with **Merge them** and **Different people**.

That stays a question on purpose. Failing to merge one person leaves an
ugly transcript whose words are still on the right voice. Merging two
people puts one person's words in another's mouth, silently, and
downstream into the summary, the action items and the commitments. A
similarity score is good enough to ask about and nowhere near good
enough to rewrite a transcript on.

Two things the automatic pass deliberately will not do: chain merges
(A and B sounding alike, and B and C, does not make A and C one person),
and merge **you** without being asked — your own segments are identified
by which device captured them, which is stronger evidence than a voice
match.

A speaker who spoke for under 1.5 seconds never gets a voice
fingerprint, so no suggestion can be made about them. The panel says so
rather than leaving an empty list to read as "checked, all fine".

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
