# v2.7.7 — Co-Pilot polish: in-bar toggle, scrolling history, persisted in the session

> v2.7.6 was a bad cut — the tag was pushed at a commit that
> predated the version bump, so the Windows installers shipped with
> `2.7.4_*` filenames and the actual Co-Pilot improvements weren't
> in the binary. **Install this release instead.**

Follow-up to the Live Co-Pilot (beta) shipped in v2.7.5 with the
things that actually make it usable across a real meeting.

## Install (macOS)

> v2.7.7 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.7_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.7_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.7_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## New since v2.7.5

- **In-bar Co-Pilot toggle.** A Co-Pilot Switch now lives in the
  recording bar next to Screenshot / Stop. Flip it on or off mid-call
  without leaving the page — safe to toggle while a recording is
  in progress.
- **Scrolling tick history.** The Co-Pilot panel now keeps every tick
  (newest first) in a scrolling container instead of overwriting the
  previous one. Each tick shows its timestamp and segment count.
  Scroll back to see every coaching pass the model made during the
  call.
- **Persisted with the session.** Every tick is also saved onto the
  recording, just like screenshots. After the meeting ends, open the
  session and use the new **Co-Pilot** tab to read the full coaching
  record alongside the transcript, summary, and screenshots.
- **Optional cheap model for live ticks.** Settings → Live Co-Pilot
  model lets you route the 45-second tick calls to **Ollama** (local,
  $0) or a free **OpenRouter** model while post-meeting summaries
  stay on your main provider. Drops the live side to $0/hr.

## Quality-of-life fixes

- **Known speakers list in Settings is now scrollable.** Capped at a
  fixed height so the rest of the Settings cards stay visible once
  you've accumulated a few dozen profiles across meetings.
- **Usage Guide updated.** New "Live Co-Pilot (beta)" section
  documents the panel, the two ways to enable it, the cost guidance,
  and the Ollama / OpenRouter override walkthrough.

## Everything from v2.7.5 and earlier

The Live Co-Pilot (beta) itself, separate live-model config (Phase B),
the auto-record-actually-records fixes, the persistent **● Recording…**
badge, the Engagements layer (per-client register + Excel export),
the Python-3.12 installer bootstrap fix, and calendar auto-record for
every timed meeting.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
