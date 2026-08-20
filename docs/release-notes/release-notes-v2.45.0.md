# v2.45.0 — two independent ways to get the detail, and cancelled meetings disappear

## Install (macOS)

> v2.45.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.45.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.45.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.45.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.9.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.9.0.

## Stop betting on one mechanism at a time

Five releases each tried exactly one way to get attendees, invite
bodies and Teams join links, and each depended on something this
project cannot observe from where it is built:

| | Depended on | Outcome |
|---|---|---|
| 1.5 | the join link being in the grid label | worked — for the 1 of 25 labels that had one |
| 1.6 | guessing the right API endpoint | wrong stack entirely; nothing worked |
| 1.7 | Outlook fetching through the main world | unproven — a service worker defeats it |
| 1.8 | — | made the failure modes distinguishable |

An endpoint, an auth scheme, a JSON shape, which thread issues a
fetch. Every one of those is invisible from here, and every wrong
guess cost a full release → reinstall → re-run cycle.

**This release adds a second, independent mechanism that depends on
none of them: what is actually on the screen.**

When you click an event, Outlook *renders* the attendees, the agenda
and the join link — that is what you are looking at. Reading the
rendered result cannot be defeated by a service worker, a bearer
token, a tenant migration or an API version, because by the time it is
on screen all of that has already happened.

It is slower, and it touches the DOM. Both are why it was passed over
four releases ago in favour of the API route. That judgement was
wrong: "cleaner" is worth nothing next to "works".

### It reads content, not markup

This does **not** target selectors inside the detail pane — that would
be one more guess about markup nobody here can see. It snapshots the
page's visible text, clicks, waits for the text to grow, and reads
what is new:

- **Attendees** — by email-address pattern. An address is an address
  in any markup.
- **Join link** — through the same host-and-path provider list the
  label extractor and the diagnostic already share.
- **Agenda** — the remaining new text, with addresses and links
  removed so it does not restate the attendee list.

None of that depends on a class name, a role, or a DOM shape.

### Bounded, and honest about what it skipped

Only events that are **still missing detail** after the first
mechanism, only those inside a 72-hour window, capped at 25 per
capture and under a 90-second budget. A working recorder therefore
costs nothing here, and the two together fail only if **both** fail.

Whatever it does not reach is reported as skipped rather than quietly
left empty. A pane that renders but yields no address, no link and no
text records nothing at all — an empty attendee list stored here would
be indistinguishable from a meeting that genuinely has none.

## Cancelled meetings no longer sit on the Record tab

A cancelled call rendered struck-through and **CANCELLED** on the
Today screen while the *same meeting* sat in Upcoming Meetings on
Record as a live, recordable row — and it was the next thing
auto-record would have fired on.

Two paths, one of which knew. Today reads the briefing agenda, whose
`status` field has always been honoured. The structured path the
Record tab uses never checked.

It was worse than a missing check. `Canceled:` was being stripped from
the subject as *noise*, in the same list as `RE:` and `FW:` — correct
for deduplication, since they are the same meeting wearing a different
subject line, and catastrophic on its own: it scrubbed the subject
clean, so the cancelled meeting became indistinguishable from a live
one.

Cancellation is now detected **before** the subject is normalised, and
the meeting is dropped — matching the rule the briefing path already
stated.

**Deliberately narrower than the strip list.** *Accepted*, *Declined*
and *Tentative* are response states: the meeting is still happening
and you may still want it recorded. Only cancellation removes the
meeting itself. And the word has to be a status prefix, not a word in
the title — a meeting called *"Cancellation policy review"* is a real
meeting, and dropping it would be a worse failure than showing a
cancelled one.

## What this release still does not claim

It may still not fill the fields. If Outlook renders its detail pane
somewhere the text snapshot cannot see, this mechanism fails too.

The difference is that there are now two independent attempts per
capture instead of one, they fail for unrelated reasons, and the
diagnostics report each separately — so a bug report says which one
ran, which one worked, and whether both were even tried.

## Tests

1264 backend tests, up from 1260, and 118 extension tests, up from 112.

The new extension tests cover attendees read as addresses with no
markup dependence, a Teams join link recognised while an incidental
link is not, a pane that never renders yielding nothing rather than
garbage, an event with no matching tile counted as skipped, the
per-run cap reporting its remainder, and the screen-derived detail
merging additively like every other source. The backend tests cover
both spellings of cancellation, response states *not* being treated as
cancellations, and a meeting merely *about* a cancellation surviving.

One bug was caught by these tests in this release's own code: the
"did the pane render" check required 40 new characters, which silently
discarded a sparse invite — one attendee, no agenda — as though
nothing had rendered.

Security scanning run against the baselines before merge: bandit 185
findings / 0 new, semgrep 3 / 0 new, personal-data 0.
