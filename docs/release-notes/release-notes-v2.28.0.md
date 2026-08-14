# v2.28.0 — the Chrome extension actually captures your calendar, and your documents index

## Install (macOS)

> v2.28.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.28.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.28.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.28.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## The Chrome extension now captures your whole calendar

If you use **Calendar source: Chrome extension only** — the mode that
never contacts Outlook and therefore never triggers a sign-in prompt —
it was badly under-delivering. In the field it captured **one meeting out
of five**, and only ever for today.

Three separate faults, all fixed:

**It only looked at today.** The capture scraped Outlook Web's *day*
view, while the Upcoming Meetings panel asks for the next **7 days**. As
the sole source, it could never fill that window — and once today's
meetings passed, it had nothing. It now captures the current week and the
next.

**It was reading the calendar as text and guessing.** The capture
flattened the calendar to a block of text and had a language model
reconstruct meetings from it. That's why delimiters in your own meeting
titles broke it: a pipe in "AWS Sales**|** Active Project Status
Reviews", brackets in "McDonnell, Bob Jr. **[US-US]**", or a meeting
rendered across three lines each defeated the reconstruction — the
subject survived but the **start time didn't**, and a meeting with no
time can't be placed in a 7-day window.

Meetings are now read directly from the calendar's own accessibility
labels, which carry subject, start and end as structured data. A pipe or
a bracket in a title is now just a character in a string. On the real
meeting titles that were failing, capture went from **1 of 5 to 5 of 5**.

This approach also doesn't depend on Microsoft's markup, which varies
between tenants and changes without notice. It keys on the accessibility
information Outlook Web must publish for screen readers — a far more
stable contract than any CSS selector. If it ever does break, the
extension falls back to the old method **and says so in the popup**,
rather than quietly returning one meeting again.

**It ran once and never again.** The calendar scrape was bundled into the
heavier four-source briefing capture, which is gated behind an
auto-capture toggle that defaults to **off**. So it only ever ran when
you manually clicked Capture & Send. There's now an independent calendar
refresh every 30 minutes that doesn't depend on that toggle.

### A bad capture can no longer destroy a good one

If a capture finds *fewer* meetings than are already stored, it now
**merges** instead of replacing. A laptop that slept mid-scrape, or an
Outlook Web page that hadn't finished rendering, used to wipe a complete
store and leave the panel empty. Stale-but-complete beats fresh-but-empty.
A capture that finds the same number or more replaces normally, so
cancelled meetings still disappear promptly.

### An empty source no longer looks like an empty calendar

Extension-only mode with nothing captured used to show "No upcoming
meetings in the next 7 days" — indistinguishable from genuinely having
nothing on. It now says the extension hasn't delivered any meetings,
shows when it last captured and how many it found, and points at Capture
& Send.

## Your documents actually index now

Pointing a client Knowledge Folder at a real work directory skipped
**87 of 87 files**. Two causes:

**The app claimed support it didn't ship.** `.pdf` and `.docx` were
listed as supported and had working extractors — but the libraries they
need were never bundled, so every file was skipped with advice to run a
`pip install` command you can't meaningfully run inside a packaged app.
Both libraries now ship.

**Spreadsheets were skipped for no reason.** The library needed to read
`.xlsx` was *already installed* and used elsewhere in the app; there was
simply no extractor.

Now indexed: **`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.html`**, alongside
`.txt` and `.md`. Slide decks include speaker notes; spreadsheets are
read sheet by sheet with formula results rather than formula text.

**Re-index your Knowledge Folders after updating** — previously skipped
files aren't picked up retroactively.

### Skips read honestly now

Images and diagrams were reported as "unsupported file type", which reads
like a defect you should go fix. They're now reported as *not text
documents* — expected, not a problem — and kept separate from genuine
failures like a corrupt file.

The skip list is also grouped by reason with counts instead of printing
one line per file, with the full list behind a disclosure. Eighty-seven
lines of near-identical text isn't a report.

### The bug behind the bug

The list of supported file types was maintained by hand, separately from
the code that actually reads them — so "we support this" and "we can read
this" were free to drift apart, which is exactly what happened. The
supported list is now **derived** from the extractors themselves. That
particular mistake can't recur.

## Note on the Whisper model

If you've noticed a delay on startup: the model isn't being re-downloaded.
`large` resolves to a 2.9 GB file that takes about five seconds to load
into the GPU on each backend start. It felt constant because the backend
was restarting frequently before the crash fix in v2.25.1.

Unused model sizes (tiny, base, small, medium) stay cached after you
switch models, and can be deleted from the HuggingFace cache folder to
reclaim space — they re-download only if you switch back.
