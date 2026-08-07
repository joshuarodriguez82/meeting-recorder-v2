# v2.20.0 — every session you ever recorded, and your documents become searchable

> **What's new:**
>
> 1. **Sessions recorded in other folders are visible again.** The app
>    scanned exactly one folder, non-recursively. Anything recorded
>    while your recordings folder pointed somewhere else — or filed into
>    a subfolder — was on disk and permanently invisible.
> 2. **One library across two machines.** A new synced archive folder
>    lets a Mac and a PC share every session, while each still records
>    to its own local disk.
> 3. **Knowledge Folders.** Point a client at a folder of SOWs,
>    discovery notes, or requirements docs and they become searchable
>    and feed Q&A answers — alongside your transcripts.

## Install (macOS)

> v2.20.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.20.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.20.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.20.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Your missing sessions

A user reported seeing 12 sessions in the app. They had 73 on disk — 61
of them sitting in an old folder the app had simply never looked at.

Nothing was lost, and nothing had gone wrong with the recordings. The
scan was one line:

```python
for path in self._recordings_dir.glob("session_*.json"):
```

`glob`, not `rglob` — **one folder, no subfolders**. A session was
visible only if it sat directly in whatever your recordings folder
points at *right now*. Change that setting at any point in the app's
life and everything recorded before it silently drops out of view.
Nothing migrated it. Nothing mentioned it.

That also explains missing clients: the Clients list is built from the
sessions the app can see, so 61 meetings' worth of client tags were
invisible right along with them.

Scanning is now recursive and spans multiple folders. If you've
overridden your recordings folder, the built-in default location is
searched automatically — no configuration needed. `ARCHIVE_RECORDINGS_DIRS`
in `config.env` (semicolon-separated) covers a history spread wider than
that.

The same session appearing in two folders collapses to one row, keeping
whichever copy was written most recently — so an old library and a
current one can't double up.

**Three follow-on bugs went with it:**

- **Clicking an archived session would have failed.** The list found
  sessions across every folder, but opening one still looked in the
  primary folder only. It now resolves across all of them, using the
  same newest-wins rule as the list — otherwise the list could show one
  version and the click open another.
- **Deleting a session left phantom search results.** Delete removed
  the `.json`, `.wav` and `.log` but orphaned the embeddings file, so
  cross-meeting Q&A kept answering from a deleted meeting with
  citations that went nowhere. All sidecars are now removed, from every
  folder, and the in-memory index is dropped unconditionally — the old
  code only invalidated it *if* it found a file to delete, which by then
  had already been removed.
- **The archive sweep missed subfolders**, so nested sessions would
  never reach your other machine.

## One library across a Mac and a PC

Recording to a cloud-synced folder is not the answer and never was — a
Drive copy stalling mid-write is what wedged the backend, tripped the
watchdog, and cost recordings in July. (An old code comment recommended
exactly that. It's now corrected; following it is how one library ended
up stranded across two machines.)

So the three jobs are split across three places:

| Folder | Holds | Written by |
|---|---|---|
| Recordings folder (local) | audio + everything, authoritative | the record path, and only the record path |
| **Session archive (synced)** | **session files only — the roaming library** | **the background worker** |
| Designated / mirror folders | `.txt` transcripts and summaries for people | the background worker |

Set `SESSION_ARCHIVE_DIR` to a synced folder (iCloud, OneDrive, Drive)
and each machine keeps recording to its own local disk while both see
one merged library. The archive write is a few-kilobyte file on a
background thread with retries — the same shape as the Designated
Folder export that's run safely since v2.19. **Audio is never copied
there**; that's what stalled in July, and your other machine doesn't
need it to show, search, or answer questions about a meeting.

Conflicts resolve by recency in both directions: a session you just
processed here beats a stale copy in the archive, and one processed on
the other machine beats a local stub — so a half-finished session can
never hide a completed summary.

## Knowledge Folders — your documents, searchable

Until now the knowledge base was transcripts and nothing else. Every SOW,
discovery note, CFDD and requirements doc you own was invisible to
search, to Q&A, and to prep briefs.

Each client now has a **Knowledge Folder** card beneath its Designated
Folder. Point it at a folder of documents and they're read, split into
passages, and indexed with the **same local model** your transcripts
already use — so one search covers both.

- **Formats:** PDF, Word (`.docx`), plain text, and Markdown.
- **Ask across both.** "What did we commit to on latency?" can now
  answer from the SOW *and* the call where you discussed it. Documents
  cite as `[DOC: Zorg-SOW.docx]`; meeting citations stay clickable as
  before.
- **Incremental.** Only new or modified files are re-read. Delete a
  document and it drops out of the index on the next run.
- **Still completely local.** The indexing model runs on your machine.
  Nothing is uploaded, and this adds no API cost.

**Anything it can't read, it tells you about.** Drop in a `.pptx` and
it's listed as skipped with the reason, rather than quietly ignored.
Same if a PDF is encrypted, or a needed library isn't installed — you
get the reason and the fix. Three separate bugs this week came from the
app treating "couldn't read it" as "there's nothing there," so nothing
gets to fail quietly anymore.

**Getting started:** Clients → pick a client → **Knowledge Folder** →
Browse → pick the folder. Indexing starts on save and shows a document
and passage count when it finishes.

> Knowledge folders are checked but never created. Unlike an export
> target, this points at documents you already have — creating a folder
> on a typo would hide the mistake.

## Under the hood

- New `document_service` — extraction, chunking and indexing, with
  optional dependencies imported lazily so a missing library is a
  reported skip and never a crash.
- Documents are indexed into the same vector space as transcripts via a
  shared embedding path, so results are directly comparable.
- `PUT /clients/config` now merges rather than rebuilds: each card sends
  only the field it owns, so saving one folder can no longer wipe the
  other.
- New `GET /sessions/diagnostics` reports which folders were scanned,
  how many session files each holds, and what was skipped with reasons.
- 160 backend tests (18 new), including one asserting that literal
  `/sessions/*` routes are registered before the catch-all — a new
  endpoint was briefly unreachable behind it, and the route-table check
  can't catch that on its own.
