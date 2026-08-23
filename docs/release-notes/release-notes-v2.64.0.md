# v2.64.0 — the unreadable responses now identify themselves

## Install (macOS)

> v2.64.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.64.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.64.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.64.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.20.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.20.0.

## What the 13:52 capture pinned down

Extension 1.19.0's first run, with the de-whitelisted diagnostics all
arriving:

- 179 Outlook responses seen, 19 matched by the recorder's gate
- 11 invite bodies gained — **every one from the click panes**
- `joinFromMarkup: 0` across 11 opened panes, matching the DOM probe's
  verdict: on this tenant, the join URL is **not in the page**, grid or
  pane
- and the decisive one: the response parser recognized **zero items**
  inside those 19 captured responses — the new Outlook stack uses
  field names it doesn't know, so the body-href scan shipped in 1.19.0
  never got to run

The responses are the only carrier left, we hold 19 of them per
capture, and we couldn't read their shape. Finding field names by
guessing costs one full release/reinstall/capture cycle per guess —
that is the story of the last week.

## So the payload identifies itself

When a capture's responses match **nothing**, the capture now records:

- **`responseKeyNames`** — up to 64 field names found in the captured
  responses. Names only, lowercased, and each entry must *look like a
  field name* (identifier characters only), enforced independently on
  both sides of the wire — so an email address, URL, or meeting
  subject cannot ride the list out even by accident. Lists under any
  other key are still rejected wholesale.
- **`responsesContainJoinShapedUrl`** — one boolean: does any captured
  response contain a Teams/Webex/Zoom/Meet-shaped URL at all? If
  true, the link is in our hands and only parsing stands between us
  and it. If false, the responses genuinely lack it and the next move
  is different.

"What the last capture found" displays both. The next diagnostics zip
— or a glance at the panel — names the exact aliases to add, ending
the guessing loop structurally.

## Tests

1325 backend tests, 154 extension tests (up from 151). The census
tests were verified failing against shipped 1.19.0; the
privacy bounds are pinned on both the extension side and the backend
sanitizer (address-shaped entries rejected, lists under other keys
rejected, healthy captures emit no census at all).
