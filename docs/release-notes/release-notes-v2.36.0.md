# v2.36.0 — one rule against invented precision, applied everywhere

## Install (macOS)

> v2.36.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.36.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.36.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.36.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

The Chrome extension is unchanged at **1.3.3**.

## The real defect was never about dates

v2.35.1 stopped summaries inventing years. It worked — but the very
next summary produced three more fabrications of the same kind:

- *"She identified **seven** candidate intents"*, followed by a list of
  **six**. The source contained six.
- An action item given the target *"By end of meeting"*. The source
  gave that item no timing at all.
- A demo scheduled for a specific named week. The source said only
  "this week and next week".

The failure was never dates specifically. It is **adding precision the
source did not carry** — and a date rule only ever catches the date
version of it.

There is now one rule, written once and shared by every prompt in the
application:

> **Write only what the source material in front of you actually
> carries. Never add precision the source did not have.**
>
> - **Counts and quantities.** Never state a number that isn't stated
>   in the source or literally countable from it. If you enumerate a
>   list, any count must equal the number of items you actually wrote.
>   Prefer omitting a count to guessing one.
> - **Timings and deadlines.** If the source gave no timing, say the
>   timing was not specified. Never manufacture a deadline, and never
>   sharpen a vague one: "next week" must not become a specific date.
> - **Identifiers and specifics.** Never introduce a name, number,
>   system, product, version or identifier that appears nowhere in the
>   source.
> - **Attribution.** Never attribute a statement, question, decision or
>   action to someone not shown saying it.
>
> This applies to section headings, table cells and JSON fields exactly
> as it applies to prose. **An unqualified or absent detail is correct;
> an invented one is a factual error the reader cannot catch.**

## Where it applies

Everywhere something is generated: summaries, action items, decisions,
requirements, structured extraction, both prep-brief builders, speaker
identification, the daily-briefing parse, Knowledge Base answers,
in-call search answers, commitment extraction, client-tagging
suggestions, the live co-pilot, and follow-up emails.

Two places got a deliberately different treatment:

**Speaker identification** receives only the identifiers and
attribution clauses. Its output is a mapping of labels to names — there
is no list to miscount and no timing anywhere in it, so the other
clauses would describe surfaces it cannot produce.

**The live co-pilot** receives a compressed form. It fires every few
seconds against a tight token budget during your call; the full block
would cost latency for rules its two-bullet output can't violate.

The date anchoring from v2.35.1 is unchanged and still applies
alongside it — that half resolves "come October" against the meeting
date, which the general rule can't do on its own.

## Follow-up emails were completely broken

Drafting a follow-up email failed every time, on Windows and macOS,
for every provider. The code called an attribute on the summarizer that
has never existed, so the feature raised an error the moment you
clicked it.

It now routes through the app's provider-agnostic path — which also
means follow-up drafting works for OpenAI-compatible providers for the
first time, something the original code could never have done.

This is also the one artifact the app produces that leaves your machine
addressed to somebody else, which is why it gets the full rule. **A
fabricated deadline in a follow-up email is a commitment the recipient
now believes they made.**

## The rule cannot quietly drift apart

The wording lives in exactly one file. Every prompt references it
rather than carrying a copy, and a test asserts that each clause exists
in exactly **one** source file, comparing with whitespace normalised so
a reflowed paste is still caught.

That test was itself verified by pasting a reformatted copy of one
clause into another module and confirming the suite went red.

New prompts are covered by default rather than by someone remembering.

## Tests

974 backend tests, up from 944. The 30 new ones assert on the literal
instruction text each prompt produces, so no model call is needed. They
were confirmed load-bearing by reverting the change: 23 of the 30 fail
without it, and the 7 that survive are the anti-drift checks, which
test the shared rule directly.
