# v2.37.0 — follow-up drafting stops blaming the model for a format it couldn't read

## Install (macOS)

> v2.37.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.37.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.37.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.37.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## "Nobody was attributed" usually meant "I couldn't read the format"

Clicking **Draft follow-up emails** on a meeting that plainly assigned
work to named people returned:

> No owner-attributed action items to draft from — Claude didn't
> attribute any items to a specific person

That blamed the model for a parsing failure. Owners were read with a
regex that demanded exactly one shape — `- [ ] **[Owner]**: task`. An em
dash instead of a colon, a table row, a missing bold, a numbered list —
any of those and every line became ownerless, reported as though nobody
had been assigned anything.

It's the same mistake this app has made in several costumes: **something
you couldn't read rendering as something that isn't there.**

## Owners now come from the commitments the app already extracted

Commitments carry a real owner field, resolved through the same alias
grouping that collapses "Sam", "Sam Doe" and "Samantha" into one person.
That's proper structured data, not a pattern-match over generated prose
— so a name written two ways no longer splits into two emails.

Only open commitments are used; something already delivered doesn't earn
a chaser. Where a commitment carries an extracted due date, it goes into
the draft as extracted data rather than as something invented.

If a session has no commitments — it predates the feature, extraction
never ran, or the meeting simply produced none — drafting falls back to
reading the action items, and every one of those cases degrades quietly
instead of failing.

## The fallback reads what the model actually writes

The markdown parser now accepts bold or plain owners, bracketed or not,
separated by a colon or an em dash, in bullets, numbered lists, nested
items, with or without checkboxes, and in tables — including tables that
put the task before the owner.

Two things stop it becoming too eager. An owner that the model *marked*
— bolded or bracketed — is trusted as written, because the model told us
which part is the name. An unmarked one has to actually look like a
person: a few capitalised words, no leading verb, nothing shaped like a
topic label. So `Send the report: include Q3 numbers` and `Next steps:`
stay descriptions, while `Sam (Acme)`, `Roe, Pat Jr. [US-EMEA]` and
`Ana van der Noh` parse correctly.

Parsing is also confined to the action-items section when one exists, so
bullets under Decisions or Open Questions can't be mistaken for
somebody's task.

## Three different problems, three different messages

They used to produce the same misleading sentence:

- **No action items yet** — extract them first, then draft.
- **Action items exist but none could be read** — says plainly that it's
  a formatting problem, not a missing-owner one, and that re-running
  extraction usually fixes it.
- **Every item belongs to the whole group** — "team", "all", "everyone".
  There's no individual to address, and that's now stated as such.

When parsing fails, the log records the *shape* of the first couple of
unreadable lines rather than their content. A line reading
`- [ ] Jane Doe — Send the Q3 deck` is logged as `- [ ] Aa Aa — Aa a A9 a`
— enough to see the deviation, with no meeting content and no names.

## One parser, not two

Windows and macOS each carried their own copy. Both now import a single
shared implementation, and a test asserts they resolve to the same
function and that the old one hasn't reappeared in either file.

## Tests

1054 backend tests, up from 974. The 80 new ones cover every accepted
format, the ambiguity boundary in both directions, commitments being
preferred when present, each fallback path when they aren't, all three
empty states, generic owners still being skipped, and the shape-only
logger keeping every word of the original line out of the log.
