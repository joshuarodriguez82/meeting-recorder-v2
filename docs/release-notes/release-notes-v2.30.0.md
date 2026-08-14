# v2.30.0 — the calendar scan finally reaches your meetings

## Install (macOS)

> v2.30.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.30.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.30.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.30.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension too

This release needs extension **1.3.2**. Settings → Templates &
Integrations → **Install / Update extension files**, then in
`chrome://extensions`:

- If your extension card is already loaded from
  `%LOCALAPPDATA%\MeetingRecorder\chrome-extension`, click **Reload**.
- If it is loaded from anywhere else — a Downloads folder, an old
  unzip — click **Remove**, then **Load unpacked** and pick that path.
  Reloading the old card silently re-reads the old folder and leaves
  you on the old version while the app reports the new one.

Confirm the card reads **1.3.2** afterwards.

## The calendar scan was stopping 30 levels short of your meetings

Extension-only mode captured a handful of meetings at best, and
usually one. Every previous explanation in v2.28.0 was wrong.

The meetings were never the problem. On a real week view, Outlook Web
publishes **28 perfectly-formed meeting labels**, like:

```
Homeserve, 8:30 AM to 9:00 AM, Friday, August 14, 2026,
Microsoft Teams Meeting, By Mark Lefky, Busy
```

Every one of those matches the extension's own time-range parser.
Subject, start, end, date — all there, all correct. The parser never
saw them.

The scan walked the page tree by hand and gave up at 30 levels of
nesting. Outlook Web renders its calendar tiles deeper than that. So
the walk collected 255 shallow wrappers — navigation chrome, toolbars,
the mini month picker — and stopped one level above every meeting on
the page. Instrumenting the old walk against a synthetic page with a
meeting at depth 35 shows it plainly:

```
elementsWalked: 31, maxDepthReached: 31 → 0 candidates found
```

Candidate discovery no longer hand-walks the tree. It uses the
browser's own selector engine, which has no depth limit, and hand-walks
only where a selector genuinely cannot reach: open shadow roots and
same-origin iframes. Meetings at depth 35 and 50 are now found; both
cases are covered by tests that fail against the old walk.

### Why this took several releases to find

The failure reported itself as `found 255 candidates, none had a
parseable time`. That message was a catch-all — it fired whenever no
other case matched, and it named the wrong cause. It sent three rounds
of investigation at the time parser, which was working correctly the
whole time.

A zero result now distinguishes no candidates, all all-day, none
meeting-shaped, and all date-unresolved, and prints the full breakdown
when the causes are mixed. A message that confidently names the wrong
cause is worse than one that admits it doesn't know.

The diagnostic also now runs the real scan alongside its own
independent probe and reports both counts. The bug lived exactly in the
gap between how the probe looked for meetings and how the real scan
did; that gap is now visible in the report itself.

## The 30-minute auto-refresh had never once run

v2.28.0 added an independent calendar refresh so capture didn't depend
on clicking Capture & Send. Field logs show it never delivered
anything: every import carried the four text blobs only a manual
capture sends.

Two early-return branches in the alarm handler — credentials not
configured, and a duplicate-run skip — returned without recording
anything. A fired alarm hitting either left no trace, indistinguishable
from an alarm that never fired at all. Every branch now records what
happened, including on an unexpected error, so a silent failure can't
look like silence again.

## You can tell which capture path produced your meetings

Two paths can populate the calendar: the extension's structured DOM
capture, and a fallback that reconstructs meetings from captured text
with a language model. They produced identical output and neither was
logged, so a degraded fallback was indistinguishable from a healthy
capture — which is how the underlying scan failure stayed hidden.

Each import now logs which path ran, how many events arrived, how many
survived, and why any were dropped. The Record tab says which one the
meetings on file came from.

## Also in this release

**Open Actions and Decisions are clickable.** On a client, the stat
cards open a drill-down grouped by meeting, respecting the active
project filter, and clicking a group jumps to that meeting. The card
count and the panel now read the same code, so they can't disagree.

**Set a client's Knowledge Folder to its Designated Folder.** A
checkbox on the Knowledge Folder card fills in the path; you still
press save, so there's one save path and one reindex trigger.

**Search can scope to a client.** The Search tab gained the
client/project scope the Ask tab already had, applied in both semantic
and full-text mode.

**Knowledge Folder documents look like documents in search.** Document
hits returned no `session_id`, so an indexed SOW rendered as a row
titled "Untitled" that opened nothing. They now show the document name,
a Document badge, the owning client, and the file path.

**Speaker name variants can be grouped.** Several spellings of the same
person can be merged into one.

**OS-drawn dropdowns follow the app theme.** The Client and Project
pickers on the Record tab are native `<datalist>` popups painted by the
browser, so on a machine set to dark they rendered as black panels
inside a light app.
