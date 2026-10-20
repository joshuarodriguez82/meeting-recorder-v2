# v2.88.0 — Recap the call so far, and take notes during the call

## Install (macOS)

> v2.88.0 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.88.0_universal.zip`.
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
> unzip -o Meeting.Recorder_2.88.0_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.88.0_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## No extension update

App-only. The Chrome extension stays at **1.25.0**.

## Recap button

A new **Recap** button on the recording bar, between **Screenshot** and
**Stop**. Press it at any point in the call and the app sends everything
the live transcript has caught so far — every line, from the start — to
your AI model and shows a short recap just under the bar:

- **Discussed**
- **Decided** — agreed on the call, not just proposed
- **Action items** — with an owner and date only where someone said one
- **Open questions**

It's headed **Recap as of 42:15** so everyone knows how far it goes.
**Copy** puts it on the clipboard (e.g. to paste into the meeting chat),
**Refresh** recaps again up to now, and the newest replaces the last.
Pressing it near the end of the call gives you something to read out
and agree on before everyone leaves.

- It uses the same model as Co-Pilot: your separate live model if you've
  set one in **Settings → AI Models**, otherwise your main one. It
  doesn't need Co-Pilot switched on, but it does need live
  transcription.
- The last recap is kept with the meeting and shown on its **Overview**
  afterwards as **Recap during the call**.
- Nothing else changes: the summary after the call is still made from
  the full recording.

## Notes during the call

A **Notes** tab on the right edge of the record screen slides a notes
panel out over the page, so it takes no room until you want it.

- What you type is saved as you type, to the meeting's **Notes** — the
  same Notes tab you see afterwards, which the summary reads as context.
  Co-Pilot reads it during the call too.
- **Insert time** stamps the current point in the call, e.g. `[23:40]`,
  so a note points back to the moment.
- Anything Co-Pilot's **Save → To my notes** adds while the panel is open
  is kept alongside what you type.

## Renaming a meeting mid-call now sticks

Changing the meeting name, client, project or template on the record
screen while recording could be undone when you pressed **Stop**, because
the recording saved its own copy over it. Those edits now stay.
