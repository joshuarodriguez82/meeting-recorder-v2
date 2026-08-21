# v2.59.0 — the join link, without guessing at the button

## Install (macOS)

> v2.59.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.59.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.59.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.59.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Update the Chrome extension

This release needs extension **1.17.0**. Settings → Templates &
Integrations → **Install / Update extension files**, then
`chrome://extensions` → **Reload**. Confirm the card reads 1.17.0.

## Three releases guessed at the Join button. This one stops.

The history is worth stating plainly, because it is the whole lesson:

| Version | Assumed the Join control was… | Field result |
| --- | --- | --- |
| 1.10 | text in the invite body | no link on Teams |
| 1.15 | an anchor in the top document | no link |
| 1.16 | an anchor behind shadow roots / frames | no link |

Each fix was correct about *a* shape and wrong about *the* shape. The
markup Outlook ships cannot be observed from here, and guessing it a
fourth time is the definition of this bug's history.

So the shape is no longer part of the question. A join URL is a highly
specific string — the provider host and path patterns are already the
contract — and **if the meeting has one, that string is somewhere in
the markup the click produced**: an `href`, a `data-` attribute, an
`aria-label`, an iframe's `srcdoc`, or the inline JSON the card was
hydrated from. The capture now scans for provider-shaped URLs across
that markup and keeps only ones that were **not present before the
click** — the same safety rule the anchor scan already used, so a
neighbouring meeting's link still can never be attributed to this one.

Percent-encoded and HTML-escaped forms are decoded, because that is how
a URL arrives when it is living inside an attribute or a JSON blob.

Proven against a Join **button** with no anchor anywhere in the
document and the URL only in an attribute — the exact shape the last
three releases assumed away.

## The agenda is the invite now, not Outlook's UI around it

v2.58.0 crossed into the frames and the invite text finally arrived —
buried:

```
GG
<organizer> invited you.
Accepted 1, Didn't respond 5
Prepare for this meeting
□
Turning this to "Daily" 15min on the following…      ← the actual invite
□
Accepted
Change
What are key talking points?
Help me prepare for this meeting
Help me understand the risks
```

One of those lines is the invite. The rest is the RSVP control, the
attendee tally, and Copilot's suggested prompts.

The tempting fix — a list of UI phrases to drop — is the exact mistake
the attendee scanner made in 1.11, where a blacklist of interface
vocabulary produced "Attendees (24)" of which 22 were buttons. A
product's UI vocabulary cannot be enumerated across every language and
redesign.

The structural fact is better: Outlook renders the invite body in its
**own frame**. So the agenda is not "page text minus things that look
like chrome" — it **is** the subframe's text, and Outlook's UI is by
definition not in it. Whole-page diffing remains the fallback for
tenants that render the body inline.

**And the boxes are gone.** Those `□` characters are Outlook's toolbar
icons: a private-use font, where each icon is a Unicode Private Use
Area character that `innerText` returns and every font renders as a
hollow box. Nothing downstream — the panel, the LLM, a follow-up email
— has any use for them. They are stripped, along with the lines that
held nothing else.

## Portal sync is automatic (it already was)

Worth stating since it came up: you never have to press **Sync**. Every
time a session finishes processing, its engagement register is rebuilt,
and any project bound to a portal opportunity is pushed automatically
(`server.py`, the register-written hook). The button is a manual "push
now" for when you want it immediately; the automatic path does not
depend on it.

## Tests

1305 backend tests, 144 extension tests (up from 141). All three new
extension regressions verified to **fail against the shipped 1.16.0
build**: the button-with-attribute link, the escaped-JSON link, and the
rule that a link already on screen before the click is never
attributed. bandit 0 new, semgrep 0 new, personal-data 0.

## Installing no longer churns your export folder

Every install re-exported every session into the synced Drive folder,
so all of them jumped to the top of a Date-modified sort carrying the
install's timestamp. The bytes were identical — only the mtimes moved.

That is not cosmetic. "Date modified" is how you find what you were
last working on, and a sync client re-uploads every file it sees
touched, so an install cost a full folder re-sync and destroyed the
ordering that makes the folder usable.

An export writes what the session says. If the file already says
exactly that, the export is already done. All five exporters skip
identical content now; a real edit still writes.

## Decisions and requirements stop counting tasks and facts

One real discovery call produced **fourteen** "decisions". Read against
the transcript, about four were decisions actually made in the meeting.
The rest were tasks already listed as action items, a pre-existing
roadmap a participant merely reported, plain facts (a contract's expiry
date nobody decided), and restatements of entries already in the list.

Worse, the same meeting was extracted **twice** — the action-items pass
also produced a "Decisions Made" section while the decisions pass
independently extracted every decision. The two lists disagreed: 11 in
one file, 14 in the other, neither a subset of the other.

- Decisions now have **one** extractor and one file.
- The decisions prompt states what a decision is **not**: a task, a
  pre-existing agreement being reported, a fact or date, an unaccepted
  recommendation, or a restatement. It requires an **Evidence** quote
  of the moment the decision was made, and says plainly that a long
  list means miscounting.
- Requirements get the same treatment — 39 rows from one discovery
  call has the same cause.
