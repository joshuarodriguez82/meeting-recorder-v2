# Meeting Recorder v2.9.4

Audio-integrity follow-up to v2.9.3. The six-layer defense in v2.9.3 prevents most orphan-recording scenarios, but it doesn't detect when a recording's WAV file is shorter than the recording window — silent partial-audio loss. v2.9.4 adds three integrity layers on top.

---

> ## macOS install — read this first
>
> The Mac build is **unsigned** (signing + notarization still pending). On first launch Gatekeeper says *"Meeting Recorder is damaged and can't be opened."* It is not damaged.
>
> **Path A — System Settings** (no Terminal):
> 1. Double-click `Meeting.Recorder_2.9.4_universal.zip` in Finder. Archive Utility auto-extracts to `Meeting Recorder.app`.
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

## Why this release

A user's morning Ricoh meeting had `session.json` claim 1 hour of audio but the actual WAV contained only ~30 minutes (the last segment of the call, the rest silently lost). v2.9.3 fixes the most likely cause (orphan processes contending for the same mic), but does NOT detect when this happens. v2.9.4 fixes the detection so you find out at stop time, not when you go to listen back.

---

## Audio integrity layers added

| Layer | Catches |
|---|---|
| **7. WAV-vs-metadata duration check at stop** | Compares the actual WAV duration (parsed from the finalized file) to `ended_at - started_at`. If the deficit exceeds 10%, the session is tagged `audio_integrity_warning` with a precise "you got X min out of Y" message that surfaces as an amber banner on the Sessions list. Logged as `AUDIO_INTEGRITY` at critical severity. |
| **8. Capture-stall detector during recording** | The watchdog now tracks the wall-clock time of the most recent audio chunk written to disk. If no chunks have arrived for 30+ seconds during an active recording, a `capture_stalled` warning fires immediately and surfaces in the Record view banner: *"No audio captured for Ns. Recording is RUNNING but data is NOT reaching the file. Check your microphone, then stop and restart the recording."* |
| **9. Ghost session audit at startup** | The backend scans every `session_*.json` in `recordings_dir` and logs any whose referenced `audio_path` doesn't exist on disk. These are phantom sessions left over from previous failure modes (the v2.9.0 orphan-process incident produced two). Logged as `GHOST_SESSIONS` at warning severity with the count + first 10 session IDs. |

Together with the six v2.9.3 layers, the defense surface is now:

| # | Layer | Source |
|---|---|---|
| 1 | Orphan-kill on every spawn | v2.9.3 |
| 2 | Parent-PID deadman switch | v2.9.3 |
| 3 | Absolute 6-hour cap | v2.9.3 |
| 4 | Watchdog runs on backend timer | v2.9.3 |
| 5 | Stop button in sidebar pill | v2.9.3 |
| 6 | Record view detects external starts + mid-call editing | v2.9.3 |
| 7 | WAV-vs-metadata duration check | v2.9.4 |
| 8 | Capture-stall detector | v2.9.4 |
| 9 | Ghost session audit at startup | v2.9.4 |

## How the warning looks

**On the Sessions list**, a session with mismatched audio gets an amber banner under its metadata line:

> ⚠ Audio is shorter than the recording window. You got 32 min of audio in a 60-min recording — about 28 min appears to be missing.

**During a recording**, if no audio chunks have reached the WAV file for 30+ seconds, the Record view's existing warning banner area now shows:

> ⚠ No audio captured for 32s. Recording is RUNNING but data is NOT reaching the file. Check your microphone, then stop and restart the recording.

**At backend startup**, ghost sessions are visible in `backend.log`:

```
GHOST_SESSIONS: 2 session(s) have a session.json but no audio file on disk:
580208FC, DFC0769D. These will fail to process. Delete them from the
Sessions list or via the filesystem.
```

---

## What didn't change

- All v2.9.3 defense layers — six layers still active, augmented not replaced
- Recording capture, transcript, summarization pipelines — unchanged
- Live Co-Pilot prompt + modes + meeting types — unchanged
- Settings, session storage layout, JSON format — unchanged (the three new fields on the session model are optional; missing values just mean the session was recorded before v2.9.4)

---

## Upgrade notes

- **Windows** — `Meeting Recorder_2.9.4_x64-setup.exe`. Installs over your existing version; sessions / settings / config preserved.
- **macOS** — see the install block at the top.
- **No new dependencies**, no bootstrap changes.
- **First launch after upgrade**: check `backend.log` for `GHOST_SESSIONS:` line. If any are listed, those are sessions you should clean up from the Sessions list (they had no audio to begin with).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
