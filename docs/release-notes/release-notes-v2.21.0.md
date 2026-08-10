# v2.21.0 — the live transcript keeps up, and tells speakers apart

> Text now appears about a second after someone stops talking instead of
> up to fifteen — and the other people on the call are split into
> individual speakers, by name where the app already knows their voice.

## Install (macOS)

> v2.21.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.21.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.21.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.21.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Live text, about a second behind

The live transcript used to work in fixed fifteen-second blocks. Text
could not appear sooner than that, no matter how short the sentence.

It now listens for the natural pause at the end of what someone says
and transcribes that. In practice text lands **under a second after
someone stops talking**, plus the moment it takes to transcribe.

Someone talking without pausing is flushed every eight seconds, so a
monologue never stalls.

## Everyone else gets their own name

Until now everything coming from the call was labelled "them" —
one bucket, no matter how many people were talking.

Each thing said is now matched against the voices heard so far in the
call, so you get **Speaker 1**, **Speaker 2**, and so on as they take
turns. When a voice matches someone you've already named in a previous
meeting, you get **their actual name** rather than a number.

Your own microphone is still labelled instantly and correctly, as
before — the app captures you and the call separately, so it never has
to guess about you.

Very short interjections — "yeah", "mm-hm" — stay attached to whoever
was already speaking. Too short to identify reliably, and inventing a
new speaker for each one is worse than being briefly stale.

## If anything goes wrong, the transcript keeps running

This sits in the middle of live recording, so every part of it fails
soft:

- If speech detection has a problem, that audio source quietly reverts
  to the old fifteen-second behaviour for the rest of the recording.
- If speaker identification fails, that line falls back to "them" and
  nothing else is affected.
- Without the voice-identification model installed, everything works
  exactly as it did before.

The live transcript can never end up less reliable than it was.

You can turn the faster mode off in **Settings → Recording & Co-Pilot →
Fast live transcript** if you'd rather have the old behaviour.

## This doesn't change your saved transcript

The transcript written when you press Stop is produced the same way it
always has been, over the whole recording, with full speaker
diarization. That remains the accurate one. This release is about the
live preview keeping up while you're still in the meeting.

## Under the hood

- New speech detector is plain numpy — no new dependencies, nothing
  extra to install.
- Voice matching runs on the existing transcription worker, so there's
  still exactly one transcription happening at a time.
- 274 backend tests (34 new), including one for a continuous talker with
  no pauses at all — a case that would otherwise have been mistaken for
  silence and transcribed as nothing.
