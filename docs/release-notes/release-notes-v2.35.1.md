# v2.35.1 — summaries stop inventing dates and stop quoting the co-pilot as a participant

## Install (macOS)

> v2.35.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.35.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.35.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.35.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

Both fixes here came from one real summary that was wrong in two
different ways. Both are the kind of error a reader cannot catch,
because the summary reads as confidently as a correct one.

## Summaries invented a year that was never said

A speaker said "come October" — once, with no year, in a 43-minute
call. The summary asserted **"October 2024"** twice, including as a
section heading. The meeting was in August 2026, so the real deadline
was six weeks out; the summary described it as two years past.

The cause was an absence rather than a mistake: **no summarization or
extraction prompt carried the meeting's own date.** With no anchor, a
relative reference like "come October" has no year, and one gets
supplied from nowhere.

Every prompt built from a transcript now states the meeting's date and
weekday, and carries two rules that only work as a pair — resolve
relative references *forward* from that date, and never state a year,
month or day that isn't in the source or derivable from the anchor.
Resolution alone would invite the model to pin down dates it was
guessing at. The prohibition alone would leave "come October"
unresolvable.

An unqualified date is correct. An invented one is a factual error the
reader has no way to spot.

This applies to summaries, action items, decisions, requirements,
structured extraction and both prep-brief builders. A session with no
recorded start date produces exactly the prompt it produced before —
never an anchor on a date the app doesn't have.

## The co-pilot's suggestions were being reported as things people said

The live co-pilot generates clarifying questions and risk flags during a
call. They're the model's own suggestions, drafted from a partial
transcript — not a record of anything anyone said.

The summary listed four "open routing questions raised by co-pilot".
**One** corresponded to a question a participant actually asked. The
other three appeared nowhere in the transcript.

Two things caused it. The summarizer was told, in as many words, to
*"prefer the co-pilot's phrasing if it's clearer than the transcript
evidence"* — which is a direct instruction to promote generated text
over what was actually said. And the co-pilot's own notes were passed in
under headings reading "Clarifying questions **raised**" and "Risks
**flagged**", so the provenance had already been laundered before the
summarizer read a word.

Both are fixed, and the rule is now explicit: **every factual claim in a
summary must be traceable to the transcript.** Co-pilot output is
presented as what it is — machine-generated, not said by anyone — and
may only point the summarizer at parts of the transcript worth a closer
look. Where the transcript supports an item, the summary uses the
participants' wording, never the co-pilot's, and never carries over a
name, number, system or date that appears only in a generated line.

Anything the transcript doesn't support can't appear in the narrative,
the decisions, the questions raised, or the action items. If it's still
worth your time it goes at the very end, under a heading that names both
its author and what it isn't:

> **Suggested follow-ups (AI-generated — not raised in the meeting)**

Labelled rather than deleted, deliberately. The co-pilot exists because
it catches real things, and a suppressed-but-genuine risk helps nobody.
The heading is placed at the end, outside every section that reads as a
record of the meeting, so that even a forwarded excerpt can't be
misread.

## Tests

944 backend tests, up from 922. The 22 new ones assert on the literal
instruction text the prompt builders produce, so no model call is
needed. They were verified to be load-bearing by reverting the fix and
confirming 21 of the 22 fail.
