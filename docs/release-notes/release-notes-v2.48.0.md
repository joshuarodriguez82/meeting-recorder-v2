# v2.48.0 — Teams meetings: the link was in the button, not the text

## Install (macOS)

> v2.48.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.48.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.48.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.48.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.11.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.11.0.

## What v2.47.0 established

v2.47.0's fixes worked, and for the first time the diagnostics said so
rather than arriving empty:

```
recorderInstalled: true      responsesSeen: 184    responsesMatched: 25
domDetailOpened: 19          domDetailGrew: 19     domDetailNoTile: 4
```

The click pass opened 19 events — it had opened zero in every previous
release. Webex and Zoom meetings gained a Join link, an organiser and
an attendee.

Teams meetings gained nothing, and every meeting reported at most one
attendee. Both have the same cause, and it is not Teams.

## The join link was never in the text

The reader took URLs out of the pane's visible text. Where a URL lives
depends entirely on the provider:

| Provider | Where the join URL is | Text reader |
|---|---|---|
| Webex, Zoom | the add-in pastes the raw URL into the invite body | found it |
| Teams | the pane renders a **Join button**; the URL is the anchor's `href` and appears nowhere in the text | found nothing |

So "Teams has no join link" was never a fact about Teams. It was a fact
about which half of the page was being read — and it is the same
mistake as v1.4's join-link probe, exactly inverted:

- **v1.4** looked at elements, missed a URL that was text.
- **v1.10** looked at text, missed a URL that is an href.

Both places are read now. Text wins where both exist, because a pasted
URL is unambiguously part of *that* invite, while an anchor is
identified only by having newly appeared. An anchor already on screen
before the click is never attributed — sending you into the previous
meeting's call is worse than an empty field.

## Attendees are names, and only addresses were being matched

The same reader collected attendees by email-address pattern. Outlook's
pane renders people as **names**; addresses usually never appear. Hence
`Attendees (1)` for a meeting with a dozen invitees — the single row
that happened to show an address — and `(0)` everywhere else.

Attendee names are now read from the pane's accessible labels, which
Outlook populates for assistive technology, so this needs no assumption
about markup. It stays deliberately strict: two to four words, letters
and the punctuation names actually contain, no digits, and nothing
matching interface vocabulary (*Accept*, *Decline*, *Show as Busy*,
*Microsoft Teams Meeting*, *Every weekday*). A wrong attendee
propagates into speaker identification and follow-up recipients, so a
missed name is the better failure.

Addresses in the text are still collected alongside the names.

## A third source, which was being thrown away

Opening an event makes Outlook fetch **that event's full detail** — the
complete attendee list with real names, the invite body, and the join
URL as data rather than as scraped text. The recorder installed in the
page captures every one of those responses.

Nothing ever read them, because the only harvests happened *before* the
clicks.

Those responses are now harvested straight after the click pass and
merged, richest answer winning — a full attendee list from the detail
response replaces a single scraped name rather than losing to it.

This is the best source in the pipeline: it is the same data Outlook
renders *from*, so it depends on no markup and cannot mistake one
meeting's Join button for another's. Scraping the pane remains the
fallback.

## "Did the pane render?" stopped being measured by volume

The check asked whether the page's text had grown — first by 40
characters, which discarded a sparse invite, then by 10, which still
discarded a Teams pane. Teams reveals a button and a few words, so its
text can grow by less than that while carrying exactly the URL this
mechanism exists to fetch.

Arrival is now judged by whether any of the three things we came for
appeared: new text, a new anchor, or newly revealed attendee labels.
Volume was a proxy for the question; content is the question.

## Tests

1276 backend tests, unchanged — no backend code was touched. 130
extension tests, up from 123.

**Six of the seven new tests were verified to fail against the shipped
v1.10 code and pass against this one.** The seventh — that an anchor
already on screen is never attributed — passes on the old code too,
because the old code read no anchors at all; it is a safety guard, not
a regression test, and is labelled as such.

They cover a Teams URL found in an href with the words-only text a
Teams pane actually shows, a pasted Webex URL still winning over an
anchor, a non-conferencing anchor never becoming a Join button, a stale
anchor not being attributed, names read from accessible labels,
interface chrome rejected as people, and addresses still collected
alongside names.

## Why the extension keeps needing updates

Because every one of these fixes lives in it. The extension is the only
component that can see Outlook Web, so all extraction — labels, clicks,
responses, parsing — happens there, and the app only consumes the
result.

That is a design choice, and it is the wrong one. The extension should
be a dumb pipe: capture labels and responses, ship them to the app, and
let the app do the parsing — at which point a parsing fix is an app
update and the extension stops changing. That refactor is next; it was
not done in this release because it would have delayed the fix above.
