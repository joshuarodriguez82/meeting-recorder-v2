# v2.85.0 — Import meetings you weren't in: videos, slides and Teams / Zoom transcripts

## Install (macOS)

> v2.85.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.85.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.85.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.85.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Import a meeting someone else recorded

**Sessions → Import Recording**, or drop files anywhere on the Sessions
tab. A Teams or Zoom video (`.mp4`, `.mov`, `.mkv`, `.webm` and more)
or an audio file becomes a session like any other — name it, tag the
client and project, pick the summary template, and it is transcribed,
summarized and exported the same way as a meeting you recorded, using
the speech models already in the app. Nothing is uploaded anywhere.

- **Only the sound track is kept.** The video itself isn't copied into
  your recordings folder, and the original file stays where it is. An
  hour-long video's audio is extracted in seconds.
- **The real meeting date** is taken from the video when it records
  one, and the meeting shows its real length.
- A video with **no sound** is caught at import with a clear message,
  instead of failing later.

Video imports could be loaded before, but processing then failed at the
speaker step: the audio was never converted, and the speaker
separation can't read a video file. That is fixed for every format.

## Slides and shared screens from the video

When an imported video has a shared screen, a still of each slide or
screen change is kept and added to the meeting's **Screenshots** tab —
and, like screenshots you take while recording, the summary reads them
alongside the transcript. A slide needs to stay up for roughly ten
seconds to be kept, and gallery-view video with nothing shared adds
none. When a meeting has more than eight screenshots, the summary now
looks at an even spread across the meeting instead of the first eight.

## Real names from the Teams / Zoom transcript

Add the meeting's transcript download — Teams or Zoom `.vtt`, `.srt`,
or the Teams **Download as .docx** — and every speaker gets their real
name from the meeting itself instead of "Speaker 1 / Speaker 2". It
also skips transcription, so the summary is ready sooner. A transcript
can be imported on its own when there is no recording.

A transcript that doesn't say who is speaking is set aside when there
is a recording, so the app's own voice separation is used instead.

## Several meetings at once

Pick or drop several files together. A video and a transcript with the
same file name ("Weekly sync.mp4" and "Weekly sync.vtt") are paired
into one meeting; everything else becomes its own. Client, project and
template apply to all of them, each can be renamed, and they process
one after another in the background.

## Smaller fixes

- Running the backend test suite on a developer machine no longer
  overwrites that machine's saved settings.
