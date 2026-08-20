# v2.40.0 — auto-recorded meetings arrive already tagged

## Install (macOS)

> v2.40.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.40.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.40.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.40.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.4.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.4.0.

## Auto-recorded meetings now arrive with a client

An auto-recorded call landed untagged, and the client had to be set by
hand afterwards.

There was a client suggester, and it could never have helped. It keys
entirely on attendee **email domains**, and extension-sourced calendar
events carry attendee **names only** — so it returned nothing every
time. It also lived only in the interface, invoked when you press
**Use**; auto-record starts in the background and never reached it.

Resolution now runs where the recording starts, and reads the signal
that was there all along: the meeting subject. A meeting titled
`… ACME-Globex Connect MVP …` matches the client **ACME** from your own
client list.

**Matching knows that `_` and `-` are not word boundaries.** A short
client name is a substring of ordinary words, and treating `_` as part
of a word is a bug this project shipped earlier the same week. So
`transcript_ACME`, `ACME-Globex`, `Programme/ACME/Stream 2`, `ACME2026`
and `(ACME)` all match, while `ACMEish` and `Reacme` do not. A longer
client name wins over a shorter one contained inside it.

**Two clients in one subject means no tag.** A wrongly-tagged session
doesn't just carry a wrong label — the export worker copies it into that
client's Designated Folder. An untagged session is a minute of your
time; a misfiled one is a client conversation in another client's
folder.

Email-domain matching still runs for calendars that carry addresses, and
behaves exactly as before.

**It tells you how it decided.** The client field shows whether the
value came from the subject, from domain history, or was refused as
ambiguous — and clears that note the moment you set the client yourself.

Automatic pre-meeting briefs also get the resolved client now, so they
draw on that client's Knowledge Folder instead of falling back to recent
calls across everyone.

## Speaker names come from the invite

Speaker identification was given the transcript and nothing else, so it
inferred names from conversation alone. The invite is a better source:
it says who was actually in the room.

The attendee list now goes in as a candidate roster. When the transcript
says a first name and the invite carries the fuller one, the speaker is
labelled properly. Where the invite has only email addresses, display
names are derived from them — `first.last@`, `first_last@`,
`first.m.last@`, hyphenated and non-ASCII names, `+tag` suffixes and
trailing digits all handled.

Deliberately **not** derived: `jdoe@`-style addresses. That looks like
initial-plus-surname, but it's the same shape as a short first name —
the rule that produces "J. Doe" also produces "J. Ane", and nothing
distinguishes them without guessing. Room and distribution mailboxes are
refused for the same reason.

The roster is a strong hint, not a cage: someone who joins without an
invite can still be named from the transcript. And two invitees sharing
a first name leaves that speaker unnamed rather than picking one —
mis-attributing speech propagates into commitments, owner grouping and
follow-up recipients.

Sessions with no attendee list produce exactly the prompt they did
before, byte for byte.

## The extension captures the organiser

Every captured meeting carried `organizer` and `join_url` fields that
were **never populated** — declared in the schema and always empty.

The organiser was sitting in text already being read. A calendar entry
reads `… , Microsoft Teams Meeting, By Jane Doe, Busy`, and that name
was being discarded. It's captured now, including the awkward forms:
`Roe, Pat Jr. [US-EMEA]` stays whole rather than splitting at the comma,
and trailing status words like *Busy* or *Exception to recurring event*
don't get absorbed into a name.

Beyond display, this feeds recipient lookup — a full name resolves to an
email address far more often than a first name.

### Join links: measured, not guessed

`join_url` stays empty, and that's a finding rather than an omission.
Both desktop calendar paths read the join link from the invite **body**;
the browser capture has no body, and the calendar grid labels a meeting
as a Teams meeting without exposing its URL.

Rather than ship an extractor that quietly returns nothing, the
extension's **Diagnose calendar capture** now measures it: how many
join-style links exist in the area already scanned, how many sit inside
a recognised meeting, and the *shape* of the addresses — host and path
pattern only, never the URL, since those are single-use meeting
credentials.

Run it against your calendar and the verdict says outright whether join
links are reachable from what's already read. If they are, filling the
field is small. If not, the only route is opening every event
individually — slow, fragile, and the kind of dependency that broke
calendar capture for weeks, so it isn't being done by default.

Nothing renders a fake link meanwhile: the Join button is already
conditional on a real value, so an empty one shows nothing.

## A note on the personal-data guard

The check added yesterday caught its first real leak — in code written
today. New comments quoted an actual past incident verbatim,
reintroducing a customer name the scrub had removed. The lesson was
worth keeping; the example is now fictional.

The same run showed the check failing on the project's *own* approved
placeholders, because it listed permitted domains one at a time. It now
accepts any address under `.example`, a top-level domain reserved
precisely so it can never resolve. A check that cries wolf on its own
placeholders is one people switch off.

## Tests

1204 backend tests, up from 1124, and 63 extension tests, up from 39.
Verified the scanner still catches a genuine address after the
relaxation, and that auto-record's trigger logic is untouched — the
client resolution is additive and a failure there can never stop a
recording from starting.
