# Meeting Recorder v2 — Handoff Document

Purpose: everything a fresh Claude session needs to pick up this project and help debug on a different machine. Read top to bottom before touching code.

---

## Who the user is and what they need

- **User:** Joshua Rodriguez — Solutions Architect at TTEC Digital
- **Primary use case:** Records customer / internal meetings, wants AI-generated summaries, action items, decisions, and requirements
- **Two machines:**
  - **Dev PC** (`C:\Users\joshu\...`) — where development happens, runs fine against the v1 Python venv at `C:\meeting_recorder\.venv\`
  - **Work laptop** (TTEC-managed, user `Joshua.Rodriguez`) — where he wants to use the app for actual work. This is where everything breaks.
- **Non-negotiable:** the app must "just work" on the laptop the same as v1 did on dev. User is extremely frustrated with repeated failed install attempts.

---

## Architecture (how the v2 app works)

```
┌──────────────────────────────────────────────┐
│  Tauri shell (Rust)                          │
│    meeting-recorder.exe                      │
│  ┌────────────────────────────────────────┐  │
│  │  Next.js + React + Tailwind + Base UI  │  │
│  │  (compiled to static HTML in out/)     │  │
│  └────────────────────────────────────────┘  │
│              HTTP 127.0.0.1:17645             │
│  ┌────────────────────────────────────────┐  │
│  │  Python FastAPI sidecar (server.py)    │  │
│  │    whisper (faster-whisper)            │  │
│  │    pyannote.audio (speaker diarization)│  │
│  │    anthropic (Claude for extractions)  │  │
│  │    pywin32 / pyaudiowpatch (Windows)   │  │
│  │    win32com (Outlook calendar)         │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

### Rust shell responsibilities

- Spawns and supervises the Python sidecar
- Redirects Python stdout/stderr to `%LOCALAPPDATA%\MeetingRecorder\backend.log`
- Kills the Python sidecar on window close
- Watchdog thread respawns Python if it exits unexpectedly (up to 5 times)
- Exposes `restart_backend` Tauri command used by the GPU toggle UI
- Registers the `tauri-plugin-notification` plugin for Windows Action Center toasts

### Python backend responsibilities

- All heavy work: recording, transcription, diarization, Claude API calls
- Reads config from `%LOCALAPPDATA%\MeetingRecorder\config.env`
- Writes recordings to `$RECORDINGS_DIR` (default `%LOCALAPPDATA%\MeetingRecorder\recordings`)
- Reads Outlook calendar via COM (requires Classic Outlook, not New Outlook)
- Serves `/health`, `/sessions`, `/recording/start|stop|status`, `/calendar/upcoming`, `/gpu/status|install`, etc.

### Tauri config resources

- `src-tauri/tauri.conf.json` bundles `../backend-bundle.zip` as a single resource
- Installer drops that zip into `C:\Program Files\Meeting Recorder\resources\`
- Rust shell extracts to `%LOCALAPPDATA%\MeetingRecorder\runtime\` on first launch using the built-in `tar.exe`

---

## ⚠️ Dev PC vs Installed PC are two completely different environments

**This is the most important concept in this whole project. Internalize it before touching anything.**

The dev PC and the work laptop are running the app under fundamentally different scenarios. Any code that tries to treat them the same will break one or the other. The installer / launcher needs to **detect which scenario it's in and behave accordingly.**

### Scenario A: Dev PC (developer with full Python install + v1 venv)

- Source code lives at `C:\meeting-recorder-v2\` (git checkout)
- Has a legacy v1 venv at `C:\meeting_recorder\.venv\` that works
- System Python 3.13 installed at `C:\Users\joshu\AppData\Local\Programs\Python\Python313\`
- Developer runs the app from `C:\meeting-recorder-v2\src-tauri\target\release\meeting-recorder.exe`
- Rust shell should:
  - Find `server.py` at `C:\meeting-recorder-v2\backend\server.py`
  - Use the v1 venv's pythonw.exe as the interpreter
  - NOT extract a bundle, NOT create a new venv
- Logs/config still go to `%LOCALAPPDATA%\MeetingRecorder\`
- This scenario is what's running RIGHT NOW on the dev PC and it works.

### Scenario B: Fresh target PC (e.g. user's work laptop, or an SA's machine)

- No git checkout, no v1 venv, possibly no Python at all
- User downloads the NSIS installer and double-clicks it
- Installer drops files at `C:\Program Files\Meeting Recorder\`
- On first launch the Rust shell needs to:
  - Provide a working Python 3.13 interpreter with all the deps installed (either extract a bundle, or invoke a Python installer, or download from the internet)
  - Create `%LOCALAPPDATA%\MeetingRecorder\config.env` with defaults
  - Create `%LOCALAPPDATA%\MeetingRecorder\recordings\`
  - Spawn the Python sidecar from the provided runtime
- The user has no ability to run `pip install`, no command line, no dev tools.

### What the Rust resolver should do (pseudo-algorithm)

```
def resolve_python_and_backend():
    # Production install? (files exist next to the exe)
    if Path(exe.parent / "resources" / "backend-bundle.zip").exists():
        extracted = ensure_extracted_runtime(zip_path)
        return extracted / "python" / "pythonw.exe", extracted / "server.py"

    # Developer checkout? (server.py at hardcoded dev path)
    dev_backend = Path(r"C:\meeting-recorder-v2\backend")
    if (dev_backend / "server.py").exists():
        # Try embedded Python in dev checkout if present
        if (dev_backend / "python" / "pythonw.exe").exists():
            return dev_backend / "python" / "pythonw.exe", dev_backend / "server.py"
        # Try a dev venv
        if (dev_backend / ".venv" / "Scripts" / "pythonw.exe").exists():
            return dev_backend / ".venv" / "Scripts" / "pythonw.exe", dev_backend / "server.py"
        # Fall back to v1 venv (legacy path — only dev machines have this)
        if Path(r"C:\meeting_recorder\.venv\Scripts\pythonw.exe").exists():
            return Path(r"C:\meeting_recorder\.venv\Scripts\pythonw.exe"), dev_backend / "server.py"

    return None  # caller shows "install corrupted, reinstall" error UI
```

The current `src-tauri/src/lib.rs` roughly does this already (`resolve_backend_dir` + `resolve_python`), but the details have shifted multiple times. **Verify it still matches the algorithm above before trusting it.**

### Consequences for new code

- **Any path that assumes "we're in the installer"** — e.g. hardcoding `%LOCALAPPDATA%\MeetingRecorder\runtime\python\...` — will break on the dev PC
- **Any path that assumes "we're in the dev checkout"** — e.g. hardcoding `C:\meeting-recorder-v2\backend\...` in Python code — will break on a customer laptop
- `backend/server.py` and everything it imports must work equally well when launched:
  - From an extracted bundle at `%LOCALAPPDATA%\MeetingRecorder\runtime\`
  - From the git checkout at `C:\meeting-recorder-v2\backend\`
- All file system paths in Python code should come from `config.settings.Settings` or `Path(__file__).resolve().parent`, never hardcoded

---

## Current state (what's working, what's not)

### Working on dev PC ✅ (Scenario A above)
- v1 venv at `C:\meeting_recorder\.venv\` handles everything
- Currently Rust is pointed at the dev checkout (`C:\meeting-recorder-v2\backend\server.py`) + legacy venv
- This config is running fine right now. Do not touch without user approval.
- **Important**: `C:\meeting-recorder-v2\backend\python.disabled\` exists (renamed from `python/`) and `C:\meeting-recorder-v2\backend-bundle.zip.broken` exists (renamed from `backend-bundle.zip`) — this is deliberate. They force Rust to fall through to the v1 venv path. If you "helpfully" rename them back you will break the dev PC. Don't.

### Broken on work laptop ❌ (Scenario B above)
- The "self-contained installer" (v2.1.x line) never successfully worked. Every attempt hit a different wall:
  - v2.1.0 → v2.1.3: embeddable Python + wrong dep versions, various import errors, GPU-toggle UX bugs, backend killed by corporate AV on PowerShell subprocess
  - v2.1.4: switched to Python 3.13 + pinned lightning/speechbrain — `DiagnosticOptions` import error because lightning 2.2.5 incompatible with torch 2.6.0
  - v2.1.5: Python version mismatch detection to force re-extract
  - v2.1.6: **stdlib was being stripped from the zip bundle** by `zip-bundle.py` excluding `.pyc` files — the embeddable Python stdlib IS `.pyc`. Fixed this but then lightning issue resurfaces.
- All of v2.1.x releases have been deleted from GitHub Releases. Only v2.0.0, v2.0.1, v2.0.2 remain. See "Release history" below.

### Laptop data
- User made a recording on the laptop (session `02C84053`, 25.5 seconds of audio)
- Data is still there at `C:\Users\Joshua.Rodriguez\AppData\Roaming\MeetingRecorder\recordings\session_02C84053.{wav,json}`
- The v2.1.x app defaults to `%LOCALAPPDATA%` path, so the laptop's new install might not see that recording. If needed, set `RECORDINGS_DIR=C:\Users\Joshua.Rodriguez\AppData\Roaming\MeetingRecorder\recordings` in `%LOCALAPPDATA%\MeetingRecorder\config.env` on the laptop.

---

## Known bugs / gotchas (don't repeat these)

### 1. `speechbrain` LazyModule + `pytorch-lightning` `is_scripting()` = infinite recursion
- **Only happens on Python embeddable distributions**, not on regular Python installs
- speechbrain 1.0+ wraps modules in a `LazyModule` whose `__getattr__` calls `inspect.getframeinfo`. When lightning 2.3+ calls `inspect.stack()` during module import, `inspect.getmodule()` walks sys.modules and hits LazyModule → infinite recursion
- v1 doesn't hit this because it uses a regular Python install (`C:\Users\joshu\AppData\Local\Programs\Python\Python313\`) where `inspect`'s behavior is slightly different
- I tried monkey-patching, eager-loading submodules, pinning lightning 2.2.5 — none fully worked on embeddable Python
- **Conclusion**: embeddable Python approach is probably a dead end for this dep stack. Ship a real Python installer instead (see "proposed path forward").

### 2. `zip-bundle.py` stripping stdlib
- Originally `SKIP_PATTERNS = ("__pycache__", ".pyc")` — but the embeddable Python's stdlib is ALL `.pyc` files under `python/Lib/`. Dropping `.pyc` silently stripped the entire stdlib → Python fails to start with `ModuleNotFoundError: No module named 'encodings'`
- Fixed: `SKIP_PATTERNS = ("__pycache__",)` only. **Keep it that way.**

### 3. `tar.exe` shows a console window on Windows
- Must pass `CREATE_NO_WINDOW` (`0x08000000`) flag when spawning tar.exe from Rust
- Otherwise a cmd window pops up and app startup blocks until user closes it
- Fixed in `src-tauri/src/lib.rs` → `ensure_runtime_extracted`. Don't remove.

### 4. PowerShell subprocess from Python killed by corporate AV
- `_detect_gpu_hardware()` originally did `subprocess.run(["powershell", ...])` to query GPUs via `Get-CimInstance`
- SentinelOne / CrowdStrike / AppLocker sees unsigned-parent spawning PowerShell and kills the whole parent Python process
- Fixed: replaced with pure `winreg` read from `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-...}`. No subprocess. Don't add subprocess calls back.

### 5. Outlook COM concurrency deadlock
- Two Python threads initializing COM as STA and calling Outlook simultaneously → one hangs indefinitely waiting for the other's message pump
- Fixed: module-level `_OUTLOOK_LOCK` in `backend/services/calendar_service.py` serializes all Outlook access. Per-key in-flight dedup too. **Don't remove the lock.**

### 6. pywintypes.datetime timezone weirdness
- Outlook COM returns `pywintypes.datetime` with `tzinfo=UTC` stamped, but the numeric field values are already in local time
- Calling `.astimezone()` double-shifts them → 4:40 PM appears as 11:40 AM
- Fixed: `_to_local_naive()` in `calendar_service.py` just copies `.year/.month/.hour/...` and ignores tzinfo. **Don't "fix" this by calling astimezone.**

### 7. NSIS resource glob stack overflow
- Tauri's build script recursively expands `"backend/**"` resource glob at compile time
- With a 40k+ file tree the recursion overflows the default Rust stack
- Fixed: bundle a single `backend-bundle.zip` resource instead of a glob. Don't go back to globbing the directory.

### 8. Large `.lib` files in torch install
- Torch CPU wheel ships 600+ MB of static `.lib` files (compile-time-only artifacts)
- Can be safely deleted from the bundled runtime: `rm backend/python/Lib/site-packages/torch/**/*.lib`
- Drops bundle size significantly

### 9. Sonnet vs Haiku accidental charges
- User's `config.env` had `CLAUDE_MODEL=claude-sonnet-4-5` at one point, causing ~4× API costs
- Default is `claude-haiku-4-5`. Default lives in `backend/config/settings.py` AND `backend/setup.py`. Keep both consistent.

### 10. OneDrive Known Folder Move
- User's laptop has `%APPDATA%` (Roaming) redirected to OneDrive by TTEC policy — NO WAIT, this turned out to be wrong. I initially assumed redirect, but user was just manually copying logs to OneDrive to share them. `%APPDATA%` is NOT redirected on his laptop.
- I already switched logs + config to `%LOCALAPPDATA%` anyway, which is architecturally correct (non-roaming data) regardless.

---

## Version pins that matter

The **v1 stack** on user's dev PC works:

- Python 3.13.2 (regular install, not embeddable)
- torch 2.6.0+cu124
- torchaudio 2.6.0+cu124
- speechbrain 1.0.3
- pytorch-lightning 2.6.1
- lightning 2.6.1
- numpy 2.1.3
- pyannote.audio 3.3.2

These are in `C:\meeting_recorder\.venv\` on dev PC. That venv is left over from v1 and v2 reuses it.

For the bundled installer, the CPU-equivalent matching set would be:
- Python 3.13.x (embeddable OR regular installer — embeddable has the speechbrain recursion issue, see bug #1)
- torch 2.6.0+cpu
- torchaudio 2.6.0+cpu
- Everything else the same

Other combinations I tested that DON'T work, don't waste time re-trying them:
- torch 2.2.2 + lightning 2.2.5: numpy ABI mismatch + Diag options missing
- torch 2.6.0 + lightning 2.2.5: `ImportError: cannot import name 'DiagnosticOptions' from 'torch.onnx._internal.exporter'`
- torch 2.6.0 + lightning 2.6.1 + speechbrain 1.0.3 on embeddable Python: LazyModule recursion
- torch 2.11: `torchaudio.AudioMetaData` was removed, pyannote.audio 3.3.2 breaks

---

## File / folder layout

```
C:\meeting-recorder-v2\
├── src/                              Next.js frontend (React + TypeScript)
│   ├── app/page.tsx                  Main layout + backend-ready polling
│   ├── components/
│   │   ├── record-view.tsx
│   │   ├── sessions-view.tsx
│   │   ├── session-detail-dialog.tsx (tabs: overview, transcript, speakers, summary, actions, decisions, requirements; inline audio player; speaker rename)
│   │   ├── clients-view.tsx          (nested projects via chip row)
│   │   ├── follow-ups-view.tsx
│   │   ├── decisions-view.tsx
│   │   ├── search-view.tsx
│   │   ├── prep-brief-view.tsx
│   │   ├── settings-view.tsx         (API keys with detailed setup instructions, retention, workflow toggles)
│   │   ├── gpu-acceleration-card.tsx (CPU/CUDA/DirectML toggle, restart-required banner)
│   │   ├── calendar-monitor.tsx      (background poll + Windows toast via Tauri plugin)
│   │   ├── usage-guide-view.tsx      (in-app help with token + Whisper model tables)
│   │   └── ui/                       (shadcn-ish Base UI primitives)
│   └── lib/api.ts                    (fetch wrappers for backend endpoints)
├── src-tauri/                        Rust shell
│   ├── src/lib.rs                    (spawn, watchdog, runtime extraction, backend lifecycle)
│   ├── tauri.conf.json               (bundles backend-bundle.zip; NSIS target only)
│   ├── capabilities/default.json     (notification + window permissions)
│   └── Cargo.toml                    (tauri 2.10.3, tauri-plugin-notification 2, tauri-plugin-log 2)
├── backend/                          Python FastAPI sidecar
│   ├── server.py                     (all endpoints; numpy.NaN patch; torch.load weights_only patch at top)
│   ├── requirements-cpu.txt          (pinned CPU dep versions)
│   ├── config/settings.py            (USER_DATA_DIR via LOCALAPPDATA; env migration from APPDATA)
│   ├── core/
│   │   ├── audio_capture.py          (WASAPI/MME/DirectSound/WDM-KS fallback chain; 2-3s stop timeouts)
│   │   ├── transcription.py          (faster-whisper wrapper)
│   │   ├── diarization.py            (pyannote wrapper)
│   │   └── summarizer.py             (anthropic SDK; default claude-haiku-4-5)
│   ├── services/
│   │   ├── calendar_service.py       (Outlook COM; _OUTLOOK_LOCK; 5-min TTL cache; resource-calendar skip)
│   │   ├── recording_service.py      (mic + loopback mixing; session log; WAV save)
│   │   ├── session_service.py        (JSON-on-disk persistence)
│   │   ├── retention_service.py
│   │   └── export_service.py
│   ├── models/session.py, segment.py, speaker.py
│   └── utils/audio_utils.py, logger.py (UTF-8 stdout patch), startup_shortcut.py
├── build-bundle.ps1                  Script that sets up backend/python/ and builds backend-bundle.zip
├── zip-bundle.py                     Python script that actually creates the zip (SKIP_PATTERNS = ('__pycache__',))
├── setup.py                          Dev setup: creates backend/.venv and installs reqs
├── make_shortcut.py                  Windows desktop shortcut creator (v1 vintage, optional)
├── README.md                         User-facing docs with detailed token setup
├── AGENTS.md, CLAUDE.md              AI assistant hints
├── .gitignore                        Excludes backend/python, backend/.venv, backend-bundle.zip, recordings, .env, .commit-msg.tmp, .release-notes.tmp
└── HANDOFF.md                        (this file)
```

---

## Paths the app uses at runtime

| Purpose                  | Path                                                                        |
|--------------------------|-----------------------------------------------------------------------------|
| Config / API keys        | `%LOCALAPPDATA%\MeetingRecorder\config.env`                                  |
| Recordings (default)     | `%LOCALAPPDATA%\MeetingRecorder\recordings\`                                 |
| Rust log                 | `%LOCALAPPDATA%\MeetingRecorder\rust.log`                                    |
| Backend log              | `%LOCALAPPDATA%\MeetingRecorder\backend.log`                                 |
| Extracted runtime        | `%LOCALAPPDATA%\MeetingRecorder\runtime\`                                    |
| Bundled zip (installed)  | `C:\Program Files\Meeting Recorder\resources\backend-bundle.zip`            |
| HF model cache (pyannote)| `%USERPROFILE%\.cache\huggingface\`                                         |

**On this user's dev PC, recordings live at `C:\meeting_recorder\recordings\`** (v1 location) because `RECORDINGS_DIR` is set in config.env.

---

## Release history

| Version | Status | Notes |
|---------|--------|-------|
| v2.0.0  | Released, on GitHub | Initial v2 release |
| v2.0.1  | Released, on GitHub | Calendar 95× faster, Outlook COM lock, nested projects, speaker rename, audio player, mic host-API fallback, overflow UI hygiene |
| v2.0.2  | Released, on GitHub | Calendar timezone fix (pywintypes.datetime UTC-stamp workaround), native Windows notifications via tauri-plugin-notification |
| v2.1.0 – v2.1.6 | **DELETED from releases** | All failed attempts at self-contained bundled installer. Source commits still exist on `main` but the installers never worked. Don't resurrect them without fixing the underlying bundling approach. |

Real user-facing features *not yet in any shipped release* but present on `main`:
- Token setup walkthrough in Settings UI (Anthropic + HuggingFace)
- Whisper model comparison table in Usage Guide
- GPU Acceleration card (CPU/CUDA/DirectML toggle) — works in UI, install path to embeddable-Python works once models load
- Pure-winreg GPU detection (no subprocess)
- Rust watchdog respawning Python
- 4-minute cold-start timeout on backend readiness

When we finally ship a working installer, these should land as v2.0.3 (per user's instruction: no dot releases past v2.0.2 until things actually work).

---

## What was attempted on the laptop (and why each failed)

1. **Install v2.1.0 through v2.1.3** (embeddable Python 3.11.9 + torch 2.2.2)
   - `speechbrain` 1.1.0 LazyModule + `pytorch-lightning` 2.6.1 `is_scripting()` → infinite RecursionError on model load
   - GPU detection subprocess spawning PowerShell → killed by corporate AV → backend died mid-session → "Failed to fetch"

2. **Install v2.1.4** (embeddable Python 3.13.1 + torch 2.6.0 + lightning 2.2.5)
   - `ImportError: cannot import name 'DiagnosticOptions' from 'torch.onnx._internal.exporter'` — lightning 2.2.5 expects torch 2.2 APIs

3. **Install v2.1.5** (same, fix Python version mismatch forcing re-extract)
   - Same DiagnosticOptions error as 2.1.4

4. **Install v2.1.6** (fix zip-bundle missing stdlib)
   - `ModuleNotFoundError: No module named 'encodings'` — zip script had been stripping `.pyc` files which IS the embeddable stdlib
   - Rebuilt, still broken because lightning 2.2.5 + torch 2.6 incompatibility is a separate bug

5. **Laptop currently**: has `v2.1.6` installed but runtime is crash-looping

---

## Proposed path forward (not yet tried)

The embeddable-Python approach has too many compounding issues. The user's v1 stack uses a **regular Python 3.13 install** and it works. The new installer/launcher should **detect what's on the target machine first** and pick the lightest viable path.

### Detection-first first-launch flow

Before doing anything else, the Rust shell (or a first-run Python bootstrap script) probes the target machine for existing Python installations and reusable components:

```
1. Look for an existing compatible venv belonging to THIS app:
   - %LOCALAPPDATA%\MeetingRecorder\.venv\Scripts\pythonw.exe
   - Verify it has torch, pyannote, faster-whisper by running a smoke test
   - If valid: use it. Skip install. Done.

2. Look for an existing v1 venv (developer machines only):
   - C:\meeting_recorder\.venv\Scripts\pythonw.exe
   - Verify the smoke test
   - If valid: use it. Skip install.

3. Look for a system Python 3.13:
   - `py -3.13 --version` or check standard install paths
   - If present AND writable venv-creation works:
     - python -m venv %LOCALAPPDATA%\MeetingRecorder\.venv
     - pip install -r requirements-cpu.txt
     - Use it.

4. No usable Python found → bundle a Python 3.13 MSI with the installer
   and run it silently at first launch:
     installer.exe /quiet InstallAllUsers=0 PrependPath=0 \
                   TargetDir=%LOCALAPPDATA%\MeetingRecorder\python\
   Then create venv at %LOCALAPPDATA%\MeetingRecorder\.venv\ from that Python
   and pip install reqs.

5. All paths fail (no admin, no internet, corporate AV blocks everything)
   → show a clear error UI explaining what to install manually.
```

Each step should **log what was detected and what path was chosen**, so when things break we can see from the log whether it picked the venv, the system Python, or bundled Python.

### Why detection matters on this user's machines

- **Dev PC already has a working v1 venv** (`C:\meeting_recorder\.venv\`) — installer should detect and reuse it, not create another one
- **Work laptop has nothing** — needs the full bundled install path
- **Future TTEC SAs' laptops** — some will have Python 3.13 system-wide from their dev tooling, others won't. Detect, don't assume.

### Installer size estimate

- **Minimum shell + detection code**: ~30-50 MB
- **Bundled Python 3.13 MSI**: +~30 MB (Python official embeddable is tiny but we'd use the full installer for this path)
- **Bundled pip wheels** (so first launch doesn't need internet): +~500 MB — compressed torch + pyannote + deps
- **Total installer**: ~600 MB max, possibly smaller if we only ship the Python MSI and let pip download at first launch
- **First-launch time on a clean laptop**: 5-10 minutes
- **Subsequent launches**: instant

### Alternatives considered and rejected

- **More attempts at Python embeddable**: every variant has hit a different wall (speechbrain recursion, missing stdlib, lightning/torch version mismatch). Don't go back to this well.
- **Ship the whole v1 venv as a zip** (3 GB compressed): fastest to implement, monster installer. Could be the emergency fallback if the detection approach hits unexpected issues.
- **Docker container**: would solve deps cleanly but requires Docker Desktop on the laptop = bigger dep than a Python installer.

---

## User's explicit constraints (from our conversation)

1. **No more dot releases until it actually works** — user was watching the Releases page fill up with broken installers and said "the release section of this github looks fucking stupid because nothing has fixed this shit"
2. **When we do ship, go back to v2.0.1 as the latest** if it's still the best working version — i.e., don't ship v2.1.x-style half-broken attempts
3. **Stop making changes without knowing the repercussions** — user called out that I was thrashing with rapid rebuilds, each one introducing a new failure mode
4. **Don't delete session data** — user panicked when the app showed 0 sessions (data was safe at the v1 path, just needed RECORDINGS_DIR pointed correctly). Be defensive about not wiping `recordings/` directories.

---

## Immediate next steps for whoever picks this up

1. **Don't touch the dev PC** unless the user asks. It's currently running on the v1 venv + dev checkout and working. Specifically leave alone:
   - `C:\meeting_recorder\.venv\` (v1 venv)
   - `C:\meeting-recorder-v2\backend\python.disabled\` (renamed; don't rename back to `python/`)
   - `C:\meeting-recorder-v2\backend-bundle.zip.broken` (don't restore)
2. **Before making ANY backend/packaging change**: verify the change on a clean VM or at minimum with `Get-Process pythonw | Stop-Process` followed by a manual smoke test. Do not ship.
3. **For the laptop problem**: confirm with the user they want to attempt it before doing anything. If yes, go with "regular Python installer + venv" path described above. Don't try more embeddable Python variants.
4. **Get the user's `%LOCALAPPDATA%\MeetingRecorder\backend.log` and `rust.log` from the laptop** before any code change — they're the ground truth for what's actually failing.
5. **Respect the user's time**. He's been debugging this with me for hours. Each "I'll rebuild and try again" has cost him 10+ minutes of waiting. Don't propose speculative rebuilds.

---

## Emergency restore for dev PC

If someone accidentally breaks the dev PC's working state:

```powershell
# Close everything
Get-Process meeting-recorder, pythonw, python -ErrorAction SilentlyContinue | Stop-Process -Force

# Make sure broken bundles aren't in the way
Rename-Item "C:\meeting-recorder-v2\backend-bundle.zip" "C:\meeting-recorder-v2\backend-bundle.zip.broken" -ErrorAction SilentlyContinue
Rename-Item "C:\meeting-recorder-v2\backend\python" "C:\meeting-recorder-v2\backend\python.disabled" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\MeetingRecorder\runtime" -ErrorAction SilentlyContinue

# Launch — Rust will fall through to backend/server.py + C:\meeting_recorder\.venv
Start-Process "C:\meeting-recorder-v2\src-tauri\target\release\meeting-recorder.exe"

# Verify
curl http://127.0.0.1:17645/health
```

If `/health` returns `{"status":"ok"}`, the v1 venv fallback is working. Sessions, calendar, everything should work as it did in v2.0.2.

---

## Open questions (things I don't know that matter)

1. **Can we install Python 3.13 silently on the laptop without admin rights?** `InstallAllUsers=0` should work for per-user installs but I haven't tested on a locked-down TTEC machine.
2. **Does the laptop have internet access to PyPI?** If TTEC blocks PyPI, the "download deps on first launch" plan dies. Would need to pre-bundle wheels.
3. **What's SentinelOne's policy on creating new processes from a venv?** If they treat `%LOCALAPPDATA%\MeetingRecorder\.venv\Scripts\pythonw.exe` as suspicious too, we're back in the same hole.
4. **Does the user have admin on the laptop?** If so, installing Python system-wide simplifies things dramatically.

These should be answered before any laptop work resumes.

---

## Git state at handoff

- Branch: `main`
- Latest commit: `0d2ddda` (v2.1.6 stdlib fix — tag was deleted but commit remains)
- Tags present: `v2.0.0`, `v2.0.1`, `v2.0.2`
- Tags deleted: `v2.1.0` through `v2.1.6`
- Uncommitted work:
  - `backend/server.py` had my untested monkey-patch that I REVERTED (the file should not contain any `_safe_getmodule` function)
  - New file: `HANDOFF.md` (this file)
  - Possibly: `.commit-msg.tmp` or `.release-notes.tmp` if I didn't clean them up

Run `git status` after restarting to see what's actually uncommitted.
