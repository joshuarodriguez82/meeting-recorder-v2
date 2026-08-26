# v2.68.0 — stop paying five times for the same transcript

## Install (macOS)

> v2.68.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.68.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.68.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.68.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

This release is app-only. The Chrome extension stays at **1.23.0** —
nothing to reinstall, nothing to reload, no permission prompt.

## What the bill actually said

A month of API usage, read from the token export:

| | |
| --- | --- |
| Input tokens | 5,545,790 |
| Output tokens | 502,602 |
| **Cache reads** | **0** |
| **Cache writes** | **0** |

Zero on both cache lines is the whole story. Every session sent its
full transcript **five separate times** — summary, action items,
decisions, requirements, structured records — and paid full price for
all five. On top of that, the three reprocessing days ran 4–8× a normal
day, and nearly all of that was regenerating text that was already
byte-identical on disk.

Two fixes, independent of each other.

## 1. The transcript is sent once and reused four times

Each extractor's request is now split in two: the notes and transcript
go **in front**, identical across all five, and the short
per-extractor instruction goes **behind** them. The boundary is marked
so the API caches everything before it. Cached input bills at a tenth
of the normal rate.

The ordering turned out to matter as much as the marker. Caching
writes on the first request and only becomes readable once that write
has landed — so five calls fired concurrently all race it and **every
one misses**. That failure is invisible: it reports zero reads and
looks exactly like the bug it was meant to fix. So the summary call now
runs first, on its own, and the other four read what it wrote.

Measured over the wire against a server that implements a real prefix
cache — a read has to be earned by the bytes actually sent:

```
5 calls: 1 write, 4 reads, all five prefixes byte-identical
input billed:  18,804 units  →  8,074 units   (43%)
```

Run against the previous build, the same harness reports 10 failures
and `cache_read: 0` — the same number the export showed.

**A cache that silently stops working looks identical to no cache at
all**, which is exactly how this went unnoticed for a month. So the
backend now logs the hit rate once per processing run. If it ever
regresses, the signal is in the log rather than in next month's
invoice.

## 2. Reprocessing skips what cannot change

Reprocessing re-ran all five extractors on every session regardless of
whether anything about that session had moved.

Each run now records a fingerprint of the only four things the output
depends on: the transcript, your notes, the template, and the version
of the extraction prompts. If all four match what the last successful
run recorded, the five calls are skipped entirely.

What still forces a real re-run:

- a re-diarized or edited transcript;
- edited notes (this is how you correct an extraction — it can never
  silently no-op);
- a different template;
- **a prompt change** — the prompt version is part of the fingerprint,
  so tightening a prompt still invalidates every session, as it must.
  Reprocessing after a prompt fix does the full work; it just stops
  being the default for sessions nobody touched.

Two things it deliberately refuses to skip:

- **A partially failed run never records "done."** If one extractor
  gets rate-limited and the other four succeed, no fingerprint is
  written — otherwise the next reprocess would skip the one session
  that still needed finishing, and the skip would be indistinguishable
  from success.
- **A session with no summary never skips**, even with a matching
  fingerprint. A stamp over missing output means the stamp is wrong,
  not that the work is done.

## Verification

| | |
| --- | --- |
| Backend tests | 1350 pass (20 new) |
| Extension tests | 161 pass |
| `npm run build` | clean |
| Wire-level cache rig | 12/12, one write + four reads |
| Same rig, previous build | 10 failures, `cache_read: 0` |
| `personal_data_scan.py` | 0 findings |

Every one of the 20 new tests was verified to **fail** against the
shipped code before the fix landed.
