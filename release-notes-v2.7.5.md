# v2.7.5 — Live Co-Pilot (beta)

Feature release. Adds an in-call coaching panel that watches the live
transcript and surfaces three short bullet lists every ~45 seconds:
clarifying questions you should ask, risks & assumptions worth
flagging, and concrete next-step suggestions. All v2.7.4 fixes are
included.

## Install (macOS)

> v2.7.5 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.5_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.5_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.5_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## New since v2.7.4

- **Live Co-Pilot (beta).** A new panel appears under the live
  transcript while you're recording. Every ~45 seconds it sends the
  last ~10 minutes of transcript to the configured LLM (default
  Anthropic Haiku) and surfaces three short lists, ≤ 3 bullets each:
    * **Clarifying questions** to ask now to fill gaps.
    * **Risks & assumptions** that haven't been acknowledged.
    * **Suggested follow-ups** — concrete next steps.

  Refresh-now and pause buttons let you steer the cadence. The panel
  disappears when you stop the recording.

## How to turn it on

The co-pilot is **opt-in** — it costs an LLM call per tick (~$0.10–
$0.20 per hour of meeting on Anthropic Haiku), so you flip it on
yourself when you want it.

1. **Settings → Workflow → Live Co-Pilot (beta)** → on → Save.
2. Start a recording. The panel appears under the live transcript and
   populates within a few seconds.

Requires **Live transcription** to also be on (it reads from the same
segment stream). It uses your existing main LLM provider + key —
nothing new to configure.

## Coming next

The next release will add a separate "Live model" config so the
co-pilot can run on **local Ollama** (or any free OpenRouter model)
while post-meeting summaries stay on Haiku — dropping the live side
to $0.

## Everything from v2.7.4 and earlier

The auto-record-actually-records fixes, the persistent
**● Recording…** badge, the auto-record skip notification when no mic
is configured, the Engagements layer (per-client register +
hand-editable Excel export), the Python-3.12 installer bootstrap fix,
and calendar auto-record for every timed meeting.

## Notes

- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
