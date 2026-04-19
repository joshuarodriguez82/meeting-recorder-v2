# Meeting Recorder v2

AI-powered meeting recorder for Windows — transcribes meetings, identifies speakers, and extracts summaries, action items, requirements, and decisions.

**Native desktop app** built with Tauri + Rust for the shell, Next.js + React + shadcn/ui for the UI, and a Python FastAPI sidecar wrapping all the heavy lifting (Whisper, Pyannote, Claude).

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
- **Auto device discovery** — mic + loopback (Stereo Mix, VB-Cable)
- **Calendar-driven start** — click a meeting from Today's Meetings to pre-fill the name

### AI extraction (Claude)
- **Summary** — template-aware (General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update)
- **Action Items** — owner, task, due date, decisions, open questions
- **Requirements** — FR/NFR tables with priority and owner
- **Decisions** — auto-generated ADR log (Decided, Rationale, Alternatives, Owner, Impact)
- **Meeting Prep Brief** — pre-meeting brief from prior meetings tagged to the same client/project
- **Default model:** Claude Haiku 4.5 (~$1/M input, $5/M output). Haiku + Sonnet selectable in Settings.

### Knowledge base
- **Sessions** — full history, bulk process, delete
- **Follow-Ups** — action items aggregated across every meeting, filterable by status/client/owner/text
- **Decisions** — ADR-style decision log, list + detail pane
- **Transcript Search** — full-text search with context snippets
- **Client Dashboard** — per-client stats, meetings, open actions, recent decisions

### Workflow
- **Auto-process after stop** — full transcribe + extract chain runs automatically
- **Auto-draft follow-up email** — Outlook draft to attendees after processing
- **Launch on Windows startup** — optional shortcut to Startup folder
- **Retention policy** — automatic cleanup of old audio WAV files, separate thresholds for processed/unprocessed. Transcripts/summaries never deleted.

### Calendar
- **Today's Meetings** panel pulled from Outlook on launch (Classic Outlook only)
- **Popup notifications** 2 min before a scheduled meeting starts
- **Attendee capture** from Outlook invites for follow-up emails

## Prerequisites

- Windows 10/11 (**Classic Outlook**, not New Outlook)
- Python 3.11+ — [python.org](https://www.python.org/downloads/)
- Node.js 20+ — [nodejs.org](https://nodejs.org/)
- Rust (rustup) — [rustup.rs](https://rustup.rs/)
- Microsoft WebView2 Runtime (already on Windows 11)
- NVIDIA GPU recommended (CPU works, slower)

## Install & build from source

```powershell
# 1. Clone
git clone https://github.com/joshuarodriguez82/meeting-recorder-v2.git
cd meeting-recorder-v2

# 2. Backend venv + dependencies (takes 5-10 min)
python setup.py

# 3. Frontend + Rust dependencies
npm install

# 4. Build the release .exe (takes 3-5 min first time)
npm run tauri build

# 5. Create a desktop shortcut
python make_shortcut.py
```

After this you have:
- `src-tauri/target/release/meeting-recorder.exe` — single executable
- `src-tauri/target/release/bundle/nsis/Meeting Recorder_2.0.0_x64-setup.exe` — NSIS installer
- `src-tauri/target/release/bundle/msi/Meeting Recorder_2.0.0_x64_en-US.msi` — MSI installer
- Desktop shortcut pointing to the release exe

Double-click the shortcut to launch.

## First-run setup

The app opens without API keys configured. Before you can process meetings:

1. **Open File > Settings** (the sidebar Settings button)
2. Paste your **Anthropic API key** ([console.anthropic.com](https://console.anthropic.com))
3. Paste your **HuggingFace token** ([huggingface.co/settings/tokens](https://huggingface.co/settings/tokens))
4. Click **Save**

Then open these URLs and click "Agree and access repository" on each (required for speaker identification):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

Restart the app. Models will load in the background on launch.

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
