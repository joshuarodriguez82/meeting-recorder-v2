# v2.29.0 — install the extension from the app, drill into your actions, and search your documents

## Install (macOS)

> v2.29.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.29.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.29.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.29.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The Chrome extension installs from inside the app

Getting the extension used to mean finding it outside the app, in a
folder you had to go hunting for. It now ships **inside** the app.

Settings → Templates & Integrations has an **Install / Update extension
files** button. It writes the extension to one stable location —
`%LOCALAPPDATA%\MeetingRecorder\chrome-extension` — with a copy button
for the path. Point Chrome's "Load unpacked" at it once; from then on
updating is click Install, then hit reload on the extension card. The
path never changes, so Chrome keeps the same extension ID and your
settings survive.

Writes go to a temporary folder first and swap in only on complete
success, so a failed install can never leave you with a half-written
extension.

### The app can now tell you your extension is out of date

The extension reports its own version on every capture. The Settings
card shows the version bundled with the app next to the version that
last checked in, and warns in amber when yours is behind. It
distinguishes "never posted", "version unknown", "update available" and
"up to date" rather than collapsing them into one ambiguous state.

This is the check that would have caught a stale v1.1.0 running against
a v1.2.0 build without anyone having to verify it by hand.

## Calendar capture gets a diagnostic that reads the actual page

Extension-only calendar capture was still returning **0 events** for
some tenants after v2.28.0. Rather than guess at causes, the extension
now carries its own diagnostic.

Settings → **Diagnose calendar capture** opens your calendar, samples it
at 2s, 5s, 10s and 15s, and reports what it actually found: the final
URL, how many grid/application containers exist, counts for each
candidate selector, the total aria-label count with the 25 longest
labels verbatim, which time patterns were tried and how many each
matched, iframe and shadow-root counts, and whether the calendar grid is
itself inside an iframe. Output is JSON with a Copy button — no DevTools,
no pasted console script.

Alongside it, capture is hardened against the causes this most likely
is:

- **Iframes and shadow DOM.** The scan walks into same-origin iframes
  and open shadow roots instead of stopping at the top document. A
  cross-origin frame it can't read is skipped rather than throwing.
- **Times that live somewhere other than the label.** A meeting's start
  and end can now be read from a `<time>` descendant, a `datetime` or
  `data-*` attribute, an adjacent sibling, or an enclosing gridcell or
  column header. Two `<time>` elements or a `data-start`/`data-end` pair
  resolve as structured data and skip text parsing entirely.
- **Scraping before the page finished rendering.** Capture now re-scans
  until the candidate count stops changing, instead of scanning once
  after a fixed wait.

### Zero now tells you *which* zero

"Calendar: 0 events" was one message for at least five different
situations. It now distinguishes: no candidate elements found; the page
never stopped changing; found N candidates but none had a parseable
time; found N candidates but all were all-day; and found candidates with
times but no resolvable date.

That distinction is the difference between "the extension is broken" and
"your calendar genuinely has nothing in that window" — and it points at
which one to fix.

**Honest limitation:** none of the live-page behavior above has been run
against a real Outlook Web tenant — there's no way to sign into one from
a build environment. That is precisely why the diagnostic exists. The
next Diagnose-and-Copy is what turns this from *hardened against the
likely causes* into *verified against the actual page*.

## Open Actions and Decisions are clickable

On the Clients tab, the Open Actions and Decisions stat cards were
numbers you could read but not use. They're now drill-downs — click one
and the items appear grouped by meeting, with a jump straight into the
meeting they came from. The panel respects whichever project chip is
active, so you can narrow to a single workstream and see just its open
items.

Meetings and Hours stay non-interactive, and a card showing zero isn't
clickable — there's nothing behind it.

The counts and the list are now computed by the same code. Previously
the number on the card came from one regex and nothing else read it;
having the panel derive from a separate pass would have let the badge
and the list disagree. They can't now.

## Search understands your Knowledge Folder

Two problems, both in the Search tab.

**Your documents were showing up as broken rows.** Semantic search
returns meetings and Knowledge Folder documents from the same index, but
document hits carry no session — no ID, no title, no date. The Search
tab rendered every hit as if it were a meeting, so an indexed SOW
appeared as a row titled **"Untitled"** with no date that went nowhere
when clicked. Your documents were being found and matched correctly the
whole time; they just couldn't be recognised or opened.

Documents now render as documents: the file name, a Document badge, the
owning client, and the matched passage. The file path is shown for
locating it yourself — deliberately not a click-to-open, because the
only existing endpoint that opens an arbitrary path *creates* it if it's
missing, which on a stale index would silently make junk folders on your
disk.

This is the same failure this app has hit repeatedly: **something you
couldn't fully read must never render as something that isn't there.**

**Search couldn't be scoped to a client.** The Ask tab has had a
client/project scope selector for a while; Search never passed one, so it
always searched everything. It now has the same selector, applied in both
semantic and full-text mode — a filter that silently works in one mode
and not the other is worse than no filter. Full-text mode states plainly
that it searches sessions only, since it has no access to document
chunks.

### Where to search what

- **Ask** — questions answered from meetings *and* Knowledge Folder
  documents, scoped to a client.
- **Search** — find the passage itself, across meetings and documents,
  now scoped the same way.

## Knowledge Folder: reuse the Designated Folder in one click

If a client already has a Designated Folder, the Knowledge Folder card
offers a **Same as Designated Folder** checkbox that fills the path in
for you. It stages the change the same way typing does — you still press
save — so there's exactly one save path and one reindex trigger rather
than a second hidden one.

The checkbox reflects the current path rather than storing its own
state, so it can't drift out of agreement with the field next to it.

## Dropdowns follow the app's theme

The Client and Project pickers on the Record tab are `<datalist>`
popups, which the browser paints itself rather than from the app's
styles — as do `<select>` menus, date pickers and scrollbars. With no
`color-scheme` declared, the webview fell back to the OS setting, so a
Windows machine set to dark rendered those popups as black panels inside
an otherwise light app. They now follow the app's theme.

## Grouping people who are the same person

Speaker names arrive in whatever form the source used — "Josh",
"[scrubbed]", "[scrubbed]" — and each variant counted as a
separate person across a client's meetings. Variants can now be grouped
so one person reads as one person.

## What costs what

Since it comes up: **indexing your Knowledge Folders costs nothing.**
Embeddings are produced by a model that runs locally on your machine, so
re-indexing is free no matter how many documents you point it at. Asking
a question costs one model call — a fraction of a cent at current
Haiku 4.5 pricing, on the order of a couple of hundred questions per
dollar.
