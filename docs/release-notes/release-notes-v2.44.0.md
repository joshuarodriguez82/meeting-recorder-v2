# v2.44.0 — the diagnostics finally say which failure this is

## Install (macOS)

> v2.44.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.44.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.44.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.44.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.8.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.8.0.

## The actual problem with the last four releases

Attendees, invite bodies and Teams join links are still empty, and
that is the fifth attempt. What made it five attempts is not that each
fix was wrong — it is that **every failure produced the identical
symptom**, so each round began by guessing which of several causes it
had been.

v2.43.0 was supposed to end that. It added counters that distinguish
the cases, and then kept them **inside the extension**. The app's
diagnostics bundle could not see them. So a field report still said
only "still empty", and the four possible causes — each needing a
different fix — remained indistinguishable.

Promising that the diagnostics would identify the cause, and shipping
a bundle that could not, is the same defect as the rest of this
sequence, committed one layer further out.

This release fixes that first, because nothing else can be diagnosed
until it is.

## The bundle now reports what the recorder did

`versions.json` in an exported diagnostics zip now carries
`extension_capture_diag`, written on every calendar capture:

| Field | What it settles |
|---|---|
| `recorderInstalled` | Did the recorder get into the page at all |
| `responsesSeen` | Did any response pass through the main world |
| `responsesMatched` | Did any response actually contain a meeting |
| `responsesNotMeetingShaped` | Was there calendar traffic we could not read |
| `detailMatched` | Did captured meetings line up with captured events |
| `detailGainedAttendees` / `Body` / `JoinUrl` | Did anything actually get filled |

These four states need four different fixes, and they are now
distinguishable from a single zip with no extra steps:

- `recorderInstalled: false` — never got into the page.
- `responsesSeen: 0` — installed, but the page does not fetch through
  the main world at all. That means a service worker or web worker is
  doing it, and no amount of tuning *what* gets recorded would ever
  have helped.
- `responsesMatched: 0` with `responsesSeen` high — traffic seen,
  nothing in it held a meeting.
- `detailMatched: 0` with matches above it — meetings read, but they
  did not line up with the captured events.

An **empty** `extension_capture_diag` means no capture has reported
since the field existed — deliberately not the same as a capture that
ran and found nothing, which reports zeros.

Counts and booleans only. The extension sends no URL, subject,
attendee or body text in this payload, and the store drops anything
that is not a number or a boolean — so a future extension build cannot
put a meeting name or a join link into a file you paste into a chat
window. A test asserts exactly that.

## Two of the four causes are removed outright

**Recording no longer depends on guessing URLs.** v2.43.0 decided what
to record from a list of URL substrings — another guess about a tenant
this project cannot see, the same mistake as the endpoint list one
layer down. If it guessed wrong, the symptom was again "empty fields".

The gate is now the **content**: any JSON response containing a
subject-like key is parsed and offered to the extractor, and whether it
holds a meeting is decided by looking. A cheap string test runs first
so this does not parse every response on the page.

**The registration race is closed.** `registerContentScripts` resolves
before Chrome has necessarily applied the registration to a tab that is
already navigating — and the capture tab was created microseconds
after that call. Capture now verifies the recorder is actually resident
and, if it is not, injects it and reloads the page so it is present
before Outlook's calendar request goes out. Both facts are reported
(`recorderInjectedLate`, `recorderReloaded`).

## What this release does not claim

It may still not fill the fields. If Outlook fetches its calendar from
a service worker, the main-world recorder cannot see it, and that needs
a different mechanism entirely.

The difference is that this time the diagnostics will say so —
`responsesSeen: 0` with `recorderInstalled: true` is that exact
signature — instead of producing another round of "still empty" and
another guess.

## Tests

1260 backend tests, up from 1257, and 112 extension tests, unchanged.

The new backend tests cover the counters reaching the bundle, an
absent report staying distinct from a zero report, and the store
dropping any non-scalar so no URL, subject or address can reach an
exported zip.

Security scanning run against the baselines before merge: bandit 185
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
