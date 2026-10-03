# v2.86.0 — A new look, voices learned from transcripts, slide-by-slide notes, and "What I missed"

## Install (macOS)

> v2.86.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.86.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.86.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.86.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## A new look

The app has a proper visual identity now instead of default styling:

- A **deep teal-navy sidebar** with a clear marker on the page you're on.
- **Inter** throughout, with bold page titles.
- Crisp white fields and dropdowns instead of grey pills, and buttons with
  a consistent shape and weight.
- A **Today** page that opens on a greeting panel rather than a heading on
  grey.
- **Client marks** on every meeting in Sessions: each client gets its own
  colour and initials, so a long list scans by client.
- Meeting tabs underline the one you're on; transcripts read in the same
  font as everything else (timestamps stay aligned).
- **Violet marks what the AI wrote** — the "What I missed" brief stands
  apart from what was actually said.

## Voices learned from a meeting's transcript

Import a meeting together with its Teams or Zoom transcript and the app
now learns each named person's voice from it — the same as naming them
yourself in the Speakers tab, for everyone at once. The next call with
those people names them automatically, live and after processing.

A transcript's clock doesn't always match the recording's, and a voice
saved under the wrong name would label the wrong person on every future
call, so nothing is saved unless two checks pass:

- **The timing matches.** A few of the transcript's lines are heard again
  from the recording at the transcript's own timestamps and the words
  compared. A transcript out by two seconds or more, or by a whole line,
  is caught.
- **Each name is one voice.** Every line has to sound like the rest of
  that person's lines. A name shared by a room full of people isn't
  saved.

If a voice you've already saved turns up under a different spelling
(the roster's "Doe, Jane" for your "Jane"), the saved one is improved
rather than duplicated. The Speakers tab says which voices were learned,
or why none were.

## Slide-by-slide notes

For a meeting imported from video, each slide or shared screen is now
paired with what was said while it was on screen, with the points,
questions and actions raised on it. They appear beside each slide in
the meeting's **Screenshots** tab and are exported as
`slide_notes_<meeting>.md` with the meeting's other files.

## What I missed

A short brief for a meeting you weren't in: the headline, what was
decided, what's **asked of you or your team**, what's still open, what
changed since your last meeting with that client, and a few moments
worth listening to yourself. It's written automatically for imported
meetings and appears at the top of the meeting's **Overview**; for any
other meeting, click **Catch me up**. Exported as
`what_i_missed_<meeting>.md`.

"You or your team" comes from the email address in Settings (follow-up
email) — only its domain is used.

