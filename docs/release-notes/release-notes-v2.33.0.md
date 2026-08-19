# v2.33.0 — the app can now explain itself

## Install (macOS)

> v2.33.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.33.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.33.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.33.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## Export diagnostics in one click

Settings now has an **Export diagnostics** button that produces a single
file you can attach to a bug report: the event log, recent log tails,
crash history, versions, hardware and audio devices, and settings with
every secret removed.

It shows you exactly what the archive contains — before you export and
again afterwards, read back out of the finished file so the listing
can't drift from what was actually written.

Redaction works by **allow-list**: the export starts from nothing and
adds only fields explicitly marked safe, so a setting added in a future
release is excluded by default rather than leaked because someone
forgot to add it to a block-list. The names of withheld fields are
listed, so nothing is hidden from you — only values. There's a test
that invents two new credentials and asserts they never appear.

No transcripts, no audio, no meeting titles, no attendee names.

## The log no longer grows without limit

`backend.log` had no rotation at all and had reached **231 MB** — every
session the machine has ever run, concatenated. Diagnosing a problem
meant scanning a fraction of it and hoping the relevant lines were in
range. During one investigation they weren't, which produced a
confidently wrong conclusion.

It's now capped at 16 MB live plus four backups. 16 MB is chosen so the
live file can be read in a single pass.

**This was harder than it looks**, and worth recording. The log isn't
written by the Python backend at all — the desktop shell opens the file
and hands the handle to the backend as its output stream. Ordinary
rotation renames the file, but an inherited handle follows the file
itself, not the name. On Windows that rename *succeeds* and then
silently redirects every subsequent line into the backup, leaving the
live log permanently empty while everything appears to work. Rotation
therefore copies and truncates in place, so the file identity survives
and writing continues into the right place.

## A structured event log

Alongside the human-readable log there's now `events.jsonl` — one
machine-readable record per meaningful outcome rather than prose to be
scraped. Recording stopped, finalize completed or failed, audio
integrity, channel attribution, calendar imports, document indexing,
backend start and stop, crash recovery.

It records **counts, durations and reason codes only** — never
transcript text, meeting titles, attendee names, file paths or secrets.
It's built to be handed to someone else. Where an error message would
have carried a file path containing your account name, the exception
*type* is recorded instead.

A test caught a real hole during development: the redaction pattern
originally permitted spaces, so a 48-character sentence passed straight
through — and so would most meeting titles. Spaces are now rejected
outright.

## More of your documents get indexed

A real Knowledge Folder skipped 19 files. Ten of those skips were
correct — images, archives and diagrams genuinely aren't text. Nine
were gaps, now closed:

**Spreadsheet-adjacent and config formats.** `.csv` and `.tsv` are read
row by row so rows stay coherent for retrieval, matching how `.xlsx`
was already handled — indexing spreadsheets but not CSVs was an
oversight, not a decision. Also added: `.yaml`, `.yml`, `.json`, `.xml`,
`.log` and common config formats.

**Legacy Word (`.doc`).** These were the valuable ones — pricing
models, a competitive battle card, an architecture document. The `.doc`
extension covers four unrelated file formats, and the library that
reads `.docx` can open none of them. The extractor now inspects the
file's actual signature and routes accordingly; three of the four
formats need no new dependency. Genuine Word 97-2003 files are parsed
through their real document structure rather than by scraping printable
characters, because plausible-looking garbage in a search index is
worse than an honest "unsupported".

That parser was validated against a **real** Word 97-2003 document
rather than one generated for the test — a self-made fixture only
proves the parser agrees with itself.

**`.drawio` remains unsupported.** Modern exports are compressed and
encoded, and distinguishing them from plain ones reliably needs a real
example we don't have. It didn't fall out cleanly, so it wasn't
guessed at.

**Re-index your Knowledge Folders after updating** to pick up
previously skipped files.

## Also in this release

The three internal cache-key hashes are now explicitly marked as
non-security, satisfying the new security scanners honestly. The values
are unchanged, so existing indexes, item ids and cached briefs stay
valid — switching hash algorithms would have silently invalidated all
of them.

## Tests

859 backend tests, up from 779. The 80 new ones cover rotation caps and
the inherited-handle case, event emission, the redaction allow-list
against invented credentials, archive contents, and every newly
supported document format.

## Not verified

Log rotation was designed specifically around Windows file-sharing
behaviour, and the test reproduces the real parent-opens/child-inherits
arrangement — but it ran on Linux only. No Windows machine exercised it.
