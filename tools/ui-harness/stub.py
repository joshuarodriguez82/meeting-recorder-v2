"""Minimal stand-in for the Meeting Recorder Python backend.

This is NOT a test double for backend logic — it returns plausible
*shapes*, not real data, and never touches recordings, sessions, or any
AI provider. Its only job is to satisfy the frontend's health gate and
initial-load fetches closely enough that the Next.js app renders past
its loading state, so a real browser can be driven against it to check
LAYOUT — spacing, sticky headers, scroll containers — without needing
the real backend's heavy stack (torch, pyannote, platform audio APIs,
COM/EventKit) inside a container that can't run any of that.

Do not extend this into a real backend simulator. If a view needs a
richer shape than `body_for` returns, add the minimum literal dict that
makes that view render, with a comment saying which view needed it —
not general-purpose request handling.

Listens on 127.0.0.1:17645, which is the frontend's own hardcoded
fallback base URL when it isn't running inside the Tauri shell (see
src/lib/api.ts's getBaseUrl(): `http://127.0.0.1:${port}` from
get_backend_port() under Tauri, else `http://127.0.0.1:17645`). So the
frontend finds this stub with zero configuration in a plain `next dev`
browser session — no env var, no code change.

Usage:
    python3 stub.py

Stopping it: `pkill -f stub.py` also matches the invoking shell's own
command line (it contains the string "stub.py" too) and can kill your
own shell instead. Note the PID this prints and `kill <pid>` instead.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST, PORT = "127.0.0.1", 17645

# Endpoints that just need "some list", not specific contents — the
# frontend only needs to see an array to get past its loading state.
_EMPTY_LISTS = (
    "/calendar/upcoming", "/calendar/today", "/calendar/meetings",
    "/briefing/today", "/prep-brief/auto/pending",
    "/providers/available-models", "/engagements", "/commitments",
    "/decisions", "/follow-ups", "/insights", "/templates",
    "/speaker-profiles", "/sessions", "/ghost-sessions", "/terminology",
    "/auto-record/blocklist", "/sessions/unprocessed",
)


def body_for(path: str) -> dict | list:
    if path == "/health":
        return {"ok": True, "status": "ok"}

    if path == "/audio/devices":
        return {
            "input": [{"index": 0, "name": "Microphone (AIRHUG 21)"}],
            "output": [{
                "index": 1,
                "name": "Speakers (Realtek(R) Audio) [Loopback]",
            }],
        }

    if path in _EMPTY_LISTS:
        return []

    if path == "/recording/status":
        return {
            "is_recording": False, "current_status": "",
            "models_loading": False, "is_processing": False,
            "mic_level": 0.0, "system_level": 0.0,
        }

    if path == "/settings":
        return {
            "recordings_dir": r"C:\\Users\\<you>\MeetingRecordings",
            "session_archive_dir": r"G:\My Drive\MRv2-Archive",
            "cloud_mirror_dir": r"G:\My Drive\MRv2",
            "anthropic_api_key": "", "openai_api_key": "", "hf_token": "",
            "whisper_model": "large-v3", "max_speakers": 6,
            "ai_provider": "anthropic", "claude_model": "claude-haiku-4-5",
        }

    if path == "/diagnostics":
        return {"checks": [], "crash_is_recent": False, "last_crash_at": None}

    return {}


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        raw = json.dumps(body_for(self.path.split("?")[0])).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    # POST/PUT/DELETE get the same GET-shaped treatment — the UI mostly
    # just needs "a 200 with a plausible body" to move on, not to
    # persist anything.
    do_POST = do_PUT = do_DELETE = do_GET

    def log_message(self, *args) -> None:  # noqa: D102 — silence per-request logging
        pass


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    print(f"ui-harness stub backend listening on http://{HOST}:{PORT} "
          f"(pid {os.getpid()}) — stop it with: kill {os.getpid()}")
    server.serve_forever()
