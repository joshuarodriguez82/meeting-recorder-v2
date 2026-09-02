# v2.80.0 — transcription and speaker accuracy

## Install (macOS)

> v2.80.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.80.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.80.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.80.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.23.0**.

## Meetings are no longer assumed to be in English

Transcription was hardcoded to English. Not defaulted — hardcoded, in
both the live preview and the transcript written to disk, with no
setting anywhere.

This does not fail loudly. Whisper told to expect English will decode
Italian into fluent, confident, wrong English, and every summary,
action item and search result built on that transcript inherits it.

**Settings → AI Models → Spoken language.** Pick your usual language,
or **Detect automatically** if your meetings vary. English stays the
default, so nothing changes unless you change it.

## Speakers are attributed by the word, not by the paragraph

Whisper produces segments several seconds long. When a segment spans a
hand-off —

> "...so we'll take that away. Actually, hold on, that's not right."

— the whole thing used to go to whoever was talking longest. The other
person's words were put in their mouth.

Because the transcript feeds the summary, the action items and the
commitments, that meant the wrong person got assigned the follow-up.

Segments that span a hand-off are now split at the word where the
speaker changes. Meetings recorded before this release keep their
existing attribution; the improvement applies to new recordings and to
anything you re-process.

## Your glossary now reaches the live transcript

The glossary biased the transcript written to disk toward your product
and customer names, and did not reach the live preview at all. So the
transcript you *watched* mis-heard exactly the terms the glossary
exists to fix, while the saved one got them right. Both paths now use
it.

## Speaker identification recovers instead of failing the meeting

If speaker identification failed on the GPU part-way through — an
out-of-memory card on a long meeting, or a Mac GPU hitting an operation
it has no support for — the whole processing run died with it. The
app's answer was a Settings dropdown asking you to switch to CPU by
hand, after the crash.

It now retries once on the CPU automatically. Slower for that one
meeting, and it finishes.

## Faster, more accurate models in the picker

The model list offered five tiers and defaulted to the weakest one.
Added:

- **turbo** — close to the top tier's accuracy for a fraction of the
  time. On a Mac this is the practical choice for a large model.
- **large-v3** is what "large" always meant; it now says so.

## Under the hood

Repeated phrases in long silences should be rarer: the decoder setting
that causes them was at its default, which is tuned for continuous
speech rather than meetings full of pauses.

Speaker fingerprinting used to load the entire recording into memory —
about 690 MB for an hour — immediately after the speaker-identification
pass. It now reads only the seconds it needs.

Both transcription paths draw their settings from one place. They had
duplicated them by hand, which is how the language ended up hardcoded
in both and the glossary in only one.
