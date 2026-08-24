# v2.66.0 — read the calendar Outlook already cached on your machine

## Install (macOS)

> v2.66.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.66.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.66.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.66.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.22.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.22.0.

## Three probes closed the scraping question. This is the answer.

Field evidence, all from your own diagnostics:

| Probe | Result |
| --- | --- |
| DOM scan (grid + pane, every root) | 0 join-shaped anchors, 0 in label text |
| Response census (190 seen) | timezone config, group lists, mailbox metadata, **license fields** — not one event field |
| API sign-in | needs a managed device; this machine is not one |

So the join URL is not in the page, not in any response the page's own
fetch can see, and the API door is closed. Every release that scraped
harder was attacking a wall with no door in it.

**But Outlook renders your calendar offline, instantly — because it is
a PWA, and a PWA keeps its data in IndexedDB on your machine.** The
capture now reads that cache directly: no sign-in, no managed device,
no new permission, and no dependence on which worker fetched the data.

It is deliberately shape-agnostic, exactly like the response parser:
walk the records, keep anything carrying a subject-ish and a start-ish
key, and hand it to the same extractor. No database name, store name
or schema is assumed — all three belong to Microsoft. Bounded hard
(12 databases, 40 stores, 4000 records, 12 seconds) and every path
wrapped, because this runs inside your Outlook tab.

It runs **before** the click pass, so anything the cache answers costs
no clicks at all.

## The "Attendees (0)" bug, found by the rig

Attendees were being extracted as **display names** — and the store
keeps only address-shaped entries (the scrub that killed the
"Attendees (24)" wall of Outlook buttons). So every name was dropped
at read time: the counters cheerfully reported attendees gained while
the meeting rendered "Attendees (0)". The rig reproduced it exactly —
5 gained, 0 shown.

Addresses are now preferred, names kept only when no address exists.
Two existing tests encoded the old preference and were updated
deliberately, with the reason recorded in them.

## Links inside bodies you already have

A meeting whose invite body contains a join link but whose join_url
field is empty now yields the link **at read time** — so meetings
already in your store gain it without waiting for a re-capture. An
explicit join_url always wins over a link found in the text.

## Tests

1330 backend tests, 157 extension tests. The full end-to-end rig —
real extension, real backend, real store — now runs **19/19**,
including the PWA-cache path proving the join URL, attendees and
invite body all arrive from Outlook's own cache when the network
carries nothing usable.
