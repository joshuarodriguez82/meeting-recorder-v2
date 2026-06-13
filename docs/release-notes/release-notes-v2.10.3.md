# Meeting Recorder v2.10.3

Critical processing-integrity fix plus live co-pilot reliability for local models. **If you process meetings, install this** — it closes a bug where one session's summary could be generated from a different meeting's transcript.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.10.3_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag into `/Applications`.
> 3. Double-click. Dismiss the "damaged" warning.
> 4. **System Settings → Privacy & Security**, scroll to bottom, click **Open Anyway**.
> 5. Double-click again, click **Open**.
>
> **Path B — Terminal:**
> ```sh
> cd ~/Downloads
> unzip -o Meeting.Recorder_*_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> Windows users — just install the `.msi` or `.exe`.

---

## Critical fix: cross-session summary contamination

A meeting's summary could be generated from a **different** meeting's transcript. Root cause: the transcribe/diarize stage mutated shared state (`current_session`) that two processing jobs could race on. Once auto-processing started running jobs in the background (v2.10.2), a background auto-process of session A overlapping a manual re-process of session B could cross the wires — B's transcript drove A's summary.

The audio and transcript were always saved correctly; only the extracted **summary/action-items/decisions** could be wrong. Still a trust-killer.

**Fix:** a processing lock now serializes the transcribe/diarize stage across both entry points (auto-process and manual Process), with a double-checked reload inside the lock. Two sessions can no longer cross. Processing was never meant to run concurrently (single model), so this is the correct behavior.

> **If you saw a wrong summary on a v2.10.x build:** the transcript is intact. Just re-process that session once (in isolation) on this build and the summary regenerates correctly from its own transcript.

## Live Co-Pilot reliability on local models

Live ticks went silently blank partway through long calls when running a local model (Ollama): the 10-minute transcript window made inference slower as the call grew until every tick exceeded the 20-second timeout.

- **Smaller tick window** (~4.5 min) keeps local-model inference roughly constant regardless of call length.
- **Provider-aware, interval-aware timeout** — local models get more headroom (up to 35s) than cloud (20s), always kept under the poll cadence so ticks never overlap.
- **Visible failures** — when a tick times out or can't reach the model, the panel now says so ("model responding too slowly" / "can't reach its model — is Ollama running?") instead of going silently blank.

---

## What didn't change
- Everything from v2.10.2 (auto-process on all stop paths, retry queue, diagnostics panel, domain terminology, auto pre-meeting brief, OneDrive cloud-file hydration, onboarding tour) carries forward.
- Existing sessions / settings / config preserved.

## Upgrade notes
- **Windows** — `Meeting Recorder_2.10.3_x64-setup.exe`. Installs over your existing version.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- Recommended for anyone on v2.10.0–v2.10.2 given the processing-integrity fix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
