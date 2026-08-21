# v2.57.0 — the capture was working; the backend was throwing it away

## Install (macOS)

> v2.57.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.57.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.57.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.57.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update needed

Extension **1.15.0** (shipped with v2.56.0) is the current version and
this release does not change it. If you are already on 1.15.0, install
the app and you are done.

## This release was written against a real capture, not a guess

Every previous attempt at this problem reasoned from diagnostics after
the fact. This one was built against a rig that runs the **real
extension** in a real Chromium, against a calendar server that answers
on Outlook Web's own hostnames, POSTing into a **real backend** — the
entire path, end to end, with the same "Capture & Send" button you
press.

The first run of that rig found the defect in under a minute.

## What it found

The extension was doing its job. It opened every meeting, read every
pane, and extracted the Teams anchor href, the pasted Webex link, the
Zoom link, the attendee addresses and the invite bodies — all correct,
all in the POST body.

The backend then **discarded the entire capture** and answered 400.

The reason: a full "Capture & Send" carries two things — the narrative
briefing text and the structured calendar events. The handler checked
whether an AI provider was available for the *briefing* half **before**
storing the *calendar* half. Any failure on the briefing side — no API
key, a rate-limited key, a model that errored mid-parse — took the
calendar down with it, even though calendar events are parsed entirely
client-side and need no AI at all.

The calendar-only refresh alarm never had this problem; it stores and
returns before any LLM is consulted. Only the manual button, the one
pressed when someone is actively trying to make this work, went through
the gate that could throw the work away.

**The briefing failing is not the calendar failing.** Structured
calendar events are now written to the store *before* the briefing
gate can reject anything. Two regression tests cover both failure
modes — no provider configured (400) and a provider that throws
mid-parse (502) — and both were verified to fail against the shipped
build.

This is the same defect this project keeps re-encountering, in its
third costume: a result you could read, destroyed because something
else nearby could not be read.

## Verified end to end, not asserted

The rig's checks, all passing against the shipped extension:

| Meeting | What had to arrive | From |
| --- | --- | --- |
| Teams call | join URL, 2 attendees, body | captured response |
| Webex call | `webex.com/meet/…` link, agenda body, 2 attendee addresses | click pass, pasted text |
| Zoom call | `zoom.us/j/…` link, body | click pass, pasted text |
| No-link internal meeting | body, attendee address | click pass |
| Meeting that ended yesterday | never opened at all | v2.56.0's floor filter |

The Teams link came from the anchor href, the Webex and Zoom links from
visible pane text, and `detail_status` read `opened` on every meeting
the click pass handled — the whole chain the last several releases have
been trying to confirm blind.

## Portal binding: paste the connection block

The portal hands out one JSON block:

```json
{"portal": "…", "api": "…", "opportunity": "…",
 "customerId": "…", "editToken": "…"}
```

The app asked for those values as four separate typed fields, and its
push target came from a Portal URL in Settings — which is the portal
*website*, not the `api` host the ingest endpoint lives on. That
mismatch meant a correctly-filled form still could not reach the
portal.

Bind now takes the block itself: paste it, the app parses it, and the
binding stores its own `api` base as the push target. The edit token
goes straight to the OS keychain and is never written to the bindings
file, a log line, or an error message — including when the portal
echoes it back in an error body. Manual field entry is still available
behind a toggle for anyone without a block.

## Tests

1305 backend tests (up from 1300) and 139 extension tests. New this
release: the two calendar-survives-a-failing-briefing regressions and
three portal connection-block tests, all verified failing first.

bandit 0 new findings, semgrep 0 new, personal-data 0.
