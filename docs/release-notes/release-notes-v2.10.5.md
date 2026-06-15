# v2.10.5 — Hotfixes for install, calendar, AI provider routing, and (critical) cloud-folder audio loss

Seven backend fixes. Two of them — bootstrap install failure and live
capture into a cloud-synced folder — were eating real user data in the
field. **Anyone running 2.10.4 with `RECORDINGS_DIR` pointed at
OneDrive / iCloud / Google Drive / Dropbox should upgrade.**

## Install (macOS)

> v2.10.5 ships **a single universal `.zip`** that runs on every Mac
> (Apple Silicon and Intel). On the [Releases page](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases),
> grab `Meeting.Recorder_2.10.5_universal.zip`.
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
> unzip -o Meeting.Recorder_2.10.5_universal.zip
> mv "Meeting Recorder.app" /Applications/
> xattr -cr "/Applications/Meeting Recorder.app"
> open "/Applications/Meeting Recorder.app"
> ```
>
> **Windows users**: download `Meeting.Recorder_2.10.5_x64-setup.exe`
> or `.msi` and double-click. No Gatekeeper / quarantine handling
> needed.

## What's fixed

### 1. Critical: live capture into a cloud-synced `RECORDINGS_DIR` was losing audio

Field repro: a 2 h 28 min customer call captured into a OneDrive-synced
`Recordings` folder produced only 28 minutes of audio. The session
flagged `mic and system-audio tracks drifted ~7251s apart`, with
*"about 121 min appears to be missing"*. Same class of loss reproduced
on a 1 h 28 min meeting (drift ~2775s) and on a separate user's
captures.

Root cause: `RecordingService` streams two live WAVs (`_recording_<id>.wav`
+ `_loopback_<id>.wav`) to `recordings_dir` continuously during a
capture. When `recordings_dir` is on a cloud sync provider (OneDrive,
Google Drive Stream, iCloud, Dropbox, Box), every audio chunk write
goes through that provider's filesystem filter driver — which holds
file locks for sync polls, throttles writes during upload, and on
corporate machines layers antivirus scans on top. The OS audio ring
buffer fills, overflows, and samples get overwritten before the
capture thread can read them. Over a 2-hour meeting that compounds to
>80% loss. Google Drive's `G:\My Drive\` Stream mount is the
worst-case — every write is proxied through a userspace daemon with
50-500 ms latency.

Fix: live-capture temp WAVs now go to `tempfile.gettempdir() /
meeting_recorder_capture/` — guaranteed local, no cloud filter driver
in the hot path. The final merged WAV at stop still writes to
`recordings_dir` (one-shot write — any cloud absorbs that fine), so
cross-device sync still works.

After upgrading you can safely keep `RECORDINGS_DIR` on OneDrive /
Google Drive / iCloud / etc. for cross-device sync. The live capture
no longer pays the cloud-filter tax.

### 2. Bootstrap pip cap — fresh installs were failing pip-resolution

Bootstrap previously did `pip install --upgrade pip` unconditionally
on first venv create. Pip 26.1.2 enforces stricter PEP 440 metadata
validation that rejects too many old `omegaconf` candidates, leaving
the `pyannote.audio → omegaconf → antlr4-python3-runtime` chain
unsolvable. Fresh installs on machines that hit the new pip looped
through five watchdog respawns into the same `ResolutionImpossible`
and gave up. Bootstrap now pins `pip>=24,<25` on both fresh-create and
the "venv exists but requirements changed" path, so existing broken
venvs from prior launches self-heal on next start.

### 3. AI Suggest under Clients honours the configured provider

`/clients/suggest-tagging` was hardcoded to build its own Anthropic
client and ship requests to `api.anthropic.com` regardless of the
user's `ai_provider` setting. Anyone on Ollama / OpenRouter / LM Studio
hit a 404 the moment they clicked AI Suggest because the local-LLM
model name doesn't exist at Anthropic. Now routed through the shared
summarizer (same provider dispatch as Process / Summarize / Action
Items / Co-Pilot — those were already correct).

### 4. OpenAI-compat timeout floor 600s — Summarize/Process worked again on local LLMs

`_chat`'s timeout was tuned for cloud LLMs that respond in well under
10 s. With a local Ollama provider it killed legitimate runs: cold
model load is 30-60 s by itself before any inference begins, and 12B
generation on a long meeting transcript is 1-60 tok/sec depending on
hardware. Floor the OpenAI-compat path at 600 s while leaving the
Anthropic path on the caller-supplied value (cloud should still fail
fast on hangs).

### 5. `/calendar/upcoming` keeps in-progress meetings visible

Both calendar backends dropped any meeting whose START was in the past
("already started") — meaning the Upcoming Meetings panel emptied the
instant an auto-recorded meeting began, while the "recording in
progress" indicator at the top still showed the meeting subject. On a
day with only one scheduled meeting, the panel collapsed entirely.
Now we drop on END < now instead of START < now, so in-progress
meetings stay in the list until they actually finish.
AutoRecordService's `next_event` hint already applies its own
`start > now` filter, so this doesn't double-trigger anything.

### 6. Auto-brief calendar signature

`auto-brief` called `get_upcoming_meetings(24, False)` but every
calendar backend exports the single-arg signature `(hours_ahead=168)`.
Auto-brief errored on every 60-second tick with *"takes from 0 to 1
positional arguments but 2 were given"*, killing the auto-prep-brief
loop. Drop the stray `False` — resource-calendar filtering already
lives inside the calendar backends.

### 7. `/sessions/unprocessed` route ordering

`@app.get("/sessions/unprocessed")` was declared after
`@app.get("/sessions/{session_id}")`. FastAPI matches routes in
registration order, so the dynamic `{session_id}` swallowed the
literal-segment route, tried to load `session_unprocessed.json`, and
returned 404. The "X sessions awaiting processing" badge and the
Windows unprocessed-toast were dead. Move the literal route ahead of
the catch-all.

## Known not yet patched

- **Sync-integrity warnings on already-recorded sessions** — the
  warning ribbon now correctly reports drift / buffer overflows from
  the OneDrive contention era. After upgrading to 2.10.5 new
  recordings won't accumulate this drift. Existing sessions with the
  warning are historical — the audio that survived is processable;
  the missing minutes are not recoverable.

- **Configuration safety net for cloud-redirected `%APPDATA%` /
  `%LOCALAPPDATA%`** — on corporate machines with OneDrive Known
  Folder Move active, the canonical config.env can silently end up
  inside OneDrive. Investigating; tracking for a follow-up.
