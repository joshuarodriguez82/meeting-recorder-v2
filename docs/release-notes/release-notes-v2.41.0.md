# v2.41.0 — the join link and the organiser, from the calendar you actually use

## Install (macOS)

> v2.41.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.41.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.41.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.41.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.5.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.5.0.

## The join link was there the whole time

v2.40.0 shipped a diagnostic that measured whether join links were
reachable from the calendar grid, and it returned a verdict:

> no join-shaped links anywhere in the scanned roots — the grid does
> not expose them; join_url cannot be filled from this DOM

That was wrong, and the same diagnostic's own output disproved it. Its
list of longest labels contained a full join URL — as **text**, inside
an aria-label, in the Location position of the label the capture
already reads and parses. The probe searched for join links as `<a>`
**elements**, found none, and reported the absence of elements as the
absence of links. It asked one question and answered a different one.

That is the same defect this project has now shipped several times over,
in a new costume: **a result you could not read must never render as a
result that is not there.** A probe that looked in one place is entitled
to say "I looked in one place" and nothing more.

Join links are now filled from the label text. Whether one exists is a
property of the individual meeting rather than of your tenant: a Teams
event's Location is the literal words *Microsoft Teams Meeting* and
never a URL, while a Zoom or Webex add-in writes the real join URL into
Location, and Outlook Web renders Location into the label.

**Only a recognised conferencing provider becomes a Join button.** Teams
`meetup-join`, Zoom, Webex and Google Meet — matched on host *and* path,
so `zoom.us/pricing` is not a meeting. Any other link sitting in that
same position is a **place**, not a meeting to join: it fills the
meeting's `location` field instead, and never the Join button. A Join
button that quietly takes you to a training library is worse than the
empty field it replaced.

The field and the diagnostic that measures it read from one shared list,
so they cannot drift apart and tell you different stories.

**The verdict now has four states instead of two** — found in anchors,
found in label text, present-but-only-positionally-associable, and
genuinely absent — and each one names what was actually checked. The
"genuinely absent" wording now says outright that *both* places were
examined, and that a Teams-only calendar is expected to look exactly
like that.

Join URLs are single-use meeting credentials, so none of this is
logged. The new counters count and classify only, and the diagnostic's
examples stay host-and-path shape.

## The organiser reaches the session

v2.40.0 taught the extension to read the organiser off the calendar
label. It stopped there — the name was captured and then went nowhere.

It now travels with the recording. It is stored on the session, shown on
the Record tab, included in diagnostics exports, and — the reason it
matters — put at the head of the speaker-identification roster.

**This is the whole roster for a lot of people.** The v2.40.0 speaker
roster is built from `attendees`, and an extension-sourced calendar has
no attendee list at all: Outlook Web's grid label carries the organiser,
while the attendee list lives one click deeper in the event detail pane.
So for anyone whose calendar comes from the browser extension,
`attendees` is empty on every session and the roster was inert. The
organiser is the one invite-derived name that path can supply, and it
goes in first, since whoever called the meeting is the likeliest person
to be both present and speaking.

Both ways of starting a recording pick it up — the **Use** button and
the brief-first modal on the Record tab, plus auto-record on the backend
side. A recording that was never started from a calendar entry has no
organiser, which reads as empty and behaves exactly as it did before.

### The organiser can be an email address

Outlook Web writes the literal SMTP address into the label whenever it
has no display name to render. That was being mangled: the
surname-first rule glued the following segment onto it, producing an
address followed by a room name — something that resolves to nobody.
An address is now recognised and kept exactly as written, casing
intact.

Room and distribution-list names got the same guard, so
`all-hands-dl, Umbrella HQ Room 3` stays two things rather than being
merged into one. The awkward forms that already worked —
`Roe, Pat Jr. [US-EMEA]`, double spaces, non-ASCII diacritics — now
have tests pinning them so they keep working.

## Follow-up drafts understand an address

Once the organiser can be an address, it reaches recipient resolution,
which had no notion of one — no `@` anywhere in the module. Three of its
assumptions were wrong for an address, and all three are now fixed:

- **It was being title-cased.** A string with no capitals is treated as
  a lowercased alias key and title-cased for display, turning
  `a.doe@…` into `A.doe@…`. The local part of an address is formally
  case-sensitive, so a mail server is entitled to treat those as
  different mailboxes.
- **It was ranked last.** Candidate name forms are ordered by token
  count, because a two-token name resolves against a corporate address
  book where a bare first name does not. An address is one token, so it
  sorted *below* every inferred name — the guess would have been tried
  against the directory ahead of the address the invite stated outright.
- **It reported itself unaddressed.** The draft was addressed in fact,
  and still counted among the "9 of 10 need an email address" that
  v2.38.0's message exists to report honestly.

An address now short-circuits the lookup entirely: there is nothing a
directory can add, and the run reports that no lookup was performed
rather than implying one happened. The test for what counts as an
address is deliberately strict — `sam@localhost` and `ask @sam about it`
are not addresses — because a false positive here would skip the
directory and put a non-address straight into a To: field.

## Tests

1254 backend tests, up from 1204, and 90 extension tests, up from 63.

The extension tests pin that a label with no URL produces a
byte-identical event to before, that date resolution and organiser
extraction survive a URL being inserted into every field, that a
non-conferencing link can never become a Join button, and that no URL
fragment can reach the diagnostic's output. The backend tests cover the
organiser on both start paths, a session saved before this field existed
loading unchanged, and each of the three address defects above.

Security scanning was run against the baselines **before** merging
rather than after: bandit 185 findings / 0 new, semgrep 3 / 0 new,
personal-data 0. The last three releases each turned Security Scan red
because that check was skipped.
