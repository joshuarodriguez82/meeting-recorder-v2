# v2.63.0 — the link was in the captured body all along; we were stripping it

## Install (macOS)

> v2.63.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.63.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.63.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.63.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.19.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.19.0.

## The first healthy capture told the whole story

The 2026-08-23 12:19 capture — the first after the signed-out weekend —
came back: 51 events structured, recorder installed, 171 responses
seen, 18 matched, 10 panes opened, **10 invite bodies gained… and 0
join URLs.**

A Teams invite's join URL lives inside the invite body **as the href
of "Join the meeting now"** — the visible text often carries no URL at
all. The response parser stripped every HTML tag from the captured
body — href included — *before* searching it. On all ten of those
meetings, the link was captured and then deleted during cleanup.

The raw body HTML is now scanned for provider-shaped URLs (Teams,
Webex, Zoom, Meet — the same host+path contract as everywhere else)
before stripping. An explicit join field on the response still wins;
the body scan is the fallback for exactly the shape your tenant
ships.

Proven end to end: the rig's captured-response item now carries **no
join field at all**, the Teams URL existing only as the body's href —
and it lands as the meeting's join link through the real extension,
real backend, and real store.

## The diagnostics channel was eating its own evidence

`buildCaptureDiag` — the function that carries the capture counters to
the app — was a **whitelist**. Three casualties:

- `joinFromMarkup` (added 1.17.0): incremented inside the pane reader,
  never copied out. The field run that would have proven whether the
  markup scan works reported nothing.
- `authRedirect` / `calendarUnreadable` (added 1.18.0): the signed-out
  banner's own flags never left the extension, so the banner built to
  end invisible failures could not fire.

The same drop-list constructor that hid invite bodies for six
releases, living in the diagnostics channel itself. It is not a
whitelist any more: every boolean and finite number the capture
records goes through, with the backend applying the same scalars-only
bound on its side. A test now pins that a counter added in the future
passes through without being individually enumerated.

## Why the app knows fields exist now

When a captured response matches a meeting, the capture records — as
booleans, never values — which field-key groups the item carried:
attendees, body, join. So `attendees gained: 0` next to
`respHadAttendeesKey: false` means "this tenant's responses don't
carry attendee keys we know", not another guessing round.

## Also

- "Series" / "Occurrence" — the recurring-meeting toggle labels — no
  longer lead the captured agenda.
- "What the last capture found" gains a **Join links from invite
  HTML** row.

## Tests

1324 backend tests, 151 extension tests (up from 146). Four of the
five new extension tests verified failing against shipped 1.18.0 (the
fifth pins existing precedence: an explicit join field beats the body
scan). Full E2E rig: 16/16 checks.
