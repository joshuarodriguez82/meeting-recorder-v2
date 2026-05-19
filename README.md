# Meeting Recorder v2

AI-powered meeting recorder — transcribes meetings, identifies speakers, and extracts summaries, action items, requirements, and decisions. Runs natively on **Windows** and **macOS**.

**Native desktop app** built with Tauri + Rust for the shell, Next.js + React + shadcn/ui for the UI, and a Python FastAPI sidecar wrapping all the heavy lifting (Whisper, Pyannote, Claude).

## Download (Windows)

Prebuilt installers are published under [**Releases**](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases). Pick one:

- **`Meeting Recorder_X.Y.Z_x64-setup.exe`** — NSIS installer, double-click to install. Creates a Start Menu shortcut and uninstaller.
- **`Meeting Recorder_X.Y.Z_x64_en-US.msi`** — MSI installer, for IT-managed / Group Policy deploys.

After install you still need a one-time setup to drop in API keys and accept the HuggingFace model terms — see [First-run setup](#first-run-setup) below.

## Download (macOS)

Prebuilt builds are published under
[**Releases**](https://github.com/joshuarodriguez82/meeting-recorder-v2/releases).
macOS ships as a **ditto-zipped `.app`** (not a `.dmg` — Tauri's DMG
packaging is unreliable on the CI runner). **One universal `.zip` runs
on every Mac** — Apple Silicon (M1–M4) and Intel:

- `Meeting.Recorder_X.Y.Z_universal.zip`

(GitHub Actions writes spaces as dots in the artifact filename — the
app's display name is still "Meeting Recorder".)

### Bypassing Gatekeeper on first launch

The build is **not signed/notarized** (no Apple Developer cert yet),
so macOS flags it as *"damaged and can't be opened"* on first launch.
This isn't real damage — it's the quarantine attribute your browser
added. Pick whichever path is easier; both work.

#### Path A: System Settings (no Terminal)

1. Double-click the `.zip` in Finder — Archive Utility auto-extracts
   it to `Meeting Recorder.app`.
2. Drag `Meeting Recorder.app` to **/Applications**.
3. Double-click it. macOS refuses with the "damaged" warning —
   **dismiss it**.
4. Open **System Settings → Privacy & Security**, scroll to Security,
   and click **Open Anyway** next to "Meeting Recorder was blocked".
5. Double-click the app again, then click **Open**.

That's it — macOS trusts the app on every subsequent launch.

#### Path B: Terminal

```sh
cd ~/Downloads
unzip -o Meeting.Recorder_*_universal.zip
mv "Meeting Recorder.app" /Applications/
xattr -cr "/Applications/Meeting Recorder.app"
open "/Applications/Meeting Recorder.app"
```

The proper long-term fix is Apple Developer ID signing + notarization;
until that's set up, the steps above are the install path. **Windows
users** can ignore all of this — just run the `.msi`/`.exe`.

### First-run setup (after install)

You still need:

1. **BlackHole** (free audio loopback driver — required to capture other
   participants' audio): `brew install blackhole-2ch`, then reboot.
2. **API keys** (Anthropic + HuggingFace) entered in Settings — same as
   Windows; see [First-run setup](#first-run-setup) below.
3. **Audio routing** through a Multi-Output Device — see
   [MAC_SETUP.md → Audio routing](./MAC_SETUP.md#audio-routing-for-system-audio-capture).

## Build from source (macOS)

If you want to hack on the code or your CPU isn't covered by the
prebuilt DMGs, build from source. The full walkthrough (BlackHole,
EventKit, notarization) lives in **[MAC_SETUP.md](./MAC_SETUP.md)**.
Short version:

```sh
xcode-select --install                    # C compiler
brew install python@3.12 node blackhole-2ch
brew install rustup-init && rustup-init -y

git clone https://github.com/joshuarodriguez82/meeting-recorder-v2.git
cd meeting-recorder-v2
python3.12 setup.py                       # backend venv (5–10 min). Use python3.12, NOT python3 or 3.13.
npm install
npx tauri build
./scripts/macos-postbuild.sh              # patches Info.plist privacy keys
```

Then **right-click → Open** the `.app` in `src-tauri/target/release/bundle/macos/`
to bypass Gatekeeper on first launch (unsigned builds), grant mic + calendar
permissions when prompted, and paste API keys in Settings.

Mac feature parity vs. Windows:
- Mic recording: ✅ Core Audio via sounddevice
- System-audio loopback: ✅ via BlackHole 2ch (free, `brew install blackhole-2ch`, reboot once)
- Calendar (Upcoming Meetings panel): ✅ EventKit (reads iCloud, Exchange/Outlook for Mac, Google — anything synced into Calendar.app)
- Follow-up email drafts: ✅ Mail.app and Outlook for Mac via AppleScript, .eml fallback
- Auto-launch on login: ✅ LaunchAgent plist
- All AI features (Whisper / Pyannote / Claude / OpenAI-compatible): ✅ identical
- GPU acceleration: ✅ Apple Silicon MPS auto-enabled by torch 2.6 (no setup)

## Architecture

```
┌────────────────────────────────────────────────────────┐
│   Tauri shell (Rust) — Windows .exe / macOS .app       │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Next.js + React + Tailwind + shadcn             │  │
│  │  ↳ Live transcript panel (SSE, during recording) │  │
│  │  ↳ Cross-meeting Q&A with citations (SSE)        │  │
│  └──────────────────────────────────────────────────┘  │
│                HTTP @ 127.0.0.1:17645                   │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Python FastAPI sidecar                          │  │
│  │  ↳ Whisper transcription (post-stop, canonical)  │  │
│  │  ↳ LiveTranscriber (15s windows, while recording)│  │
│  │  ↳ Pyannote speaker diarization                  │  │
│  │  ↳ Speaker fingerprinting (cross-session match)  │  │
│  │  ↳ Sentence-transformers semantic index (local)  │  │
│  │  ↳ Anthropic / OpenAI-compat summaries +         │  │
│  │    action items + requirements + decisions       │  │
│  │  ↳ Calendar + email — Outlook COM (Win) or       │  │
│  │    EventKit + Mail.app/AppleScript (Mac)         │  │
│  └──────────────────────────────────────────────────┘  │
│                                                         │
│  Secrets → OS keychain (Win Credential Manager /        │
│            macOS Keychain), never plaintext on disk     │
└────────────────────────────────────────────────────────┘
```

## Features

### Recording
- **Captures mic + system audio** via WASAPI loopback (Win) or BlackHole (Mac). Works with headphones.
- **Live transcript panel** — rolling 15-second windows of mic + system audio appear next to the recording controls while you record. Toggle off in Settings → Workflow on slow machines; the canonical post-stop transcript runs regardless.
- **Unlimited recording duration** — streams to disk
- **Auto device discovery** with host-API fallback — if WASAPI refuses the mic, the backend silently retries under MME → DirectSound → WDM-KS before giving up
- **Persistent device selection** — mic and loopback choices saved by name, survive reboots and USB re-plugs
- **Calendar-driven start** — click a meeting from Upcoming Meetings to pre-fill the name + attendees
- **Screenshots** — capture any monitor mid-recording (multi-monitor picker); shots are saved with the meeting, fed to Claude as visual context for the summary, and browsable on the session's Screenshots tab
- **Wallclock-anchored merge** — mic and loopback streams are time-stamped on first frame and aligned by absolute timing, not sample-count heuristics. Reduces speaker drift on long recordings.

### AI extraction
- **Multi-provider** — native Anthropic SDK (default) or any OpenAI-compatible endpoint (OpenRouter, Ollama, LM Studio, vLLM). Switch in Settings; same prompts on either side.
- **Summary** — template-aware (General, Requirements Gathering, Design Review, Sprint Planning, Stakeholder Update, plus your own custom templates)
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
- **Semantic search** — type a phrase and get hits where the wording differs but the meaning matches. Powered by a local 22 MB sentence-transformer (MiniLM-L6); embeddings never leave the machine.
- **Cross-meeting Q&A** — ask natural-language questions across every transcript, get streamed answers with inline citations like `[ABC123 @ 12:34]` that jump to the source session. Scope by client or project.
- **Clients + nested Projects** — Projects live inside Clients (one-to-many). Client dashboard shows a chip row of its projects; click a chip to drill into just that project's meetings. AI-assisted tagging suggests which meetings belong to a given client.
- **Engagements** — per-client (optionally per-project) register that rolls every meeting's requirements, decisions, action items, and open questions into one deduped, provenance-tagged view, and exports it to a hand-editable Excel workbook. Re-export after each meeting regenerates the same file in place, carrying your Status/Notes columns forward and adding a "Changes since last export" sheet.

### Speakers
- **Automatic naming** — speakers who introduce themselves ("Hi, I'm Sarah") or are addressed by name are auto-labeled and their voiceprint saved, with no manual tagging
- **Cross-session fingerprinting** — rename a speaker once (e.g. SPEAKER_01 → "Maria Chen") and the embedding is saved. Future meetings auto-label her without re-tagging.
- **Known Speakers UI** — manage the roster from Settings: rename, delete, or merge two profiles that ended up as separate entries. All local, stored as JSON.

### Workflow
- **Auto-process after stop (on by default)** — full transcribe → speakers → summary → action items → decisions → requirements → commitments chain runs automatically when you hit Stop
- **Auto-refreshing views** — Follow-Ups / Commitments / Decisions update when you open the tab or return to the app, so freshly-processed calls show up without a reload
- **Auto-draft follow-up email** — Outlook draft (Win) or Mail.app / Outlook for Mac draft (Mac) to attendees after processing
- **Launch on startup** — optional. Windows: Startup folder shortcut. macOS: LaunchAgent plist.
- **Retention policy** — automatic + manual cleanup of old audio WAVs, separate thresholds for processed/unprocessed, across the main folder **and** every client Designated Folder (including orphaned copies whose session was deleted). Transcripts/summaries never deleted.
- **In-app updates** — checks GitHub Releases and the Download button grabs the correct installer for your OS directly

### Security
- **API keys → OS keychain** — Anthropic, HuggingFace, and OpenRouter tokens go into Windows Credential Manager (Win) or Keychain (Mac), never plaintext on disk. Existing `config.env` keys auto-migrate the first time the new build reads them.
- **Local-only** — every model that can run locally does (Whisper, Pyannote, sentence-transformers, speaker fingerprints). Only the LLM extraction step calls out to a network service, and only the provider you picked.

### Calendar
- **Upcoming Meetings** panel pulled from Outlook (Win) / EventKit (Mac) on launch
- **Click to expand any meeting** — attendees, the invite agenda/body, and a one-click **Join meeting** button (link auto-detected). The agenda also feeds the AI prep brief. Loaded lazily so the list stays fast.
- **Auto-record** — starts recording at a meeting's scheduled time (skips all-day events; requires a Teams/Zoom/Meet/Webex/etc. link). Per-meeting **No auto** toggle permanently excludes a meeting/series. Auto-stop is silence + overrun aware and isn't fooled by you muting yourself.
- **Popup notifications** 2 min before a scheduled meeting starts
- **Real attendee names/emails** resolved from Exchange (no raw directory codes)
- **Fast** — calendar is pre-warmed at startup, per-day + upcoming scans cached, and Exchange resource / shared calendars are skipped automatically

### Performance
- Backend is responsive within ~500 ms of launch; AI models load lazily on first use
- Every blocking call (Outlook COM, audio device enumeration, disk I/O for session list) runs off the asyncio event loop so one slow endpoint never stalls the others
- Calendar and audio devices are cached in-memory with in-flight dedup — concurrent callers share a single COM round-trip

## Prerequisites

### Windows

- Windows 10/11 (**Classic Outlook**, not New Outlook)
- Python 3.11+ — [python.org](https://www.python.org/downloads/)
- Node.js 20+ — [nodejs.org](https://nodejs.org/)
- Rust (rustup) — [rustup.rs](https://rustup.rs/)
- Microsoft WebView2 Runtime (already on Windows 11)
- NVIDIA GPU recommended for Whisper transcription (CPU works, slower)

### macOS

- macOS 12 Monterey or later
- Calendar.app set up with whichever calendars you care about (iCloud,
  Exchange / Outlook for Mac, Google) — Meeting Recorder reads through
  Calendar.app, not directly from the providers
- Xcode Command Line Tools (`xcode-select --install`)
- Homebrew, Python 3.12, Node 20+, Rust, BlackHole 2ch — see
  [MAC_SETUP.md](./MAC_SETUP.md) for the exact commands
- Apple Silicon GPU acceleration is automatic (PyTorch 2.6 MPS backend)

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
- `src-tauri/target/release/bundle/nsis/Meeting Recorder_X.Y.Z_x64-setup.exe` — NSIS installer (ships in Releases)
- `src-tauri/target/release/bundle/msi/Meeting Recorder_X.Y.Z_x64_en-US.msi` — MSI installer (ships in Releases)

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

Tokens are stored in your OS's native credential vault — **Windows Credential Manager** on Windows, **Keychain** on macOS. Never written to plaintext on disk, never roams to other machines. If you upgraded from an earlier build that kept tokens in `config.env`, the values are migrated into the keychain automatically the first time the new build reads them; the env file lines are blanked on the next save.

Other (non-secret) settings still live at:

- Windows: `%LOCALAPPDATA%\MeetingRecorder\config.env`
- macOS: `~/Library/Application Support/MeetingRecorder/config.env`

## Dev loop (hot reload)

```powershell
npm run tauri dev
```

Starts Next.js dev server + launches Tauri window with hot reload. Python backend starts automatically. First-time Rust compile: 3-5 minutes. Subsequent runs: seconds.

The backend runs at `http://127.0.0.1:17645` — hit endpoints with `curl` for debugging. FastAPI auto-docs at `http://127.0.0.1:17645/docs`.

## Audio setup (loopback capture)

To capture other participants (not just your own voice):

### Windows

1. Right-click speaker icon → Sound settings → Recording tab
2. Right-click empty space → Show Disabled Devices
3. Enable **Stereo Mix** (right-click → Enable)
4. In Meeting Recorder: **Record** view → System Audio → select your loopback device

Or install [VB-Cable](https://vb-audio.com/Cable/) (free) as a virtual loopback.

### macOS

macOS has no first-party loopback API for general apps, so you install a
virtual audio driver — see [MAC_SETUP.md → Audio routing](./MAC_SETUP.md#audio-routing-for-system-audio-capture)
for the full walkthrough. Short version:

1. `brew install blackhole-2ch && reboot`
2. Open Audio MIDI Setup → create a Multi-Output Device that includes
   both your normal output AND BlackHole 2ch.
3. Set the Multi-Output Device as your system output.
4. In Meeting Recorder: **Record** view → System Audio → select
   **BlackHole 2ch**.

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
│   │   ├── search-view.tsx           # Semantic search across meetings
│   │   ├── qa-view.tsx               # Cross-meeting Q&A with citations
│   │   ├── live-transcript-panel.tsx # Live transcript stream while recording
│   │   ├── known-speakers-section.tsx# Speaker profile management (in Settings)
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
│   ├── server.py                     # All HTTP endpoints (incl. SSE streams)
│   ├── config/
│   │   ├── settings.py               # User-visible settings (env-backed)
│   │   └── secrets.py                # OS keychain wrapper for API keys
│   ├── core/
│   │   ├── audio_capture.py          # Mic + WASAPI/BlackHole loopback
│   │   ├── transcription.py          # Whisper (post-stop canonical)
│   │   ├── live_transcriber.py       # Whisper streaming windows during record
│   │   ├── diarization.py            # Pyannote
│   │   ├── speaker_embeddings.py     # Cross-session fingerprint matching
│   │   ├── embeddings.py             # Sentence-transformers (semantic search)
│   │   └── summarizer.py             # Anthropic + OpenAI-compat router
│   ├── models/                       # Session, Segment, Speaker, SpeakerProfile
│   ├── services/
│   │   ├── recording_service.py      # Recording orchestration
│   │   ├── session_service.py        # Session CRUD
│   │   ├── speaker_profile_service.py# Known Speakers persistence
│   │   ├── search_service.py         # Semantic-index lookup
│   │   ├── qa_service.py             # Cross-meeting Q&A retrieval + LLM
│   │   ├── calendar_service.py       # Outlook COM (Win) / EventKit (Mac)
│   │   ├── follow_up_email.py        # Outlook (Win) / Mail.app (Mac)
│   │   ├── retention_service.py
│   │   └── export_service.py
│   ├── utils/
│   └── .env                          # Dev fallback (gitignored)
├── setup.py                          # One-command backend install
├── make_shortcut.py                  # Desktop shortcut creator
└── README.md
```

## License

MIT License.
