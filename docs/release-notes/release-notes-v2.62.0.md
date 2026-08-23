# v2.62.0 — the signed-out Outlook session stops being invisible

## Install (macOS)

> v2.62.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.62.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.62.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.62.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.18.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.18.0.

## What the diagnostics finally proved

The capture history in the 2026-08-23 diagnostics bundle:

| When | Capture path | Events |
| --- | --- | --- |
| Fri Aug 21, 21:24–22:54 | structured (the real pipeline) | 51, four runs in a row |
| Sat Aug 22 onward | text fallback, `calendar_events` absent | 9–10, no detail |
| Sun Aug 23, 11:08 | fallback | 0 |

And the source sizes since Saturday: **OWA tab 0 characters, Inbox tab
0 characters — while the Teams tabs kept reading ~2,400 characters.**
Recorder never installed, zero candidates, zero responses.

The extension didn't regress — **the browser's Outlook session
expired.** When the capture opens `outlook.office.com` in a background
tab, the tab is bounced to a sign-in page: an origin the extension has
no permission to script, so every read fails in a way that is
indistinguishable from "the calendar was empty". Teams kept working
because its session is separate. The Outlook desktop app kept working
because it holds its own authentication and asks the mailbox API — it
never depends on a signed-in browser tab.

The app then silently served Friday night's stale meetings for two
days, which made every fix shipped in between look like it did
nothing.

## What changed

**The capture now classifies where the tab landed.** A hostname
suffix-match against the Microsoft sign-in origins
(`login.microsoftonline.com`, `login.live.com`, `login.microsoft.com`,
`account.microsoft.com`, `login.windows.net` — suffix, never
substring, so a lookalike phishing host can't count). The result rides
the existing capture-diagnostics channel as two booleans:

- `authRedirect` — bounced to a sign-in page. The capture skips the
  pointless 45-second settle-poll and returns immediately, with the
  flag.
- `calendarUnreadable` — an Outlook origin that yielded neither one
  candidate nor 200 characters of text: a login interstitial rendered
  on Outlook's own origin, or a redesign the scan cannot see. Distinct
  from a genuinely free calendar, which still renders plenty of page
  text.

**And the app says it out loud.** Upcoming Meetings shows a red banner
for the signed-out case — *"Outlook Web is signed out in Chrome … the
meetings below are from the last successful capture and will not
update. Open outlook.office.com, sign in, then Capture & Send"* — and
an amber one for the unreadable-calendar case. No more two days of
stale data wearing a working face.

The flag is carried on the failure path deliberately — not thrown past
the return — because losing the diagnostic exactly when the capture
fails is the bug v2.54.0 already paid for.

## What to do right now

Open `outlook.office.com` in Chrome. If it asks you to sign in, sign
in. Then run one Capture & Send. Everything shipped since Friday —
frame-scoped bodies, the markup join-link scan, clean agendas — has
been waiting behind that sign-in page.

## Tests

1323 backend tests, 146 extension tests (up from 144). Both new tests
verified failing against shipped 1.17.0: the sign-in-origin
classification (including the lookalike-host cases) and the wiring
that puts the flag into the POSTed diagnostics.
