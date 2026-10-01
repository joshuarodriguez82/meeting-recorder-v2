# v2.84.0 — Co-Pilot follow-ups, live names for known voices, one-click speaker merges

## Install (macOS)

> v2.84.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.84.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.84.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.84.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Which Co-Pilot questions got answered — and what's left to send

The Co-Pilot's questions and follow-ups used to be a live list that was
gone in practice once the call ended. Now, when a meeting is processed,
each question and follow-up you didn't dismiss is checked against the
full transcript:

- **Answered** — with the answer and who gave it.
- **Partly answered** — what was covered and what wasn't.
- **Still open** — candidates to send the customer.

You'll find them in three places:

- **A new file in the meeting's folder,** `copilot_followups_<meeting>.md`,
  next to the summary and action items — still-open items first.
- **The meeting's Co-Pilot tab,** with a **Copy open items** button for
  pasting into a follow-up email.
- **The engagement register,** as a new **Co-Pilot Follow-ups** section
  (and sheet in the Excel export) across every meeting with that client.
  Something left open in one call and answered in a later one shows as
  answered, with the answer.

They're always labelled as AI suggestions, never as things anyone
agreed to. Older meetings get this when you reprocess them. The check
is one extra model call per meeting that reuses the transcript the
summary already sent, so it adds roughly a cent or two.

## Saved voices get their names during the call

The live transcript only tried to recognise a saved voice from one
2–3 second clip at a time, and held that to a very high bar — so people
whose voices you'd saved still showed as "Speaker 2" all call. Now, once
someone has spoken for about 8 seconds, their voice so far is compared
to your saved speakers, and a match renames them everywhere on screen.
If two live labels turn out to be the same saved voice, they become one.

## One person split into several speakers: one click

When a meeting ends with one voice split across several labels — say a
name and three "SPEAKER_n" entries — the Speakers tab used to offer every
pair separately: six cards for one decision. It now offers one card,
"… sound like the same person · **Merge all 4**". A label only joins the
group if it sounds like *every* other member, so two different people
are never pulled together through a third. Nothing merges without your
click.

On speakers rather than a headset, voices also reach the recording
through your microphone, which is the most common cause of these splits;
a headset avoids most of them.

## Smaller fixes

- **Co-Pilot cost estimate (Settings):** it priced Haiku 4.5 at a quarter
  of its real price, and showed no estimate at all for the model ID
  Settings actually stores. At 30-second intervals with the fast update
  on, the Co-Pilot costs about **$0.70 an hour** on Haiku 4.5.
- **Voice fingerprinting repairs itself** when its speech library is left
  half-installed by an interrupted update; if it can't, the Speakers tab
  now says so and that reinstalling fixes it.
- **Meeting passcodes** are no longer written to the app's log when you
  open a join link.
- **AI provider settings** only accept web addresses.
