# v2.87.3 — Fewer phantom speakers on big calls, a live transcript that keeps time, and AI answers that aren't cut off

## Install (macOS)

> v2.87.3 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.87.3_universal.zip`.
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
> unzip -o Meeting.Recorder_2.87.3_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.87.3_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Fewer phantom speakers on big calls

On a long call with many people, short reactions — "yeah", "right",
"thank you", "so" — were sometimes grouped into speakers of their own,
because a one-word clip doesn't carry enough of a voice to place it.
A 12-person call listed 14 speakers that way.

- A group made only of short reactions (under 45 seconds, mostly one to
  three words a line) that sounds like someone who spoke properly is
  now folded into that person.
- One that doesn't clearly sound like anyone goes to **Unattributed**
  rather than becoming another person.
- Anyone who spoke in real sentences is left alone, however briefly.
  Nothing named, linked to a saved voice, or labelled as you is ever
  moved this way, and none of it changes your saved voices.

**Unattributed** (previously shown as "SPEAKER_UNKNOWN") is the lines
the app couldn't place on anyone. It's now labelled that way, listed
last, and no longer counted as a speaker.

To apply this to a meeting you've already recorded, re-process it.

## Live transcript keeps time

The live transcript's clock stopped during long pauses (8 seconds or
more of silence on a stream), so later lines were stamped early — by
the end of a 2-hour call, your lines were about 40 minutes early. That
also meant:

- the v2.87.2 check that hides the call's echo from your mic was
  comparing each mic line against the wrong moment of the call, so it
  caught far less than it should;
- Co-Pilot, which reads the last few minutes, stopped seeing what you'd
  said.

Both work as intended now. The transcript made after the call was never
affected.

## AI answers no longer cut off on Claude Haiku 5.5

Haiku 5.5 thinks before it answers, and that thinking shares the same
length limit as the answer. Some outputs came back cut off — a daily
briefing stopped partway through, and a Co-Pilot suggestion was lost
mid-reply. Models that think now get extra room from the first request.
You're only charged for what the model actually writes, so the extra
room costs nothing unless it's used.
