# v2.82.5 — One person, one line in the live transcript

## Install (macOS)

> v2.82.5 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.82.5_universal.zip`.
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
> unzip -o Meeting.Recorder_2.82.5_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.82.5_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.24.0**.

## The same sentence no longer shows up as two people

The live transcript regularly showed one sentence twice, at the same
moment — once under **You** and once under **Speaker N** — on headsets
and on speakers alike. It read as two people talking over each other.

The app records your microphone and the call's audio as two separate
streams and transcribes each on its own. When the same speech reaches
both, it was transcribed twice:

- **On speakers**, the other participants play out loud and your
  microphone picks them up, so their sentence also appeared as yours.
- **On a headset** with sidetone or "Listen to this device" turned on,
  your own voice is fed into the call audio, so your sentence also
  appeared as someone else's.

Now, when a sentence turns up in both streams within a few seconds, the
app keeps the copy from the stream it really came from and drops the
other. The copy that leaked is always much quieter than that stream
usually is, which is how the app tells them apart. If the copy that
leaked was already on screen, it's removed when the real one arrives.

It stays out of the way of real conversation: short replies ("yeah",
"okay") are never touched, since two people really do say those at
once; and when the two copies are about equally loud — which is two
people saying similar things, not a leak — both stay.

## One person no longer splits into Speaker 1 and Speaker 2

Separately, the live view could split one person on the call across two
labels for the whole meeting. Two causes, both fixed:

- **A new speaker was created from one noisy moment and never merged
  back.** The app now notices when two labels have settled on the same
  voice and merges them — and every line already shown under the second
  label moves to the first.
- **A saved voice alternated between its name and a number.** A saved
  speaker's name is only attached on longer stretches of speech; their
  shorter remarks were matched separately and came out as "Speaker N".
  Now, once the app has recognised the voice, shorter remarks from the
  same person get the name too, and if they had been showing as
  "Speaker N" until then, those lines take the name as well.

Two different saved names are never merged, and two genuinely different
voices stay separate.

Both fixes are to the live view during the meeting. The transcript
saved after processing is produced separately, from the whole recording,
and never contained these live duplicates.
