# v2.87.2 — The live transcript stops putting the call under "You", and Co-Pilot stops re-asking the same question

## Install (macOS)

> v2.87.2 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.87.2_universal.zip`.
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
> unzip -o Meeting.Recorder_2.87.2_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.87.2_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Live transcript: the call no longer shows as "You"

On laptop speakers your mic also hears everyone on the call. Muting
yourself in Teams or Zoom doesn't change that: it stops the call
sending your voice, but the app records the mic directly. The live
transcript was meant to drop the mic's copy of what it heard from the
call, but when you were quiet or muted it couldn't tell which copy was
the echo, so it kept both, and other people's words showed under
**You** while you were on mute.

Now the app watches when the call audio is playing. Once most of what
your mic hears in a meeting is landing on top of the call audio, the
mic's copy of that speech is no longer shown; the call's own audio
already transcribes it. When you speak while the others are quiet, you
still show as **You**.

This changes the live view only. The transcript produced after the call
was fixed in v2.87.1.

## Co-Pilot stops asking the same question in new words

Co-Pilot folded a repeated suggestion into the existing one only when
it used mostly the same words, so a question re-asked with the words
changed showed up again — on a long call about one topic, the list
filled with versions of the same few questions.

Co-Pilot now also compares what suggestions mean, using the small
language model the app already uses for search. A reworded repeat bumps
the existing entry instead of adding a new one, and a reworded version
of something you dismissed stays dismissed. This only applies when that
model is already on your computer (it is if search has run); Co-Pilot
never downloads it mid-call, and works as before without it.
