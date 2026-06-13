# v2.7.0 — Engagements: per-client register + Excel export

Adds the **Engagements** layer for Solutions Architects: every meeting
for a client rolled up into one living register, exportable to a
hand-editable Excel workbook. Also fixes the from-clean-machine
installer so first launch works without a developer toolchain. All
v2.6.2 fixes are included.

## Install (macOS)

> v2.7.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## New: Engagements

- **Per-client (optionally per-project) register.** A new
  **Engagements** sidebar tab rolls every meeting's requirements,
  decisions, action items, and open questions into one deduped view,
  with provenance (which meetings each came from, when last seen) and
  a "from notes" marker for items that came from your session notes
  rather than the transcript.
- **One-click Export to Excel.** Writes a workbook to the client's
  export folder (or the recordings folder) with an Overview sheet, a
  sheet per record type, and a **Changes since last export** sheet.
- **Built to be hand-edited.** Every record sheet has two human-owned
  columns — **Status** and **Notes** — that are never overwritten.
  Re-export after the next meeting and it regenerates the same file in
  place, carrying your edits forward (matched by a durable record id,
  with a text fallback so a re-extraction can't orphan them). Items no
  longer detected aren't deleted — they stay, flagged "carried over",
  with your edits intact.
- **Conflict-safe.** If the workbook is open in Excel or mid-sync, the
  re-export writes a dated copy and warns you instead of destroying
  hand-entered Status/Notes.
- Under the hood, processing a session now also produces structured
  records (the engagement layer reads these). Sessions recorded by an
  older build show up once reprocessed.

## Fixed since v2.6.2

- **Clean-machine install failed on first launch.** The first-run
  Python bootstrap targeted Python 3.13; the pinned ML stack has no
  3.13 wheels, so without a Rust toolchain installed it tried (and
  failed) to compile `tokenizers` from source. The bootstrap now uses
  **Python 3.12** (3.13 still works as a fallback), so a fresh install
  on a normal machine just works.
- **Calendar auto-record never fired for normal meetings.** It only
  auto-started meetings whose join URL appeared in the calendar
  Location/Subject, but Outlook puts the Teams/Zoom link in the
  meeting *body* (omitted from the fast calendar scan), so standard
  invites were silently skipped. Auto-record now starts **every timed
  meeting** at its start time (all-day events and blocklisted meetings
  excluded); the auto-stop watchdog ends it per your Settings. Requires
  the auto-record toggle on (opt-in, unchanged).

## Everything from v2.6.2

API-key persistence fix (no more spurious 401s), correct Claude Haiku
3.5 model id, OneDrive/iCloud cloud-placeholder handling, live
OpenRouter free-model roster, Android APK build fix, and screenshot
handling for text-only models.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
- The Engagements register only includes sessions that have structured
  records. New recordings get them automatically; reprocess older
  sessions (or use the per-session backfill) to pull them in.
