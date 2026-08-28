# v2.71.0 — two field bugs fixed, and a defect register

## Install (macOS)

> v2.71.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.71.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.71.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.71.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Two bugs you hit in v2.70.0

**Insights → Open Loops opened an empty window.** Clicking a Stale
Commitment or an Unchecked Follow-Up opened the session with its tabs
along the top — "Transcript (733)", "Speakers (9)" — and nothing below
them. Clicking an Un-implemented Decision worked, which made it look
random.

It wasn't. Each of those three links asks the session window to open on
a particular tab, and two of them named a tab that doesn't exist. When
that happens the window has no panel to show, so it shows nothing —
with no error to explain it. Commitments and follow-ups now open on
**Actions**, where they live. Separately, the window no longer trusts
the name it's given: anything it doesn't recognise falls back to
Overview, so a wrong name can never produce a blank screen again.

**"Show" beside a diagnostics export was refused.** Exporting
diagnostics and pressing Show returned *"Couldn't open the folder: Bad
Request: Path is outside the app's folders"* — the app refusing to
reveal a file it had just written, on the same screen that told you
where it was. v2.70.0 restricted folder-opening to your recordings and
client folders and left out the app's own data folder, which is where
diagnostics go. Fixed, without loosening anything else: folders outside
the app are still refused, and opening a folder still never creates one.

## Defect register for delivery work

UAT and defect triage is the highest-volume meeting of a delivery
engagement, and until now its output stayed inside one session's
summary. Defects are now tracked as records in their own right —
description, the customer's defect ID, severity, status, owner, target
date, and whether the scope question was settled — and rolled up across
every session into a single register, with its own sheet in the
engagement export.

Two things it does that a generic list would get wrong:

- **A reopened defect reads as open.** Fixed, failed retest, back to
  open is an ordinary week. Everything else in the register treats a
  finished item as finished forever, which is right for a requirement
  and wrong for a defect — so a defect's status and severity come from
  the **most recent** time it was discussed, not the best it ever was.
- **Rows merge on the defect ID.** Triage says "DEF-142" far more often
  than it restates the description, so two differently-worded mentions
  of the same ID are one row. Where no ID was spoken it falls back to
  matching the description.

Also: "awaiting retest" counts as **open**, because a fix nobody has
verified is still your team's work — and the register reports open
critical/high separately, which is the number you get asked for in
every status call.

Reprocess a session to populate it. Meetings with no defects produce an
empty register rather than inventing rows.

## Dropped audio is now visible

If the audio buffer overflowed *and* the recovery also failed, the
affected audio was discarded with no record anywhere — a recording with
a hole in it and nothing to explain the hole. Those losses are now
counted and reported with the other capture statistics at the end of
every recording, so a problem shows up as a number instead of as an
unexplained gap. Recording behaviour is otherwise unchanged.

Alongside it, eight other places in the recording path that were
failing silently now say what went wrong — including a WAV file that
fails to close cleanly, which is how a recording ends up truncated with
nothing in the log about it.
