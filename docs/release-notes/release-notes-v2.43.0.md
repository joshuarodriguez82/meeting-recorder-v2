# v2.43.0 — attendees, agendas and Teams join links, without guessing an endpoint

## Install (macOS)

> v2.43.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.43.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.43.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.43.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.7.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.7.0.

## Why the last attempt failed, precisely

v2.42.0 shipped a probe that tried four candidate calendar endpoints.
The field run answered:

```
origin:         https://outlook.cloud.microsoft
canaryPresent:  false
rest-v2:        401 auth-rejected
```

That tenant runs the **new** Outlook web stack. It has no
`X-OWA-CANARY` — not missing, not applicable — and authenticates with
bearer tokens. All four candidates were modelled on classic OWA at
`outlook.office.com`. Three were never attempted; the fourth was
refused.

Every guess was wrong, and each one cost a full
release → reinstall → re-run cycle.

**The lesson is not "guess better endpoints."** Replicating someone
else's authenticated request means tracking their auth scheme forever,
and this project cannot observe that scheme from where it is built. Any
fix shaped like *call endpoint X with credential Y* is one Microsoft
change away from another round of this.

## So this release does not make a request at all

Outlook is **already** fetching the attendee list, the invite body and
the Teams join URL — it has to, to draw the calendar. This release
installs a passive recorder in the page before Outlook's own scripts
run, lets Outlook authenticate however it likes to whatever endpoint it
likes, and reads the responses on the way back.

No endpoint to guess. No token ever handled. Classic OWA and the new
stack work identically, because nothing in the code has to tell them
apart.

**All three empty fields close together**, because they were always one
problem:

- **Attendees** — `ATTENDEES (0) / None listed.` on every row
- **The agenda** — `(No description on this invite.)` on every row
- **Teams join links** — the 7 meetings whose Location is the literal
  words *Microsoft Teams Meeting*, plus the 16 with no Location at all

The Zoom/Webex links v2.41.0 already read from the label keep working
and take precedence: merging is strictly additive, and a field the
label filled is never overwritten.

### Shape-agnostic on purpose

The parser does not know which API produced a payload. It walks the
response for anything that looks like a meeting — a subject key and a
start key — and reads the field names it recognises across both
vocabularies (`attendees` / `RequiredAttendees`, `body.content` /
`Body.Value`, `onlineMeeting.joinUrl` / `JoinUrl`). Tests run the same
parser against a Graph-shaped payload and an EWS-shaped one and assert
identical results.

Start times are matched to the minute. The API returns seconds and a
timezone; the calendar label parse returns neither. An exact string
compare would have matched nothing — and would have looked exactly like
"the response carried no detail."

### What the recorder does not do

It never modifies, blocks, delays or retries a request. It never reads
a credential — request headers, `Authorization` included, are not
touched; only response bodies are read. It sends nothing anywhere:
bodies sit on a page global that the extension reads once and clears.
It is bounded in count and bytes, registered only for the seconds a
capture is running, and unregistered afterwards whatever happens.

Every path is wrapped so a fault inside the recorder cannot break
Outlook's own fetch. A broken calendar would be far worse than a
missing attendee list.

HTML invite bodies are reduced to readable text, with `<script>` and
`<style>` contents dropped rather than flattened into the agenda.

### It reports whether it actually ran

Capture now records whether the recorder registered, whether it
installed, how many responses it saw, how many matched, and how many
were dropped at the cap. "The recorder could not install" and "the
recorder ran and found nothing" are different facts, and this release
refuses to collapse them — that collapse is the specific defect behind
the last three rounds.

On a Chrome too old for main-world content scripts, capture degrades to
exactly its previous behaviour and says so.

## A test that exists because of how this fails

`registerContentScripts` takes a **filename**, not a function. A rename
or a missing file fails at runtime inside a background service worker,
where nobody sees it, and the only symptom is attendees quietly staying
empty. Two build-time tests now assert the registered file exists, and
that the registration covers `outlook.cloud.microsoft` and not only
`outlook.office.com` — the exact gap that made v2.42.0 useless on this
tenant.

## Tests

1257 backend tests, up from 1254, and 112 extension tests, up from 98.

The new extension tests cover both API shapes through one parser, HTML
and script-tag handling, minute-precision matching, merge being
additive, an unmatched event coming through byte-identical, no captured
responses changing nothing at all, an unrecognised payload yielding
nothing rather than inventing a meeting, two responses for one meeting
merging rather than overwriting, and attendee de-duplication. The
backend tests pin that the invite body round-trips, is capped, and that
an event stored before the field existed still loads.

Security scanning run against the baselines before merge: bandit 185
findings / 0 new, semgrep 3 / 0 new, personal-data 0.
