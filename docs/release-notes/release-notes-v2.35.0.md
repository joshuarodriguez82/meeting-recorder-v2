# v2.35.0 — one Knowledge Base tab, and prep briefs that read your documents

## Install (macOS)

> v2.35.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.35.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.35.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.35.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## Search and Ask are one tab: Knowledge Base

They were always two doors into the same index. Now there's one input.

**Typing searches, and searching is free.** Results refresh as you
type, in either keyword or semantic mode, with no language-model call
and no cost.

**Answering is something you ask for.** An answer costs about half a
cent and takes a few seconds, so it never fires on its own. One button
beside the input, or ⌘/Ctrl+Enter. When your query reads like a
question, a strip appears offering to answer it — offering, not doing.

That distinction is enforced rather than intended: the answer path has
exactly one entry point, reachable from those three gestures and
nothing else. Automated checks drive the merged view in a real browser
and **count the network calls** — typing a full question produces zero,
clicking Answer produces exactly one, and cancelling mid-stream leaves
it at one.

Everything from both tabs survives: keyword and semantic modes, client
and project scoping applied to search and answer alike, documents
rendering as documents rather than as meetings that open nothing,
streaming answers you can stop, and citations that jump to the moment
in the call.

Keyword search also got faster — it now matches against data the view
already has, so there's no network round-trip per keystroke. Searching
inside full transcript bodies is a toggle, also offered as one click
from the empty state.

The name is the app's own word for the corpus, and it matches how the
rest of the sidebar is named — Sessions, Decisions, Commitments, all
named for what they hold rather than what you do to them.

## Prep briefs read your Knowledge Folder

A brief for an upcoming client meeting used to read only your past
calls with that client. It ignored every document you'd indexed for
them — SOWs, requirements, notes. For one client that meant ignoring
114 documents.

Briefs now draw on both, and keep them clearly apart. A document
records what was **contracted or written down**; a meeting records what
was **said**. The brief cites each accordingly, never attributes one to
the other, and says so explicitly when they disagree — which is exactly
the thing you want to know before walking in.

Documents appear as amber file chips; meeting citations stay clickable
and jump to the moment. Both the modal and the brief view list which
documents were used.

**How it picks what to include.** Rather than one blended query, it
searches up to three times — the meeting subject and project, the
invite body with joining boilerplate stripped, and any context you
typed — then interleaves the results so each signal is represented. A
600-character agenda concatenated onto a 40-character subject produces
a query dominated by the agenda; a short, authoritative note of yours
would average away to nothing.

Attendee names are deliberately **not** part of the search. Personal
names matched against contract prose retrieve RACI tables, approver
lists and signature blocks — the parts of a SOW that mention people
rather than the scope, dates and obligations a brief needs. The
attendees are still given to the model directly.

The budget fills breadth-first: one excerpt from each relevant document
before any document gets a second. With only a few slots available,
three different documents tell you more about an account than two
paragraphs of one.

**Cost, measured rather than estimated:** about **$0.002 per brief**,
roughly doubling the prompt. Your past calls keep their full existing
allowance — documents get their own budget beside it, not a share of
theirs.

**Two things fall out of this.** A client with indexed documents but no
recorded calls yet now gets a real brief instead of "no prior meetings
available". And when no client can be determined, the brief
deliberately pulls **no** documents at all — another account's SOW
appearing in your brief would be worse than having none.

If a client has no Knowledge Folder, an empty index, or a disconnected
drive, the brief is exactly what it was before. Not degraded, not an
error, and not a brief that announces "no documents found" — there's a
test asserting the word "document" appears nowhere in that case.

## Known gap, not fixed here

Automatic pre-meeting briefs don't resolve which client a meeting
belongs to — that logic currently lives only in the interface. So an
automatic brief already fell back to recent calls across all clients,
and therefore pulls no documents either. This predates the change and
sits in the calendar code being worked on separately. Briefs you open
yourself are fully client-scoped and do read documents.

## Tests

922 backend tests, up from 883. Notable among the 39 new ones: a guard
that reads the document-result field names directly out of the search
service, so renaming one there fails here rather than silently
retrieving nothing; and a test asserting the no-documents prompt is
byte-identical to the previous version.
