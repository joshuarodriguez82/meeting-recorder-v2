# v2.87.1 — Fewer phantom speakers, your own sentences kept whole, and AI-guessed names no longer overwrite saved voices

## Install (macOS)

> v2.87.1 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.87.1_universal.zip`.
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
> unzip -o Meeting.Recorder_2.87.1_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.87.1_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Speakers on calls where your mic hears the room

The app uses which device heard each sound — your mic or the computer's
call audio — to decide which words are yours. When your mic also hears
the other people (laptop speakers, or people in the room with you), that
shortcut is wrong, and a check is meant to switch it off. That check
could miss: on such a call, other people's words were labelled **You**,
alternating word by word with the real speaker, and a four-person call
showed eight speakers.

Now, when most of what would be labelled yours happened while someone on
the call was talking, the app separates speakers by voice alone for that
meeting.

## Your sentences stay in one piece

On calls where you were labelled correctly, the short pauses between
your words could keep another person's name, so single words of your
sentences appeared under someone who barely spoke. A pause inside your
own speech now stays yours, unless the call audio shows someone else
talking at that moment.

## AI-guessed names no longer overwrite saved voices

After processing, the AI names speakers from what was said ("I went to
Sam" names someone, not necessarily the next voice). Those guesses were
saved as voices, and one could rename a person you'd already saved or
blend a different voice into theirs — which is how a wrong name spread
to later meetings.

A guessed name now labels that meeting. It's added to a saved voice only
when it agrees with the voice match, or the voice actually sounds like
the saved person with that name.

**Worth doing once after updating:** in **Speakers**, delete saved voices
created from recent meetings that keep matching the wrong people. They
were built from mixed audio and won't fix themselves. Then re-process a
recent meeting that came out wrong to see the new result.
