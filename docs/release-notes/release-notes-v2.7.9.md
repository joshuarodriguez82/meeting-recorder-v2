# v2.7.9 — Auto-record actually persists devices; smarter summaries

Patch release. Fixes a silent bug that has been quietly breaking
calendar auto-record for every user since v2.7.4 — and closes the gap
surfaced by a real meeting in v2.7.8 where two screenshots were
attached to the session but didn't show up in the summary, the action
items, or any of the AI extractions.

## Install (macOS)

> v2.7.9 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.7.9_universal.zip`.
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
> unzip -o Meeting.Recorder_2.7.9_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.7.9_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## Fixed since v2.7.8

- **Auto-record can finally remember your microphone.** Since v2.7.4,
  manual recordings have been *silently failing* to persist your
  selected mic + loopback to disk because of a missing `import json`
  in the backend. The exception was caught and logged as a warning, so
  the user-facing behavior was just "auto-record never fires, says
  no microphone configured." Net effect for the calendar auto-record
  feature shipped in 2.7.4: it has never worked for anyone who upgraded
  from a pre-2.7.4 build (fresh installs were unaffected only by
  coincidence). v2.7.9 imports `json` properly. Existing users get
  their `last_devices.json` written on the next manual recording, and
  auto-record fires reliably from that point on.
- **Screenshots now influence every AI output, not just the summary.**
  Action items, decisions, requirements, and the structured engagement
  records all receive your meeting screenshots as visual context now —
  earlier they only got the transcript text. Whiteboards, slides,
  diagrams, and error screens are part of the picture for every
  extraction.
- **Summaries explicitly reference screenshots.** All five built-in
  templates (General, Requirements Gathering, Design Review, Sprint
  Planning, Stakeholder Update) now instruct Claude to call out
  screenshots inline ("as shown in screenshot 1...") and add a closing
  **Visuals** section that names each one and describes what it shows.
  Without the explicit directive, whether Haiku referenced screenshots
  in the prose was inconsistent — same meeting, same images, sometimes
  yes, sometimes no.
- **Custom templates are preserved.** The template migration that
  delivers these new defaults checks each built-in template: if your
  current prompt matches the **old** default exactly, you get the new
  one automatically; if you've hand-edited the template, your version
  is untouched. Reset-to-default in Settings always reflects the latest
  canonical text.
- **Live Co-Pilot tick failures now log a useful reason.** The earlier
  `coach_tick chat call failed:` log line was empty when the cause was
  a 20-second Ollama timeout (asyncio.TimeoutError's `__str__` is the
  empty string). Now logs the exception type — e.g.
  `coach_tick chat call failed: TimeoutError:`.
- **Structured-extraction failures log the model's raw output.** When
  the model returns prose or fenced markdown instead of JSON,
  `extract_structured` now logs a 300-char preview before raising, so
  the next time it fails you can see what went wrong instead of
  reproducing the case.

## Everything from v2.7.8 and earlier

The Live Co-Pilot (beta) with separate live-model config (Ollama /
OpenRouter override), the in-bar Co-Pilot toggle, the scrolling tick
history, the per-session **Co-Pilot** tab, the in-bar
**● Recording…** badge, auto-record fixes, the Engagements layer
(per-client register + Excel export), the Python-3.12 installer
bootstrap fix, and calendar auto-record for every timed meeting.

## Notes

- The new template defaults are applied via in-place migration on
  first launch of v2.7.9 — no user action needed. Inspect Settings →
  Summary Templates after upgrading to see the new built-in text.
- Custom templates and the Live Co-Pilot model override are
  preserved across the upgrade.
- First screenshot on macOS prompts for Screen Recording permission.
- Unsigned build — the Gatekeeper steps above are required on first
  macOS launch until the app is notarized.
