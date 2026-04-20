# Meeting Recorder v2

AI-powered meeting recorder for Windows — transcribes meetings, identifies speakers, and extracts summaries, action items, requirements, and decisions.

**Native desktop app** built with Tauri + Rust for the shell, Next.js + React + shadcn/ui for the UI, and a Python FastAPI sidecar wrapping all the heavy lifting (Whisper, Pyannote, Claude).

## Download (Windows)

Prebuilt installers are published under [**Releases**](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases). Pick one:

- **`Meeting Recorder_2.0.0_x64-setup.exe`** — NSIS installer, double-click to install. Creates a Start Menu shortcut and uninstaller.
- **`Meeting Recorder_2.0.0_x64_en-US.msi`** — MSI installer, for IT-managed / Group Policy deploys.

After install you still need a one-time setup to drop in API keys and accept the HuggingFace model terms — see [First-run setup](#first-run-setup) below.

## Architecture

```
┌──────────────────────────────────────────────┐
│   Tauri shell (Rust, native Windows .exe)    │
│  ┌────────────────────────────────────────┐  │
│  │  Next.js + React + Tailwind + shadcn   │  │
│  └────────────────────────────────────────┘  │
│                HTTP @ 127.0.0.1:17645         │
│  ┌────────────────────────────────────────┐  │
│  │  Python FastAPI sidecar                │  │
│  │  ↳ Whisper transcription               │  │
│  │  ↳ Pyannote speaker diarization        │  │
│  │  ↳ Claude summaries + action items +   │  │
│  │    requirements + decisions            │  │
│  │  ↳ Outlook COM (calendar + email)      │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

## Features

### Recording
- **Captures mic + system audio** via WASAPI loopback (works with headphones)
- **Unlimited recording duration** — streams to disk
- **Auto device discovery** with host-API fallback — if WASAPI refuses the mic, the backend silently retries under MME → DirectSound → WDM-KS before giving up
- **Persistent device selection** — mic and loopback choices saved by name, survive reboots and USB re-plugs
- **Calendar-driven start** — click a meeting from Upcoming Meetings to pre-fill the name + attendees

### AI extraction (Claude)
- **Summary** — template-aware (General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update)
- **Action Items** — owner, task, due date, decisions, open questions
- **Requirements** — FR/NFR tables with priority and owner
- **Decisions** — auto-generated ADR log (Decided, Rationale, Alternatives, Owner, Impact)
- **Meeting Prep Brief** — pre-meeting brief from prior meetings tagged to the same client/project
- **Default model:** Claude Haiku 4.5 (~$1/M input, $5/M output). Haiku + Sonnet selectable in Settings.

### Knowledge base
- **Sessions** — full history, bulk process, delete, click any row to open the session dialog
- **Session Detail dialog** — inline audio player, editable tags, rename speakers with one click, run any AI extraction on the fly
- **Follow-Ups** — action items aggregated across every meeting, filterable by status/client/owner/text
- **Decisions** — ADR-style decision log, list + detail pane
- **Transcript Search** — full-text search with context snippets
- **Clients + nested Projects** — Projects live inside Clients (one-to-many). Client dashboard shows a chip row of its projects; click a chip to drill into just that project's meetings. AI-assisted tagging suggests which meetings belong to a given client.

### Workflow
- **Auto-process after stop** — full transcribe + extract chain runs automatically
- **Auto-draft follow-up email** — Outlook draft to attendees after processing
- **Launch on Windows startup** — optional shortcut to Startup folder
- **Retention policy** — automatic cleanup of old audio WAV files, separate thresholds for processed/unprocessed. Transcripts/summaries never deleted.

### Calendar
- **Upcoming Meetings** panel pulled from Outlook on launch (Classic Outlook only)
- **Popup notifications** 2 min before a scheduled meeting starts
- **Attendee capture** from Outlook invites for follow-up emails
- **Fast** — calendar is pre-warmed in a background thread at startup, cached 5 minutes, and Exchange resource / shared calendars are skipped automatically (they used to add 60+ seconds of COM latency)

### Performance
- Backend is responsive within ~500 ms of launch; AI models load lazily on first use
- Every blocking call (Outlook COM, audio device enumeration, disk I/O for session list) runs off the asyncio event loop so one slow endpoint never stalls the others
- Calendar and audio devices are cached in-memory with in-flight dedup — concurrent callers share a single COM round-trip

## Prerequisites

- Windows 10/11 (**Classic Outlook**, not New Outlook)
- Python 3.11+ — [python.org](https://www.python.org/downloads/)
- Node.js 20+ — [nodejs.org](https://nodejs.org/)
- Rust (rustup) — [rustup.rs](https://rustup.rs/)
- Microsoft WebView2 Runtime (already on Windows 11)
- NVIDIA GPU recommended (CPU works, slower)

## Install & build from source

Most users should just download the installer from [Releases](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases). Build from source only if you're hacking on the code.

```powershell
# 1. Clone
git clone https://github.com/joshuarodriguez82/meeting-recorder-v2.git
cd meeting-recorder-v2

# 2. Backend venv + dependencies (takes 5-10 min)
python setup.py

# 3. Frontend + Rust dependencies
npm install

# 4. Build the release .exe + installers (takes 3-5 min first time)
npx tauri build

# 5. (Optional) Create a desktop shortcut to the portable exe
python make_shortcut.py
```

After this you have:
- `src-tauri/target/release/meeting-recorder.exe` — single portable executable
- `src-tauri/target/release/bundle/nsis/Meeting Recorder_2.0.0_x64-setup.exe` — NSIS installer (ships in Releases)
- `src-tauri/target/release/bundle/msi/Meeting Recorder_2.0.0_x64_en-US.msi` — MSI installer (ships in Releases)

Double-click either installer to install, or run the portable exe directly.

## First-run setup

You need **two tokens** before Meeting Recorder can process recordings:

### 1. Anthropic API key — powers AI extraction

Used for summaries, action items, requirements, decisions, and prep briefs. Costs money (~$0.05 per meeting on Haiku 4.5, the default).

1. Sign up at [console.anthropic.com](https://console.anthropic.com)
2. **Billing → Buy credits** — add $5–10 to start
3. [**Settings → API Keys**](https://console.anthropic.com/settings/keys) → **Create Key**
4. **Permissions:** default (read/write is fine)
5. Copy the value (starts with `sk-ant-api03-`)

### 2. HuggingFace token — powers speaker identification

Used to download the pyannote diarization models (runs locally on your machine after download). Free.

1. Sign up at [huggingface.co/join](https://huggingface.co/join)
2. [**Settings → Access Tokens**](https://huggingface.co/settings/tokens) → **Create new token**
3. **Token type:** `Read` (Write and Fine-grained are unnecessary)
4. Copy the value (starts with `hf_`)
5. **Critical — accept model terms on BOTH of these pages** (otherwise speaker identification 403s on first Process):
   - <https://huggingface.co/pyannote/speaker-diarization-3.1> → click "Agree and access repository"
   - <https://huggingface.co/pyannote/segmentation-3.0> → click "Agree and access repository"

### Plug them in

1. Launch Meeting Recorder, go to **Settings** in the sidebar
2. Paste both tokens into the respective fields
3. Click **Save Settings**
4. **Restart the app** so the backend reloads config and downloads the pyannote models (~200 MB, one-time, happens on first Process)

Tokens are stored locally in `%LOCALAPPDATA%\MeetingRecorder\config.env` — never roams to other machines.

## Dev loop (hot reload)

```powershell
npm run tauri dev
```

Starts Next.js dev server + launches Tauri window with hot reload. Python backend starts automatically. First-time Rust compile: 3-5 minutes. Subsequent runs: seconds.

The backend runs at `http://127.0.0.1:17645` — hit endpoints with `curl` for debugging. FastAPI auto-docs at `http://127.0.0.1:17645/docs`.

## Audio setup (loopback capture)

To capture other participants (not just your own voice):

1. Right-click speaker icon → Sound settings → Recording tab
2. Right-click empty space → Show Disabled Devices
3. Enable **Stereo Mix** (right-click → Enable)
4. In Meeting Recorder: **Record** view → System Audio → select your loopback device

Or install [VB-Cable](https://vb-audio.com/Cable/) (free) as a virtual loopback.

## Troubleshooting

| Issue | Fix |
|---|---|
| Only my voice was recorded | System Audio isn't a loopback device. Enable Stereo Mix or install VB-Cable. |
| Calendar shows no meetings | Requires Classic Outlook, not New Outlook. |
| Models failed to load | Invalid HuggingFace token, or you haven't accepted pyannote model terms. |
| App won't start | Check that `backend/.venv/Scripts/pythonw.exe` exists. Re-run `python setup.py` if missing. |
| Help → Usage Guide inside the app | Full walkthrough of every feature |

## Project structure

```
meeting-recorder-v2/
├── src/                              # Next.js frontend
│   ├── app/                          # App router (page, layout, global CSS)
│   ├── components/
│   │   ├── record-view.tsx           # Recording + AI extraction
│   │   ├── sessions-view.tsx         # Session history + bulk process
│   │   ├── follow-ups-view.tsx       # Action items aggregator
│   │   ├── decisions-view.tsx        # Decision log
│   │   ├── search-view.tsx           # Transcript search
│   │   ├── clients-view.tsx          # Per-client dashboard
│   │   ├── prep-brief-view.tsx       # Meeting prep brief generator
│   │   ├── settings-view.tsx         # Full settings page
│   │   ├── usage-guide-view.tsx      # In-app help
│   │   ├── calendar-monitor.tsx      # Background meeting notifications
│   │   └── ui/                       # shadcn/ui primitives
│   └── lib/
│       ├── api.ts                    # FastAPI client
│       └── utils.ts
├── src-tauri/                        # Rust shell
│   ├── src/lib.rs                    # Spawns Python sidecar on startup
│   ├── tauri.conf.json               # App metadata + bundler config
│   └── icons/
├── backend/                          # Python FastAPI sidecar
│   ├── server.py                     # All HTTP endpoints
│   ├── config/                       # Settings (shared with v1)
│   ├── core/                         # AudioCapture, Transcription, Diarization, Summarizer
│   ├── models/                       # Session, Segment, Speaker
│   ├── services/                     # Recording, Session, Calendar, Retention, Export
│   ├── utils/
│   └── .env                          # API keys (gitignored)
├── setup.py                          # One-command backend install
├── make_shortcut.py                  # Desktop shortcut creator
└── README.md
```

## License

MIT License.
