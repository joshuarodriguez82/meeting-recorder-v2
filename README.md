# Meeting Recorder v2 (Tauri + Next.js)

Complete UI rewrite of the Python Meeting Recorder as a modern native app.

## Architecture

```
┌──────────────────────────────────────────┐
│   Tauri shell (Rust, native Windows)     │
│  ┌────────────────────────────────────┐  │
│  │  Next.js + React + Tailwind        │  │
│  │  + shadcn/ui components            │  │
│  └────────────────────────────────────┘  │
│               HTTP @ :17645              │
│  ┌────────────────────────────────────┐  │
│  │  Python sidecar (FastAPI)          │  │
│  │  → reuses backend/ (copy of v1     │  │
│  │    services, models, config)       │  │
│  └────────────────────────────────────┘  │
└──────────────────────────────────────────┘
```

- **Frontend:** `src/` — Next.js 15 app router, TypeScript, Tailwind CSS, shadcn/ui
- **Rust shell:** `src-tauri/` — spawns the Python sidecar on startup, kills it on close
- **Python backend:** `backend/server.py` — FastAPI wrapper around the existing
  `config/`, `core/`, `models/`, `services/`, `utils/` directories copied from v1

## Status

- [x] Scaffold: Tauri v2 + Next.js + Tailwind + shadcn/ui
- [x] FastAPI backend exposing existing services as HTTP endpoints
- [x] Tauri launches Python sidecar on startup, kills it on close
- [x] Beautiful sidebar + main content layout
- [x] Today's meetings panel (reads Outlook via existing calendar_service)
- [x] Session list panel
- [ ] Recording start/stop wiring (backend endpoints not yet implemented)
- [ ] Settings dialog
- [ ] Follow-up tracker, decision log, transcript search, client dashboard
- [ ] Production packaging (bundle Python venv as resource)

The v1 Python app at `C:\meeting_recorder` stays fully functional during development.

## Dev loop

Prereqs: Node.js 20+, Rust (`rustup default stable`), existing Python venv at
`C:\meeting_recorder\.venv` (used for the FastAPI sidecar in dev).

```powershell
cd meeting-recorder-v2
npm install                # once
npm run tauri dev          # starts Next.js + Rust + Python sidecar
```

The app window opens with hot-reload. The Python backend runs at
`http://127.0.0.1:17645` — you can hit endpoints directly with `curl` for
debugging.

## Building a release

```powershell
npm run tauri build
```

Produces `src-tauri/target/release/bundle/` with the installer.

Note: production packaging of the Python venv is still TODO. Current build
assumes Python venv at a known location (see `src-tauri/src/lib.rs`).
