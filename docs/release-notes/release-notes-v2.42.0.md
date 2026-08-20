# v2.42.0 — measuring whether Teams links and attendees are reachable at all

## Install (macOS)

> v2.42.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.42.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.42.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.42.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.6.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.6.0.

## What v2.41.0 actually achieved, stated honestly

v2.41.0 taught the extension to read join links out of the calendar
label. Field result: **one meeting in a full week gained a Join
button.**

That is not a partial failure of the extraction. It is the complete
result, and the numbers say so. Across 25 captured labels:

| What the label carried | Count | Outcome |
|---|---|---|
| A Zoom URL in Location | **1** | Join button — filled |
| A non-conferencing URL in Location | 1 | filed as a location, correctly not a Join button |
| The literal words "Microsoft Teams Meeting" | 7 | no URL exists in the label |
| No Location segment at all | 16 | no URL exists in the label |

The capture diagnostic agrees independently: `anchorCount: 0`,
`labelJoinCount: 1`. Extraction found **one of one** available link.

So the grid has now been asked and has answered. A Teams event's
Location is the words "Microsoft Teams Meeting"; the join URL lives in
the invite **body**, which the grid never renders. The attendee list
lives in the event detail pane. The invite description lives there too.
All three empty fields — the missing Join buttons, `Attendees (0)`, and
"(No description on this invite.)" — are one problem, not three.

## The route that remains, and why this release only measures it

Two ways to reach the detail pane:

**Open all 25 events one at a time.** Slow, visibly drives your
calendar while it runs, and re-introduces exactly the DOM dependency
that broke calendar capture for weeks.

**Ask the API that Outlook Web itself asks**, from inside the session
you are already signed into. Better architecture, and it returns
attendees, body and join URL together.

The second is the right answer. It is also a set of facts about a live
authenticated tenant — which endpoint answers, whether it needs a CSRF
canary, whether the payload carries the fields — and **nothing in this
codebase can observe any of that.**

Twice now this project has shipped a confident verdict about a page it
could not see. v1.4's join-link probe searched for `<a>` elements,
found none, and reported that join links "cannot be filled from this
DOM" — while its own output carried one as text. The rule that came
out of it:

> **A result you could not read must never render as a result that is
> not there.**

Guessing an endpoint would be that same mistake wearing a new hat. So
this release ships the **measurement**, and the implementation gets
written against what your tenant actually says.

## Diagnose calendar API access

A new button in the extension's options page. It opens your calendar in
the background, asks each candidate endpoint once, and reports what
each one did.

**Four outcomes, kept deliberately separate**, because the failure that
matters is the one that reads like success:

- **usable** — answered, and the payload carries attendees / body /
  join URL
- **answered-thin** — answered, but carries none of them. Reachable,
  wrong shape.
- **auth-rejected** — 401/403. Reachable; we are not entitled. This is
  *not* evidence the API is absent.
- **not-attempted** — the CSRF canary was missing, so no request was
  sent. Never recorded as a failure, because we never asked.

A 200 carrying HTML is reported as unreachable rather than as data — a
sign-in redirect returns a perfectly healthy 200, and treating that as
success is how "it works" gets reported for a session that isn't signed
in.

**It asks for everything.** The probe requests the full property set
rather than a minimal one. Asking for identifiers only would guarantee
that attendees and body came back missing — a false negative
manufactured by the question, which is precisely v1.4's mistake.

### It reports none of your calendar

Status codes, response sizes, item counts, and which **field names**
came back. No subject, no attendee, no body text, no URL. The CSRF
token is reported as present or absent, never as a value. A test asserts
that a response stuffed with a subject, an address, a join URL and a
token produces a report containing none of them — while still correctly
reaching a verdict.

## A bug the tests caught in this release's own code

The first version of the field detection scanned the whole response for
key names. An Exchange reply is wrapped in a top-level `Body` envelope,
which collides with a meeting's own `Body` — so **every** such response
scored "carries the invite body" and would have been recorded as
usable no matter how empty it was.

That is the same defect as the anchor search: a question whose shape
guarantees its answer. The scan now runs only inside the returned
meeting items, and the case is pinned by a regression test.

Nothing about capture, parsing, auto-record or the calendar feed
changed. The probe is read-only and additive; it shares the existing
diagnostic's tab lifecycle, including the cleanup that guarantees no
stray tab is left in your browser if it throws.

## Tests

1254 backend tests, unchanged — no backend code was touched. 98
extension tests, up from 90. The eight new ones cover each of the four
verdict states, a 200 of HTML, a thrown fetch, the envelope-collision
regression, that field matching reads key names and never values, and
that no calendar content or token can reach the report.

Security scanning run against the baselines before merge: bandit 185
findings / 0 new, semgrep 6 / 0 new, personal-data 0.
