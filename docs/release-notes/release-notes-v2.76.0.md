# v2.76.0 — templates for account managers, and documents you didn't know were missing

## Install (macOS)

> v2.76.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.76.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.76.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.76.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Seven templates for account managers

Pick these on the Record tab or in a session's detail view, the same way
as any other template.

**Qualification Call** · **Executive Briefing** · **Solution Demo** ·
**Pricing & Commercial** · **Account Review / QBR** ·
**Competitive Displacement** · **Sales-to-Delivery Handoff**

The template decides what survives. Run a pricing call through a
technical template and you get a faithful record of the architecture and
no trace of the discount that was offered out loud.

Two are worth knowing about specifically:

- **Pricing & Commercial** records every figure with what it covers and
  whether it was firm or indicative, and ties each concession to the
  condition it was offered under. In that meeting a sentence can become
  a contractual position.
- **Sales-to-Delivery Handoff** exists to surface the gap between what
  was promised verbally during the sale and what the signed scope
  actually covers. That gap is where delivery escalations come from, and
  it is invisible to everyone except the person who sat in both rooms.

**Qualification Call is not Requirements Gathering.** The second is
about what a system must do; the first is about whether there is a deal
— budget authority, compelling event, who else is being looked at. They
are deliberately separate.

## Four live co-pilot lenses for sales calls

Set your co-pilot mode to **Sales** once, then pick the meeting type per
call: **Qualification**, **Pricing / Negotiation**, **Executive
Briefing**, **Renewal / Account Review**.

The pricing one is worth trying first. It watches for a concession
offered with no condition attached, an indicative number being repeated
back as firm, and negotiating against yourself before the other side has
countered.

## "0 documents" when the folder was full

If your clients showed **0 indexed documents**, your SOWs and proposals
were invisible to search and to your AI assistant — and nothing in the
app said so.

Here is what was happening. Documents are indexed when you **set** a
client's Knowledge Folder, and not again. Anything you add to that
folder afterwards is not indexed until someone clicks Reindex. That part
is deliberate: reading and embedding a folder is slow and usually
touches a network drive.

What was wrong is that the app only ever reported how many documents
were *indexed*, never how many were *there*. So a folder with 47
documents and an empty index looked exactly like an empty folder.

Now the Clients tab says it plainly: **"47 documents in this folder,
none indexed — they are invisible to search and to your AI assistant
until you click Reindex."** A genuinely empty folder says so instead,
and a folder with newer files shows how many arrived since the last run.
Your AI assistant gets the same distinction, so it stops telling you a
client has no documents when the folder is full.

**Worth doing after you upgrade:** open the Clients tab and look for
amber. Anything flagged needs one click of Reindex.

## The tour goes further

The first-run walkthrough used to stop at "record your first meeting".
It now continues through choosing a template, tagging the client — which
is what turns a pile of recordings into an account history — and
connecting an AI assistant.

Reopen it any time from the Help tab.

## Documentation

The in-app guide listed five templates when there are eighteen, and
never mentioned AI assistant access at all. Both fixed, plus a new guide
for account managers at `docs/account-managers.md`.
