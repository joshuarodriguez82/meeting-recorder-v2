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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 17645

# Endpoints that just need "some list", not specific contents — the
# frontend only needs to see an array to get past its loading state.
_EMPTY_LISTS = (
    "/calendar/upcoming", "/calendar/today", "/calendar/meetings",
    "/briefing/today", "/prep-brief/auto/pending",
    "/providers/available-models", "/engagements", "/commitments",
    "/decisions", "/follow-ups", "/insights", "/templates",
    "/speaker-profiles", "/ghost-sessions", "/terminology",
    "/auto-record/blocklist", "/sessions/unprocessed",
)

# Owner-grouping (2026-08): one session whose action_items carry the
# exact real-world owner-string zoo from the Follow Ups filter bug
# report, so screenshotting the Follow Ups view exercises actual
# multi-owner splitting / org-suffix stripping instead of an empty list.
_FOLLOW_UPS_MD = "\n".join([
    "- [ ] **All Sales Team Members**: Prep the QBR deck",
    "- [ ] **Dale/Dan**: Confirm renewal terms",
    "- [ ] **Dan [scrubbed]**: Send updated pricing",
    "- [ ] **Emily**: Schedule kickoff call",
    "- [ ] **Jake (AWS)**: Provision sandbox account",
    "- [ ] **Jeremy**: Review architecture diagram",
    "- [ ] **Josh**: Follow up on SOW",
    "- [ ] **Josh (AWS)**: Loop in AWS TAM",
    "- [ ] **[scrubbed]**: Send meeting notes",
    "- [ ] **Joshua**: Confirm timeline with client",
    "- [ ] **Kamal (Umbrella)**: Share security questionnaire",
    "- [ ] **Karthik**: Review data model",
    "- [ ] **Ken (AWS)**: Set up VPC peering",
    "- [ ] **Lisa**: Draft onboarding checklist",
    "- [ ] **Madonna [scrubbed]**: Approve budget",
    "- [ ] **Mark**: Sign contract",
    "- [ ] **Mark/Josh**: Coordinate go-live date",
    "- [ ] **Melissa**: Update runbook",
    "- [ ] **Melissa & Kendra**: Finalize training materials",
    "- [ ] **Osmo/Craig**: Review network diagram",
    "- [ ] **Osmo/Craig/Josh**: Plan cutover window",
    "- [ ] **Paul**: Approve change request",
    "- [ ] **Paul/Craig/Josh**: Coordinate DR test",
    "- [ ] **Quincy**: Send invoice",
])

_SESSIONS = [{
    "session_id": "owner-grouping-demo",
    "display_name": "Acme Corp — Weekly Sync",
    "client": "Acme Corp",
    "project": "Renewal",
    "started_at": "2026-08-13T15:00:00",
    "action_items": _FOLLOW_UPS_MD,
    "speakers": {},
    # Knowledge Base view (Search + Ask merge): keyword mode matches
    # locally against these metadata fields off the session list, so
    # they need real prose for a typed query to hit anything.
    "summary": "Renewal check-in. Pricing for the 2027 term is still open; "
               "Dan is rebuilding the discount model before the QBR.",
    "decisions": "- Hold the current pricing tier through Q4.",
    "requirements": "- SSO via Okta before go-live.",
    "has_transcript": True,
}]

# Knowledge Base view: /search/semantic returns a UNION of two hit
# shapes — a session-transcript chunk and a Knowledge Folder document
# chunk. Both are here on purpose: the document hit has no session_id,
# and rendering it as a session row is a bug that has shipped before,
# so the harness has to be able to see one on screen.
_SEMANTIC = {
    "query": "",
    "results": [
        {
            "source": "session",
            "session_id": "owner-grouping-demo",
            "display_name": "Acme Corp — Weekly Sync",
            "started_at": "2026-08-13T15:00:00",
            "client": "Acme Corp",
            "project": "Renewal",
            "start_s": 742,
            "end_s": 771,
            "text": "…we agreed to hold the current pricing tier through "
                    "Q4 and revisit the discount model at the QBR…",
            "similarity": 0.81,
        },
        {
            "source": "document",
            "doc_name": "Acme MSA 2026.docx",
            "doc_path": r"G:\My Drive\Knowledge\Acme Corp\Acme MSA 2026.docx",
            "client": "Acme Corp",
            "text": "…pricing set out in Schedule B shall remain fixed for "
                    "the initial term unless renegotiated in writing…",
            "similarity": 0.64,
        },
    ],
}

# Knowledge Base view: the answer half streams over SSE. Fragments are
# sent with a delay so a harness run can watch text accumulate and can
# exercise Stop mid-stream. The `[id @ mm:ss]` form is the citation
# syntax the view parses into click-to-open buttons.
_QA_SOURCES = [{
    "session_id": "owner-grouping-demo",
    "display_name": "Acme Corp — Weekly Sync",
    "started_at": "2026-08-13T15:00:00",
    "client": "Acme Corp",
    "project": "Renewal",
    "start_s": 742,
    "end_s": 771,
    "text": "…we agreed to hold the current pricing tier through Q4…",
    "similarity": 0.81,
}]
_QA_FRAGMENTS = [
    "You held the current pricing tier through Q4 ",
    # Citation ids are alphanumeric in the real backend — the view's
    # parser only accepts [A-Za-z0-9]{4,16}, so a hyphenated id would
    # render as plain text and quietly under-test the citation path.
    "[A1B2C3D4 @ 12:22] ",
    "and pushed the discount-model rework to the QBR. ",
    "The MSA fixes Schedule B pricing for the initial term, ",
    "so any change needs a written amendment.",
]

# Count of /qa/stream requests served this run. Printed on every call so
# a harness run can assert that a search never bills an LLM call and
# that clicking Answer bills exactly one.
_QA_CALLS = 0

# One confirmed group (Josh's spelling variants already merged) plus
# two pending suggestions, so the management dialog has something in
# every section to screenshot.
_OWNER_ALIASES = {"aliases": [{
    "id": "alias-josh",
    "canonical": "Josh",
    "members": ["josh", "[scrubbed]", "joshua"],
}]}
_OWNER_SUGGESTIONS = {"groups": [
    {
        "group_id": "dan",
        "suggested_canonical": "Dan",
        "members": [
            {"key": "dan", "display": "Dan", "count": 1},
            {"key": "dan [scrubbed]", "display": "Dan [scrubbed]", "count": 1},
        ],
    },
    {
        "group_id": "ken",
        "suggested_canonical": "Ken",
        "members": [
            {"key": "ken", "display": "Ken", "count": 1},
            {"key": "kendra", "display": "Kendra", "count": 1},
        ],
    },
]}


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

    if path == "/sessions":
        return _SESSIONS

    # Knowledge Base view's opt-in transcript scan (and the session
    # detail dialog): GET /sessions/<id> has to carry real segments for
    # a transcript-BODY regex match to be possible at all. The word
    # "kubernetes" appears only here, never in the session metadata
    # above, so a hit on it proves the deep scan ran.
    if path.startswith("/sessions/") and path.count("/") == 2:
        return {
            "session_id": "owner-grouping-demo",
            "display_name": "Acme Corp — Weekly Sync",
            "started_at": "2026-08-13T15:00:00",
            "client": "Acme Corp",
            "project": "Renewal",
            "summary": _SESSIONS[0]["summary"],
            "action_items": _FOLLOW_UPS_MD,
            "decisions": _SESSIONS[0]["decisions"],
            "requirements": _SESSIONS[0]["requirements"],
            "attendees": [],
            "notes": "",
            "template": "default",
            # Required, not optional: the detail dialog calls
            # Object.keys(session.speakers) unguarded and throws a
            # runtime TypeError without it.
            "speakers": {},
            "segments": [
                {"speaker_id": "SPEAKER_00", "start": 740.0, "end": 748.0,
                 "text": "So on the platform side we are still running "
                         "kubernetes for the ingest workers."},
                {"speaker_id": "SPEAKER_01", "start": 748.0, "end": 756.0,
                 "text": "Right, and pricing stays where it is until the QBR."},
            ],
        }

    # Knowledge Base view: semantic result union + the corpus count the
    # results header reads.
    if path == "/search/semantic":
        return _SEMANTIC

    if path == "/search/index/status":
        return {
            "available": True,
            "total_sessions": 1,
            "indexed_sessions": 1,
            "model_id": "sentence-transformers/all-MiniLM-L6-v2",
        }

    if path == "/owners/aliases":
        return _OWNER_ALIASES

    if path == "/owners/suggestions":
        return _OWNER_SUGGESTIONS

    if path == "/recording/status":
        return {
            "is_recording": False, "current_status": "",
            "models_loading": False, "is_processing": False,
            "mic_level": 0.0, "system_level": 0.0,
        }

    if path == "/settings":
        return {
            "recordings_dir": r"C:\Users\<you>\MeetingRecordings",
            "session_archive_dir": r"G:\My Drive\MRv2-Archive",
            "cloud_mirror_dir": r"G:\My Drive\MRv2",
            "anthropic_api_key": "", "openai_api_key": "", "hf_token": "",
            "whisper_model": "large-v3", "max_speakers": 6,
            "ai_provider": "anthropic", "claude_model": "claude-haiku-4-5",
        }

    if path == "/diagnostics":
        return {"checks": [], "crash_is_recent": False, "last_crash_at": None}

    # Record view's extension-calendar-source empty state (2026-08
    # change): needs a non-default /calendar/available shape to render
    # at all, since the plain "no meetings" message is the default.
    if path == "/calendar/available":
        return {
            "available": False,
            "source": "extension",
            "last_capture_at": "2026-08-13T13:00:00",
            "event_count": 1,
            "future_event_count": 0,
        }

    # Settings' Chrome Extension card (2026-08 change): needs a
    # plausible /extension/info shape to render the bundled/last-seen
    # version rows and the mismatch warning, not just an empty {}.
    if path == "/extension/info":
        return {
            "bundled_version": "1.2.0",
            "last_seen_version": "1.1.0",
            "last_seen_at": "2026-08-13T13:00:00",
            "status": "update_available",
            "install_path": r"C:\Users\<you>\AppData\Local\MeetingRecorder\chrome-extension",
        }

    if path == "/extension/install":
        return {
            "ok": True,
            "path": r"C:\Users\<you>\AppData\Local\MeetingRecorder\chrome-extension",
            "files": [
                "background.js", "manifest.json", "options.html",
                "options.js", "popup.html", "popup.js",
            ],
            "file_count": 6,
        }

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

    # The Knowledge Base view's answer half is Server-Sent Events over
    # POST, not JSON, so it needs its own branch. Everything else keeps
    # the GET-shaped treatment below.
    def do_POST(self) -> None:
        if self.path.split("?")[0] == "/qa/stream":
            self._qa_stream()
            return
        self.do_GET()

    def _qa_stream(self) -> None:
        global _QA_CALLS
        _QA_CALLS += 1
        print(f"[stub] LLM CALL /qa/stream #{_QA_CALLS}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()

        def send(event: str, data) -> None:
            payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
            self.wfile.write(payload.encode())
            self.wfile.flush()

        try:
            send("sources", _QA_SOURCES)
            for fragment in _QA_FRAGMENTS:
                time.sleep(0.8)
                send("message", {"text": fragment})
            send("done", {})
        except (BrokenPipeError, ConnectionResetError):
            # The user hit Stop — the view aborted the fetch. Expected.
            print(f"[stub] /qa/stream #{_QA_CALLS} cancelled by client",
                  flush=True)

    # PUT/DELETE get the same GET-shaped treatment — the UI mostly just
    # needs "a 200 with a plausible body" to move on, not to persist
    # anything.
    do_PUT = do_DELETE = do_GET

    def log_message(self, *args) -> None:  # noqa: D102 — silence per-request logging
        pass


if __name__ == "__main__":
    # Threading, not the plain HTTPServer: /qa/stream holds its
    # connection open for several seconds, and a single-threaded server
    # would stall every other fetch the page makes while it streams.
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ui-harness stub backend listening on http://{HOST}:{PORT} "
          f"(pid {os.getpid()}) — stop it with: kill {os.getpid()}")
    server.serve_forever()
