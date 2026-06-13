# Meeting Recorder v2.9.3

Bug-fix release driven by a real incident. The headline is the **six independent safety layers** that now defend against orphan recordings, plus the matched-pair v2.9.2 fixes around insights drill-down and engagement editing.

If you ran v2.9.0 today, read the post-mortem section. The bug was serious.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.9.3_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
> 2. Drag into `/Applications`.
> 3. Double-click. Dismiss the "damaged" warning.
> 4. **System Settings → Privacy & Security**, scroll to bottom, click **Open Anyway**.
> 5. Double-click the app again, click **Open**.
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

## Post-mortem: the v2.9.0 orphan-recording incident

### What happened

On v2.9.0, an orphaned `pythonw.exe` backend silently recorded **4 hours 17 minutes** of audio across multiple meetings before being discovered. Both mic and system-audio loopback were captured. The recording started at 7:00 AM and ran until 11:17 AM, mixing every call and meeting from that window into a single audio file in OneDrive.

### How it was possible

Five independent failure modes stacked together:

1. **Tauri shell cleanup is unreliable on Windows.** Force-quits, crashes, double-launches all leave the Python backend process running. Three orphan `pythonw.exe` instances had accumulated over a 30-hour window of normal app usage.

2. **Calendar auto-record fired on one of the orphans.** The orphan still had a working backend, including auto-record polling Outlook. When a calendar event triggered, it started capturing audio — invisibly, since the user's foreground app was a different (newer) backend.

3. **The watchdog only ran when the frontend polled.** `/recording/status` ticked the auto-stop conditions; without a UI polling that endpoint, the watchdog never evaluated hard cap, silence, or overrun. The orphan had no UI talking to it. Recording continued indefinitely.

4. **The hard cap was set to 1 hour** in user settings — already too short for a real meeting, would have stopped the visible recording at 60 min if the watchdog had been running.

5. **The Record view UI didn't detect external recording starts.** Even when the user navigated to the Record tab, the form for starting a new recording was still showing instead of the in-progress controls — so the user had no Stop button anywhere in the UI.

The recovery code at backend startup *did* work — it merged the orphan's `_recording_*.wav` files into a finalized `session_*.json` once the orphan was killed. But that's a band-aid, not a fix. The fix is preventing the orphan from existing.

### What changed (six independent defense layers)

| # | Layer | Catches |
|---|---|---|
| 1 | **Orphan-kill on every spawn** (Rust shell, before Python launch) | Scans `Win32_Process` for any `pythonw.exe` matching our venv path, kills it. Prevents orphans from existing in the first place. |
| 2 | **Parent-PID deadman switch** (Python backend) | Tauri passes its own PID via env var; backend polls every 5s; on parent death the backend stops any active recording cleanly and exits. Handles force-quit / crash / BSOD. |
| 3 | **Absolute 6-hour cap** (Python recording watchdog, unconfigurable) | Hard ceiling that fires regardless of user `hard_cap_hours` setting. The longest legitimate meeting is ~3.5h; the cap exists to prevent runaway capture in failure scenarios. Cannot be disabled. |
| 4 | **Watchdog timer task** (Python, runs every 1s) | The watchdog now fires on a backend-owned timer instead of piggybacking on frontend polls. Auto-stops trigger even with no UI connected, no frontend running, network blip, anything. |
| 5 | **Stop button in sidebar recording pill** (frontend) | A dedicated Stop button beside the recording indicator. User can halt any recording from any screen, no race with view state. |
| 6 | **Record view detects external starts + edits mid-call** (frontend) | The Record view polls unconditionally; an auto-record appearing externally now shows the full recording UI (bar + Stop + Screenshot + Co-Pilot). Meeting Name / Client / Project / Template are editable during a recording; changes auto-save to the active session via debounced PATCH. |

For the v2.9.0 scenario to recur, **all six would need to fail simultaneously**. The orphan-kill in layer 1 alone would have caught it; layer 4 would have hit the hard cap at 1 hour; layer 3 would have stopped at 6 hours; layer 5/6 would have made the recording visible to the user immediately.

---

## Other fixes in this release

### Copy buttons on Co-Pilot ticks tab in session detail
Already covered in v2.8.0 for Summary / Actions / Decisions / Requirements / Transcript via `MarkdownBlock`, but the post-meeting Co-Pilot tab uses its own `CoPilotTicksView` and was missed. Now has Copy at three levels — whole-ticks dump, single tick, single bullet.

### Anthropic 429 retry with exponential backoff
`process_full` extracted summary / action items / decisions / requirements in sequence. A single 429 from Anthropic would fail one extractor; the next extractor would hit the same rate-limit minute-bucket and fail too. By the time the cascade finished, four extractors were errored. Now each extractor retries up to 3 times at 2s / 8s / 30s on rate-limit errors. Non-429 errors propagate fast as before.

### Mid-call mode/type switcher fixed (Python NameError)
v2.9.0 shipped with `dataclasses.replace()` called in `/settings/copilot-active` without a corresponding `import dataclasses` in scope. Every mid-recording mode/type change failed with `NameError`, surfacing in the frontend as "Couldn't set type: Failed to fetch." Fixed by adding the local import (matching the sibling `/settings/live-copilot` endpoint).

---

## What didn't change

- Recording capture, transcript, summarization pipelines — unchanged
- Live Co-Pilot prompt + modes + meeting types — unchanged
- All sessions / commitments / decisions / engagements / insights flows — unchanged
- Backend storage layout, session JSON format — unchanged

---

## Action items for users on v2.9.0

1. **Check Settings → Auto-stop → Hard cap.** If it's set below 4 hours, bump it. The absolute 6h cap (layer 3) is now your hard ceiling regardless, but the configurable cap is what stops normal meetings cleanly.

2. **If you saw an unexpected recording on v2.9.0**, check `%LOCALAPPDATA%\MeetingRecorder\rust.log` for the `Spawning Python` lines. Multiple recent ones (without `Killed N orphan backends` lines preceding the new ones) indicates orphans from before this release. v2.9.3 will clean any that exist on next launch.

3. **If you have a `Recovered Session XXXXXXXX` in your Sessions list**, that's a session the backend auto-finalized after detecting orphan WAVs. Review its contents — it may contain mixed audio from multiple meetings.

---

## Coming next

- **v2.9.4**: System tray icon with red dot when ANY recording is in progress. Visible regardless of which window has focus.
- **v3.0**: The visual overhaul — design system locked in `design/v3.0-system.md`.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
