"""
FastAPI sidecar server for the Tauri frontend.
Exposes the Python services as HTTP endpoints.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time

# ── Native-crash hardening (MUST run before any ML import) ──────────
# faster-whisper (ctranslate2) and pyannote (torch) each bundle their
# own OpenMP runtime. Loading both into one Windows process can abort
# with STATUS_ACCESS_VIOLATION (0xC0000005) at native init — the
# 2026-07-21 rust.log shows the backend segfaulting repeatedly during
# "Loading transcription engine", crash-looping through watchdog
# respawns. KMP_DUPLICATE_LIB_OK is Intel OpenMP's documented escape
# hatch for exactly this double-runtime case. setdefault so a user-set
# value still wins.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# Set CWD to this file's directory so relative paths (like "recordings/")
# resolve consistently regardless of how the server was launched.
os.chdir(Path(__file__).resolve().parent)
# Also ensure backend dir is on sys.path so `config`, `services`, etc.
# import cleanly even if launched with an odd CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ── Native-crash capture — MUST be the first thing after sys.path ────
#
# The backend has been exiting with 3221225477 (0xC0000005,
# STATUS_ACCESS_VIOLATION) on Windows since v2.0.18, and it is still
# undiagnosed in v2.19.3 for one reason: an access violation kills the
# interpreter before Python can log anything, so backend.log just stops
# mid-line. Five hypotheses have been proposed and dismissed on the
# strength of restart-timing guesswork alone (field report 2026-08-11).
# faulthandler installs an OS-level handler that dumps every thread's
# Python traceback with a raw write() to a pre-opened fd — it works
# from inside the fault. Enabling it HERE, before the dependency
# self-heal and before numpy / torch / faster-whisper / pyannote, means
# a fault during native library init (the 2026-07-21 "Loading
# transcription engine" crash loop) is captured too.
#
# utils.crash_log is stdlib-only on purpose so this cannot itself be
# the thing that stops the backend booting.
try:
    from utils.crash_log import enable_crash_logging as _enable_crash_logging
    _CRASH_LOG_PATH = _enable_crash_logging()
    if _CRASH_LOG_PATH:
        sys.stderr.write(f"[crash] faulthandler → {_CRASH_LOG_PATH}\n")
    else:
        sys.stderr.write(
            "[crash] crash.log unavailable; faulthandler is on stderr only\n")
except Exception as _e:  # noqa: BLE001
    # A backend that won't start because its crash logger failed is
    # strictly worse than a backend with no crash logger.
    _CRASH_LOG_PATH = None
    try:
        sys.stderr.write(f"[crash] faulthandler setup failed: {_e}\n")
    except Exception:
        pass

# Windows-specific hardening to stop pythonw.exe from ever showing a
# visible window. We hit two distinct problems in the wild:
#
# 1. WER dialog on crash. Certain access-violation crashes during DLL
#    load (corporate EDR scanning torch/CUDA DLLs mid-load) pop up the
#    classic "pythonw.exe stopped working" dialog. SetErrorMode with
#    SEM_NOGPFAULTERRORBOX suppresses it; the process still exits with
#    the error code so the Rust watchdog can respawn.
#
# 2. Phantom console window (the "black cmd window with pythonw.exe in
#    the tab" users report). CREATE_NO_WINDOW on the Rust side only
#    prevents a console at spawn time. Several native libraries
#    (certain Intel MKL / OpenMP runtime versions, some CUDA runtime
#    builds) call AllocConsole() at runtime to install a console
#    control handler. AllocConsole creates a visible conhost-hosted
#    window titled with the parent EXE name — "pythonw.exe". Closing
#    it only kills conhost; any next call to AllocConsole reopens one.
#    Fix: call FreeConsole() once up front, then every 2 seconds
#    forever in a background thread — any console a library sneaks in
#    gets detached almost immediately. pythonw is the only client, so
#    conhost exits with it and the window closes.
if os.name == "nt":
    try:
        import ctypes
        SEM_FAILCRITICALERRORS = 0x0001
        SEM_NOGPFAULTERRORBOX = 0x0002
        ctypes.windll.kernel32.SetErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
    except Exception:
        # Don't let SetErrorMode failure stop the backend from coming up —
        # worst case the WER dialog still shows on a rare crash.
        pass

    try:
        import ctypes
        import threading as _threading_for_console_watchdog
        _k32 = ctypes.windll.kernel32
        # Detach from any console we might have (spawned or inherited).
        # Safe to call when no console is attached — returns 0 and sets
        # last-error, which we ignore.
        _k32.FreeConsole()

        def _console_watchdog():
            """
            Poll for a console window allocated by native libs and detach.

            Intel MKL / OpenMP / CUDA runtimes in some configurations call
            AllocConsole() during their first real use (not just import),
            so a one-shot FreeConsole at startup isn't enough. 2-second
            polling is frequent enough that any console that appears is
            gone before the user notices.
            """
            import time as _time
            while True:
                try:
                    if _k32.GetConsoleWindow() != 0:
                        _k32.FreeConsole()
                except Exception:
                    # Never let this thread die — a bad call is better than
                    # a phantom console window we can't close.
                    pass
                _time.sleep(2)

        _t = _threading_for_console_watchdog.Thread(
            target=_console_watchdog, daemon=True, name="console-watchdog")
        _t.start()
    except Exception:
        # Console-hiding is best-effort. If it fails the user may still
        # see a phantom console in rare cases but the backend still runs.
        pass

# Intel Fortran runtime (shipped with numpy/scipy/torch's MKL) installs a
# Windows console-close handler that aborts the Python process with exit
# code 200 ("forrtl: error (200): program aborting due to window-CLOSE
# event"). This fires when pyannote.audio loads on pythonw.exe: MKL
# attaches a transient console to install the handler, Windows raises the
# CLOSE event when the console detaches, the handler kills the process.
# These env vars tell the Fortran runtime to skip the handler entirely.
# Must be set BEFORE importing numpy/torch/scipy, which is why it's here
# at the very top of server.py (the Rust shell also sets them on spawn).
os.environ.setdefault("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")
os.environ.setdefault("FOR_DISABLE_STACK_TRACE", "1")


# ── Dependency self-heal ────────────────────────────────────────────
#
# Auto-repair the venv when a feature's package is missing. Fires on
# every backend boot but only does work when a critical import fails —
# otherwise it's ~10ms of import-testing.
#
# What this catches that the Rust-side bootstrap (lib.rs::bootstrap_app_venv)
# doesn't:
#
#   1. An interrupted pip install on initial bootstrap. Wheels download in
#      sequence; if the user's network blipped on whichever batch was
#      installing sentence-transformers, the venv ends up with everything
#      else but missing that one package. The bootstrap fingerprint check
#      sees a populated venv and skips re-install on next launch, so the
#      missing package stays missing forever.
#
#   2. Upgrades where a new package gets added to requirements-{cpu,mac}.txt
#      after the user's venv was created. The bootstrap version-fingerprint
#      check (commit ca836ad) is supposed to detect this and re-pip-install,
#      but it's only as good as the fingerprint scheme. This is a runtime
#      defense-in-depth — even if the Rust shell decided not to re-bootstrap,
#      we re-run pip install here for the missing pieces.
#
#   3. User manually pip-uninstalled something, then complained that
#      Settings → Semantic Index says "not installed."
#
# The trade-off: backend boot stalls for ~30s the ONE time we have to
# repair. The watchdog in lib.rs uses try_wait() (process-alive check)
# rather than port-check, so it doesn't respawn the backend during the
# repair pip install.

def _verify_and_repair_dependencies() -> None:
    """Check critical packages are installed (metadata only — no
    imports). If any are missing, re-run `pip install -r
    requirements-*.txt` to repair the venv in place."""
    import importlib.util

    # Map of (importable module name) → (human label for log lines).
    # Whisper / pyannote / faster-whisper are intentionally NOT here:
    # they fail loudly on first /process call so the user notices.
    # The packages below are different — their absence shows up as a
    # quiet "feature not installed" warning in Settings that's easy
    # to miss until you go looking for the feature.
    critical = {
        "sentence_transformers": "sentence-transformers (semantic search + Q&A)",
        "speechbrain": "speechbrain (cross-session speaker fingerprints)",
        "anthropic": "anthropic (default LLM provider)",
    }
    missing: list[str] = []
    for module, label in critical.items():
        # find_spec checks installed-ness from package metadata WITHOUT
        # executing the import. The old import_module() check silently
        # imported sentence_transformers + speechbrain — both of which
        # pull in torch — at process start, BEFORE uvicorn could bind.
        # On corporate machines where AV scans every native DLL, that
        # held /health hostage for 30s–3min and the frontend sat on
        # "Starting backend…" the whole time. Metadata check: <10ms.
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(label)
            sys.stderr.write(
                f"[deps] {module} is missing from the venv\n")

    if not missing:
        return

    # Pick the right requirements file for the platform — same logic
    # as the Rust shell's requirements_filename().
    req_name = "requirements-cpu.txt" if os.name == "nt" else "requirements-mac.txt"
    req_path = Path(__file__).resolve().parent / req_name
    if not req_path.exists():
        # Fall back to the generic file if the platform-specific one
        # got dropped. Better than no-op when the bundle is in a weird
        # state.
        fallback = Path(__file__).resolve().parent / "requirements.txt"
        if fallback.exists():
            req_path = fallback
        else:
            sys.stderr.write(
                f"[deps] cannot self-heal — no requirements file found at "
                f"{req_path}. Reinstall Meeting Recorder.\n")
            return

    sys.stderr.write(
        f"[deps] missing: {', '.join(missing)}. "
        f"Re-running pip install -r {req_path.name} to repair the venv. "
        f"This is a one-time ~30s pause; subsequent launches won't repeat it.\n"
    )

    # Use the SAME interpreter the Rust shell launched us with — that's
    # the venv's python. We pin a 10-min timeout because torch wheels
    # are big; on a cold pip cache the repair can take a few minutes.
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check",
        "-r", str(req_path),
    ]
    # Same pin set the bootstrap installs against (freeze-deps.yml →
    # constraints-{cpu,mac}.txt). A repair that resolves fresh could
    # otherwise pull versions the tested resolution never saw. Missing
    # file degrades to the old floating behavior.
    constraints_path = req_path.with_name(
        req_path.name.replace("requirements", "constraints"))
    if constraints_path.exists():
        cmd += ["-c", str(constraints_path)]
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=600,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            "[deps] pip install timed out after 10 min. "
            "Manually run `pip install -r {}` in the venv.\n".format(req_path))
        return
    except Exception as e:
        sys.stderr.write(f"[deps] repair raised: {e}\n")
        return

    if result.returncode == 0:
        sys.stderr.write("[deps] repair pip install completed.\n")
    else:
        # Surface the last few lines of pip's output to the backend.log
        # so the user has a fighting chance of diagnosing wheel-build
        # failures (e.g. xcode-select missing on Mac).
        tail = (result.stderr or result.stdout or "").splitlines()[-15:]
        sys.stderr.write(
            f"[deps] pip exited {result.returncode}. Last lines:\n  "
            + "\n  ".join(tail) + "\n"
        )


# Run before any heavy imports below — if torch / pyannote / whisper
# are also missing, that's a deeper venv corruption the regular
# repair can't fix (importing torch may itself crash). We let those
# fail loudly at their use site instead.
#
# Skippable via MEETING_RECORDER_SKIP_DEP_REPAIR=1. The repair shells
# out to `pip install` at import time, which is correct for the
# packaged app (self-heals a corrupt venv) but makes `import server`
# impossible in CI / tests without a full ML venv. Setting the flag
# lets the route-parity smoke test import the app headlessly — heavy
# deps stay lazy and fail only at their use site, which the smoke test
# never reaches.
if os.environ.get("MEETING_RECORDER_SKIP_DEP_REPAIR") != "1":
    _verify_and_repair_dependencies()


# Compatibility patches needed before importing pyannote/torch:
#   - NumPy 2.0 removed np.NaN (pyannote uses it)
#   - PyTorch 2.6 changed torch.load default to weights_only=True (pyannote breaks)
import numpy as _np
if not hasattr(_np, 'NaN'):
    _np.NaN = _np.nan
if not hasattr(_np, 'NAN'):
    _np.NAN = _np.nan

try:
    import torch as _torch
    from torch.torch_version import TorchVersion as _TorchVersion
    _torch.serialization.add_safe_globals([_TorchVersion])
    _orig_torch_load = _torch.load
    def _patched_torch_load(f, *args, **kwargs):
        kwargs['weights_only'] = False
        return _orig_torch_load(f, *args, **kwargs)
    _torch.load = _patched_torch_load
except Exception:
    # torch not installed or can't patch — will fail later with clearer msg
    pass

# speechbrain 1.0+ wraps its top-level package in a LazyModule that
# hijacks __getattr__ to trigger submodule imports via inspect. When
# pytorch_lightning.utilities.model_helpers.is_scripting (called during
# pyannote.audio's Pipeline construction) iterates inspect.stack(),
# inspect.getmodule() reaches into the LazyModule, which itself calls
# inspect.getframeinfo() — infinite recursion, crash.
# Workaround: eagerly import every lazy-proxied speechbrain submodule
# so the LazyModule never needs to resolve anything on demand.
try:
    import importlib as _importlib
    import speechbrain as _sb
    # These are the ones pytorch_lightning touches transitively; force
    # them fully loaded before any pyannote import happens.
    for _sub in ("utils", "utils.importutils", "utils.quirks",
                 "utils.checkpoints", "utils.data_utils",
                 "inference", "pretrained",
                 "dataio", "nnet", "lobes", "processing"):
        try:
            _importlib.import_module(f"speechbrain.{_sub}")
        except Exception:
            pass
    # Touch every attribute on the top-level package so any remaining
    # lazy shims resolve now instead of during inspect.stack walking.
    for _attr in list(dir(_sb)):
        try:
            getattr(_sb, _attr)
        except Exception:
            pass
    # Also bump recursion limit — belt and suspenders. Pytorch-lightning's
    # is_scripting check walks every frame; on some Python layouts that
    # walks quite deep.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 5000))
except Exception:
    pass

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from config.settings import Settings, USER_DATA_DIR
from core.audio_capture import list_input_devices, list_output_devices
from core._precision import no_invented_precision
from services.template_service import TemplateService
from services.copilot_mode_service import CoPilotModeService
from services.copilot_meeting_type_service import CoPilotMeetingTypeService
from services.engagement_overlay_service import (
    EngagementOverlayService, KNOWN_STATUSES,
)
from services.daily_briefing_service import (
    BriefingUnreadableError,
    DailyBriefingService,
)
from services.extension_calendar_service import (
    ExtensionCalendarService, describe_structured_source, events_from_briefing,
    events_from_structured,
)
# The single merged two-source calendar view shared by
# /calendar/upcoming and AutoRecordService — see its module docstring.
from services import calendar_feed
from services.extension_bundle_service import (
    bundled_extension_version, export_dir as extension_export_dir,
    export_extension_files, extension_version_status,
)
from services import mcp_bundle_service
from services.outlook_web_scraper import (
    OutlookAuthExpired, OutlookScraperError, OutlookScraperUnavailable,
    format_for_briefing_parser, open_signin_window,
    scrape_today_briefing_text, scrape_today_teams_text,
)
from services.terminology_service import TerminologyService
from services.prep_brief_cache_service import (
    PrepBriefCacheService, meeting_key as _prep_meeting_key,
)
from services.prep_brief_context import (
    MAX_CONTEXT_SESSIONS as _PREP_MAX_SESSIONS,
    MAX_FALLBACK_SESSIONS as _PREP_MAX_FALLBACK_SESSIONS,
    format_document_context as _prep_format_documents,
    referenced_documents as _prep_referenced_documents,
    retrieve_for_brief as _prep_retrieve_documents,
)
from models.session import Session
from services.calendar_service import (
    get_todays_meetings, get_upcoming_meetings, is_outlook_available,
)
from services._cloud_sync import CloudFileNotReadyError
from services.client_config_service import ClientConfig, ClientConfigService
from services import document_service
from services.engagement_service import EngagementService
from services.portal_push_service import (
    PortalBindingBroken, PortalPermanent, PortalPushService, PortalTransient)
from services.export_service import ExportService
from services.export_worker import ExportWorker, PortalPushWorker, resolve_export_folder
from services import export_reconcile
from services import archive_reconcile
from services import shared_state_sync
from services.shared_state_sync import CLIENT_CONFIGS_FILE
from services.recording_service import RecordingService
from services.retention_service import cleanup as run_retention_cleanup, folder_stats
from services.recovery_service import recover_orphans
from services.commitments_service import (
    CommitmentsService, extract_commitments_from_session,
)
from services.item_status_service import (
    ItemStatusService, VALID_DECISION_STATUSES,
)
from services.owner_service import (
    OwnerAliasStore, aggregate_raw_owners, load_alias_index, suggest_groups,
)
from services.qa_service import QAService
from services.auto_record_blocklist_service import AutoRecordBlocklistService
from services.search_service import SearchService
from services.session_service import SessionService
from services.speaker_profile_service import (
    SpeakerProfile, SpeakerProfileService,
)
from utils import events
from utils import diagnostics_bundle
from utils.logger import get_logger

# Heavy ML imports deferred to avoid blocking startup. These load torch +
# pyannote which take several seconds. Imported lazily inside
# ensure_models_loaded() so the API is reachable within ~500ms of launch.
TranscriptionEngine = None  # type: ignore
DiarizationEngine = None  # type: ignore
Summarizer = None  # type: ignore

logger = get_logger(__name__)

app = FastAPI(title="Meeting Recorder Backend", version="2.0.0")

# Only the Tauri WebView (and the Next dev server during `npm run dev`)
# are legitimate browsers for this API. The old `allow_origins=["*"]`
# meant any webpage the user visited could read responses from
# 127.0.0.1 — combined with no auth, that was a drive-by exfiltration
# surface. CORS is the first layer; the token middleware below is the
# real boundary (CORS doesn't stop non-browser clients or no-cors
# state-changing requests).
_ALLOWED_ORIGINS = [
    "http://tauri.localhost",    # Tauri v2 WebView origin on Windows
    "https://tauri.localhost",
    "tauri://localhost",         # Tauri v2 WebView origin on macOS/Linux
    "http://localhost:3000",     # `npm run dev` against a manual backend
    "http://127.0.0.1:3000",
]

# Chrome extensions originate from chrome-extension://<id>/. We can't
# enumerate the user's installed extension ID at startup (the ID is
# derived from the unpacked extension's path on disk), so we match
# the whole scheme via allow_origin_regex. Safe because:
#   - The backend still requires a per-launch Bearer token on EVERY
#     mutation request (see token middleware below). CORS without the
#     token gets you nothing.
#   - The extension is user-installed; allowing chrome-extension://
#     doesn't open up arbitrary websites — they have a different
#     origin scheme entirely.
_ALLOWED_ORIGIN_REGEX = r"^chrome-extension://[a-z0-9]+/?$"

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Sidecar auth ─────────────────────────────────────────────────────
#
# Binding to 127.0.0.1 keeps remote hosts out but NOT other local
# processes or webpages — any browser tab can fetch http://127.0.0.1:*.
# The Tauri shell generates a per-launch random token
# (lib.rs::generate_backend_token), hands it to us via the
# MEETING_RECORDER_TOKEN env var and to the frontend via the
# get_backend_token IPC command. Every request must present it:
#
#   - Authorization: Bearer <token>   (the api.ts request wrapper)
#   - ?token=<token>                  (EventSource and <audio>/<img>
#                                      src URLs, which can't set headers)
#
# Fails CLOSED: if MEETING_RECORDER_TOKEN is absent, every non-exempt
# request gets 401 rather than sailing through unauthenticated. The
# packaged app always injects the token (see lib.rs::generate_backend_token
# / the single spawn call site that sets MEETING_RECORDER_TOKEN before
# starting server.py) so this can never lock the shipped app out.
#
# For the one legitimate case where there IS no token — running
# `python server.py` standalone for debugging — set
# MEETING_RECORDER_AUTH_DISABLED=1 explicitly. That is an opt-in dev
# escape hatch, never a default: an unset token no longer silently
# disables auth on its own.
#
# /health stays open: it's the liveness probe and carries nothing
# sensitive — same reasoning as exposing a dial-tone test number.

_AUTH_TOKEN = os.environ.get("MEETING_RECORDER_TOKEN", "").strip()
_AUTH_DISABLED = os.environ.get(
    "MEETING_RECORDER_AUTH_DISABLED", "").strip() == "1"
_AUTH_EXEMPT_PATHS = frozenset({"/health"})

if _AUTH_DISABLED:
    logger.warning(
        "MEETING_RECORDER_AUTH_DISABLED=1 — API auth is DISABLED. Only "
        "ever set this for standalone `python server.py` debugging; the "
        "packaged app never sets it and always injects a real token.")
elif not _AUTH_TOKEN:
    logger.warning(
        "MEETING_RECORDER_TOKEN is not set — failing closed: every "
        "non-exempt request will get 401 until it is set. Set "
        "MEETING_RECORDER_TOKEN for authenticated standalone use, or "
        "MEETING_RECORDER_AUTH_DISABLED=1 to explicitly run without auth.")


def _request_presents_token(request: Request) -> bool:
    """True if the request carries the shared token in either channel."""
    import secrets as _secrets

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        presented = auth_header[len("Bearer "):].strip()
        if presented and _secrets.compare_digest(presented, _AUTH_TOKEN):
            return True
    query_token = request.query_params.get("token", "")
    return bool(query_token) and _secrets.compare_digest(query_token, _AUTH_TOKEN)


@app.middleware("http")
async def require_backend_token(request: Request, call_next):
    if _AUTH_DISABLED or request.method == "OPTIONS" \
            or request.url.path in _AUTH_EXEMPT_PATHS \
            or (_AUTH_TOKEN and _request_presents_token(request)):
        return await call_next(request)
    # RFC 7807 body, matching every other error path in this file.
    return JSONResponse(
        status_code=401,
        media_type="application/problem+json",
        content={
            "type": "tag:meeting-recorder/errors/unauthorized",
            "title": "Unauthorized",
            "status": 401,
            "detail": "Missing or invalid backend auth token.",
            "instance": str(request.url.path),
        },
    )


# ── RFC 7807 Problem Details ────────────────────────────────────────
#
# Every error path now returns application/problem+json with a
# structured body so the frontend (and any future API consumers) get a
# consistent shape with title + detail + a stable type URI, instead of
# the FastAPI default `{"detail": "..."}` for HTTPException and an
# opaque 500 for unhandled exceptions. This is what made tonight's
# `process_full: summary failed` debug a log dive — the route returned
# 200 with no body shape; an RFC 7807 response would have surfaced the
# TypeError straight to the UI.
#
# Wire-format (RFC 7807 §3):
#   {
#     "type":     "tag:meeting-recorder/errors/<slug>",  // stable per error class
#     "title":    "<short human summary>",
#     "status":   <int>,
#     "detail":   "<specifics for this occurrence>",
#     "instance": "/sessions/abc/process_full"
#   }
# Plus optional class-specific extensions (RFC 7807 §3.2 allows them).

PROBLEM_CONTENT_TYPE = "application/problem+json"

_HTTP_STATUS_PHRASE = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _status_phrase(status: int) -> str:
    return _HTTP_STATUS_PHRASE.get(status, f"HTTP {status}")


def _problem_response(
    request: Request,
    *,
    status: int,
    title: str,
    detail: str = "",
    problem_type: str = "about:blank",
    **extensions,
) -> JSONResponse:
    body = {
        "type": problem_type,
        "title": title,
        "status": status,
        "instance": str(request.url.path),
    }
    if detail:
        body["detail"] = detail
    body.update(extensions)
    return JSONResponse(
        content=body, status_code=status, media_type=PROBLEM_CONTENT_TYPE)


@app.exception_handler(StarletteHTTPException)
async def _problem_http_exception_handler(
    request: Request, exc: StarletteHTTPException,
):
    """Convert FastAPI/Starlette HTTPException → RFC 7807. Existing
    `raise HTTPException(status_code=…, detail=…)` calls keep working
    unchanged; they just produce richer responses."""
    detail = exc.detail if isinstance(exc.detail, str) else ""
    return _problem_response(
        request,
        status=exc.status_code,
        title=_status_phrase(exc.status_code),
        detail=detail,
    )


@app.exception_handler(RequestValidationError)
async def _problem_validation_handler(
    request: Request, exc: RequestValidationError,
):
    """Pydantic validation errors get the field-level detail attached
    as an `errors` extension so the frontend can highlight the offending
    field rather than just say "422"."""
    return _problem_response(
        request,
        status=422,
        title="Request validation failed",
        detail="One or more fields failed validation; see `errors`.",
        problem_type="tag:meeting-recorder/errors/validation",
        errors=exc.errors(),
    )


@app.exception_handler(Exception)
async def _problem_unhandled_handler(request: Request, exc: Exception):
    """Catch-all for uncaught exceptions. Logs the full traceback then
    returns a structured 500 with the exception class name as an
    extension — useful for hardware errors ("Microphone in use by
    another app"), library bugs, and anything else that bypassed an
    explicit raise."""
    logger.exception(
        f"Unhandled {exc.__class__.__name__} during "
        f"{request.method} {request.url.path}")
    return _problem_response(
        request,
        status=500,
        title="Internal server error",
        detail=str(exc) or exc.__class__.__name__,
        problem_type="tag:meeting-recorder/errors/unhandled",
        exception_class=exc.__class__.__name__,
    )


# ── Lazy service container ──────────────────────────────────────────
class Services:
    def __init__(self):
        self.settings: Optional[Settings] = None
        # False until load_settings() has built EVERY service below.
        # `self.settings` is assigned on the first line of that method,
        # so it cannot be used as the "already initialised" guard: any
        # exception partway through (see the UnboundLocalError that
        # broke v2.26.0) left settings set, every later service None,
        # and the guard skipping re-initialisation forever — a backend
        # that answered /settings but 500'd on /clients/config and
        # could not record. Gating on completion instead means a failed
        # init is retried on the next call rather than being permanent.
        self._services_ready: bool = False
        self.session_svc: Optional[SessionService] = None
        self.export_svc: Optional[ExportService] = None
        self.client_cfg_svc: Optional[ClientConfigService] = None
        # Confirmed owner-name merges (e.g. "Samantha" -> "Sam"). Same
        # directory, same lifecycle as client_cfg_svc — see
        # services/owner_service.py's OwnerAliasStore docstring.
        self.owner_alias_store: Optional[OwnerAliasStore] = None
        self.engagement_svc: Optional[EngagementService] = None
        self.template_svc: Optional[TemplateService] = None
        self.copilot_mode_svc: Optional[CoPilotModeService] = None
        self.copilot_meeting_type_svc: Optional[CoPilotMeetingTypeService] = None
        self.engagement_overlay_svc: Optional[EngagementOverlayService] = None
        self.daily_briefing_svc: Optional[DailyBriefingService] = None
        # Structured calendar events lifted out of the Chrome
        # extension's import — the second source behind
        # /calendar/upcoming. See services/extension_calendar_service.py.
        self.extension_calendar_svc: Optional[ExtensionCalendarService] = None
        self.terminology_svc: Optional[TerminologyService] = None
        self.prep_brief_cache_svc: Optional[PrepBriefCacheService] = None
        self.recording_svc: Optional[RecordingService] = None
        self.speaker_profile_svc: Optional[SpeakerProfileService] = None
        self.auto_record_blocklist_svc: Optional[AutoRecordBlocklistService] = None
        self.search_svc: Optional[SearchService] = None
        self.qa_svc: Optional[QAService] = None
        self.commitments_svc: Optional[CommitmentsService] = None
        self.item_status_svc: Optional[ItemStatusService] = None
        # Insights aggregator — typed loosely to keep the heavy import
        # off the module level (mirrors how RecordingService is held).
        self.insights_svc = None
        # Calendar-driven auto-recorder. Started/stopped from the
        # /settings handler whenever auto_record_enabled flips.
        self.auto_record_svc = None
        # Set when AutoRecordService fires a new recording so the
        # frontend can show "Auto-recording: <subject>" notification +
        # the persistent recording badge with a meaningful label.
        # Cleared on stop and on manual /recording/start.
        self.auto_record_subject: Optional[str] = None
        # Set when AutoRecordService had to skip a meeting because the
        # user has never run a manual recording (so we have no saved
        # mic/loopback device to use). Frontend polls + surfaces it.
        # Cleared the moment the user starts a recording manually.
        self.auto_record_skip_reason: Optional[str] = None
        self.transcription: Optional[TranscriptionEngine] = None
        self.diarization: Optional[DiarizationEngine] = None
        self.summarizer: Optional[Summarizer] = None
        # Optional override summarizer used by the live co-pilot tick
        # endpoint only. Constructed in load_settings() when
        # `live_ai_provider` is set; otherwise we point this at the same
        # instance as `summarizer` so the tick code can stay branch-free.
        self.live_summarizer: Optional[Summarizer] = None
        self.current_session: Optional[Session] = None
        self.models_ready = False
        self.models_loading = False
        self.models_error: Optional[str] = None
        # Makes ensure_models_loaded's check-and-set atomic so only ONE
        # thread can ever run the native torch/ctranslate2 init — see
        # the single-flight note on ensure_models_loaded.
        self._model_load_lock = threading.Lock()
        self.record_started_at: Optional[datetime] = None
        # Latest status message from the recording/processing pipeline,
        # surfaced to the frontend via /recording/status so the user can
        # see "Transcribing…", "Identifying speakers…" while the long
        # POST /process call is blocking. Previously this signal only
        # went to the log file, so the UI had no way to show progress.
        #
        # BUG: this string is a write-once mailbox — _record_status only
        # ever overwrites it, never clears it. So a TERMINAL message
        # ("Processing complete.", "Recording saved. Ready to process.",
        # "Error saving audio: …") stays parked here forever, and the
        # old frontend logic inferred "still working" from the mere
        # presence of a non-empty current_status. Once processing
        # actually finished the sidebar spinner never stopped. Fixed by
        # NOT inferring busy-ness from this string at all — see
        # `_processing_count` / `is_processing` below, which track real
        # work instead of a leftover message.
        self.current_status: str = ""
        # Real busy signal for /recording/status, replacing the old
        # "current_status is non-empty" proxy the frontend used to infer
        # activity from (fragile — it never distinguished "still
        # working" from "done, here's the last thing that happened").
        #
        # A COUNTER, not a bool: pipeline work can genuinely overlap.
        # process_session (manual /process) and process_full (manual
        # "process full" AND the backend auto-process-after-stop path)
        # can run concurrently for DIFFERENT sessions — only the
        # transcribe/diarize stage is serialized, under
        # _PROCESSING_LOCK (see its docstring: "two overlapping
        # processings — e.g. a backend auto-process and a manual
        # re-process of a different session"). The LLM-extraction stage
        # of process_full runs entirely outside that lock. A bool would
        # have job A's finally-clause turn the indicator off while job B
        # is still genuinely running. Guarded by a plain threading.Lock
        # (not asyncio.Lock) because the stop/finalize path increments
        # it from a worker thread via asyncio.to_thread, not just from
        # coroutines on the event loop.
        self._processing_lock = threading.Lock()
        self._processing_count: int = 0

    def _begin_processing(self) -> None:
        """Mark one unit of pipeline work as started. MUST be paired
        with `_end_processing()` in a `finally` — see that method's
        docstring for why."""
        with self._processing_lock:
            self._processing_count += 1

    def _end_processing(self) -> None:
        """Mark one unit of pipeline work as finished. Callers MUST call
        this from a `finally` block wrapping the work `_begin_processing`
        started, not just on the success path — an exception, a raised
        HTTPException, or a mid-processing crash must still release the
        indicator. Skipping the `finally` here reproduces the exact bug
        this mechanism exists to fix (current_status never clearing),
        just one layer down: an in-flight flag stuck true forever after
        the request that set it blew up.

        Clamped at 0 so a stray extra release (a bug elsewhere, or a
        test) can't drive the counter negative and make `is_processing`
        permanently lie True->False for the wrong reason."""
        with self._processing_lock:
            self._processing_count = max(0, self._processing_count - 1)

    @property
    def is_processing(self) -> bool:
        with self._processing_lock:
            return self._processing_count > 0

    def _record_status(self, msg: str) -> None:
        """Log + stash the status so /recording/status can return it."""
        # Translate internal stage tokens into human-readable strings.
        stage_labels = {
            "__stage:transcribe:active__":  "Transcribing…",
            "__stage:transcribe:done__":    "Transcription complete",
            "__stage:diarize:active__":     "Identifying speakers…",
            "__stage:diarize:done__":       "Speaker identification complete",
            "__stage:speakers:active__":    "Assigning speakers to segments…",
        }
        display = msg
        for token, label in stage_labels.items():
            display = display.replace(token, label)
        display = display.strip()
        if display:
            self.current_status = display
            logger.info(f"[rec] {display}")

    def load_settings(self) -> Settings:
        if self.settings is None or not self._services_ready:
            self.settings = Settings.from_env()
            self.session_svc = SessionService(
                self.settings.recordings_dir,
                extra_dirs=_archive_recordings_dirs(self.settings.recordings_dir),
                # SQLite session-list cache, kept next to config.env and
                # the log files — deliberately NOT inside recordings_dir
                # or an archive root, either of which can be a
                # cloud-synced folder (a SQLite file there can corrupt
                # under sync). See services/session_index.py and
                # config/settings.py's session_index_enabled docstring.
                index_enabled=self.settings.session_index_enabled,
                index_db_path=str(USER_DATA_DIR / "session_index.db"),
            )
            self.export_svc = ExportService(self.settings.recordings_dir)
            # Per-client configs and user-authored templates live ALONGSIDE
            # the recordings dir so they travel with the session library.
            #
            # DO NOT point RECORDINGS_DIR at a cloud-synced folder to get
            # roaming. An earlier version of this comment recommended
            # exactly that, and it is wrong: recordings_dir is where the
            # record/process path writes multi-hundred-MB WAVs, and a
            # cloud-stream mount stalling mid-write is the 2026-07-09
            # incident that wedged the backend, tripped the Tauri
            # watchdog, and cost recordings (see services/export_worker.py).
            # It also stranded one user's library across two machines.
            #
            # For roaming, set SESSION_ARCHIVE_DIR to the synced folder:
            # session JSONs are copied there by the BACKGROUND worker and
            # read back as an archive root, so every machine sees one
            # library while each records to its own local disk.
            #
            # Migration on first v2.4 launch (or on a recordings_dir
            # change) copies the file from the legacy USER_DATA_DIR
            # location, leaving the old copy as a fallback in case the
            # user downgrades.
            #
            # USER_DATA_DIR is imported at MODULE level (see the top of
            # this file). Do NOT re-import it here: a function-local
            # `from config.settings import USER_DATA_DIR` makes the name
            # local to this whole method, so every reference ABOVE that
            # line raises UnboundLocalError. That is exactly what broke
            # v2.26.0 — the session-index `index_db_path` argument added
            # near the top of this method referenced USER_DATA_DIR, blew
            # up before `client_cfg_svc` was ever assigned, and because
            # `self.settings` is set on the first line the `is None`
            # guard then skipped re-initialisation forever. Half-built
            # Services, no retry, recording impossible.
            from pathlib import Path as _Path
            import shutil as _shutil
            _recordings_dir = _Path(self.settings.recordings_dir)
            for _filename in ("client_configs.json", "summary_templates.json"):
                _new = _recordings_dir / _filename
                _old = USER_DATA_DIR / _filename
                if not _new.exists() and _old.exists():
                    try:
                        _new.parent.mkdir(parents=True, exist_ok=True)
                        _shutil.copy2(_old, _new)
                        logger.info(
                            f"Migrated {_filename}: {_old} -> {_new}")
                    except Exception as _e:
                        logger.warning(
                            f"Migration of {_filename} failed ({_e}); "
                            f"reading from legacy USER_DATA_DIR location.")
            self.client_cfg_svc = ClientConfigService(_recordings_dir)
            self.owner_alias_store = OwnerAliasStore(_recordings_dir)
            # CommitmentsService is built BEFORE the engagement service
            # because the engagement register pulls open / outstanding
            # commitment counts via it. Sidecar JSONs next to the
            # session's other sidecar files; no state of its own.
            self.commitments_svc = CommitmentsService(self.session_svc)
            # Pure aggregator over session JSONs + client configs +
            # commitments — no state of its own, so it's safe to build
            # eagerly here.
            # Portal push: bindings + the ingest POST. The worker is
            # a dedicated daemon thread (see export_worker's 2026-07-09
            # incident note) so a flaky HTTPS endpoint can never touch
            # record → finalize → process. The URL is read per push so
            # the dev/prod host is a live setting.
            self.portal_push_svc = PortalPushService(
                _recordings_dir,
                get_portal_url=lambda: getattr(
                    self.settings, "portal_url", "") if self.settings else "")
            self.portal_push_worker = PortalPushWorker(
                self.portal_push_svc.push)

            def _on_register_written(client_key: str, project_key: str,
                                     _svc=self) -> None:
                # The enqueue filter lives here, not in the worker: a
                # project with no enabled, unbroken binding never even
                # queues (acceptance criterion 5), and a broken binding
                # stays silent instead of error-logging on every
                # register regeneration.
                if _svc.portal_push_svc.should_push(client_key, project_key):
                    _svc.portal_push_worker.enqueue(client_key, project_key)

            self.engagement_svc = EngagementService(
                self.session_svc, self.client_cfg_svc, self.commitments_svc,
                on_register_written=_on_register_written)
            self.template_svc = TemplateService(_recordings_dir)
            # Co-Pilot mode + meeting-type libraries. Same shape as
            # TemplateService — seeds defaults on first launch, user
            # can edit / reset / delete from Settings.
            self.copilot_mode_svc = CoPilotModeService(_recordings_dir)
            self.copilot_meeting_type_svc = CoPilotMeetingTypeService(_recordings_dir)
            # Manual fields the SA pins per-engagement (status, sponsor,
            # next milestone, notes). Layered on top of the auto-rolled
            # register so users get one merged view.
            self.engagement_overlay_svc = EngagementOverlayService(_recordings_dir)
            # Daily briefing imports (Today view) — one parsed briefing
            # per calendar date, populated by user pasting M365 Copilot
            # scheduled-prompt output into the Today tab's Import dialog.
            self.daily_briefing_svc = DailyBriefingService(_recordings_dir)
            # Calendar events the Chrome extension scraped out of
            # Outlook Web. Lives next to the other per-user JSON stores
            # in recordings_dir so it roams with them; replaced
            # wholesale on each import so stale events age out.
            self.extension_calendar_svc = ExtensionCalendarService(
                _recordings_dir)
            # Domain terminology glossary (Whisper bias + mis-hear
            # corrections). Seeds a curated SA/CCaaS/cloud/sales vocab on
            # first launch; user-editable from Settings.
            self.terminology_svc = TerminologyService(_recordings_dir)
            # Auto pre-meeting brief cache — backend loop fills it before
            # meetings; frontend reads it for the "ready" notification +
            # instant brief view.
            self.prep_brief_cache_svc = PrepBriefCacheService(_recordings_dir)
            # Speaker profiles stay per-machine: voice fingerprints are
            # mic-hardware-dependent, syncing them across devices with
            # different mics risks false positives.
            self.speaker_profile_svc = SpeakerProfileService(USER_DATA_DIR)
            # "Never auto-record this meeting" list. Per-machine like
            # speaker profiles — it's tied to how this user works, not
            # account data worth syncing.
            self.auto_record_blocklist_svc = AutoRecordBlocklistService(
                USER_DATA_DIR)
            # SearchService stays a thin wrapper around session_service —
            # session embeddings live next to session JSONs, so it just
            # needs that handle. Lazy index load happens on first search.
            self.search_svc = SearchService(self.session_svc)
            # ItemStatusService overlays per-session "checked off" state
            # on top of the markdown-parsed follow-ups and decisions.
            self.item_status_svc = ItemStatusService(self.session_svc)
            # InsightsService — pure aggregator over the three services
            # above. No file state, no warmup; instantiated at the same
            # time so the /insights endpoints have a target right away.
            from services.insights_service import InsightsService
            self.insights_svc = InsightsService(
                self.session_svc, self.commitments_svc, self.item_status_svc)
            self.recording_svc = RecordingService(
                settings=self.settings,
                profile_service=self.speaker_profile_svc,
                on_status=self._record_status,
            )
            # Wire the domain glossary into the recorder so transcription
            # biases toward the user's vocabulary (Whisper initial_prompt)
            # and corrects known mis-hears afterward. Set after construction
            # so the RecordingService signature stays unchanged.
            self.recording_svc.terminology = self.terminology_svc
            # The summarizer is constructed whenever an LLM is configured
            # — either Anthropic (anthropic_api_key) or an OpenAI-compatible
            # endpoint (openai_base_url / openai_api_key, or a local Ollama
            # URL which needs no real key).
            s = self.settings
            have_llm = False
            if s.ai_provider == "openai":
                have_llm = bool(s.openai_api_key) or bool(s.openai_base_url)
            else:
                have_llm = bool(s.anthropic_api_key)
            if have_llm:
                # Lazy import Summarizer (pulls in anthropic / openai SDKs)
                global Summarizer
                if Summarizer is None:
                    from core.summarizer import Summarizer as _Summarizer
                    Summarizer = _Summarizer
                try:
                    self.summarizer = Summarizer(
                        api_key=s.anthropic_api_key,
                        model=s.claude_model,
                        provider=s.ai_provider,
                        base_url=s.openai_base_url,
                        openai_api_key=s.openai_api_key,
                    )
                except Exception as e:
                    # Missing openai package etc. shouldn't prevent other
                    # endpoints from loading — leave summarizer None and
                    # surface the error at first use.
                    logger.warning(f"Summarizer init failed: {e}")
                    self.summarizer = None
            # Live Co-Pilot override summarizer. When `live_ai_provider`
            # is set we build a second Summarizer pointing at whatever
            # cheap/free model the user picked for live ticks (local
            # Ollama, a free OpenRouter model, a smaller Anthropic
            # model). Unset → reuse the main summarizer so the tick
            # endpoint doesn't have to branch.
            live_provider = (s.live_ai_provider or "").strip().lower()
            if live_provider and self.summarizer is not None:
                try:
                    if live_provider == "anthropic":
                        # Fall back to the main Anthropic key when the
                        # override-specific one is blank — users on
                        # Anthropic-everywhere shouldn't have to paste
                        # the same key twice.
                        live_key = (
                            s.live_anthropic_api_key or s.anthropic_api_key)
                        self.live_summarizer = Summarizer(
                            api_key=live_key,
                            model=s.live_claude_model or s.claude_model,
                            provider="anthropic",
                        )
                    else:
                        self.live_summarizer = Summarizer(
                            api_key="",  # ignored for the openai path
                            model=s.live_claude_model or s.claude_model,
                            provider="openai",
                            base_url=s.live_openai_base_url,
                            openai_api_key=s.live_openai_api_key,
                        )
                except Exception as e:
                    logger.warning(
                        f"Live Co-Pilot summarizer init failed "
                        f"(falling back to main summarizer): {e}")
                    self.live_summarizer = self.summarizer
            else:
                self.live_summarizer = self.summarizer
            # QAService threads search_svc + summarizer together. Either
            # being None at this moment is fine — QAService.is_ready
            # reports False and the endpoint emits a clear "not configured"
            # message instead of crashing.
            self.qa_svc = QAService(self.search_svc, self.summarizer)
            # Only NOW is the object fully built. Set last, on
            # purpose — see _services_ready in __init__.
            self._services_ready = True
        return self.settings

    def ensure_models_loaded(self):
        """Blocking: load transcription + diarization engines if not loaded.

        SINGLE-FLIGHT — the check-and-set below is atomic under
        _model_load_lock. Before this, five endpoints each spawned their
        own load thread (Record-view mount fires /models/load, auto-
        record start, manual start, both process endpoints); at app-open
        several fired near-simultaneously, both threads passed the
        non-atomic `ready or loading` check, and two concurrent
        torch/ctranslate2 native inits in one process intermittently
        segfaulted (0xC0000005) — the 2026-07-21 rust.log crash loop
        during "Loading transcription engine". One loader ever; every
        other caller returns immediately and polls models_ready.
        """
        with self._model_load_lock:
            if self.models_ready or self.models_loading:
                return
            self.models_loading = True
            self.models_error = None
        try:
            s = self.load_settings()
            if not s.is_configured:
                raise RuntimeError("API keys not configured")

            # Lazy import the heavy ML modules here (torch + pyannote +
            # faster-whisper can take 3-5 seconds to import).
            global TranscriptionEngine, DiarizationEngine
            if TranscriptionEngine is None:
                from core.transcription import TranscriptionEngine as _T
                TranscriptionEngine = _T
            if DiarizationEngine is None:
                from core.diarization import DiarizationEngine as _D
                DiarizationEngine = _D

            logger.info("Loading transcription engine...")
            self.transcription = TranscriptionEngine(s.whisper_model)
            logger.info("Loading diarization engine...")
            self.diarization = DiarizationEngine(
                s.hf_token, s.max_speakers, s.diarization_device)
            self.recording_svc.set_engines(self.transcription, self.diarization)
            self.models_ready = True
            logger.info("Models loaded")
        except Exception as e:
            logger.exception("Model load failed")
            self.models_error = str(e)
            raise
        finally:
            self.models_loading = False


svc = Services()


# ── Models ───────────────────────────────────────────────────────────
class SettingsDTO(BaseModel):
    anthropic_api_key: str
    hf_token: str
    whisper_model: str
    max_speakers: int
    recordings_dir: str
    email_to: str
    claude_model: str
    notify_minutes_before: int
    auto_process_after_stop: bool
    launch_on_startup: bool
    auto_follow_up_email: bool
    retention_enabled: bool
    retention_processed_days: int
    retention_unprocessed_days: int
    is_configured: bool
    # AI provider selection. Defaults preserve existing behavior for
    # clients that predate this field — they'll just round-trip empty
    # strings and stay on Anthropic.
    ai_provider: str = "anthropic"
    openai_api_key: str = ""
    openai_base_url: str = ""
    # Streaming live-transcription preview during recording. Default
    # True; set False on slower machines or for calls where the user
    # finds the live preview noisy / inaccurate (the canonical
    # post-stop transcript runs regardless).
    live_transcription_enabled: bool = True
    # Auto-screenshot cadence during recording. 0 = off (manual button
    # only). The frontend's record-view fires the screenshot capture
    # on a setInterval while a session is active.
    auto_screenshot_interval_minutes: int = 0
    # Auto-stop watchdog. Defaults match Settings.from_env: warnings on,
    # auto-stops opt-in, 4h hard cap. 0 disables a given trigger.
    silence_warn_min: int = 5
    silence_stop_min: int = 0
    overrun_warn_min: int = 5
    overrun_stop_min: int = 0
    hard_cap_hours: int = 4
    # Calendar-driven auto-start. Defaults off — user opts in via the
    # toggle on the Record view. Persisted across restarts.
    auto_record_enabled: bool = False
    # Live in-call co-pilot panel. Defaults off — it makes one LLM call
    # every ~45s of recording, so users opt in.
    live_copilot_enabled: bool = False
    # Optional separate LLM for the live co-pilot. Empty strings (the
    # default) mean "reuse the main provider config". Set
    # live_ai_provider to "openai" + a base_url to point ticks at a
    # local Ollama / a free OpenRouter model.
    live_ai_provider: str = ""
    live_claude_model: str = ""
    live_openai_api_key: str = ""
    live_openai_base_url: str = ""
    portal_url: str = ""
    live_anthropic_api_key: str = ""
    # Active co-pilot persona + meeting-type modifier. Names resolve
    # through CoPilotModeService / CoPilotMeetingTypeService; the
    # prompts themselves are edited in Settings as their own library.
    # Defaults match what those services seed: SA persona, General type.
    live_copilot_mode: str = "SA"
    live_copilot_meeting_type: str = "General"
    # Polling intervals (seconds). Wide = full ~10 min window, hot =
    # last ~90s. Hot=0 disables the hot tier (only wide runs).
    live_copilot_wide_interval_sec: int = 45
    live_copilot_hot_interval_sec: int = 0
    # Free-text context the SA pins for the live co-pilot — appended to
    # every coach_tick prompt as authoritative role / topic framing.
    # Examples: "Current engagement is a Genesys → Connect migration for
    # a healthcare client, ~800 agents, focus on PHI compliance." Empty
    # by default so the baked-in SA-flavored prompt runs as-is.
    copilot_custom_context: str = ""
    # Opt-in toggle for the "Today" daily-briefing tab. OFF by default —
    # depends on the user running an M365 Copilot scheduled prompt and
    # pasting its output in. Persisted; gates the nav item + default
    # landing view on the frontend.
    today_view_enabled: bool = False
    # Auto pre-meeting brief: generate a brief shortly before each calendar
    # meeting and notify when ready. OFF by default (one LLM call/meeting).
    auto_prep_brief_enabled: bool = False
    auto_prep_brief_lead_min: int = 10
    # Root network folder for background per-client exports ("Cloud
    # Mirror"). Empty = off. See services/export_worker.py.
    cloud_mirror_dir: str = ""
    # Session Archive: roaming folder for session JSONs so a library
    # shows up on every machine pointed at the same synced folder.
    # Field report 2026-08-07 — see config/settings.py's
    # session_archive_dir docstring and _session_archive_dir() below.
    session_archive_dir: str = ""
    # Speech-boundary chunking for the live transcript (default on) —
    # see config/settings.py's field docstring and
    # core/live_transcriber.py (field report 2026-08-10, Zoom notetaker
    # parity). False falls back to the legacy fixed-15s-window path.
    live_vad_enabled: bool = True
    # Live "Speaker 1 / Speaker 2" labelling of the far-end stream in
    # the live transcript preview. Default on. False keeps every
    # loopback segment on the plain "them" label and skips all live
    # embedding work — the escape hatch for the over-splitting /
    # wrong-name behavior in field report 2026-08-11. See
    # config/settings.py's live_speaker_split_enabled docstring and
    # core/live_speakers.py's threshold notes.
    live_speaker_split_enabled: bool = True
    # Device the pyannote speaker-diarization pipeline loads on. "auto"
    # (default) preserves pre-2026-08-11 behavior (CUDA > MPS > CPU).
    # "cpu" forces CPU — the workaround for the field-reported
    # 0xC0000005 crash a few seconds after recording stop, believed
    # caused by faster-whisper's CUDA/cuDNN runtime and pyannote's
    # separate CUDA/cuDNN runtime colliding in one process. "cuda" forces
    # GPU, falling back to CPU with a warning if none is present. See
    # config/settings.py's diarization_device docstring and
    # core/diarization.py's _resolve_device.
    diarization_device: str = "auto"
    # Kill switch for the pycaw-backed WASAPI mix-format lookup behind
    # /audio/sync-risk. Default True — the lookup runs subprocess-
    # isolated (never in-process) as of v2.25.1. False skips it
    # entirely (sync-risk reports "unknown"). See
    # config/settings.py's audio_mix_format_lookup_enabled docstring
    # and core/audio_format_inspector.py.
    audio_mix_format_lookup_enabled: bool = True
    # Offline acoustic echo cancellation for the mic channel during
    # finalize (before the mic+loopback mix). Helps when recording
    # with an external mic + speakers (not a headset): unmuting lets
    # the far-end caller's voice come back out of the speakers and get
    # picked up a second time on the mic, duplicating that speech in
    # the transcript under the user's own name and degrading speaker
    # diarization. Default False — off while this is validated; a
    # rejected/failed attempt always falls back to the untouched mic,
    # never damages the recording. See config/settings.py's
    # echo_cancellation_enabled docstring and utils/aec.py.
    echo_cancellation_enabled: bool = False
    # Kill switch for the SQLite session-list index. Default True — see
    # config/settings.py's session_index_enabled docstring and
    # services/session_index.py. False forces every /sessions read back
    # onto the old direct-scan path (services/session_service.py's
    # _list_sessions_direct).
    session_index_enabled: bool = True
    # Kill switch for channel-aware diarization. Default True — see
    # config/settings.py's channel_attribution_enabled docstring and
    # core/channel_attribution.py. False makes speaker attribution
    # depend purely on voice similarity again (no channel override, no
    # sidecar written at finalize).
    channel_attribution_enabled: bool = True
    # Which calendar source(s) the backend may consult. "auto" (default)
    # preserves existing behavior: local calendar (Outlook COM / macOS
    # EventKit) + Chrome-extension events, merged. "outlook" is local
    # calendar only. "extension" NEVER touches Outlook COM / EventKit —
    # for a user whose tenant throws a Microsoft sign-in prompt every
    # time the app touches Outlook (field report 2026-08-14). "off"
    # disables calendar data entirely. See config/settings.py's
    # calendar_source docstring and services/calendar_service.py, the
    # single choke point that enforces this for every caller.
    calendar_source: str = "auto"


class StartRecordingRequest(BaseModel):
    mic_device_index: Optional[int] = None
    output_device_index: Optional[int] = None
    meeting_name: str = ""
    template: str = "General"
    client: str = ""
    project: str = ""
    # Provenance for `client`, set only by the calendar auto-record path
    # (see _resolve_client_for_meeting). Left None by the manual Start
    # button, where the user picked the client themselves and there is
    # nothing to explain. See Session.client_source.
    client_source: Optional[str] = None
    client_source_detail: Optional[str] = None
    attendees: list[str] = []
    # Who called the meeting, from the calendar invite's organiser
    # field. Sent by both calendar-driven start paths — the Record
    # tab's Use button and `_auto_record_start` — and left "" for an
    # ad-hoc recording. See Session.organizer for why this matters
    # more than it looks: for an extension-sourced calendar it is the
    # ONLY invite-derived name available, because `attendees` above is
    # always empty there.
    #
    # Optional with an empty default on purpose. A client that doesn't
    # send it (an older frontend, a script, the Start button on an
    # ad-hoc recording) starts a recording exactly as before.
    organizer: str = ""
    # ISO datetime when the meeting is scheduled to end, when this
    # recording was started from a calendar entry. Optional. Used by
    # the auto-stop watchdog to warn / stop after the meeting overruns
    # by the user-configured grace period.
    scheduled_end_iso: Optional[str] = None
    # Conference room mode: laptop sits in the middle of a room, mic
    # picks up everyone, no one is on speakers. We skip system-audio
    # loopback capture entirely (no double-recording = no echo possible
    # by construction) and tell the live transcriber that the mic is
    # capturing multiple in-room speakers rather than just "you". The
    # post-stop pyannote diarization pass is unchanged — it already
    # splits a single mic stream into SPEAKER_00, SPEAKER_01, … which
    # is exactly what we want here.
    conference_room_mode: bool = False


class RecordingStatus(BaseModel):
    is_recording: bool
    session_id: Optional[str] = None
    started_at: Optional[str] = None
    duration_s: int = 0
    models_ready: bool = False
    models_loading: bool = False
    models_error: Optional[str] = None
    # Latest status from the recording/processing pipeline. Updated as
    # each stage progresses (e.g. "Transcribing…", "Identifying
    # speakers…") so the UI can show progress during the long POST
    # /sessions/{id}/process call. Empty string when idle.
    #
    # NOTE: this is a write-once mailbox that is never cleared (see
    # Services.current_status) — a terminal message like "Processing
    # complete." stays here indefinitely after work finishes. The
    # frontend must NOT infer "still busy" from this being non-empty;
    # use `is_processing` below for that. This field is display text
    # only.
    current_status: str = ""
    # True while the backend is genuinely doing pipeline work — transcribe
    # /diarize, LLM extraction, or finalizing a just-stopped recording.
    # This is the real busy signal `current_status` was being
    # (incorrectly) used as a proxy for; see Services._begin_processing /
    # _end_processing. Defaulted to False so an older frontend (which
    # doesn't know this field exists) degrades gracefully, and a newer
    # frontend against an older backend that omits it also degrades
    # gracefully (optional on the TS side too).
    is_processing: bool = False
    # Auto-stop / dead-air watchdog warnings, one per active condition.
    # Each entry is a small dict the frontend renders as a banner +
    # native notification. Codes are stable so the frontend can dedupe
    # (only fire one notification per code per recording session).
    #   {"code": "dead_air"|"meeting_overdue"|"hard_cap_warn"|"auto_stopped",
    #    "message": "...",
    #    "since_seconds": int}
    warnings: list[dict] = []
    # When the running recording was kicked off by AutoRecordService
    # (and not the user clicking Start), this is the meeting subject;
    # the frontend uses it for the "Auto-recording: <subject>" toast +
    # native notification + the persistent recording-badge label.
    auto_record_subject: Optional[str] = None
    # Set on a tick where AutoRecordService had to skip a meeting it
    # would have recorded (e.g. no saved mic device). One-shot cue for
    # the frontend to surface so the user isn't left wondering why
    # nothing recorded. Backend clears it after one read.
    auto_record_skip_reason: Optional[str] = None
    # ── Capture-confidence meters ────────────────────────────────────
    # Live mic / system-audio level + liveness, from RecordingService.
    # get_capture_levels(). All optional/defaulted so an older frontend
    # against a newer backend (missing fields ignored) and a newer
    # frontend against an older backend (fields default to a benign
    # "nothing to report" shape) both degrade gracefully — see AGENTS.md
    # "Diagnose with data, not guesses" for why this feature exists:
    # capture failures were previously invisible until the next day.
    mic_level: float = 0.0
    system_level: float = 0.0
    mic_state: Optional[str] = None
    system_state: Optional[str] = None
    capture_warning: Optional[str] = None


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


# Free-model roster is fetched live from OpenRouter's public catalog so
# it never goes stale. Hardcoded lists rotate out and start 404ing
# within weeks (this is the class of bug that broke claude-3-5-haiku-
# latest and the old :free ids). Cached in-process so opening Settings
# doesn't hammer the upstream.
_FREE_MODELS_CACHE: dict = {"at": 0.0, "models": []}
_FREE_MODELS_TTL = 6 * 3600  # 6h — free roster changes on the order of days


def _fetch_openrouter_free() -> list:
    """Return [{value,label}] of free, text-capable OpenRouter models.
    stdlib-only (no httpx dependency assumption); any failure returns []
    so the frontend falls back to its bundled list."""
    import json as _json
    import urllib.request as _urlreq

    req = _urlreq.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "MeetingRecorder/2"},
    )
    with _urlreq.urlopen(req, timeout=10) as resp:
        data = _json.loads(resp.read().decode("utf-8"))

    out: list = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        pricing = m.get("pricing") or {}
        # Free = both prompt and completion priced at 0. The ":free"
        # suffix is the canonical free variant; require it so we don't
        # surface a paid model the user gets billed for.
        if not mid.endswith(":free"):
            continue
        try:
            if float(pricing.get("prompt", "1")) != 0.0:
                continue
            if float(pricing.get("completion", "1")) != 0.0:
                continue
        except (TypeError, ValueError):
            continue
        # Text in/out only — skip image/audio-only endpoints.
        arch = m.get("architecture") or {}
        in_mods = arch.get("input_modalities") or []
        if in_mods and "text" not in in_mods:
            continue
        ctx = m.get("context_length") or 0
        name = m.get("name") or mid
        ctx_k = f" · {round(ctx / 1000)}k ctx" if ctx else ""
        out.append({"value": mid, "label": f"{name}{ctx_k}"})

    # Longest context first (best for long meeting transcripts), then name.
    out.sort(key=lambda x: x["label"])
    return out


@app.get("/models/free")
async def get_free_models(provider: str = "openrouter"):
    """Live free-model roster for the OpenRouter provider preset.
    Returns {models:[{value,label}]}; empty list on any failure (the UI
    keeps its bundled fallback list)."""
    if provider != "openrouter":
        return {"models": []}
    now = time.time()
    if (now - _FREE_MODELS_CACHE["at"]) < _FREE_MODELS_TTL and _FREE_MODELS_CACHE["models"]:
        return {"models": _FREE_MODELS_CACHE["models"]}
    try:
        models = await asyncio.to_thread(_fetch_openrouter_free)
    except Exception as e:
        logger.warning(f"OpenRouter free-model fetch failed ({e}); UI uses fallback")
        return {"models": _FREE_MODELS_CACHE["models"]}
    if models:
        _FREE_MODELS_CACHE["at"] = now
        _FREE_MODELS_CACHE["models"] = models
    return {"models": models}


# ── Live provider-model discovery ────────────────────────────────────
#
# Every OpenAI-compatible provider (Anthropic, Gemini, Groq, OpenRouter,
# LM Studio, vLLM, …) exposes a "list available models" endpoint. The
# Settings page used to ship a HARDCODED dropdown per provider, which
# meant every new model release (Gemini 2.5 Flash, Claude Haiku 4.5,
# etc.) required an app update before the user could pick it. Now the
# dropdown pulls live from the provider on Settings open; the hardcoded
# list survives as a fallback only.
#
# All fetches are stdlib-only (no httpx assumption — matches the
# _fetch_openrouter_free pattern above) and time out at 8s so a flaky
# provider can't hang the settings page indefinitely. Results are
# cached per (provider, base_url) for 5 minutes so opening Settings,
# saving, and reopening doesn't pound the provider's API.

_PROVIDER_MODELS_CACHE: dict[tuple[str, str], dict] = {}
_PROVIDER_MODELS_TTL = 300  # 5 minutes


def _stdlib_get_json(
    url: str, headers: Optional[dict] = None, timeout: float = 8.0,
) -> dict:
    """One-shot GET → JSON. Raises urllib.error.URLError / JSONDecodeError
    on failure; caller logs + returns []."""
    import json as _json
    import urllib.request as _urlreq

    req = _urlreq.Request(
        url, headers=headers or {"User-Agent": "MeetingRecorder/2"},
    )
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _fetch_anthropic_models(api_key: str) -> list[dict]:
    """Anthropic's native /v1/models. Requires the API key in
    x-api-key (NOT Bearer — different from OpenAI). Schema is
    ``{ data: [{ id, display_name, type, created_at }] }``. We surface
    only models with type=="model" and a non-empty display_name so the
    UI doesn't pollute with deprecated aliases."""
    if not api_key:
        return []
    data = _stdlib_get_json(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": "MeetingRecorder/2",
        },
    )
    out: list[dict] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        if m.get("type") and m.get("type") != "model":
            continue
        label = m.get("display_name") or mid
        out.append({"value": mid, "label": label})
    out.sort(key=lambda x: x["label"])
    return out


def _fetch_openai_compat_models(
    base_url: str, api_key: str,
) -> list[dict]:
    """Standard ``GET {base_url}/models`` shape used by OpenAI, Groq,
    LM Studio, vLLM, and Gemini's OpenAI-compat endpoint. Bearer auth.
    Schema: ``{ data: [{ id, object, owned_by, created }] }``.

    Some providers (Ollama via OpenAI shim, certain LM Studio configs)
    return models with no metadata; we just surface ``id`` as the label
    in that case rather than dropping the entry."""
    if not base_url:
        return []
    url = base_url.rstrip("/") + "/models"
    headers = {"User-Agent": "MeetingRecorder/2"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = _stdlib_get_json(url, headers=headers)
    out: list[dict] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        if not mid:
            continue
        owned = m.get("owned_by") or ""
        label = f"{mid} · {owned}" if owned else mid
        out.append({"value": mid, "label": label})
    out.sort(key=lambda x: x["value"])
    return out


def _fetch_gemini_models(api_key: str) -> list[dict]:
    """Gemini's native /v1beta/models endpoint (NOT the OpenAI-compat
    one — the native one returns more useful metadata including
    supported generation methods, so we filter to chat-capable models
    only). Auth via ``?key=`` query param, not Authorization header."""
    if not api_key:
        return []
    data = _stdlib_get_json(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
    )
    out: list[dict] = []
    for m in data.get("models", []):
        name = m.get("name") or ""
        # Gemini returns "models/gemini-2.5-flash"; we want just the id.
        mid = name.rsplit("/", 1)[-1] if name else ""
        if not mid:
            continue
        # Only models that support generateContent (the chat surface).
        methods = m.get("supportedGenerationMethods") or []
        if methods and "generateContent" not in methods:
            continue
        display = m.get("displayName") or mid
        out.append({"value": mid, "label": display})
    out.sort(key=lambda x: x["label"])
    return out


def _fetch_ollama_local_models(base_url: str) -> list[dict]:
    """Ollama's native /api/tags lists LOCALLY INSTALLED models (what
    the user has pulled). The OpenAI-compat /v1/models endpoint also
    works but returns the same set with less metadata. We use /api/tags
    here for the size + modified-at fields, which let us show the user
    which models are big / old."""
    if not base_url:
        return []
    # Ollama's API root is `/`; the OpenAI compat path is `/v1/`. The
    # user's base_url could point at either; normalize to the host.
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    data = _stdlib_get_json(f"{root}/api/tags")
    out: list[dict] = []
    for m in data.get("models", []):
        name = m.get("name") or ""
        if not name:
            continue
        size_b = m.get("size") or 0
        size_gb = size_b / (1024 ** 3) if size_b else 0
        label = f"{name} · {size_gb:.1f} GB" if size_gb else name
        out.append({"value": name, "label": label})
    out.sort(key=lambda x: x["value"])
    return out


@app.get("/providers/available-models")
async def get_provider_available_models(
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    scope: Optional[str] = None,
):
    """Live model roster for the configured (or query-overridden) AI
    provider. The UI calls this on Settings open; the hardcoded
    GEMINI_MODELS / GROQ_MODELS / OLLAMA_MODELS lists are kept as
    fallbacks for offline / bad-key cases.

    ``scope=live`` reads ``live_*`` settings keys instead of the main
    ones so the Live Co-Pilot section can populate its own model
    dropdown without forcing the user to type model IDs by hand.
    Default scope is "main" — preserves existing call shape.

    Query params let the UI test a candidate provider+url BEFORE saving
    — same shape as the test-connection endpoint. When omitted, reads
    the currently-saved settings."""
    s = svc.load_settings()
    # Scope routes BOTH the provider/url defaults and the API key
    # selection. The live config has its own anthropic + openai_compat
    # keys (live_anthropic_api_key, live_openai_api_key) so users can
    # point the tick model at a separate provider/account from the
    # main summarizer without leaking the main key.
    use_live = (scope or "").lower() == "live"
    if use_live:
        default_provider = (s.live_ai_provider or s.ai_provider or "anthropic")
        default_base = s.live_openai_base_url or s.openai_base_url or ""
        anthropic_key = s.live_anthropic_api_key or s.anthropic_api_key
        openai_key = s.live_openai_api_key or s.openai_api_key
    else:
        default_provider = s.ai_provider or "anthropic"
        default_base = s.openai_base_url or ""
        anthropic_key = s.anthropic_api_key
        openai_key = s.openai_api_key
    prov = (provider or default_provider or "anthropic").lower()
    base = (base_url or default_base or "").strip()
    # Cache key gains the scope so the main + live sections don't
    # poison each other's caches (they may legitimately have different
    # results when keys differ between accounts).
    cache_key = (prov, base, "live" if use_live else "main")
    now = time.time()
    cached = _PROVIDER_MODELS_CACHE.get(cache_key)
    if cached and (now - cached["at"]) < _PROVIDER_MODELS_TTL:
        return {"models": cached["models"], "source": "cache",
                "provider": prov, "age_seconds": int(now - cached["at"])}

    models: list[dict] = []
    err: Optional[str] = None
    try:
        if prov == "anthropic":
            models = await asyncio.to_thread(
                _fetch_anthropic_models, anthropic_key)
        elif prov == "openai":
            base_lower = base.lower()
            if "generativelanguage.googleapis" in base_lower:
                # Use Gemini's NATIVE endpoint (richer metadata) even
                # though the user's base_url points at /v1beta/openai/.
                # We have the key either way.
                models = await asyncio.to_thread(
                    _fetch_gemini_models, openai_key)
            elif ("ollama" in base_lower or
                  ":11434" in base_lower or
                  "localhost:11434" in base_lower):
                models = await asyncio.to_thread(
                    _fetch_ollama_local_models, base)
            else:
                models = await asyncio.to_thread(
                    _fetch_openai_compat_models, base, openai_key)
        else:
            err = f"Unknown provider: {prov!r}"
    except Exception as e:
        # Network failure, bad key, 4xx/5xx — surface the error message
        # but DON'T raise. UI catches the empty list + error string and
        # falls back to its hardcoded roster.
        err = f"{type(e).__name__}: {e}"
        logger.info(f"available-models fetch failed for {prov} {base!r}: {err}")

    if models:
        _PROVIDER_MODELS_CACHE[cache_key] = {"at": now, "models": models}
    return {
        "models": models,
        "source": "live" if models else "empty",
        "provider": prov,
        "error": err,
    }


# ── Settings ─────────────────────────────────────────────────────────
@app.get("/settings", response_model=SettingsDTO)
async def get_settings():
    s = svc.load_settings()
    return SettingsDTO(
        anthropic_api_key=s.anthropic_api_key,
        hf_token=s.hf_token,
        whisper_model=s.whisper_model,
        max_speakers=s.max_speakers,
        recordings_dir=s.recordings_dir,
        email_to=s.email_to,
        claude_model=s.claude_model,
        notify_minutes_before=s.notify_minutes_before,
        auto_process_after_stop=s.auto_process_after_stop,
        launch_on_startup=s.launch_on_startup,
        auto_follow_up_email=s.auto_follow_up_email,
        retention_enabled=s.retention_enabled,
        retention_processed_days=s.retention_processed_days,
        retention_unprocessed_days=s.retention_unprocessed_days,
        is_configured=s.is_configured,
        ai_provider=s.ai_provider or "anthropic",
        openai_api_key=s.openai_api_key,
        openai_base_url=s.openai_base_url,
        live_transcription_enabled=s.live_transcription_enabled,
        auto_screenshot_interval_minutes=s.auto_screenshot_interval_minutes,
        silence_warn_min=s.silence_warn_min,
        silence_stop_min=s.silence_stop_min,
        overrun_warn_min=s.overrun_warn_min,
        overrun_stop_min=s.overrun_stop_min,
        hard_cap_hours=s.hard_cap_hours,
        auto_record_enabled=s.auto_record_enabled,
        live_copilot_enabled=s.live_copilot_enabled,
        live_ai_provider=s.live_ai_provider,
        live_claude_model=s.live_claude_model,
        live_openai_api_key=s.live_openai_api_key,
        live_openai_base_url=s.live_openai_base_url,
        portal_url=getattr(s, "portal_url", ""),
        live_anthropic_api_key=s.live_anthropic_api_key,
        live_copilot_mode=s.live_copilot_mode,
        live_copilot_meeting_type=s.live_copilot_meeting_type,
        live_copilot_wide_interval_sec=s.live_copilot_wide_interval_sec,
        live_copilot_hot_interval_sec=s.live_copilot_hot_interval_sec,
        copilot_custom_context=s.copilot_custom_context,
        today_view_enabled=s.today_view_enabled,
        auto_prep_brief_enabled=s.auto_prep_brief_enabled,
        auto_prep_brief_lead_min=s.auto_prep_brief_lead_min,
        cloud_mirror_dir=s.cloud_mirror_dir,
        session_archive_dir=s.session_archive_dir,
        live_vad_enabled=s.live_vad_enabled,
        live_speaker_split_enabled=s.live_speaker_split_enabled,
        diarization_device=s.diarization_device,
        audio_mix_format_lookup_enabled=s.audio_mix_format_lookup_enabled,
        echo_cancellation_enabled=s.echo_cancellation_enabled,
        session_index_enabled=s.session_index_enabled,
        channel_attribution_enabled=s.channel_attribution_enabled,
        calendar_source=s.calendar_source,
    )


@app.post("/settings")
async def save_settings(payload: SettingsDTO):
    # Refuse if a recording is in progress. save_settings re-instantiates
    # the RecordingService inside load_settings(), which would orphan the
    # active capture threads — they keep writing to temp WAVs but no one
    # in the new service instance can finalize them on Stop, and the UI
    # loses track of the recording entirely. Surfaced as 409 so the
    # frontend can show "stop your recording first" instead of silently
    # half-saving and wedging the session.
    if svc.recording_svc is not None and svc.recording_svc.is_recording:
        raise HTTPException(
            status_code=409,
            detail=(
                "A recording is currently in progress. Stop it before "
                "saving Settings — otherwise the in-flight capture loses "
                "track of where it's writing and the audio can't be "
                "finalized cleanly."
            ),
        )

    # Capture the previous launch_on_startup value BEFORE writing the new
    # one — we only call into the OS to install/remove the auto-launch
    # entry when it actually changes. Otherwise every Save Settings click
    # would hammer the LaunchAgent / Startup folder.
    prev_launch = bool(svc.settings.launch_on_startup) if svc.settings else False

    # Capture previous recordings_dir so we can migrate client/template
    # state if the user is changing folders. load_settings()'s own
    # migration path only knows about the legacy USER_DATA_DIR location;
    # it can't see the *previously-active* recordings_dir, which is what
    # holds the live state when a user changes folders mid-session.
    prev_recordings_dir = (
        svc.settings.recordings_dir if svc.settings else None
    )

    # Capture the previous Session Archive folder so we know AFTER the
    # save whether it actually changed — that's what decides whether we
    # kick a background reconcile (see below).
    prev_archive_dir = (
        (svc.settings.session_archive_dir or "").strip() if svc.settings else ""
    )

    # Validate the Session Archive folder BEFORE writing it. Deliberately
    # does NOT mkdir a missing path — this setting exists specifically to
    # point at a cloud-synced folder shared with another machine, and a
    # typo'd path (or a Drive/OneDrive mount that hasn't come up yet)
    # must surface as an error immediately rather than silently creating
    # a fresh, empty, un-synced folder that looks like it's "working"
    # (field report 2026-08-07).
    new_archive_dir = (payload.session_archive_dir or "").strip()
    if new_archive_dir and not Path(new_archive_dir).expanduser().is_dir():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Session Archive folder does not exist: {new_archive_dir}. "
                "Create the folder first (or fix the path) — an "
                "unreachable sync path should surface here, not be "
                "created and silently start out empty."
            ),
        )

    Settings.save_to_env(
        anthropic_api_key=payload.anthropic_api_key,
        hf_token=payload.hf_token,
        whisper_model=payload.whisper_model,
        max_speakers=payload.max_speakers,
        recordings_dir=payload.recordings_dir,
        email_to=payload.email_to,
        claude_model=payload.claude_model,
        notify_minutes_before=payload.notify_minutes_before,
        auto_process_after_stop=payload.auto_process_after_stop,
        launch_on_startup=payload.launch_on_startup,
        auto_follow_up_email=payload.auto_follow_up_email,
        retention_enabled=payload.retention_enabled,
        retention_processed_days=payload.retention_processed_days,
        retention_unprocessed_days=payload.retention_unprocessed_days,
        ai_provider=payload.ai_provider or "anthropic",
        openai_api_key=payload.openai_api_key,
        openai_base_url=payload.openai_base_url,
        live_transcription_enabled=payload.live_transcription_enabled,
        auto_screenshot_interval_minutes=payload.auto_screenshot_interval_minutes,
        silence_warn_min=max(0, payload.silence_warn_min),
        silence_stop_min=max(0, payload.silence_stop_min),
        overrun_warn_min=max(0, payload.overrun_warn_min),
        overrun_stop_min=max(0, payload.overrun_stop_min),
        hard_cap_hours=max(0, payload.hard_cap_hours),
        auto_record_enabled=bool(payload.auto_record_enabled),
        live_copilot_enabled=bool(payload.live_copilot_enabled),
        live_ai_provider=(payload.live_ai_provider or "").strip(),
        live_claude_model=(payload.live_claude_model or "").strip(),
        live_openai_api_key=payload.live_openai_api_key or "",
        live_openai_base_url=(payload.live_openai_base_url or "").strip(),
        portal_url=(payload.portal_url or "").strip().rstrip("/"),
        live_anthropic_api_key=payload.live_anthropic_api_key or "",
        live_copilot_mode=(payload.live_copilot_mode or "").strip() or "SA",
        live_copilot_meeting_type=(payload.live_copilot_meeting_type or "").strip() or "General",
        live_copilot_wide_interval_sec=max(15, min(300, payload.live_copilot_wide_interval_sec or 45)),
        live_copilot_hot_interval_sec=max(0, min(60, payload.live_copilot_hot_interval_sec or 0)),
        copilot_custom_context=(payload.copilot_custom_context or "").strip(),
        today_view_enabled=bool(payload.today_view_enabled),
        auto_prep_brief_enabled=bool(payload.auto_prep_brief_enabled),
        auto_prep_brief_lead_min=max(1, min(120, payload.auto_prep_brief_lead_min or 10)),
        cloud_mirror_dir=(payload.cloud_mirror_dir or "").strip(),
        session_archive_dir=new_archive_dir,
        live_vad_enabled=bool(payload.live_vad_enabled),
        live_speaker_split_enabled=bool(payload.live_speaker_split_enabled),
        diarization_device=(payload.diarization_device or "auto").strip().lower(),
        audio_mix_format_lookup_enabled=bool(payload.audio_mix_format_lookup_enabled),
        echo_cancellation_enabled=bool(payload.echo_cancellation_enabled),
        session_index_enabled=bool(payload.session_index_enabled),
        channel_attribution_enabled=bool(
            payload.channel_attribution_enabled),
        calendar_source=(payload.calendar_source or "auto").strip().lower(),
    )
    # If the recordings folder changed, migrate client + template state
    # from the previous folder to the new one. Copy, not move, so the
    # old location keeps working if the user reverts. load_settings()'s
    # own migration covers the USER_DATA_DIR→recordings_dir first-launch
    # path; this handles the recordings_dir→other-recordings_dir change.
    new_recordings_dir = (payload.recordings_dir or "").strip()
    if (prev_recordings_dir
            and new_recordings_dir
            and prev_recordings_dir != new_recordings_dir):
        import shutil as _shutil
        from pathlib import Path as _Path
        for _filename in (
            "client_configs.json", "summary_templates.json",
            "owner_aliases.json",
        ):
            _old = _Path(prev_recordings_dir) / _filename
            _new = _Path(new_recordings_dir) / _filename
            if _old.exists() and not _new.exists():
                try:
                    _new.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(_old, _new)
                    logger.info(
                        f"Migrated {_filename} on folder change: "
                        f"{_old} -> {_new}")
                except Exception as _e:
                    logger.warning(
                        f"Migration of {_filename} on folder change "
                        f"failed: {_e}")

    # Force reload
    svc.settings = None
    svc.models_ready = False
    svc.load_settings()

    # Toggle the auto-record loop to match the freshly-saved flag.
    try:
        _ensure_auto_record_service()
    except Exception as e:
        logger.warning(f"AutoRecordService toggle failed: {e}")

    # Apply launch-on-login state to the OS only on actual transitions.
    # The startup_shortcut module is platform-aware: Windows installs a
    # .lnk in the Startup folder, macOS installs a LaunchAgent plist,
    # Linux is a no-op. Failures are logged but don't block the save —
    # the user can re-toggle later if they fix the underlying issue
    # (e.g. denied LaunchAgents permission on locked-down Macs).
    if bool(payload.launch_on_startup) != prev_launch:
        try:
            from utils.startup_shortcut import apply as apply_startup
            await asyncio.to_thread(apply_startup, bool(payload.launch_on_startup))
        except Exception as e:
            logger.warning(f"launch_on_startup transition failed: {e}")

    # If the Session Archive folder actually changed (set, cleared, or
    # repointed), kick a reconcile in the background right away rather
    # than waiting for the next process/export to trigger one — that's
    # what makes existing sessions start populating the shared folder
    # immediately instead of only "the next meeting onward" (field
    # report 2026-08-07). Fire-and-forget: _reconcile_archive() only
    # enqueues onto the ExportWorker, it doesn't copy inline, so this
    # returns fast either way — the background task is purely so a slow
    # rglob over a huge library can't delay the Settings save response.
    if new_archive_dir != prev_archive_dir:
        async def _archive_reconcile_after_save() -> None:
            try:
                queued = await asyncio.to_thread(_reconcile_archive)
                if queued:
                    logger.info(
                        f"Archive reconcile after settings save queued "
                        f"{queued} session(s)")
            except Exception as e:
                logger.warning(
                    f"Archive reconcile after settings save failed: {e}")
        asyncio.create_task(_archive_reconcile_after_save())

    return {"ok": True}


# ── GPU acceleration toggle ──────────────────────────────────────────
# The bundled installer ships with CPU-only torch. Users with an NVIDIA
# GPU can opt-in to CUDA torch; users with AMD/Intel/other GPUs can
# opt-in to DirectML. Each backend wheel swap runs via pip in a
# subprocess so the running backend doesn't have to restart mid-install.
# After a successful swap the UI restarts the backend to pick up the
# new torch build.

_GPU_TASK_LOCK = threading.Lock()
_gpu_task_state = {
    "running": False,
    "phase": "idle",   # idle | installing | complete | error
    "message": "",
    "progress_lines": [],  # last ~30 pip output lines
}


_GPU_DETECTION_CACHE: dict | None = None


def _detect_gpu_hardware() -> dict:
    """Best-effort GPU probe.

    Windows: reads the registry's DisplayAdapters key — no subprocess, no
    PowerShell. (Previous versions shelled out to Get-CimInstance, which
    AppLocker / SentinelOne / CrowdStrike / Zscaler kept killing on
    corporate laptops, taking the backend down with it.)

    macOS: reports whether the host is Apple Silicon (in which case torch's
    MPS backend gives transparent GPU acceleration with no extra
    install). No registry / IOKit poking — torch already knows.
    """
    global _GPU_DETECTION_CACHE
    if _GPU_DETECTION_CACHE is not None:
        return _GPU_DETECTION_CACHE

    result = {"nvidia": False, "amd": False, "intel": False, "gpus": []}

    if sys.platform == "darwin":
        import platform as _platform
        is_arm = _platform.machine() == "arm64"
        result["platform"] = "macos"
        if is_arm:
            result["apple_silicon"] = True
            result["gpus"].append("Apple Silicon (Metal / MPS)")
            result["recommended"] = "mps"
        else:
            result["apple_silicon"] = False
            result["gpus"].append("Intel Mac (CPU only)")
            result["recommended"] = "cpu"
        _GPU_DETECTION_CACHE = result
        return result

    if sys.platform != "win32":
        # Linux / other — no detection. UI hides the GPU panel.
        result["platform"] = "linux"
        result["recommended"] = "cpu"
        _GPU_DETECTION_CACHE = result
        return result

    # Falls through to Windows-registry detection below.
    result["platform"] = "windows"

    try:
        import winreg
        # Each installed display adapter gets a subkey under this path
        # with a "DriverDesc" value holding its human-readable name.
        key_path = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                # Registry has "Properties" and "Configuration" subkeys
                # we want to skip — only numeric-named ones are adapters.
                if not sub_name.isdigit():
                    continue
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        desc, _ = winreg.QueryValueEx(sub, "DriverDesc")
                except OSError:
                    continue
                if not isinstance(desc, str) or not desc:
                    continue
                result["gpus"].append(desc)
                low = desc.lower()
                if any(t in low for t in ("nvidia", "geforce", "quadro", "rtx", "gtx")):
                    result["nvidia"] = True
                elif "amd" in low or "radeon" in low:
                    result["amd"] = True
                elif "intel" in low:
                    result["intel"] = True
    except Exception as e:
        # Never let this tip over the whole backend.
        logger.warning(f"GPU detection failed (registry): {e}")

    # DirectML intentionally not recommended even when AMD/Intel GPU is
    # present: torch-directml only ships wheels for Python 3.10, and the
    # app runtime is Python 3.13. `pip install torch-directml` fails with
    # "could not find a version that satisfies the requirement". Until
    # Microsoft publishes a Python 3.13-compatible wheel, non-NVIDIA
    # machines should stay on CPU.
    if result["nvidia"]:
        result["recommended"] = "cuda"
    else:
        result["recommended"] = "cpu"
    _GPU_DETECTION_CACHE = result
    return result


def _current_gpu_backend() -> str:
    """Introspect the installed torch to report what flavour is live."""
    try:
        import torch
        v = torch.__version__  # e.g. "2.6.0+cpu", "2.6.0+cu124"
        if "+cu" in v:
            return "cuda"
        if "+rocm" in v:
            return "rocm"
        # Apple Silicon: torch ships universal2 wheels with MPS baked in.
        # `is_available()` requires both an Apple GPU and a built-with-MPS
        # torch — true for any 2.x build on macOS.
        try:
            if (sys.platform == "darwin"
                    and getattr(torch.backends, "mps", None) is not None
                    and torch.backends.mps.is_available()):
                return "mps"
        except Exception:
            pass
        try:
            import torch_directml  # noqa: F401
            return "directml"
        except ImportError:
            pass
        return "cpu"
    except Exception:
        return "unknown"


@app.get("/gpu/status")
async def gpu_status():
    def _status():
        return {
            "current": _current_gpu_backend(),
            "detected": _detect_gpu_hardware(),
            "task": dict(_gpu_task_state),
            "python_exe": sys.executable,
        }
    return await asyncio.to_thread(_status)


class GpuInstallRequest(BaseModel):
    backend: str  # "cpu" | "cuda" | "directml"


def _run_pip_install(args: list[str]) -> None:
    """Run pip as a subprocess of the CURRENT venv's python and stream
    stdout into the task state so the UI can poll /gpu/status for live
    progress. Does NOT raise — errors are captured in the task state."""
    import subprocess
    _gpu_task_state["running"] = True
    _gpu_task_state["phase"] = "installing"
    _gpu_task_state["progress_lines"] = []
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]
    _gpu_task_state["message"] = "Starting pip install..."
    logger.info(f"GPU swap: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            lines = _gpu_task_state["progress_lines"]
            lines.append(line)
            if len(lines) > 50:
                del lines[:-50]
            _gpu_task_state["message"] = line[:200]
        rc = proc.wait()
        if rc == 0:
            _gpu_task_state["phase"] = "complete"
            _gpu_task_state["message"] = "Install complete. Restart the app to activate."
        else:
            _gpu_task_state["phase"] = "error"
            _gpu_task_state["message"] = f"pip exited {rc}"
    except Exception as e:
        _gpu_task_state["phase"] = "error"
        _gpu_task_state["message"] = f"Exception: {e}"
        logger.exception("GPU swap failed")
    finally:
        _gpu_task_state["running"] = False


@app.post("/gpu/install")
async def gpu_install(req: GpuInstallRequest):
    if _gpu_task_state["running"]:
        raise HTTPException(status_code=409, detail="A GPU install is already running")

    backend_id = req.backend.lower().strip()
    if backend_id == "cpu":
        args = [
            "--index-url", "https://download.pytorch.org/whl/cpu",
            "--force-reinstall", "--no-deps",
            "torch==2.6.0", "torchaudio==2.6.0",
        ]
        # Also remove torch-directml if present
        post = ["uninstall", "-y", "torch-directml"]
    elif backend_id == "cuda":
        # Apple dropped NVIDIA driver support in 2018 and PyTorch ships
        # zero CUDA wheels for macOS. If we let pip run it would churn for
        # ~30s and fail with "no matching distribution" — confusing the
        # user. Reject up front with the right answer instead.
        if sys.platform == "darwin":
            raise HTTPException(
                status_code=400,
                detail=(
                    "CUDA isn't available on macOS. NVIDIA drivers haven't "
                    "shipped on Mac since 2018, and PyTorch publishes no "
                    "CUDA wheels for macOS. On Apple Silicon, MPS (Metal "
                    "Performance Shaders) is already active by default — "
                    "no install needed. On Intel Macs, stay on CPU."
                ),
            )
        args = [
            # cu124 is the 12.x flavor PyTorch ships for torch 2.6.0;
            # cu121 was dropped from the index for this release. Bumped
            # from torch 2.2.2 because PyTorch only added Python 3.13
            # wheels starting at torch 2.5.0, and our bundled runtime
            # is Python 3.13.
            "--index-url", "https://download.pytorch.org/whl/cu124",
            "--force-reinstall", "--no-deps",
            "torch==2.6.0", "torchaudio==2.6.0",
        ]
        post = ["uninstall", "-y", "torch-directml"]
    elif backend_id == "directml":
        # DirectML is disabled at the API level until torch-directml ships
        # wheels for Python 3.13. Reject with a clear explanation instead of
        # letting pip fail with a cryptic "no matching distribution" error.
        raise HTTPException(
            status_code=400,
            detail=(
                "DirectML isn't available on this build yet. torch-directml "
                "only publishes wheels for Python 3.10; the app runs on "
                "Python 3.13. Stay on CPU — on non-NVIDIA laptops the "
                "speed difference isn't large. This will re-enable when "
                "Microsoft releases a newer torch-directml wheel."
            ),
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown backend '{req.backend}'. Use cpu or cuda.",
        )

    def _do():
        _run_pip_install(args)
        if post and _gpu_task_state["phase"] == "complete":
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", *post],
                    capture_output=True, timeout=60,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
            except Exception:
                pass
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "backend": backend_id}


# ── Audio devices ────────────────────────────────────────────────────
@app.get("/audio/devices")
async def get_audio_devices():
    # sd.query_devices() is synchronous and can take 1-3s on Windows
    # (Bluetooth stack enumeration). Run in a thread so the event loop
    # stays responsive for other endpoints.
    def _list_both():
        return {"input": list_input_devices(), "output": list_output_devices()}
    return await asyncio.to_thread(_list_both)


@app.get("/audio/sync-risk")
async def get_audio_sync_risk(mic: str = "", loopback: str = ""):
    """
    Compare the Windows shared-mode mix format of the mic vs. the
    selected System Audio loopback device. The Record-view banner uses
    this to warn the user about default-format mismatches that cause
    mic↔loopback drift on long recordings (v2.10.5 field repro: 16-bit
    mic + 24-bit speakers = ~31 s drift over a 49-min meeting).

    Returns a dict with `ok / level / reason / mic_format /
    loopback_format / fix_hint`. The frontend renders a banner whenever
    level != "ok". On non-Windows (or when pycaw is unavailable) we
    return level="unknown" and the UI hides the banner — the warning
    can't be authoritative on macOS / Linux where the OS audio stack
    handles format conversion differently.

    Both query params are optional. When empty (e.g. the user hasn't
    selected a device yet) we short-circuit to "unknown" so the
    endpoint stays cheap to poll.
    """
    if not mic or not loopback:
        return {
            "ok": True, "level": "unknown",
            "reason": None,
            "mic_format": None, "loopback_format": None, "fix_hint": None,
        }

    # Kill switch: Settings.audio_mix_format_lookup_enabled (default
    # True). False skips the lookup entirely — no subprocess spawn, no
    # pycaw involvement anywhere. See core/audio_format_inspector.py's
    # module docstring for why this is the only degraded mode offered.
    lookup_enabled = bool(
        getattr(svc.settings, "audio_mix_format_lookup_enabled", True))

    def _do():
        from core.audio_format_inspector import (
            compare_formats, get_device_mix_format,
        )
        mic_fmt = get_device_mix_format(mic, "input", enabled=lookup_enabled)
        loop_fmt = get_device_mix_format(
            loopback, "output", enabled=lookup_enabled)
        return compare_formats(mic_fmt, loop_fmt)
    return await asyncio.to_thread(_do)


# ── Calendar ─────────────────────────────────────────────────────────
def _serialize_meetings(meetings):
    return [{
        **m,
        "start": m["start"].isoformat() if hasattr(m["start"], "isoformat") else m["start"],
        "end": m["end"].isoformat() if hasattr(m["end"], "isoformat") else m["end"],
    } for m in meetings]


def _calendar_source() -> str:
    """Current `calendar_source` setting ("auto" / "outlook" /
    "extension" / "off"), defaulting to "auto" if settings haven't
    loaded yet. Every calendar endpoint below calls `svc.load_settings()`
    first, so `svc.settings` is populated by the time this runs."""
    raw = (svc.settings.calendar_source if svc.settings else "") or "auto"
    return raw.strip().lower()


@app.get("/calendar/today")
async def get_calendar_today():
    """Today's meetings (date-based, doesn't cross midnight).

    Gated on `calendar_source`: "extension" and "off" skip the local
    calendar (Outlook COM / EventKit) call entirely — see
    config/settings.py's calendar_source docstring for why (a stray
    Outlook COM touch re-triggers a Microsoft sign-in prompt on some
    tenants). services/calendar_service.py enforces the same gate
    internally as the catch-all for every caller; this check just keeps
    this endpoint from making the call at all when it would be a no-op.
    """
    try:
        svc.load_settings()
        if _calendar_source() in ("extension", "off"):
            return []
        meetings = await asyncio.to_thread(get_todays_meetings)
        return _serialize_meetings(meetings)
    except Exception as e:
        logger.exception("Calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


async def _merged_upcoming(hours: int) -> List[dict]:
    """PANEL/HINT WINDOW — the merged two-source view, `hours` ahead.

    THE call site for "which meetings exist in the next N hours".
    `GET /calendar/upcoming` renders it and AutoRecordService uses it for
    its "next: …" hint, so neither can develop its own idea of what's on
    the calendar — see services/calendar_feed.py's module docstring for
    why that mattered enough to centralize.

    `get_upcoming_meetings` is looked up in this module's namespace on
    every call (not captured at import) so calendar_service's
    `calendar_source` gate — and the test suite's monkeypatches — stay
    effective.
    """
    svc.load_settings()
    return await calendar_feed.merged_upcoming(
        hours,
        source=_calendar_source(),
        fetch_local=lambda: get_upcoming_meetings(hours),
        extension_svc=svc.extension_calendar_svc,
    )


async def _merged_today() -> List[dict]:
    """TRIGGER WINDOW — the same merged two-source view, today only and
    including meetings already in progress.

    THE call site AutoRecordService scans for its start trigger. Same
    sources, same gating, same dedup, same naive-local timestamps as
    `_merged_upcoming` above; only the window differs.
    """
    svc.load_settings()
    return await calendar_feed.merged_today(
        source=_calendar_source(),
        fetch_local=get_todays_meetings,
        extension_svc=svc.extension_calendar_svc,
    )


@app.get("/calendar/upcoming")
async def get_calendar_upcoming(hours: int = 168, refresh: bool = False):
    """
    Meetings from now through N hours ahead.
    Default 168h (7 days) — bumped from 36h because the narrower window
    left the panel empty late-Friday and all weekend, which looks broken
    even though nothing's wrong.
    Pass refresh=true to bypass the 5-minute cache (triggered by the
    Refresh button in the UI when the user added a meeting in Outlook
    and needs it reflected immediately).

    Wrapped in an asyncio timeout so a truly hung Outlook COM call never
    leaves the frontend with a dead fetch. The cap was 15s, but a user
    with many shared/resource Exchange calendars sees first-fetch times
    of ~30s+ — so every cold start timed out to an empty list and the
    panel looked permanently broken until a manual Refresh. 45s clears
    the realistic slow case while still bounding a genuine hang. On
    timeout we still return [] (the background thread keeps going and
    populates the 5-min cache, so the next call is instant).

    TWO SOURCES since v2.22.2. The local calendar (Outlook COM /
    EventKit) stays authoritative; events the Chrome extension scraped
    out of Outlook Web are merged in on top, deduped by normalized
    subject + a ±5-minute start-time window, local copy always
    preferred (it carries attendees, body, organizer, resolved join
    link — the scrape carries none of that). Every returned meeting now
    carries a `source` field, "outlook" or "extension"; no pre-existing
    field changed name or meaning. Rationale: a meeting visible only in
    Outlook Web previously never reached this panel at all, so it could
    not be used to start a recording (field report 2026-08-11).

    CONCURRENT since field report 2026-08-14: the local fetch and the
    extension fetch used to run sequentially — a blocked/slow/prompting
    Outlook (up to the full 45s timeout above) withheld the extension
    events for the entire wait, even though they were already on disk
    and instantly available. The two fetches now run concurrently via
    `asyncio.gather`; the 45s timeout still applies to the local fetch
    only, and whichever side finishes first no longer waits on the
    other. `calendar_source` gates which side(s) run at all:
    "extension" skips the local fetch outright (never touches Outlook
    COM / EventKit), "off" skips both and returns [].

    SHARED WITH AUTO-RECORD since the extension-source fix: everything
    described above — gating, concurrency, the 45s local cap, dedup,
    naive-local timestamps — now lives in services/calendar_feed.py and
    is reached through `_merged_upcoming`, because AutoRecordService
    needs the identical answer. It used to read the LOCAL calendar
    directly, so an extension-only meeting rendered here as perfectly
    recordable while auto-record could never see it at all. Read
    calendar_feed's docstring before reintroducing any fetch/merge logic
    into this handler.
    """
    try:
        if refresh:
            from services.calendar_service import invalidate_calendar_cache
            invalidate_calendar_cache()
        # `_merged_upcoming` calls svc.load_settings() itself —
        # idempotent + cheap once loaded, and needed because the
        # extension-calendar store is only constructed inside it.
        return _serialize_meetings(await _merged_upcoming(hours))
    except Exception as e:
        logger.exception("Upcoming calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/calendar/available")
async def calendar_available():
    """Whether the Record tab's Upcoming Meetings panel has a usable
    calendar source right now.

    calendar_source-aware:
      - "off" is always unavailable — no calendar source is in use.
      - "extension" reports availability based on whether the Chrome
        extension has ever synced events, NOT on Outlook — probing
        Outlook here would defeat the entire point of this mode (never
        touching Outlook COM / EventKit; see config/settings.py's
        calendar_source docstring). Also includes `last_capture_at` and
        `event_count` (from ExtensionCalendarService.capture_status) so
        the Record tab's empty state can say plainly "no meetings from
        the extension, last checked at X" instead of rendering
        identically to a genuinely free calendar (field report
        2026-08-13 — that ambiguity is how a whole day of meetings
        went missing without the user realizing the mode was at fault).
      - "auto" / "outlook" preserve the original behavior: probe the
        local calendar backend.
    """
    svc.load_settings()
    source = _calendar_source()
    if source == "off":
        return {"available": False, "source": source}
    if source == "extension":
        status = {
            "updated_at": None, "event_count": 0, "future_event_count": 0,
            "last_import_path": None, "last_import_raw": None,
            "last_import_kept": None, "last_import_dropped": None,
            "last_import_fallback_reason": None, "last_import_at": None,
        }
        if svc.extension_calendar_svc:
            try:
                status = await asyncio.to_thread(
                    svc.extension_calendar_svc.capture_status)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Extension calendar status unavailable: {e}")
        return {
            "available": status["event_count"] > 0,
            "source": source,
            "last_capture_at": status["updated_at"],
            "event_count": status["event_count"],
            "future_event_count": status["future_event_count"],
            # Which calendar-parse path produced the currently-retained
            # events, and its raw/kept/dropped counts — see
            # ExtensionCalendarService.replace_all's `import_meta` and
            # the server's "Extension calendar: path=..." log line.
            # Lets the Record tab say "2 meetings from the extension's
            # structured capture" vs. "1 meeting recovered from text"
            # instead of rendering identically regardless of which path
            # ran (field report chain culminating 2026-08-14).
            "last_import_path": status.get("last_import_path"),
            "last_import_raw": status.get("last_import_raw"),
            "last_import_kept": status.get("last_import_kept"),
            "last_import_dropped": status.get("last_import_dropped"),
            "last_import_fallback_reason": status.get("last_import_fallback_reason"),
            "last_import_at": status.get("last_import_at"),
        }
    available = await asyncio.to_thread(is_outlook_available)
    return {"available": bool(available), "source": source}


@app.get("/calendar/meeting-detail")
async def calendar_meeting_detail(subject: str, start: str):
    """Lazy detail for one calendar invite — agenda/body, attendees, and
    a parsed one-click join link. Fetched on demand (per meeting) so the
    bulk calendar list stays fast: pulling Outlook bodies for every
    meeting in the window would blow the 15s COM budget.

    Gated on `calendar_source` like every other entry point here —
    "extension"/"off" return the empty shape without ever calling into
    Outlook COM / EventKit."""
    svc.load_settings()
    if _calendar_source() in ("extension", "off"):
        return {"attendees": [], "body": "", "join_url": None}
    from services.calendar_service import get_meeting_detail
    return await asyncio.to_thread(get_meeting_detail, subject, start)


# ── Recording ────────────────────────────────────────────────────────
def _load_models_async():
    """Fire-and-forget model load on a thread."""
    import threading
    if svc.models_ready or svc.models_loading:
        return
    threading.Thread(target=svc.ensure_models_loaded, daemon=True).start()


@app.post("/models/load")
async def trigger_model_load():
    """Kick off async model load."""
    _load_models_async()
    return {"loading": True}


@app.get("/recording/status", response_model=RecordingStatus)
async def recording_status():
    svc.load_settings()
    rec = svc.recording_svc
    is_rec = rec is not None and rec.is_recording
    session_id = rec.current_session.session_id if is_rec and rec.current_session else None
    duration_s = 0
    started_iso = None
    if is_rec and svc.record_started_at:
        duration_s = int((datetime.now() - svc.record_started_at).total_seconds())
        started_iso = svc.record_started_at.isoformat()

    # Tick the auto-stop watchdog here too — duplicate of the timer
    # in _watchdog_loop, kept on the status path so the UI sees the
    # most-recent warnings list on every poll. The timer is the
    # authoritative driver (it fires regardless of whether anyone's
    # polling); this handler's tick is a freshness optimization.
    # If the watchdog says we should stop, do it BEFORE returning the
    # status so the UI sees is_recording=False on the same tick.
    warnings: list[dict] = []
    if is_rec and rec is not None:
        try:
            decision = await asyncio.to_thread(rec.watchdog_tick)
            warnings = decision.get("warnings", []) or []
            if decision.get("should_auto_stop"):
                logger.info(
                    f"Watchdog auto-stopping recording: "
                    f"{decision.get('reason', '?')}")
                # Trigger the same path the user's Stop button uses so
                # the audio file finalises cleanly. Off-loop because
                # stop_recording does I/O.
                try:
                    session = await asyncio.to_thread(_stop_recording_sync)
                    # Same backend-owned auto-process as every other stop
                    # path. Idempotent per session, so if the timer-driven
                    # watchdog already fired for this session this is a
                    # no-op.
                    _maybe_auto_process(session)
                except Exception as e:
                    logger.exception(
                        f"Watchdog auto-stop raised: {e}")
                # Re-read recording state so the response reflects the
                # auto-stop that just happened.
                is_rec = rec is not None and rec.is_recording
                if not is_rec:
                    svc.record_started_at = None
        except Exception as e:
            logger.exception(f"Watchdog tick failed: {e}")

    # One-shot skip-reason: read + clear so the frontend only sees it
    # once and we never spam-notify on every status poll.
    skip_reason = svc.auto_record_skip_reason
    svc.auto_record_skip_reason = None

    # Capture-confidence snapshot — cheap attribute reads, no I/O. Only
    # meaningful while actually recording; defaults otherwise so the
    # frontend's "not recording" state never shows a stale meter.
    levels: dict = {}
    if is_rec and rec is not None:
        try:
            levels = await asyncio.to_thread(rec.get_capture_levels)
        except Exception as e:
            logger.exception(f"get_capture_levels failed: {e}")
            levels = {}

    return RecordingStatus(
        is_recording=is_rec,
        session_id=session_id,
        started_at=started_iso,
        duration_s=duration_s,
        models_ready=svc.models_ready,
        models_loading=svc.models_loading,
        models_error=svc.models_error,
        current_status=svc.current_status,
        is_processing=svc.is_processing,
        warnings=warnings,
        auto_record_subject=(svc.auto_record_subject if is_rec else None),
        auto_record_skip_reason=skip_reason,
        mic_level=levels.get("mic_level", 0.0),
        system_level=levels.get("system_level", 0.0),
        mic_state=levels.get("mic_state"),
        system_state=levels.get("system_state"),
        capture_warning=levels.get("capture_warning"),
    )


def _start_recording_sync(req: StartRecordingRequest):
    # Parse scheduled_end_iso into a datetime if present. Bad values
    # silently degrade to None so the watchdog's overrun trigger
    # doesn't fire — better than an opaque error from the start path.
    scheduled_end = None
    if req.scheduled_end_iso:
        try:
            scheduled_end = datetime.fromisoformat(req.scheduled_end_iso)
        except ValueError:
            logger.warning(
                f"Could not parse scheduled_end_iso="
                f"{req.scheduled_end_iso!r}; meeting-overrun watchdog "
                f"will be inactive for this recording.")
    # Conference room mode forces system-audio loopback off no matter
    # what the UI sent for output_device_index — the whole point of
    # the mode is "we're in a room with the laptop on the table; the
    # mic captures everyone; there's nothing meaningful to record off
    # the system output."
    output_idx = None if req.conference_room_mode else req.output_device_index
    session = svc.recording_svc.start_recording(
        mic_device_index=req.mic_device_index,
        output_device_index=output_idx,
        scheduled_end=scheduled_end,
        conference_room_mode=req.conference_room_mode,
    )
    session.display_name = req.meeting_name or ""
    session.template = req.template or "General"
    session.client = req.client or ""
    session.project = req.project or ""
    session.client_source = req.client_source or None
    session.client_source_detail = req.client_source_detail or None
    session.attendees = req.attendees or []
    # The invite's organiser. Coerced to a plain string here so the
    # session field is never None regardless of what the client sent —
    # core/speaker_roster.roster_names() reads it positionally and ""
    # is its documented "no organiser" input.
    session.organizer = str(req.organizer or "")
    svc.current_session = session
    svc.record_started_at = datetime.now()
    return session


# Last-used recording devices. Persisted by name (indices shift across
# reboots / USB re-plugs) in a small sidecar JSON, written by the
# manual /recording/start route on every successful start and read by
# _auto_record_start so the calendar auto-recorder uses the same mic /
# loopback the user picked manually. The old auto-record path passed
# `None` indices, which made the capture silent and produced empty
# recordings — surfacing that to the user is the auto_record_skip_reason
# path below.
def _last_devices_path():
    from config.settings import USER_DATA_DIR
    from pathlib import Path as _Path
    return _Path(USER_DATA_DIR) / "last_devices.json"


def _read_last_devices() -> dict:
    p = _last_devices_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"last_devices.json unreadable ({e}); ignoring.")
        return {}


def _write_last_devices(mic_name: str, output_name: str) -> None:
    p = _last_devices_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"mic_name": mic_name, "output_name": output_name}),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"could not persist last devices: {e}")


def _name_for_input_index(idx: Optional[int]) -> str:
    if idx is None:
        return ""
    try:
        for d in list_input_devices():
            if int(d.get("index", -1)) == idx:
                return str(d.get("name") or "")
    except Exception as e:
        logger.warning(f"input device lookup failed: {e}")
    return ""


def _name_for_output_index(idx: Optional[int]) -> str:
    if idx is None:
        return ""
    try:
        for d in list_output_devices():
            if int(d.get("index", -1)) == idx:
                return str(d.get("name") or "")
    except Exception as e:
        logger.warning(f"output device lookup failed: {e}")
    return ""


def _input_index_for_name(name: str) -> Optional[int]:
    if not name:
        return None
    try:
        for d in list_input_devices():
            if str(d.get("name") or "") == name:
                return int(d.get("index"))
    except Exception as e:
        logger.warning(f"input device lookup failed: {e}")
    return None


def _output_index_for_name(name: str) -> Optional[int]:
    if not name:
        return None
    try:
        for d in list_output_devices():
            if str(d.get("name") or "") == name:
                return int(d.get("index"))
    except Exception as e:
        logger.warning(f"output device lookup failed: {e}")
    return None


def _resolve_client_for_meeting(subject: str, attendees: list) -> dict:
    """Resolve a calendar meeting to a client, server-side.

    Returns the `ClientResolution.to_dict()` shape
    (`client` / `project` / `method` / `detail`). See
    services/client_resolution_service.py for the signal order and the
    token-boundary matching rule.

    STRICTLY ADDITIVE AND NON-FATAL. Every caller is on a path whose
    real job is starting a recording or generating a brief; a tagging
    failure must never cost someone either of those. Anything that goes
    wrong in here — an un-downloaded client_configs.json, an unreadable
    recordings dir, a bug in the resolver — is logged and degrades to
    "no client", which is exactly the behaviour that shipped before this
    existed.
    """
    # Import inside the try as well: a broken import here must degrade
    # to "no client", never propagate into the record-start path.
    _empty = {"client": "", "project": "", "method": "none", "detail": ""}
    try:
        from services.client_resolution_service import resolve_client
        try:
            configs = svc.client_cfg_svc.get_all() if svc.client_cfg_svc else {}
        except Exception as e:
            # CloudFileNotReadyError lives here: the synced
            # client_configs.json hasn't downloaded yet. Session-derived
            # client names still work, so carry on with an empty config.
            logger.warning(f"client resolution: client configs unavailable ({e})")
            configs = {}
        sessions = svc.session_svc.list_sessions() if svc.session_svc else []
        res = resolve_client(
            subject=subject or "",
            attendees=list(attendees or []),
            client_configs=configs,
            sessions=sessions,
        )
    except Exception as e:
        logger.warning(f"client resolution failed for {subject!r}: {e}")
        return dict(_empty)
    if res.resolved:
        logger.info(
            f"client resolution: '{subject}' → client={res.client!r} "
            f"project={res.project!r} via {res.method}")
    else:
        logger.info(
            f"client resolution: '{subject}' → no client ({res.method}: "
            f"{res.detail})")
    return res.to_dict()


def _organizer_for_meeting(meeting: Any) -> str:
    """The invite's organiser for one merged calendar meeting, or "".

    All three calendar backends already emit an ``organizer`` key on
    every meeting dict — ``_calendar_outlook._parse_appointment`` /
    ``_parse_appointment_any_date`` (Outlook COM's ``Organizer``),
    ``_calendar_eventkit._serialize_event`` (EventKit's
    ``organizer().name()``, and the Rust calendar cache's own
    ``organizer`` field), and
    ``extension_calendar_service.events_from_structured`` (the Outlook
    Web aria-label tail, added in v2.40.0). ``calendar_feed`` merges
    whole dicts and ``_serialize_meetings`` spreads them, so the key
    survives to both record-start paths untouched. This function is
    just the total, never-raising read of it.

    TOTAL BY CONSTRUCTION, for the same reason
    ``_resolve_client_for_meeting`` is: the caller's real job is
    STARTING A RECORDING. A meeting that arrives without the key, with
    None, or as something that isn't a dict at all must cost the user
    a name in the roster — never the recording. Everything degrades to
    "", which is precisely the state every pre-organiser session is
    already in.
    """
    try:
        raw = meeting.get("organizer") if hasattr(meeting, "get") else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"organiser lookup failed ({e}); continuing without one")
        return ""
    # Only a STRING is an organiser. A number or an object from a
    # third-party backend is coerced to "" rather than str()'d — the
    # roster prints its entries as people, so "42" would render as a
    # participant who does not exist. Same refusal
    # core/speaker_roster.roster_names() makes for a non-string
    # attendee entry.
    if not isinstance(raw, str):
        if raw is not None:
            logger.warning(
                f"calendar organiser was {type(raw).__name__}, not a string; "
                f"continuing without one")
        return ""
    try:
        return raw.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"organiser value unusable ({e}); continuing without one")
        return ""


def _auto_record_start(meeting: dict) -> None:
    """Adapter the AutoRecordService calls when a meeting window opens.
    Synthesizes a StartRecordingRequest from the calendar event and hands
    it to the same sync start path the HTTP route uses, so the watchdog
    (overrun + silence) gets the same scheduled_end_iso treatment as a
    user-driven start. Runs in a worker thread (called via to_thread)."""
    from services.calendar_service import make_session_name
    end = meeting.get("end")
    end_iso = end.isoformat() if hasattr(end, "isoformat") else None
    name = make_session_name(meeting) if meeting.get("start") else (meeting.get("subject") or "")

    # Resolve the user's last-used mic/loopback by NAME. Without this we
    # used to pass None and capture silence. If we have no saved mic,
    # skip the meeting and surface a clear reason — silently recording
    # nothing is the worst possible failure mode.
    saved = _read_last_devices()
    mic_idx = _input_index_for_name(saved.get("mic_name", ""))
    out_idx = _output_index_for_name(saved.get("output_name", ""))
    if mic_idx is None:
        reason = (
            "Auto-record skipped — no microphone configured. Start a "
            "manual recording once to register your devices."
        )
        logger.warning(f"auto-record skipped for '{name}': {reason}")
        svc.auto_record_skip_reason = reason
        return

    # Tag the session at creation instead of making the user come back
    # after the call and file it by hand. Placed AFTER the device/skip
    # checks above so it can't waste work on a meeting we've already
    # decided not to record, and it only ever ADDS fields to the request
    # — nothing here can decide whether we record.
    # `_resolve_client_for_meeting` swallows its own failures, and the
    # belt-and-braces try here means even a programming error in it
    # leaves the recording starting exactly as it does today.
    resolution = {"client": "", "project": "", "method": "none", "detail": ""}
    try:
        resolution = _resolve_client_for_meeting(
            str(meeting.get("subject") or "") or name,
            list(meeting.get("attendees") or []),
        )
    except Exception as e:
        logger.warning(f"auto-record: client resolution skipped ({e})")

    # The invite's organiser, obtained the same way client/project are
    # above: from the meeting already in hand, additively, and behind a
    # belt-and-braces try even though `_organizer_for_meeting` swallows
    # its own failures. A missing organiser is a missing NAME, not a
    # missing recording — this block must never be able to return
    # early or raise.
    organizer = ""
    try:
        organizer = _organizer_for_meeting(meeting)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"auto-record: organiser resolution skipped ({e})")

    req = StartRecordingRequest(
        meeting_name=name,
        attendees=list(meeting.get("attendees") or []),
        organizer=organizer,
        client=resolution.get("client") or "",
        project=resolution.get("project") or "",
        client_source=resolution.get("method") or None,
        client_source_detail=resolution.get("detail") or None,
        scheduled_end_iso=end_iso,
        mic_device_index=mic_idx,
        output_device_index=out_idx,
        # Empty saved output name → conference-room mode (no loopback).
        conference_room_mode=(out_idx is None),
    )

    # Stamp the meeting subject so the frontend can show
    # "Auto-recording: <subject>" + the persistent badge label.
    subject = str(meeting.get("subject") or "").strip()
    svc.auto_record_subject = (subject[:120] if subject else name) or "Untitled meeting"

    # Mirror the HTTP route's pre-warm step so live transcription has
    # Whisper ready when the first window completes. ONLY when live
    # transcription is on — recording itself never needs models, and a
    # model load that crashes (the 0xC0000005 class) must not be able
    # to take down a recording that didn't ask for AI.
    if (svc.settings and svc.settings.is_configured
            and svc.settings.live_transcription_enabled
            and not svc.models_ready and not svc.models_loading):
        threading.Thread(target=svc.ensure_models_loaded, daemon=True).start()
    _start_recording_sync(req)


def want_safe_mode() -> bool:
    """True when the desktop shell asked for a reduced startup.

    Set by the Rust supervisor (`MEETING_RECORDER_SAFE_MODE=1`) after
    several backends died before finishing startup — see SAFE_MODE in
    src-tauri/src/lib.rs for the incident that produced it.

    Read at CALL time rather than cached at import, so a `restart_backend`
    that clears the flag takes effect without the module being reloaded.

    Defaults to off, and any unexpected value reads as off: safe mode
    disables real features, so the failure direction has to be "ran
    normally" rather than "silently degraded".
    """
    return os.environ.get("MEETING_RECORDER_SAFE_MODE", "").strip() == "1"


def _ensure_auto_record_service() -> None:
    """Lazily build the AutoRecordService once settings exist, and
    start/stop its loop to match the current `auto_record_enabled` flag.
    Safe to call from any HTTP handler — it's idempotent.

    The two meeting getters are the SAME merged two-source windows
    `GET /calendar/upcoming` renders (`_merged_upcoming` /
    `_merged_today` → services/calendar_feed.py). They used to be
    `calendar_service.get_upcoming_meetings` / `get_todays_meetings` —
    the LOCAL calendar only — which is why a user whose whole calendar
    comes from the Chrome extension had an auto-record toggle that
    worked and never fired: the panel listed every meeting, the trigger
    loop could see none of them. Do not point these back at a single
    source; the point is that the thing the user SEES and the thing that
    ACTS are one list.
    """
    from services.auto_record_service import AutoRecordService
    if svc.auto_record_svc is None:
        svc.auto_record_svc = AutoRecordService(
            get_upcoming_meetings=_merged_upcoming,
            get_todays_meetings=_merged_today,
            is_recording=lambda: bool(
                svc.recording_svc and svc.recording_svc.is_recording),
            start_recording=_auto_record_start,
            is_enabled=lambda: bool(
                svc.settings and svc.settings.auto_record_enabled),
            is_blocked=lambda m: bool(
                svc.auto_record_blocklist_svc
                and svc.auto_record_blocklist_svc.is_blocked(m)),
        )
    # SAFE MODE. The Rust supervisor sets this after several backends
    # died before finishing startup (see SAFE_MODE in src-tauri/src/
    # lib.rs). Auto-record is the startup path that has actually caused
    # that: a meeting already in progress makes it fire a recording
    # within milliseconds of boot, which on 2026-08-20 collided with the
    # audio pre-warm inside PortAudio and took the process down —
    # repeatedly, with the app unusable and Settings unreachable because
    # Settings is served BY the backend.
    #
    # Refusing to start the loop here is what makes the app reachable
    # again. The user's SETTING is untouched: this does not write
    # auto_record_enabled, so nothing is silently turned off behind
    # their back — a normal restart leaves safe mode and restores it.
    if want_safe_mode():
        if svc.auto_record_svc.running:
            asyncio.create_task(svc.auto_record_svc.stop())
        logger.warning(
            "SAFE MODE: auto-record loop not started. The app repeatedly "
            "crashed during startup; restart normally once the cause is "
            "addressed.")
        return

    want_on = bool(svc.settings and svc.settings.auto_record_enabled)
    if want_on and not svc.auto_record_svc.running:
        svc.auto_record_svc.start()
    elif not want_on and svc.auto_record_svc.running:
        # Schedule the stop without awaiting — the loop unwinds on its
        # own cancellation; we don't want to block the settings handler.
        asyncio.create_task(svc.auto_record_svc.stop())


@app.post("/recording/start")
async def start_recording(req: StartRecordingRequest):
    svc.load_settings()
    if not svc.recording_svc:
        raise HTTPException(status_code=500, detail="Recording service not initialized")
    if svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="Already recording")
    # Pre-warm AI models so live transcription has Whisper ready when the
    # first 15-second window completes (~15s into the recording). Without
    # this the first window sits waiting 5-10s for the cold model load
    # before any text appears, which feels like the live preview is
    # broken. The load runs in a thread; recording starts immediately.
    # ONLY when live transcription is on — recording never requires
    # models; with the preview off there is nothing to warm, and a
    # crashing model load must not be able to touch a recording.
    if (svc.settings and svc.settings.is_configured
            and svc.settings.live_transcription_enabled
            and not svc.models_ready and not svc.models_loading):
        threading.Thread(target=svc.ensure_models_loaded, daemon=True).start()
    try:
        # start_recording can take a couple seconds opening audio streams —
        # run off the event loop so /recording/status stays responsive
        session = await asyncio.to_thread(_start_recording_sync, req)
        # Manual start — wipe any leftover auto-record state and
        # persist the chosen devices BY NAME so calendar auto-record
        # uses the same ones (indices shift across reboots / re-plugs).
        svc.auto_record_subject = None
        svc.auto_record_skip_reason = None
        try:
            mic_name = _name_for_input_index(req.mic_device_index)
            out_name = ("" if req.conference_room_mode
                        else _name_for_output_index(req.output_device_index))
            _write_last_devices(mic_name, out_name)
        except Exception as e:
            logger.warning(f"could not persist last devices: {e}")
        return {"session_id": session.session_id}
    except Exception as e:
        logger.exception("Start recording failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/recording/auto-status")
async def recording_auto_status():
    """Lightweight status feed for the auto-record toggle on the main
    page. Returns whether the loop is currently running and, when on,
    the next qualifying calendar event (so the UI can render
    'Auto-record on — next: <subject> at <time>')."""
    svc.load_settings()
    running = bool(svc.auto_record_svc and svc.auto_record_svc.running)
    next_event = svc.auto_record_svc.next_event if svc.auto_record_svc else None
    return {
        "enabled": bool(svc.settings and svc.settings.auto_record_enabled),
        "running": running,
        "next_event": next_event,
    }


class BlocklistRequest(BaseModel):
    subject: str


@app.get("/auto-record/blocklist")
async def get_auto_record_blocklist():
    """Subjects + patterns the user flagged 'never auto-record'. Exact
    entries are matched case/whitespace-insensitively by subject; patterns
    are matched as case-insensitive substrings anywhere in the subject
    so a recurring 'Cancelled: …' prefix can be caught with one pattern."""
    if not svc.auto_record_blocklist_svc:
        return {"subjects": [], "patterns": []}
    subjects = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_all)
    patterns = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_patterns)
    return {"subjects": subjects, "patterns": patterns}


@app.post("/auto-record/blocklist")
async def add_auto_record_blocklist(req: BlocklistRequest):
    if not svc.auto_record_blocklist_svc:
        raise HTTPException(status_code=503,
                            detail="Blocklist service not initialized")
    subject = (req.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    await asyncio.to_thread(svc.auto_record_blocklist_svc.add, subject)
    subjects = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_all)
    patterns = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_patterns)
    return {"ok": True, "subjects": subjects, "patterns": patterns}


@app.delete("/auto-record/blocklist")
async def remove_auto_record_blocklist(req: BlocklistRequest):
    if not svc.auto_record_blocklist_svc:
        raise HTTPException(status_code=503,
                            detail="Blocklist service not initialized")
    await asyncio.to_thread(
        svc.auto_record_blocklist_svc.remove, (req.subject or "").strip())
    subjects = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_all)
    patterns = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_patterns)
    return {"ok": True, "subjects": subjects, "patterns": patterns}


@app.post("/auto-record/blocklist/patterns")
async def add_auto_record_blocklist_pattern(req: BlocklistRequest):
    """Add a case-insensitive substring pattern. Any meeting whose
    subject contains the pattern anywhere will be skipped — e.g. the
    pattern 'canceled' blocks 'Canceled: Weekly Sync' and
    'Project X (Canceled)' alike."""
    if not svc.auto_record_blocklist_svc:
        raise HTTPException(status_code=503,
                            detail="Blocklist service not initialized")
    pattern = (req.subject or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    await asyncio.to_thread(svc.auto_record_blocklist_svc.add_pattern, pattern)
    subjects = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_all)
    patterns = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_patterns)
    return {"ok": True, "subjects": subjects, "patterns": patterns}


@app.delete("/auto-record/blocklist/patterns")
async def remove_auto_record_blocklist_pattern(req: BlocklistRequest):
    if not svc.auto_record_blocklist_svc:
        raise HTTPException(status_code=503,
                            detail="Blocklist service not initialized")
    await asyncio.to_thread(
        svc.auto_record_blocklist_svc.remove_pattern,
        (req.subject or "").strip())
    subjects = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_all)
    patterns = await asyncio.to_thread(svc.auto_record_blocklist_svc.list_patterns)
    return {"ok": True, "subjects": subjects, "patterns": patterns}


@app.get("/recording/screenshot/dir")
async def get_screenshot_dir():
    """Where the Tauri shell should write a screenshot for the active
    recording. The actual screen capture happens in the Rust layer
    (macOS attributes Screen Recording permission to the signed app
    bundle, not the Python child), so Python only owns the destination
    path + session bookkeeping."""
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="Not recording")
    d = await asyncio.to_thread(svc.recording_svc.screenshot_dir)
    if d is None:
        raise HTTPException(status_code=409, detail="No active session")
    sess = svc.recording_svc.current_session
    return {"dir": str(d), "session_id": sess.session_id if sess else None}


class ScreenshotRequest(BaseModel):
    path: str


@app.post("/recording/screenshot")
async def attach_screenshot(req: ScreenshotRequest):
    """Register a screenshot the shell just captured against the active
    session. Fed to the summarizer as visual context once the session is
    processed.

    Also mirrors the updated screenshot list to disk immediately
    (best-effort) rather than waiting for stop/process. Field reports of
    the backend dying mid-recording showed this was silent data loss:
    the PNG sat on disk but nothing on disk linked it to the session, so
    a crash between "screenshot taken" and "recording stopped cleanly"
    orphaned it. The in-memory list on recording_svc stays authoritative
    for the running session either way — this write is purely a
    crash-resilience mirror, so a failure here must never fail the
    attach or interrupt the recording."""
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="Not recording")
    ok = await asyncio.to_thread(
        svc.recording_svc.add_screenshot, (req.path or "").strip())
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Screenshot file not found or no active session")
    sess = svc.recording_svc.current_session
    count = len(sess.screenshots) if sess else 0
    if sess is not None:
        try:
            await asyncio.to_thread(svc.session_svc.save, sess)
        except Exception as e:
            logger.warning(
                "Could not persist screenshot attach for session "
                "%s (will still be saved on stop): %s",
                sess.session_id, e)
    return {"ok": True, "count": count}


@app.get("/recording/screenshots/{index}")
async def get_active_recording_screenshot(index: int):
    """Serve one screenshot from the ACTIVE, in-memory recording.

    Unlike /sessions/{id}/screenshots/{index}, this doesn't depend on
    the session having reached disk yet — before the persistence added
    to attach_screenshot() above, screenshots only landed in the session
    JSON on stop/process, so the live thumbnail strip in the Record view
    would 404 for the whole recording. Reading straight off
    recording_svc.current_session (the same object attach_screenshot
    appends to) means a screenshot is servable moments after capture.
    There's only ever one active recording, so no session id in the URL.

    Same containment story as the historical endpoint: a screenshot path
    is not fully trusted input, so it still goes through
    _resolve_within_scan_roots() rather than being served directly."""
    from fastapi.responses import FileResponse
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=404, detail="No active recording")
    sess = svc.recording_svc.current_session
    if sess is None:
        raise HTTPException(status_code=404, detail="No active recording")
    shots = list(sess.screenshots or [])
    if index < 0 or index >= len(shots):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    raw_path = shots[index]
    resolved = _resolve_within_scan_roots(raw_path)
    if resolved is None:
        logger.warning(
            "Refusing to serve live screenshot for session %s: %r is "
            "outside the configured recordings/archive roots",
            sess.session_id, raw_path)
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404,
                            detail="Screenshot file missing on disk")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(resolved.suffix.lower(), "application/octet-stream")
    return FileResponse(str(resolved), media_type=media, filename=resolved.name)


@app.get("/recording/transcript/stream")
async def stream_live_transcript():
    """Server-Sent Events endpoint for live transcription segments.

    Frontend opens an EventSource as soon as recording starts; segments
    arrive as `data: {json}\\n\\n` lines. A `done` event fires when the
    recording stops (or on a backend error) so the client closes the
    connection cleanly.

    Quirks worth knowing:
      - The route is only useful while a recording is active. Hitting
        it any other time returns 409, not 404, so the frontend can
        distinguish "wrong URL" from "no recording right now".
      - Heartbeat events go out every 5 seconds even when no transcript
        is happening, so proxies / browsers don't kill an idle stream
        and the frontend can detect a broken connection.
    """
    from fastapi.responses import StreamingResponse
    from core.live_transcriber import serialize_segment_sse

    if not svc.recording_svc or not svc.recording_svc.live_transcriber:
        raise HTTPException(status_code=409,
                            detail="No live transcription is active.")
    transcriber = svc.recording_svc.live_transcriber
    if not transcriber.is_running:
        raise HTTPException(status_code=409,
                            detail="No live transcription is active.")

    q = transcriber.subscribe()

    async def event_stream():
        try:
            # SSE comment line on connect — tells curl/browsers the stream
            # opened successfully without committing to a data event yet.
            yield ": connected\n\n"
            while True:
                try:
                    item = await asyncio.to_thread(q.get, True, 5.0)
                except Exception:
                    # Queue.Empty after the 5s timeout — emit a heartbeat
                    # comment so the connection stays warm.
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    # Recording stopped — flush a done event and exit.
                    yield "event: done\ndata: \n\n"
                    return
                yield serialize_segment_sse(item)
        finally:
            transcriber.unsubscribe(q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/recording/copilot/tick")
async def copilot_tick():
    """Live Co-Pilot tick.

    Reads the last ~10 minutes of live-transcript segments from the
    active recording and asks the configured LLM for three short bullet
    lists (clarifying questions / risks / suggested follow-ups). The
    frontend polls this every ~45s while a recording is in progress.

    Returns 409 when no recording is active or live transcription is
    disabled — the panel only makes sense alongside live segments. The
    feature itself is gated by the `live_copilot_enabled` setting so
    users have to opt in; we return 403 when it's off so the frontend
    can quietly hide the panel without retrying.
    """
    s = svc.load_settings()
    if not s.live_copilot_enabled:
        raise HTTPException(
            status_code=403,
            detail="Live Co-Pilot is disabled in Settings.",
        )
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(
            status_code=409,
            detail="No recording is active.",
        )
    transcriber = svc.recording_svc.live_transcriber
    if transcriber is None or not transcriber.is_running:
        raise HTTPException(
            status_code=409,
            detail="Live transcription isn't running for this recording.",
        )
    # The live co-pilot reads from `live_summarizer` so users can route
    # ticks to a cheaper or local model (Ollama, free OpenRouter) while
    # post-meeting summaries stay on the main provider. When no live
    # override is configured this points at the same instance as
    # `summarizer`, so the fallback is automatic.
    coach = svc.live_summarizer or svc.summarizer
    if coach is None:
        raise HTTPException(
            status_code=503,
            detail="Summarizer not ready — check provider/API key in Settings.",
        )

    # Window: feed only the recent conversation, not the whole call. A
    # 10-min window made local-model (Ollama) inference slower and slower
    # as a meeting ran long — eventually every tick exceeded the timeout
    # and the panel went silently blank. ~4.5 min keeps inference roughly
    # constant regardless of call length while still giving the model
    # enough context to coach on.
    segments = transcriber.recent_segments(last_seconds=270.0)
    meeting_name = ""
    sess = svc.recording_svc.current_session
    if sess is not None:
        meeting_name = getattr(sess, "meeting_name", "") or ""

    # Pass any custom coaching context the SA pinned in Settings —
    # per-engagement framing the baked-in prompt can't anticipate.
    # Also pass the prior tick (if any) so the model can build on its
    # last suggestion instead of repeating it.
    custom_context = getattr(svc.settings, "copilot_custom_context", "") or ""
    prior_ticks = (
        list(sess.copilot_ticks)
        if sess is not None and getattr(sess, "copilot_ticks", None)
        else None
    )
    # Resolve mode + meeting-type names to their current prompt text.
    # Both libraries seed defaults at startup so missing entries should
    # only happen if the user deleted everything; the services fall
    # back internally so we don't need to defend here.
    mode_name = getattr(svc.settings, "live_copilot_mode", "") or "SA"
    type_name = getattr(svc.settings, "live_copilot_meeting_type", "") or "General"
    mode_prompt = (
        svc.copilot_mode_svc.get_prompt(mode_name)
        if svc.copilot_mode_svc else ""
    )
    type_prompt = (
        svc.copilot_meeting_type_svc.get_prompt(type_name)
        if svc.copilot_meeting_type_svc else ""
    )
    # Interval-aware timeout: keep it safely under the poll cadence so
    # ticks never overlap/pile up, but give slow local models room.
    # Anthropic is fast (cloud); Ollama/OpenRouter get more headroom.
    interval = max(15, int(getattr(s, "live_copilot_wide_interval_sec", 45) or 45))
    provider = getattr(coach, "_provider", "anthropic")
    base = 20.0 if provider == "anthropic" else 35.0
    tick_timeout = max(8.0, min(base, float(interval) - 5.0))
    result = await coach.coach_tick(
        segments=segments, meeting_name=meeting_name,
        custom_context=custom_context, prior_ticks=prior_ticks,
        mode_name=mode_name, mode_prompt=mode_prompt,
        meeting_type_name=type_name, meeting_type_prompt=type_prompt,
        timeout_s=tick_timeout,
    )
    payload = {
        "clarifying_questions": result.get("clarifying_questions", []),
        "risks": result.get("risks", []),
        "follow_ups": result.get("follow_ups", []),
        # Surface a model failure (timeout / unreachable) so the panel can
        # explain the quiet instead of looking like an empty meeting.
        "error": result.get("error"),
        "error_detail": result.get("error_detail"),
        "segment_count": len(segments),
        "generated_at": datetime.now().isoformat(),
    }
    # Persist every tick into the active session so the bullets the
    # model produced mid-call survive past the recording. The session
    # JSON is written on stop_recording / process_session — appending
    # in-memory here is enough; we don't write the file every 45s.
    # Skip empty payloads (no segments yet, no bullets either) so the
    # saved list isn't padded with no-ops from the first ticks before
    # anyone has spoken.
    if sess is not None and (
        payload["clarifying_questions"]
        or payload["risks"]
        or payload["follow_ups"]
    ):
        sess.copilot_ticks.append(payload)
    return payload


@app.post("/recording/copilot/hot-tick")
async def copilot_hot_tick():
    """Hot variant of /recording/copilot/tick.

    Reads only the last ~90 seconds of transcript (vs ~10 min for the
    wide tick) and uses a tighter prompt biased toward EMPTINESS —
    fires only when something time-sensitive is happening RIGHT NOW.
    Frontend can poll this every ~15 seconds in parallel with the
    wide tick; most calls return empty arrays, the ones that fire
    arrive while the moment is still live.

    Uses the same mode + meeting-type + custom-context composition as
    the wide tick; just swaps the operational rules. Cheaper per
    call (max_tokens=256, timeout=10s) so the 3-4x call rate doesn't
    triple LLM cost. Hot-tick payloads are STILL persisted to
    session.copilot_ticks so the post-meeting summary sees them.
    """
    s = svc.load_settings()
    if not s.live_copilot_enabled:
        raise HTTPException(status_code=403, detail="Live Co-Pilot is disabled in Settings.")
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="No recording is active.")
    transcriber = svc.recording_svc.live_transcriber
    if transcriber is None or not transcriber.is_running:
        raise HTTPException(status_code=409, detail="Live transcription isn't running for this recording.")
    coach = svc.live_summarizer or svc.summarizer
    if coach is None:
        raise HTTPException(
            status_code=503,
            detail="Summarizer not ready — check provider/API key in Settings.")

    segments = transcriber.recent_segments(last_seconds=90.0)
    sess = svc.recording_svc.current_session
    meeting_name = ""
    if sess is not None:
        meeting_name = getattr(sess, "meeting_name", "") or ""

    custom_context = getattr(svc.settings, "copilot_custom_context", "") or ""
    prior_ticks = (
        list(sess.copilot_ticks)
        if sess is not None and getattr(sess, "copilot_ticks", None)
        else None
    )
    mode_name = getattr(svc.settings, "live_copilot_mode", "") or "SA"
    type_name = getattr(svc.settings, "live_copilot_meeting_type", "") or "General"
    mode_prompt = (
        svc.copilot_mode_svc.get_prompt(mode_name)
        if svc.copilot_mode_svc else ""
    )
    type_prompt = (
        svc.copilot_meeting_type_svc.get_prompt(type_name)
        if svc.copilot_meeting_type_svc else ""
    )
    # Interval-aware timeout for the hot poll (default 0 = off; when on,
    # min 5s). Local models get more headroom than cloud.
    hot_interval = max(5, int(getattr(s, "live_copilot_hot_interval_sec", 0) or 15))
    provider = getattr(coach, "_provider", "anthropic")
    base = 10.0 if provider == "anthropic" else 15.0
    hot_timeout = max(6.0, min(base, float(hot_interval) - 3.0))
    result = await coach.coach_tick(
        segments=segments, meeting_name=meeting_name,
        custom_context=custom_context, prior_ticks=prior_ticks,
        mode_name=mode_name, mode_prompt=mode_prompt,
        meeting_type_name=type_name, meeting_type_prompt=type_prompt,
        hot=True, timeout_s=hot_timeout,
    )
    payload = {
        "clarifying_questions": result.get("clarifying_questions", []),
        "risks": result.get("risks", []),
        "follow_ups": result.get("follow_ups", []),
        "error": result.get("error"),
        "error_detail": result.get("error_detail"),
        "segment_count": len(segments),
        "generated_at": datetime.now().isoformat(),
        "hot": True,
    }
    if sess is not None and (
        payload["clarifying_questions"]
        or payload["risks"]
        or payload["follow_ups"]
    ):
        sess.copilot_ticks.append(payload)
    return payload


@app.post("/settings/live-copilot")
async def set_live_copilot_enabled(payload: dict):
    """Lightweight setter for `live_copilot_enabled` only.

    The full POST /settings refuses while a recording is in progress
    because it rebuilds RecordingService — orphaning the active capture
    threads. This endpoint flips just the co-pilot flag, so the user can
    toggle the panel mid-call from the recording bar without stopping.
    Persists to config.env so the choice survives a restart.
    """
    import dataclasses
    enabled = bool(payload.get("enabled", False))
    s = svc.load_settings()
    Settings.save_to_env(
        anthropic_api_key=s.anthropic_api_key,
        hf_token=s.hf_token,
        whisper_model=s.whisper_model,
        max_speakers=s.max_speakers,
        recordings_dir=s.recordings_dir,
        email_to=s.email_to,
        claude_model=s.claude_model,
        notify_minutes_before=s.notify_minutes_before,
        auto_process_after_stop=s.auto_process_after_stop,
        launch_on_startup=s.launch_on_startup,
        auto_follow_up_email=s.auto_follow_up_email,
        retention_enabled=s.retention_enabled,
        retention_processed_days=s.retention_processed_days,
        retention_unprocessed_days=s.retention_unprocessed_days,
        ai_provider=s.ai_provider,
        openai_api_key=s.openai_api_key,
        openai_base_url=s.openai_base_url,
        live_transcription_enabled=s.live_transcription_enabled,
        auto_screenshot_interval_minutes=s.auto_screenshot_interval_minutes,
        silence_warn_min=s.silence_warn_min,
        silence_stop_min=s.silence_stop_min,
        overrun_warn_min=s.overrun_warn_min,
        overrun_stop_min=s.overrun_stop_min,
        hard_cap_hours=s.hard_cap_hours,
        auto_record_enabled=s.auto_record_enabled,
        live_copilot_enabled=enabled,
        live_ai_provider=s.live_ai_provider,
        live_claude_model=s.live_claude_model,
        live_openai_api_key=s.live_openai_api_key,
        live_openai_base_url=s.live_openai_base_url,
        live_anthropic_api_key=s.live_anthropic_api_key,
        live_copilot_mode=s.live_copilot_mode,
        live_copilot_meeting_type=s.live_copilot_meeting_type,
        live_copilot_wide_interval_sec=s.live_copilot_wide_interval_sec,
        live_copilot_hot_interval_sec=s.live_copilot_hot_interval_sec,
        copilot_custom_context=s.copilot_custom_context,
        today_view_enabled=s.today_view_enabled,
        auto_prep_brief_enabled=s.auto_prep_brief_enabled,
        auto_prep_brief_lead_min=s.auto_prep_brief_lead_min,
        cloud_mirror_dir=s.cloud_mirror_dir,
        session_archive_dir=s.session_archive_dir,
        live_vad_enabled=s.live_vad_enabled,
        live_speaker_split_enabled=s.live_speaker_split_enabled,
        diarization_device=s.diarization_device,
        audio_mix_format_lookup_enabled=s.audio_mix_format_lookup_enabled,
        echo_cancellation_enabled=s.echo_cancellation_enabled,
        session_index_enabled=s.session_index_enabled,
        channel_attribution_enabled=s.channel_attribution_enabled,
        calendar_source=s.calendar_source,
    )
    # Update the cached Settings in-place so the change is visible
    # immediately, without going through load_settings() which would
    # rebuild RecordingService and orphan the active capture.
    svc.settings = dataclasses.replace(s, live_copilot_enabled=enabled)
    return {"live_copilot_enabled": enabled}


@app.get("/recording/transcript/history")
async def get_transcript_history():
    """Return every live-transcript segment captured during the current
    recording so the UI's transcript panel can rehydrate after a tab
    switch / page reload. Without this, the panel resets to empty and
    only catches segments published from that moment forward, even
    though the backend still has the full history in memory.

    Returns 409 when no recording is active — same shape as the SSE
    stream endpoint so the client can distinguish "no recording" from
    "empty history."""
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="No recording active.")
    transcriber = svc.recording_svc.live_transcriber
    if transcriber is None or not transcriber.is_running:
        return {"segments": []}
    return {"segments": transcriber.all_segments()}


@app.get("/recording/copilot/history")
async def get_copilot_history():
    """Return all Co-Pilot ticks persisted on the active session so the
    panel can rehydrate after a page reload mid-recording (otherwise the
    bullets vanish until the next 45s tick fires)."""
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        return {"ticks": []}
    sess = svc.recording_svc.current_session
    if sess is None:
        return {"ticks": []}
    return {"ticks": list(sess.copilot_ticks)}


class CoPilotSaveRequest(BaseModel):
    """One co-pilot tick bullet the user wants persisted onto the
    active session as a real artifact (not just a render-cache item).

    kind: "follow_up" -> appended to session.action_items as a `- [ ]`
                         markdown line, so post-processing surfaces it
                         in the Follow-ups tab alongside everything
                         else extracted from the transcript.
          "decision"  -> appended to session.decisions as a `###` heading
                         followed by the bullet text. Post-processing
                         picks it up in the Decisions tab.
          "note"      -> appended to session.notes as a free-form line.
                         Notes never get LLM-extracted; this lets the
                         user pin the suggestion as a personal reminder.
    text: the bullet content itself.
    """
    kind: str
    text: str


@app.post("/recording/copilot/save")
async def save_copilot_suggestion(req: CoPilotSaveRequest):
    """Append a co-pilot tick suggestion as a real artifact on the
    active session. Idempotent: if the exact text is already present
    in the target field, we no-op rather than double-write."""
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="No recording active.")
    sess = svc.recording_svc.current_session
    if sess is None:
        raise HTTPException(status_code=409, detail="No active session.")

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    kind = (req.kind or "").strip().lower()

    def _append_if_missing(current: str, new_block: str) -> str:
        """Append new_block to current iff the trimmed payload isn't
        already present anywhere in current. Keeps a blank line between
        existing content and the new addition for markdown readability."""
        if current and text in current:
            return current
        if not current:
            return new_block
        sep = "\n\n" if not current.endswith("\n\n") else ""
        return f"{current}{sep}{new_block}"

    if kind == "follow_up":
        line = f"- [ ] {text}"
        sess.action_items = _append_if_missing(sess.action_items or "", line)
    elif kind == "decision":
        block = f"### Decision\n{text}"
        sess.decisions = _append_if_missing(sess.decisions or "", block)
    elif kind == "note":
        line = f"• {text}"
        sess.notes = _append_if_missing(sess.notes or "", line)
    else:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of follow_up/decision/note (got {req.kind!r})")

    # We persist in-memory only here — the session JSON gets written
    # on stop_recording / process_session, same as copilot_ticks.append
    # in the tick endpoint. Saving N times mid-call has zero I/O cost.
    return {"ok": True, "kind": kind}


def _stop_recording_sync():
    # In-flight indicator, same mechanism as process_session/process_full
    # — see Services._end_processing. This runs off the event loop (via
    # asyncio.to_thread from every call site), and finalize itself can
    # take 10-30s for a long meeting (closing streams, resampling,
    # mixing loopback, writing the WAV — see the callers' comments), so
    # it's genuine work worth spinning for. It's also where the terminal
    # "Recording saved. Ready to process." / "Error saving audio: …"
    # status strings get set (stop_recording -> self._on_status), which
    # is exactly the class of message that used to leave the spinner
    # stuck on forever — releasing this in `finally` means the spinner
    # stops the instant finalize is actually done, whichever way it ends.
    svc._begin_processing()
    try:
        session = svc.recording_svc.stop_recording()
        if session:
            svc.current_session = session
            svc.session_svc.save(session)
            # No enqueue here: v2.19+ never copies the raw WAV to the
            # network folder (that was the Drive-stall culprit). At stop
            # time there are also no text artifacts yet — those arrive when
            # process/summarize/extract runs, which enqueue their own
            # export as each artifact appears.
        return session
    finally:
        svc._end_processing()


# ── Backend-driven auto-process after stop ──────────────────────────
#
# CRITICAL: auto-processing used to be triggered ONLY by the Record
# view's frontend stop() handler — it called processFull after stopping.
# That meant any stop path that DIDN'T go through that handler finalized
# the WAV but never processed it:
#   - the watchdog auto-stop (silence / overrun / hard cap), which calls
#     _stop_recording_sync directly on a backend timer
#   - the sidebar recording-pill Stop button, which calls /recording/stop
#     without the frontend processFull hook
# An auto-recorded + auto-stopped meeting therefore saved audio with zero
# AI output, even with AUTO_PROCESS_AFTER_STOP=true. Same class of bug as
# the watchdog-was-frontend-polled issue (v2.9.3): a critical step was
# UI-driven, so it silently didn't happen when the UI wasn't the thing
# driving the stop.
#
# Fix: the backend owns auto-process now. Every stop path calls
# _maybe_auto_process(session); it fires the full pipeline as a
# fire-and-forget task when the setting is on. Idempotent per session so
# overlapping triggers (e.g. endpoint + a racing watchdog tick) can't
# double-process. The frontend no longer triggers processing itself.
_auto_processed_sessions: set[str] = set()


# Serializes the transcribe/diarize stage, which mutates shared state on
# the RecordingService (`_session`) and on `svc.current_session`. Without
# this, two overlapping processings — e.g. a backend auto-process and a
# manual re-process of a different session — race on that shared session
# object and CROSS-CONTAMINATE: one session's transcript ends up driving
# another's summary. This actually happened (a Nashville call's summary
# came out as a different meeting's content) once auto-process started
# running jobs in the background. Processing was never concurrency-safe
# and there's one model anyway, so serializing is correct, not just safe.
_PROCESSING_LOCK = asyncio.Lock()


# Retry schedule for backend auto-processing. Most failures are
# transient — Claude 429s, a brief Ollama hiccup, a OneDrive file lock on
# the WAV mid-finalize. Backoff gives them time to clear. Delays in
# seconds; the list length is the attempt count.
_AUTO_PROCESS_RETRY_DELAYS = [30, 120, 300]  # 30s, 2min, 5min


def _stamp_processing_error(session_id: str, message: Optional[str]) -> None:
    """Persist (or clear) a session's processing_error so the Sessions
    list can badge a failed auto-process. message=None clears it."""
    try:
        session = svc.session_svc.load_full(session_id)
        if not session:
            return
        session.processing_error = message
        svc.session_svc.save(session)
    except Exception as e:
        logger.warning(
            f"[auto-process] could not stamp error on {session_id}: {e}")


def _stamp_auto_process_pending(
    session_id: str, marker: Optional[dict],
) -> None:
    """Persist (or clear, marker=None) the crash-resilient auto-process
    marker. It's the ONLY record that survives a mid-processing backend
    death (segfault, watchdog kill, power loss) — no exception handler
    runs in those cases, so retry loops and in-memory sets are useless.
    The startup resume pass reads this to re-queue the session."""
    try:
        session = svc.session_svc.load_full(session_id)
        if not session:
            return
        session.auto_process_pending = marker
        svc.session_svc.save(session)
    except Exception as e:
        logger.warning(
            f"[auto-process] could not stamp pending marker on "
            f"{session_id}: {e}")


# Poison-pill cap: how many CRASH resumes a session gets before we stop
# retrying and surface the failure. A WAV that reliably segfaults
# native transcription must not turn into an infinite crash-loop where
# every backend boot re-queues the job that kills the backend.
_AUTO_PROCESS_MAX_CRASH_RESUMES = 2
# Markers older than this are stale (machine was off for days, user has
# moved on) — surface, don't silently re-run big LLM spends.
_AUTO_PROCESS_RESUME_MAX_AGE_H = 48


def _auto_process_resume_decision(marker: dict, now: datetime) -> str:
    """Pure decision for a found marker: 'resume' | 'give_up' | 'stale'."""
    try:
        resumes = int(marker.get("resumes", 0))
    except (TypeError, ValueError):
        resumes = 0
    if resumes >= _AUTO_PROCESS_MAX_CRASH_RESUMES:
        return "give_up"
    try:
        started = datetime.fromisoformat(str(marker.get("started_at", "")))
        age_h = (now - started).total_seconds() / 3600.0
    except (TypeError, ValueError):
        age_h = 0.0
    if age_h > _AUTO_PROCESS_RESUME_MAX_AGE_H:
        return "stale"
    return "resume"


async def _auto_process_session(session_id: str, template: str,
                                follow_up: bool) -> None:
    """Run the full extraction pipeline for a just-stopped session in the
    background, with retry + backoff. Reuses the manual process_full code
    path. On final failure, stamps session.processing_error so the failure
    is VISIBLE in the Sessions list instead of the session silently
    sitting unprocessed (the exact silent-failure class behind the ASM
    no-AI-output incident).

    'Failure' = process_full raised, or returned ok:False (the critical
    transcribe/diarize stage died). Per-extraction failures (a single 429
    on, say, decisions) leave ok:True with a transcript + partial output —
    not worth retrying the whole pipeline for, and the user can re-run a
    single extraction from the session dialog."""
    req = ProcessFullRequest(
        template=template or "General", follow_up_drafts=follow_up)
    attempts = len(_AUTO_PROCESS_RETRY_DELAYS) + 1
    last_reason = ""
    try:
        for attempt in range(1, attempts + 1):
            try:
                result = await process_full(session_id, req)
                ok = result.get("ok", False) if isinstance(result, dict) else False
                if ok:
                    logger.info(
                        f"[auto-process] session {session_id} complete "
                        f"(attempt {attempt}): "
                        f"{result.get('stages')}")
                    _stamp_processing_error(session_id, None)  # clear any prior
                    _stamp_auto_process_pending(session_id, None)
                    return
                # ok:False → critical stage failed; capture reason + retry.
                stages = result.get("stages", {}) if isinstance(result, dict) else {}
                last_reason = next(
                    (v for v in stages.values()
                     if isinstance(v, str) and v.startswith("failed")),
                    "processing failed")
            except HTTPException as e:
                # Not-configured / not-found: no point retrying.
                logger.warning(
                    f"[auto-process] session {session_id} skipped: {e.detail}")
                _stamp_processing_error(session_id, str(e.detail))
                _stamp_auto_process_pending(session_id, None)
                return
            except Exception as e:
                last_reason = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[auto-process] session {session_id} attempt {attempt} "
                    f"raised: {last_reason}")

            if attempt <= len(_AUTO_PROCESS_RETRY_DELAYS):
                delay = _AUTO_PROCESS_RETRY_DELAYS[attempt - 1]
                logger.info(
                    f"[auto-process] retrying session {session_id} in {delay}s "
                    f"(attempt {attempt + 1}/{attempts})")
                await asyncio.sleep(delay)

        # Exhausted all attempts — make the failure visible.
        msg = f"Auto-processing failed after {attempts} attempts: {last_reason}"
        logger.error(f"[auto-process] session {session_id}: {msg}")
        _stamp_processing_error(session_id, msg)
        _stamp_auto_process_pending(session_id, None)
    finally:
        _auto_processed_sessions.discard(session_id)


def _maybe_auto_process(session) -> None:
    """Kick off backend auto-processing for a freshly-finalized session
    when AUTO_PROCESS_AFTER_STOP is on. Safe to call from every stop path
    — idempotent per session. Must be called on the event loop (uses
    asyncio.create_task), i.e. after the to_thread(_stop_recording_sync)
    returns, not inside it."""
    if session is None:
        return
    s = svc.settings
    if not s or not getattr(s, "auto_process_after_stop", False):
        return
    sid = getattr(session, "session_id", "") or ""
    if not sid or sid in _auto_processed_sessions:
        return
    _auto_processed_sessions.add(sid)
    template = getattr(session, "template", "") or "General"
    follow_up = bool(getattr(s, "auto_follow_up_email", False))
    logger.info(
        f"[auto-process] kicking off for session {sid} "
        f"(template={template}, follow_up={follow_up})")
    # Stamp the crash-resilient marker BEFORE starting: if the backend
    # dies mid-transcription (segfault — no handler runs), the startup
    # resume pass finds this and re-queues instead of leaving the
    # session silently unprocessed after the UI said "Transcribing…".
    _stamp_auto_process_pending(sid, {
        "resumes": 0, "template": template, "follow_up": follow_up,
        "started_at": datetime.now().isoformat(),
    })
    asyncio.create_task(_auto_process_session(sid, template, follow_up))


@app.post("/recording/stop")
async def stop_recording():
    svc.load_settings()
    if not svc.recording_svc or not svc.recording_svc.is_recording:
        raise HTTPException(status_code=409, detail="Not recording")
    try:
        # stop_recording closes streams, re-reads WAV, resamples, mixes
        # loopback audio, and saves the final file. Can take 10-30s for
        # long meetings. Must run off the event loop or polling from the
        # frontend gets blocked and fetch() eventually gives up.
        svc.record_started_at = None  # set immediately so status reflects stopped
        svc.auto_record_subject = None  # recording is ending; clear the auto label
        session = await asyncio.to_thread(_stop_recording_sync)
        if session:
            # Backend-owned auto-process: fires for ANY stop that reaches
            # this endpoint (Record view button, sidebar pill). The
            # frontend no longer triggers processing itself.
            _maybe_auto_process(session)
            return {"session_id": session.session_id, "audio_path": session.audio_path}
        raise HTTPException(status_code=500, detail="Stop returned no session")
    except Exception as e:
        logger.exception("Stop recording failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Sessions ─────────────────────────────────────────────────────────
@app.get("/sessions")
async def list_sessions():
    # Reading every session JSON off disk can be slow with lots of
    # sessions — 50+ sessions × small file reads adds up. Run off-loop.
    def _do():
        svc.load_settings()
        return svc.session_svc.list_sessions()
    return await asyncio.to_thread(_do)


# Declared BEFORE /sessions/{session_id} for the same reason as the
# note below — the dynamic pattern matches the literal segment
# "diagnostics" and would 404 as a missing session.
@app.get("/sessions/diagnostics")
async def sessions_diagnostics():
    """Where the app is looking for sessions, how many files each root
    holds, and what it had to skip.

    Every skip in list_sessions() is otherwise silent — an un-hydrated
    cloud placeholder, truncated JSON, or a non-dict file is logged and
    dropped, which looks exactly like "you have no sessions". Three
    separate field reports have now traced back to that ambiguity, so
    the numbers are queryable instead of buried in a log file.
    """
    svc.load_settings()
    # scan_report() is the single source for primary_dir / visible_in_app
    # (see its docstring, field report 2026-08-10) — this handler is a
    # thin off-loop pass-through, not a second place that computes them.
    return await asyncio.to_thread(svc.session_svc.scan_report)


# IMPORTANT: declare this BEFORE @app.get("/sessions/{session_id}").
# FastAPI matches routes in registration order; the dynamic {session_id}
# pattern matches literal segments too, so a later /sessions/unprocessed
# declaration is dead — every request falls into the catch-all, which
# tries to load session_unprocessed.json and returns 404. Keeps the
# "X sessions awaiting processing" badge + the unprocessed-toast working.
@app.get("/sessions/unprocessed")
async def unprocessed_sessions():
    """
    Return sessions that have audio on disk but no transcript yet. Frontend
    polls this to show an "X sessions awaiting processing" badge + a Windows
    toast notification when the count goes up.
    """
    def _do():
        svc.load_settings()
        results = []
        for s in svc.session_svc.list_sessions():
            if s.get("audio_exists") and not s.get("has_transcript"):
                results.append({
                    "session_id": s["session_id"],
                    "display_name": s["display_name"],
                    "started_at": s.get("started_at"),
                    "duration_s": s.get("duration_s", 0),
                    "client": s.get("client", ""),
                    "project": s.get("project", ""),
                })
        return results
    return await asyncio.to_thread(_do)


# Declared BEFORE @app.get("/sessions/{session_id}") for the same reason
# as /sessions/diagnostics and /sessions/unprocessed above — the dynamic
# {session_id} pattern matches a single literal path segment too, so a
# later declaration here would be dead and 404 as "session not found"
# (field report 2026-08-07: /sessions/diagnostics itself was caught by
# exactly this ~900 lines too late).
@app.get("/sessions/archive-status")
async def get_archive_status():
    """Status of the roaming Session Archive: configured folder, whether
    it's currently reachable, and how many sessions are on each side.
    Polled by the Session Archive card in Settings after load and after
    every Sync now click."""
    svc.load_settings()
    return await asyncio.to_thread(_archive_status_report)


@app.post("/sessions/archive/sync")
async def sync_archive():
    """"Sync now" — queue every local session missing from the Session
    Archive, then return the refreshed status. Idempotent: a fully
    archived library queues 0 and the status comes back unchanged."""
    svc.load_settings()
    queued = await asyncio.to_thread(_reconcile_archive)
    report = await asyncio.to_thread(_archive_status_report)
    return {**report, "queued": queued}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    def _do():
        svc.load_settings()
        return svc.session_svc.load(session_id)
    data = await asyncio.to_thread(_do)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    svc.load_settings()
    svc.session_svc.delete(session_id)
    # Drop the search cache so the in-memory chunk matrix stops
    # answering Q&A with citations pointing at a session that no longer
    # exists.
    #
    # Field report 2026-08-07 (bug 2b): SessionService.delete() now
    # removes the session's embedding sidecar itself (across every root,
    # not just the primary dir — see SessionService.delete's docstring),
    # so by the time delete_session_index() ran here its file-exists
    # check always failed and it silently skipped invalidate(). The
    # sidecar was correctly gone from disk, but the OLD in-memory matrix
    # stayed loaded until something else happened to invalidate it — the
    # search index kept answering with citations that 404'd.
    # invalidate() is unconditional and cheap (it only drops a cache,
    # index_session() rebuilds it lazily on next query), so call it
    # regardless of whether a file happened to still be there to delete.
    if svc.search_svc:
        await asyncio.to_thread(svc.search_svc.invalidate)
    # And the commitments sidecar — same reasoning. Stale commitments
    # pointing at a deleted session would surface in the tracker.
    if svc.commitments_svc:
        await asyncio.to_thread(
            svc.commitments_svc.delete_session_commitments, session_id)
    # Drop any item-status overrides (follow-up "done" / decision status)
    # so a re-imported session with the same ID starts clean.
    if svc.item_status_svc:
        await asyncio.to_thread(
            svc.item_status_svc.delete_for_session, session_id)
    return {"ok": True}


def _resolve_within_scan_roots(raw_path: Optional[str]) -> Optional[Path]:
    """Resolve `raw_path` and confirm it sits under one of the session
    service's configured roots (recordings dir, default user-data dir,
    ARCHIVE_RECORDINGS_DIRS entries, session archive dir).

    Session JSON files are synced between machines through cloud
    storage, so a path pulled out of one (audio_path, a screenshot
    entry) is not fully trusted local input — a tampered or
    cross-machine-stale JSON must never be able to make these endpoints
    serve an arbitrary file from elsewhere on disk. Returns the resolved
    Path on success, None on any failure or containment violation.
    """
    if not raw_path:
        return None
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    for root in svc.session_svc.scan_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


@app.get("/sessions/{session_id}/audio")
async def get_session_audio(session_id: str):
    """Stream the session's WAV file so the UI can play it in an <audio> element."""
    from fastapi.responses import FileResponse
    svc.load_settings()
    data = svc.session_svc.load(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    # A session mid-finalize has no playable file at audio_path YET —
    # that's not the same as "not found". Tell the truth (still
    # finalizing / finalize failed) instead of a bare 404 that reads as
    # an empty/lost recording. See _finalize_status_detail.
    _raise_if_finalizing_dict(data)
    audio_path = data.get("audio_path")
    resolved = _resolve_within_scan_roots(audio_path)
    if resolved is None:
        if audio_path:
            logger.warning(
                "Refusing to serve audio for session %s: %r is outside "
                "the configured recordings/archive roots",
                session_id, audio_path)
        raise HTTPException(status_code=404, detail="Audio file not found")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(str(resolved), media_type="audio/wav", filename=resolved.name)


@app.get("/sessions/{session_id}/screenshots/{index}")
async def get_session_screenshot(session_id: str, index: int):
    """Serve one screenshot the user captured during this session, by
    its position in the session's screenshots list.

    Serving by index (rather than accepting an arbitrary path from the
    client) only stops the CALLER from choosing an arbitrary index —
    it does NOT by itself prevent path traversal, because the path at
    that index still comes from the session's JSON file, and those
    files are synced between machines through cloud storage (so a
    tampered or corrupted JSON is not fully trusted input). Containment
    under a configured recordings/archive root is what actually closes
    the traversal surface; the index just narrows which of the
    session's own entries can be requested.
    """
    from fastapi.responses import FileResponse
    svc.load_settings()
    data = svc.session_svc.load(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    shots = list(data.get("screenshots") or [])
    if index < 0 or index >= len(shots):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    raw_path = shots[index]
    resolved = _resolve_within_scan_roots(raw_path)
    if resolved is None:
        logger.warning(
            "Refusing to serve screenshot for session %s: %r is outside "
            "the configured recordings/archive roots",
            session_id, raw_path)
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if not resolved.is_file():
        raise HTTPException(status_code=404,
                            detail="Screenshot file missing on disk")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(resolved.suffix.lower(), "application/octet-stream")
    return FileResponse(str(resolved), media_type=media, filename=resolved.name)


class SessionPatchRequest(BaseModel):
    display_name: Optional[str] = None
    client: Optional[str] = None
    project: Optional[str] = None
    template: Optional[str] = None
    notes: Optional[str] = None


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, req: SessionPatchRequest):
    """Update editable session metadata (name, tags, template, notes)."""
    svc.load_settings()
    session = svc.session_svc.load_full(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.display_name is not None:
        session.display_name = req.display_name
    if req.client is not None:
        session.client = req.client
        # The user just decided. Drop any auto-tagging provenance —
        # leaving "Auto-tagged from the meeting title" attached to a
        # value the user corrected by hand is worse than no label.
        session.client_source = None
        session.client_source_detail = None
    if req.project is not None:
        session.project = req.project
    if req.template is not None:
        session.template = req.template
    if req.notes is not None:
        session.notes = req.notes
    svc.session_svc.save(session)
    # Tagging an already-processed meeting to a client is the single most
    # common way a session acquires a Designated Folder, and until
    # 2026-08-07 it exported nothing — the artifacts stayed local
    # forever. display_name matters too: it's the filename stem, so a
    # rename means a new set of files is owed.
    if (req.client is not None or req.project is not None
            or req.display_name is not None):
        _auto_export_to_client(session)
    return {"ok": True}


class SpeakerRenameRequest(BaseModel):
    display_name: str
    # When True, also create / update a persistent SpeakerProfile so future
    # sessions auto-recognize this voice. Default True — almost always
    # what the user wants; set False for one-off relabels (typo fixes).
    save_profile: bool = True


async def _auto_extract_commitments(session) -> None:
    """Background helper called by /process and /process_full to mine
    commitments out of the freshly-processed session. Best-effort: if
    the summarizer is None or the model returns junk, we just don't
    have commitments for this session — the tracker shows fewer rows
    until the user manually re-runs extraction.

    The customer_hint field comes from the session's client tag so
    Claude can label commitments as customer-side vs internal more
    confidently. Empty client tag → "the customer organization" as
    a generic placeholder; works less well but doesn't fail."""
    try:
        if not svc.commitments_svc or not svc.summarizer:
            return
        commits = await extract_commitments_from_session(
            svc.summarizer, session,
            customer_hint=session.client or "",
        )
        if commits:
            await asyncio.to_thread(
                svc.commitments_svc.replace_session_commitments,
                session.session_id, commits)
    except Exception as e:
        logger.exception(f"Commitment auto-extract failed: {e}")


async def _auto_identify_and_save_speakers(session) -> int:
    """Ask the LLM who each diarized speaker is — from an explicit
    self-introduction ("Hi, I'm Sarah") or a direct-address hand-off
    ("Sarah, your thoughts?" → the next speaker is Sarah) — then label
    the speaker AND persist their voice fingerprint to the known-speaker
    store so future meetings auto-match without the user typing anything.

    The session's CALENDAR INVITE goes in alongside the transcript. The
    invite is ground truth for who was in the room, so it supplies the
    candidate set: a transcript that only ever says "Jane" resolves to
    the roster's "Jane Doe" instead of stopping at a first name, which
    is what follow_up_recipients.py needs to resolve an address at all.
    Both the ATTENDEES and the ORGANISER feed it; the organiser leads,
    and for an extension-sourced calendar they are the whole roster,
    because that scrape can read the organiser out of Outlook Web's
    grid label but not the attendee list. Additive — a session with
    neither still sends the prompt it always did, byte for byte. See
    core/speaker_roster.py.

    Mirrors the create/link/refine logic of the manual rename endpoint.
    Best-effort: returns the number of speakers named; logs and continues
    on any failure."""
    if not svc.summarizer or not svc.speaker_profile_svc:
        return 0
    if not session.segments or not session.speakers:
        return 0
    try:
        mapping = await svc.summarizer.identify_speakers(
            session.full_transcript(),
            attendees=list(getattr(session, "attendees", None) or []),
            # The seam is now CONNECTED. `Session.organizer` is set at
            # record start from the calendar event on both paths (the
            # Record tab's Use button and `_auto_record_start`), so the
            # organiser leads the roster — see core/speaker_roster.py's
            # `roster_names`. Still read with `getattr`/`or ""` because
            # this function is handed sessions rehydrated from JSON
            # written before the field existed, and the empty string is
            # the documented "no organiser" input.
            #
            # This is the one invite-derived name an extension-sourced
            # calendar can supply at all: that scrape reads Outlook
            # Web's grid aria-label, which carries the organiser and
            # NOT the attendee list, so `attendees` above is [] for a
            # user whose whole calendar comes from the extension. For
            # them the organiser is the entire roster.
            organizer=str(getattr(session, "organizer", "") or ""),
        )
    except Exception as e:
        logger.warning(f"auto speaker-id call failed: {e}")
        return 0
    if not mapping:
        return 0

    import numpy as np
    named = 0
    for speaker_id, raw_name in mapping.items():
        speaker = session.speakers.get(speaker_id)
        if speaker is None:
            continue
        new_name = (raw_name or "").strip()
        if not new_name:
            continue
        # Never override a name the user already confirmed by hand.
        if speaker.match_confirmed and speaker.profile_id:
            continue

        speaker.display_name = new_name
        speaker.match_confirmed = True
        speaker.match_confidence = None

        if not speaker.embedding:
            # Spoke too briefly to fingerprint (<1.5s). Keep the label;
            # we just can't save a reusable voiceprint this time.
            named += 1
            continue
        try:
            emb = np.asarray(speaker.embedding, dtype=np.float32)
            existing = (svc.speaker_profile_svc.get(speaker.profile_id)
                        if speaker.profile_id else None)
            if existing is not None:
                # Already linked (e.g. an auto-match) — trust the
                # explicit name and refine the centroid.
                svc.speaker_profile_svc.rename(existing.profile_id, new_name)
                svc.speaker_profile_svc.confirm_match(
                    existing.profile_id, emb, session.session_id)
            else:
                wanted = new_name.lower()
                same_name = next(
                    (p for p in svc.speaker_profile_svc.list_all()
                     if p.display_name.lower() == wanted), None)
                if same_name is not None:
                    speaker.profile_id = same_name.profile_id
                    svc.speaker_profile_svc.confirm_match(
                        same_name.profile_id, emb, session.session_id)
                else:
                    profile = svc.speaker_profile_svc.create(
                        new_name, emb, session.session_id)
                    speaker.profile_id = profile.profile_id
            named += 1
        except Exception as e:
            logger.warning(
                f"auto-save profile for {speaker_id} ({new_name}) failed: {e}")

    if named:
        logger.info(f"Auto-identified + saved {named} speaker(s) for "
                    f"session {session.session_id}")
    return named


def _serialize_speaker(sp) -> dict:
    """Speaker dict for the API. Mirrors Speaker.to_dict() but drops the
    raw embedding (192 floats add ~3 KB per speaker per response and the
    UI doesn't need them — they're internal to the matching pipeline)."""
    d = sp.to_dict()
    d.pop("embedding", None)
    return d


def _ensure_session_embeddings(session: Session) -> bool:
    """Lazily compute speaker embeddings for an already-processed session
    that doesn't have them yet.

    Sessions processed before the fingerprinting feature shipped have
    `speaker.embedding == []`. Without that centroid we can't create or
    match a SpeakerProfile during rename — the user's "save my name"
    silently no-ops in Settings → Known Speakers.

    This helper backfills embeddings on demand by re-running the ECAPA
    encoder over the saved audio + segment timings (segments are 1:1 with
    diarization turns by speaker, so they're a fine substitute for the
    raw turn list we no longer have at this point in the pipeline).

    Returns True if any new embedding was computed and written. Caller
    should `session_svc.save(session)` after a True return so the
    embeddings persist for subsequent renames.
    """
    if not session.audio_path:
        return False
    if not session.segments:
        return False

    # Skip if every speaker already has an embedding — common path for
    # sessions processed AFTER this feature shipped.
    needs = [
        sp for sp in session.speakers.values()
        if not sp.embedding
    ]
    if not needs:
        return False

    from pathlib import Path as _P
    if not _P(session.audio_path).exists():
        logger.info(f"Cannot fingerprint session {session.session_id}: "
                    f"audio file {session.audio_path} not on disk")
        return False

    try:
        from core.speaker_embeddings import (
            extract_speaker_centroids, is_available,
        )
    except ImportError:
        return False
    if not is_available():
        return False

    # Reconstruct turns_by_speaker from the saved segments. Segments
    # already aggregate diarization + transcription, so the timing is
    # identical to the original turns for our purposes.
    turns_by_speaker: dict[str, list[tuple[float, float]]] = {}
    for seg in session.segments:
        turns_by_speaker.setdefault(seg.speaker_id, []).append(
            (float(seg.start), float(seg.end))
        )
    if not turns_by_speaker:
        return False

    centroids = extract_speaker_centroids(session.audio_path, turns_by_speaker)
    if not centroids:
        return False

    changed = False
    for speaker_id, centroid in centroids.items():
        sp = session.speakers.get(speaker_id)
        if sp is None or sp.embedding:
            continue
        sp.embedding = [float(x) for x in centroid.tolist()]
        changed = True
    if changed:
        logger.info(
            f"Backfilled embeddings for session {session.session_id} "
            f"({sum(1 for s in session.speakers.values() if s.embedding)} "
            f"of {len(session.speakers)} speakers fingerprinted)")
    return changed


@app.patch("/sessions/{session_id}/speakers/{speaker_id}")
async def rename_speaker(session_id: str, speaker_id: str, req: SpeakerRenameRequest):
    """Rename a speaker on a session.

    Side effects when save_profile=True (the default):
      - If the speaker was already linked to a profile, refine + rename it.
      - Else if the new name matches an existing profile (case-insensitive),
        link this speaker to that profile.
      - Else create a fresh profile from this speaker's centroid.

    For sessions processed before the fingerprinting feature shipped (no
    stored embedding), we transparently backfill the embedding now from
    the saved audio file. That makes the rename behave the same way for
    old and new sessions — the user doesn't need to know about the
    history of when fingerprinting got added.

    Response includes `profile_action` so the frontend toast can be
    honest about what happened: "created", "linked", "refined" (existing
    match got better), or "skipped" (no audio / no centroid available).
    """
    svc.load_settings()
    session = svc.session_svc.load_full(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if speaker_id not in session.speakers:
        raise HTTPException(status_code=404, detail="Speaker not on this session")

    new_name = req.display_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="display_name cannot be empty")

    speaker = session.speakers[speaker_id]
    speaker.display_name = new_name
    speaker.match_confirmed = True

    profile_action: str = "skipped"
    skip_reason: Optional[str] = None

    if not req.save_profile:
        skip_reason = "save_profile=false on the request"
    elif not svc.speaker_profile_svc:
        skip_reason = "speaker_profile_svc not initialised"
    else:
        # Lazy backfill for legacy sessions. Slow path the first time
        # (~3s for ECAPA load + per-segment encoding) but only runs once
        # per session — embedding then persists with the session JSON.
        if not speaker.embedding:
            try:
                if await asyncio.to_thread(_ensure_session_embeddings, session):
                    speaker = session.speakers[speaker_id]  # refresh ref
            except Exception as e:
                logger.exception(f"Embedding backfill failed: {e}")

        if not speaker.embedding:
            skip_reason = (
                "no voice fingerprint available for this speaker — they "
                "may have spoken too briefly (<1.5s), or the session "
                "audio may be missing from disk"
            )
        else:
            import numpy as np
            emb = np.asarray(speaker.embedding, dtype=np.float32)

            # Case 1: speaker was already linked (e.g. correcting an
            # auto-match's name). Update the linked profile.
            existing = (svc.speaker_profile_svc.get(speaker.profile_id)
                        if speaker.profile_id else None)
            if existing is not None:
                svc.speaker_profile_svc.rename(existing.profile_id, new_name)
                svc.speaker_profile_svc.confirm_match(
                    existing.profile_id, emb, session.session_id)
                profile_action = "refined"
            else:
                # Case 2: name matches an existing profile case-insensitively
                # → link instead of duplicating.
                wanted = new_name.lower()
                same_name = next(
                    (p for p in svc.speaker_profile_svc.list_all()
                     if p.display_name.lower() == wanted), None)
                if same_name is not None:
                    speaker.profile_id = same_name.profile_id
                    svc.speaker_profile_svc.confirm_match(
                        same_name.profile_id, emb, session.session_id)
                    profile_action = "linked"
                else:
                    # Case 3: brand-new name → create a fresh profile.
                    profile = svc.speaker_profile_svc.create(
                        new_name, emb, session.session_id)
                    speaker.profile_id = profile.profile_id
                    profile_action = "created"
            speaker.match_confidence = None

    svc.session_svc.save(session)
    return {
        "ok": True,
        "speaker": _serialize_speaker(speaker),
        # What we did with the persistent SpeakerProfile store:
        #   "created" — new profile created from this speaker's centroid
        #   "linked"  — linked to an existing profile with the same name
        #   "refined" — already-linked profile got the new centroid blended in
        #   "skipped" — couldn't fingerprint (see profile_skip_reason)
        "profile_action": profile_action,
        "profile_skip_reason": skip_reason,
    }


class SpeakerConfirmRequest(BaseModel):
    # Pass the expected profile_id so a stale confirm from an old UI
    # state can't accidentally confirm a different match if the
    # backend re-ran fingerprinting in the meantime.
    profile_id: str


@app.post("/sessions/{session_id}/speakers/{speaker_id}/confirm")
async def confirm_speaker_match(
    session_id: str, speaker_id: str, req: SpeakerConfirmRequest,
):
    """Accept the auto-match: the speaker really is the suggested
    profile. Refines the profile centroid via the running mean."""
    svc.load_settings()
    session = svc.session_svc.load_full(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if speaker_id not in session.speakers:
        raise HTTPException(status_code=404, detail="Speaker not on this session")
    speaker = session.speakers[speaker_id]
    if speaker.profile_id != req.profile_id:
        raise HTTPException(
            status_code=409,
            detail=("This speaker's profile changed since the page loaded. "
                    "Refresh and try again."),
        )
    if not svc.speaker_profile_svc or not speaker.embedding:
        raise HTTPException(
            status_code=400,
            detail="No fingerprint to confirm. (Was this session processed "
                   "before speaker fingerprinting was enabled?)",
        )
    import numpy as np
    emb = np.asarray(speaker.embedding, dtype=np.float32)
    profile = svc.speaker_profile_svc.confirm_match(
        req.profile_id, emb, session.session_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    speaker.match_confirmed = True
    speaker.match_confidence = None
    svc.session_svc.save(session)
    return {"ok": True, "speaker": _serialize_speaker(speaker)}


@app.post("/sessions/{session_id}/speakers/{speaker_id}/reject")
async def reject_speaker_match(session_id: str, speaker_id: str):
    """Reject the auto-match. Resets the speaker to an unlabeled
    SPEAKER_XX state. Does NOT touch the profile — other sessions of
    the same person stay valid; only THIS speaker is unlinked."""
    svc.load_settings()
    session = svc.session_svc.load_full(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if speaker_id not in session.speakers:
        raise HTTPException(status_code=404, detail="Speaker not on this session")
    speaker = session.speakers[speaker_id]
    speaker.profile_id = None
    speaker.match_confidence = None
    speaker.match_confirmed = False
    speaker.display_name = speaker.speaker_id  # back to "SPEAKER_XX"
    svc.session_svc.save(session)
    return {"ok": True, "speaker": _serialize_speaker(speaker)}


# ── Speaker Profiles (cross-session voice fingerprints) ────────────

def _profile_to_public_dict(p: SpeakerProfile) -> dict:
    """Profile dict for the API. Drops the raw 192-dim embedding for
    response size — the UI just needs name + counts + timestamps."""
    return {
        "profile_id": p.profile_id,
        "display_name": p.display_name,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "confirmation_count": p.confirmation_count,
        "session_count": len(p.sessions_seen_in),
        "sessions_seen_in": list(p.sessions_seen_in),
    }


@app.get("/speaker-profiles")
async def list_speaker_profiles():
    svc.load_settings()
    if not svc.speaker_profile_svc:
        return []
    profiles = await asyncio.to_thread(svc.speaker_profile_svc.list_all)
    return [_profile_to_public_dict(p) for p in profiles]


class ProfileRenameRequest(BaseModel):
    display_name: str


@app.patch("/speaker-profiles/{profile_id}")
async def rename_speaker_profile(profile_id: str, req: ProfileRenameRequest):
    svc.load_settings()
    if not svc.speaker_profile_svc:
        raise HTTPException(status_code=500, detail="Profile service unavailable")
    new_name = req.display_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="display_name cannot be empty")
    profile = await asyncio.to_thread(
        svc.speaker_profile_svc.rename, profile_id, new_name)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_to_public_dict(profile)


@app.delete("/speaker-profiles/{profile_id}")
async def delete_speaker_profile(profile_id: str):
    svc.load_settings()
    if not svc.speaker_profile_svc:
        raise HTTPException(status_code=500, detail="Profile service unavailable")
    ok = await asyncio.to_thread(svc.speaker_profile_svc.delete, profile_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"ok": True}


class ProfileMergeRequest(BaseModel):
    profile_ids: list[str]
    display_name: str  # name for the merged profile


@app.post("/speaker-profiles/merge")
async def merge_speaker_profiles(req: ProfileMergeRequest):
    svc.load_settings()
    if not svc.speaker_profile_svc:
        raise HTTPException(status_code=500, detail="Profile service unavailable")
    if len(req.profile_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="Need at least 2 profile_ids to merge")
    new_name = req.display_name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="display_name cannot be empty")
    merged = await asyncio.to_thread(
        svc.speaker_profile_svc.merge, req.profile_ids, new_name)
    if merged is None:
        raise HTTPException(
            status_code=404,
            detail="One or more profile_ids not found")
    return _profile_to_public_dict(merged)


class BulkTagRequest(BaseModel):
    session_ids: list[str]
    client: Optional[str] = None
    project: Optional[str] = None


@app.post("/tags/apply")
async def bulk_tag_sessions(req: BulkTagRequest):
    """Apply client and/or project tags to multiple sessions at once."""
    svc.load_settings()
    updated = 0
    for sid in req.session_ids:
        session = svc.session_svc.load_full(sid)
        if not session:
            continue
        if req.client is not None:
            session.client = req.client
            # Explicit user decision — see patch_session.
            session.client_source = None
            session.client_source_detail = None
        if req.project is not None:
            session.project = req.project
        svc.session_svc.save(session)
        # Bulk-tagging is how a back-catalogue gets filed under a client.
        # Without this the folder stays empty no matter how many meetings
        # you tag (2026-08-07 field report).
        _auto_export_to_client(session)
        updated += 1
    return {"updated": updated}


class SuggestTaggingRequest(BaseModel):
    client: str
    project: str = ""


@app.post("/clients/suggest-tagging")
async def suggest_tagging(req: SuggestTaggingRequest):
    """
    Use the configured LLM to suggest which untagged sessions likely belong
    to a client. Returns [{session_id, display_name, confidence, reason}].
    """
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(
            status_code=400,
            detail="LLM not configured — set an Anthropic key, or pick an "
                   "OpenAI-compatible provider (Ollama / OpenRouter / etc.) in Settings.")

    all_sessions = svc.session_svc.list_sessions()
    # Candidates: sessions without the target client/project tag
    candidates = [
        s for s in all_sessions
        if s.get("client", "").strip().lower() != req.client.strip().lower()
    ]
    if not candidates:
        return {"suggestions": []}

    # Build lightweight context for Claude
    candidate_lines = []
    for s in candidates[:50]:  # cap to keep prompt small
        display = s.get("display_name", "")
        summary = (s.get("summary") or "")[:180]
        line = f"ID:{s['session_id']} | {display}"
        if summary:
            line += f" | {summary}"
        candidate_lines.append(line)

    prompt = (
        f"I have a client named '{req.client}'"
        + (f" with project '{req.project}'" if req.project else "")
        + ". Below is a list of meeting recordings that are NOT currently "
        "tagged with this client. For each one, decide if it likely belongs "
        "to this client based on its title and/or summary.\n\n"
        "Return ONLY a JSON array, no other text:\n"
        '[{"id": "ABC123", "confidence": 0.0-1.0, "reason": "short why"}]\n\n'
        "Only include items with confidence >= 0.5.\n"
        # The applicable half of the shared rule. The output is a JSON
        # array of ids drawn from a list we supplied, so IDENTIFIERS is
        # the load-bearing clause — an id the model composes rather than
        # copies silently drops a suggestion (it won't match `by_id`
        # below), and a "reason" naming a client or system nobody
        # mentioned is what makes a wrong suggestion look researched.
        + no_invented_precision()
        + "\nMeetings:\n" + "\n".join(candidate_lines)
    )

    try:
        import json
        # Route through the shared summarizer so this endpoint honours the
        # ai_provider setting (anthropic vs OpenAI-compat / Ollama / etc.).
        # The previous code built its own AsyncAnthropic and shipped the
        # request straight to api.anthropic.com regardless of provider —
        # which 404'd for any Ollama / OpenRouter user the moment they
        # clicked AI Suggest, because Anthropic doesn't host their model.
        text = (await svc.summarizer._chat(prompt, max_tokens=2048)).strip()
        # Strip code fences if any
        if text.startswith("```"):
            text = "\n".join(line for line in text.split("\n")
                              if not line.startswith("```"))
        try:
            suggestions = json.loads(text)
        except json.JSONDecodeError:
            # Extract JSON array from text if wrapped in prose
            import re
            m = re.search(r"\[[\s\S]*\]", text)
            if m:
                suggestions = json.loads(m.group())
            else:
                suggestions = []

        # Enrich with display_name
        by_id = {s["session_id"]: s for s in candidates}
        enriched = []
        for item in suggestions:
            sid = item.get("id", "")
            if sid in by_id:
                enriched.append({
                    "session_id": sid,
                    "display_name": by_id[sid].get("display_name", ""),
                    "started_at": by_id[sid].get("started_at", ""),
                    "confidence": item.get("confidence", 0),
                    "reason": item.get("reason", ""),
                })
        enriched.sort(key=lambda x: x["confidence"], reverse=True)
        return {"suggestions": enriched}
    except Exception as e:
        logger.exception("Suggest tagging failed")
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_primary_file(src_dir: Path, name: str) -> Optional[Path]:
    """Find `name` (an exact filename) anywhere under the primary
    recordings dir, recursively. Restricted to the primary root by
    design — see _archive_session's bug 3 note; archiving must never
    read from an archive/extra root.

    When more than one copy exists (shouldn't normally happen within a
    single root, but subfolders make it possible), the newest by mtime
    wins, matching SessionService._resolve_json's rule.
    """
    best: Optional[Path] = None
    best_mtime = -1.0
    try:
        candidates = list(src_dir.rglob(name))
    except OSError:
        return None
    for p in candidates:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = p
    return best


def _archive_session(session_id: str) -> None:
    """Copy a session's JSON + small sidecars into SESSION_ARCHIVE_DIR.

    Runs ONLY on the ExportWorker thread (see _do_export_session), so a
    stalled cloud mount delays other exports and never the app.

    Deliberately excludes the WAV. Audio is what wedged the backend on
    2026-07-09 and is not what another machine needs in order to show,
    search, or answer questions about a meeting — the JSON carries the
    transcript, summary, action items, decisions and requirements.

    Skips the copy when the destination already holds a same-or-newer
    file, so two machines syncing the same folder don't fight.

    Only ever copies the CURRENT embeddings sidecar format
    (.embeddings.npz / .embeddings.json), never a legacy
    ".embeddings.pkl" — this archive dir is exactly the cross-machine
    cloud-sync path that made unpickling those files a real remote code
    execution risk in the first place (see services/search_service.py),
    so a leftover legacy pickle is deliberately NOT propagated to other
    machines through it.
    """
    archive = _session_archive_dir()
    if not archive:
        return
    dest = Path(archive).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    src_dir = Path(svc.settings.recordings_dir)
    names = [f"session_{session_id}.json"]
    for suffix in (".embeddings.npz", ".embeddings.json",
                   ".commitments.json", ".item_status.json"):
        names.append(f"session_{session_id}{suffix}")
    copied = 0
    for name in names:
        # Bug 3 (field report 2026-08-07): this used to be a flat
        # `src_dir / name` lookup, so a session filed into a subfolder
        # of the primary dir (list_sessions() has recursed into
        # subfolders since this same field report) was never found and
        # so never reached the roaming archive. Search recursively, but
        # ONLY within the primary dir — never an archive/extra root,
        # since that would copy an archived file onto itself (or worse,
        # copy a stale archive copy over a fresher local one).
        src = _resolve_primary_file(src_dir, name)
        if src is None or not src.is_file():
            continue
        dst = dest / name
        try:
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue
        except OSError:
            pass  # can't stat the destination — just attempt the copy
        shutil.copy2(src, dst)
        copied += 1
    if copied:
        logger.info(
            f"Archived session {session_id} ({copied} file(s)) to {dest}")


def _archive_recordings_dirs(primary: str) -> list:
    """Read-only roots to scan for sessions besides the active one.

    Field report 2026-08-07: a user with hundreds of recordings saw ~15.
    Nothing was lost — session discovery was a single non-recursive glob
    over whatever RECORDINGS_DIR currently points at, so every session
    recorded while it pointed elsewhere became permanently invisible.

    Two sources, both read-only:

      1. The BUILT-IN DEFAULT (<USER_DATA_DIR>/recordings), included
         automatically whenever the user has overridden RECORDINGS_DIR.
         This is the common case — the app defaulted somewhere, the user
         later pointed it at their own folder, and the original library
         silently dropped out of view. Costs nothing when the directory
         doesn't exist.
      2. ARCHIVE_RECORDINGS_DIRS in config.env — a semicolon-separated
         list, for anyone whose history is spread wider than that.
    """
    out: list = []
    try:
        from config.settings import USER_DATA_DIR
        default_dir = Path(USER_DATA_DIR) / "recordings"
        if str(default_dir).lower() != str(Path(primary)).lower():
            out.append(str(default_dir))
    except Exception as e:
        logger.warning(f"Could not resolve default recordings dir: {e}")
    raw = os.getenv("ARCHIVE_RECORDINGS_DIRS", "") or ""
    out.extend(part.strip() for part in raw.split(";") if part.strip())
    # The roaming archive is both written (by the background worker) and
    # read here, so a session processed on one machine shows up on every
    # other machine pointed at the same synced folder.
    archive = _session_archive_dir()
    if archive:
        out.append(archive)
    return out


def _session_archive_dir() -> str:
    """Cloud-synced folder holding session JSONs so a library roams
    across machines.

    THE THREE-LOCATION RULE (2026-08-07). A user with a Windows box and a
    Mac cannot share a library if each only reads its own local disk, and
    cannot point RECORDINGS_DIR at the cloud either — that's the
    2026-07-09 Drive-stall incident, where a WAV copy on the record path
    wedged the backend and cost recordings. So the jobs are split:

      RECORDINGS_DIR      local, authoritative, holds the audio. The
                          record/process path writes here and ONLY here.
      SESSION_ARCHIVE_DIR synced. Session JSONs + their small sidecars,
                          copied by the BACKGROUND worker with retries.
                          Never audio, never on the record path.
      cloud_mirror_dir /  derived .txt artifacts for humans to read.
      Designated Folders

    Writing a few-KB JSON off the hot path is the same shape as the
    Designated Folder export that has run safely since v2.19; the thing
    that stalled was a multi-hundred-MB WAV copied synchronously.

    SOURCE OF THE VALUE (2026-08-07). This used to be a bare
    os.getenv() read — there was no Settings UI for it at all, so a user
    could only turn it on by hand-editing config.env or setting a
    process env var, and one did exactly that and then couldn't find it
    anywhere in the app. It's a first-class Settings field
    (session_archive_dir) now, editable from the Session Archive card in
    Settings. The env var is kept as a fallback ONLY so anyone already
    setting SESSION_ARCHIVE_DIR by hand keeps working unchanged — the
    Settings value always wins when both are present.
    """
    if svc.settings is not None:
        configured = (getattr(svc.settings, "session_archive_dir", "") or "").strip()
        if configured:
            return configured
    return (os.getenv("SESSION_ARCHIVE_DIR", "") or "").strip()


def _client_export_folder(session: Session) -> Optional[str]:
    """Resolve the export target for this session: the client's explicit
    Designated Folder, else <cloud_mirror_dir>/<client> (or /Unfiled)
    when the global mirror root is configured, else None."""
    client = (getattr(session, "client", "") or "").strip()
    explicit = ""
    if client and svc.client_cfg_svc:
        cfg = svc.client_cfg_svc.get(client)
        if cfg and cfg.export_folder:
            explicit = cfg.export_folder
    mirror_root = getattr(svc.settings, "cloud_mirror_dir", "") or ""
    return resolve_export_folder(explicit, client, mirror_root)


def _do_export_session(session_id: str, _copy_audio: bool) -> None:
    """Worker-side export body — runs ONLY on the ExportWorker thread.

    v2.19+ rule: **only derived text artifacts** (transcript, summary,
    action items, decisions, requirements) are copied to the network /
    Designated Folder. The raw session WAV and session JSON always stay
    on local disk. The audio was the artifact that stalled Google Drive
    mid-copy in every incident, and it's not what teams read from the
    shared folder anyway — a .txt transcript is. The old copy_audio
    flag on the enqueue signature is preserved but IGNORED so the old
    call sites don't have to change.

    Re-loads the session fresh from disk (the enqueuer's in-memory
    object may be stale by the time this runs) and RAISES on failure so
    the worker's retry schedule fires — a cloud-stream mount that
    stalls once usually succeeds on the 30s retry.
    """
    session = svc.session_svc.load_full(session_id)
    if not session:
        return
    # Roaming archive first, and independent of any client tag — an
    # untagged meeting still needs to reach your other machine. Raises on
    # failure so the worker's 5s/30s/120s retry schedule covers a sync
    # client that's momentarily holding the file.
    _archive_session(session_id)
    folder = _client_export_folder(session)
    if not folder:
        return
    # If the session has no exportable text yet (e.g. just-stopped, not
    # processed) there's literally nothing to write to the network
    # folder — bail out cheaply instead of touching the mount.
    if not any((session.segments, session.summary, session.action_items,
                session.decisions, session.requirements)):
        return
    # Dedicated ExportService for the worker thread. export_all swaps
    # its own `_dir` for the duration of the copy; sharing
    # svc.export_svc would let that swap race request-path exports and
    # land one session's artifacts in another client's folder.
    global _WORKER_EXPORT_SVC
    if _WORKER_EXPORT_SVC is None:
        _WORKER_EXPORT_SVC = ExportService(svc.settings.recordings_dir)
    _WORKER_EXPORT_SVC.export_all(
        session, target_dir=folder, copy_audio=False, strict=False)
    logger.info(f"Auto-exported session {session_id} to {folder}")
    # Deliberately NOT writing exported_audio_paths back to the session
    # JSON here: this worker holds a snapshot loaded BEFORE the copy, so
    # saving it would clobber transcript/summary the main thread wrote
    # while the copy ran (a real data-loss race). Retention instead
    # discovers these mirror copies via _client_export_dirs (which now
    # enumerates the mirror subfolders) and sweeps them by
    # recorder-generated filename.


# Worker-owned ExportService (see _do_export_session) — built lazily on
# first export so it picks up the configured recordings_dir.
_WORKER_EXPORT_SVC: Optional[ExportService] = None

# Single background thread for all designated-folder / cloud-mirror
# copies. THE INVARIANT (2026-07-09 Drive-stall incident): the record →
# finalize → process path never writes to a network folder — a stalled
# cloud mount can no longer freeze the backend, trip the Tauri
# watchdog, and kill an in-flight recording.
_EXPORT_WORKER = ExportWorker(_do_export_session)


def _auto_export_to_client(session: Session, copy_audio: bool = False) -> None:
    """Queue this session's export. Non-blocking, never raises — call
    sites keep the same signature they had when this ran inline."""
    if session is None:
        return
    _EXPORT_WORKER.enqueue(session.session_id, copy_audio=copy_audio)


def _export_folder_for_client(client: str) -> Optional[str]:
    """Designated Folder for a client name (no Session needed).

    Same resolution order as _client_export_folder: explicit folder,
    else <cloud_mirror_dir>/<client>, else None.
    """
    client = (client or "").strip()
    explicit = ""
    if client and svc.client_cfg_svc:
        cfg = svc.client_cfg_svc.get(client)
        if cfg and cfg.export_folder:
            explicit = cfg.export_folder
    mirror_root = getattr(svc.settings, "cloud_mirror_dir", "") or ""
    return resolve_export_folder(explicit, client, mirror_root)


def _client_export_report(client: str) -> dict:
    """Compare what every session tagged to `client` owes its Designated
    Folder against what is actually on disk.

    Pure inspection — stats files, never copies. Returns per-session
    detail so the UI can name the meetings that haven't mirrored instead
    of only showing a count.
    """
    folder = _export_folder_for_client(client)
    summaries = svc.session_svc.list_sessions()
    mine = export_reconcile.sessions_for_client(summaries, client)
    folder_present = bool(folder) and Path(folder).expanduser().is_dir()

    pending: list[dict] = []
    complete = 0
    for row in mine:
        expected = export_reconcile.expected_artifacts(row)
        if not expected:
            # Nothing processed yet — owes the folder nothing.
            continue
        missing = export_reconcile.missing_artifacts(row, folder)
        if missing:
            pending.append({
                "session_id": row.get("session_id", ""),
                "display_name": row.get("display_name", ""),
                "missing": missing,
            })
        else:
            complete += 1
    return {
        "client": client,
        "folder": folder or "",
        "folder_present": folder_present,
        "total": len(mine),
        "exportable": complete + len(pending),
        "mirrored": complete,
        "pending": pending,
    }


def _reconcile_client(client: str) -> dict:
    """Enqueue an export for every session of `client` missing artifacts.

    THE CORRECTNESS GUARANTEE (2026-08-07). Enqueue-on-mutation is the
    fast path; this is what makes a missed trigger a delay rather than a
    permanent hole. Idempotent — a fully-mirrored client queues nothing,
    so it is safe to call on folder-set, on rename, on startup, and from
    the Sync now button.
    """
    report = _client_export_report(client)
    if not report["folder"]:
        return {**report, "queued": 0}
    for row in report["pending"]:
        _EXPORT_WORKER.enqueue(row["session_id"], copy_audio=False)
    queued = len(report["pending"])
    if queued:
        logger.info(
            f"Reconcile '{client}': queued {queued} session(s) for export "
            f"to {report['folder']}")
    return {**report, "queued": queued}


def _reset_shared_state_services() -> None:
    """Rebuild ClientConfigService / TemplateService against the current
    recordings dir after shared_state_sync.pull() has overwritten
    client_configs.json and/or summary_templates.json on disk.

    Neither service caches file contents in memory — every get()/get_all()
    call re-reads the JSON from disk fresh (see ClientConfigService._read_all
    / TemplateService._read_all_locked) — so this reconstruction doesn't
    change what the NEXT call returns; it exists to keep the invalidation
    explicit at this call site rather than leaning on that as an unstated
    implementation detail, matching the intent of the recordings_dir-change
    path in save_settings() (which re-migrates and reconstructs both
    services on a folder change).
    Deliberately narrower than that path's "Force reload"
    (`svc.settings = None; svc.load_settings()`), which also tears down
    SessionService / RecordingService / the export worker's view of
    settings — right for a user-initiated Settings save, wrong here: this
    runs from a background sweep (and from POST /sessions/archive/sync)
    and must never risk disturbing an in-flight recording just because a
    config file synced in from another machine (field report 2026-08-07).
    """
    if svc.settings is None:
        return
    recordings_dir = Path(svc.settings.recordings_dir)
    svc.client_cfg_svc = ClientConfigService(recordings_dir)
    svc.template_svc = TemplateService(recordings_dir)
    svc.owner_alias_store = OwnerAliasStore(recordings_dir)
    # engagement_svc holds a reference to client_cfg_svc (and re-reads
    # through it on every call rather than caching), but rebuild it too
    # so it never holds a reference to the OLD ClientConfigService
    # instance — cheap, and removes any doubt.
    def _on_register_written_rebuilt(client_key: str,
                                     project_key: str) -> None:
        if svc.portal_push_svc and svc.portal_push_svc.should_push(
                client_key, project_key):
            svc.portal_push_worker.enqueue(client_key, project_key)

    svc.engagement_svc = EngagementService(
        svc.session_svc, svc.client_cfg_svc, svc.commitments_svc,
        on_register_written=_on_register_written_rebuilt)


# Last outcome of shared_state_sync.sanitize_local_paths(), captured so
# GET /sessions/archive-status can show it without re-running (and
# re-mutating!) the sanitize pass on every poll — status() elsewhere in
# this module is deliberately pure/read-only, so the one operation that
# actually rewrites client_configs.json (healing foreign G:\ paths,
# field report 2026-08-07) has to remember its own result rather than
# being recomputed from a "pure inspection" call. Reset to [] on every
# _reconcile_archive() run, including runs that find nothing to clear,
# so a healed machine's readout clears itself on the next sweep instead
# of showing a stale warning forever.
_LAST_SANITIZE_CLEARED: list = []


def _reconcile_archive() -> int:
    """Roam client_configs.json / summary_templates.json, then queue
    every local session that isn't in the roaming archive yet.

    SANITIZE FIRST (field report 2026-08-07, second incident same day):
    before either side of the roam runs, clear any per-machine folder
    path in the LOCAL client_configs.json that is structurally foreign
    to this platform (e.g. `G:\\My Drive\\Zorg` on macOS) — damage done by
    the pre-fix whole-file copy (or any other route) that would
    otherwise keep getting queued against every reconcile sweep. See
    services/shared_state_sync.py:sanitize_local_paths for the
    conservative detection rule.

    PULL SECOND: a newer client list or template set sitting in the
    archive must land locally BEFORE anything else in this sweep (or a
    request racing it) reads the client roster — otherwise a client that
    was added, or a template edited, only on the OTHER machine keeps
    looking absent for one more cycle even after the data is sitting
    right there in the archive folder. PUSH runs last so a local edit
    made since the last sweep still goes out promptly — pushed with
    export_folder/knowledge_folder blanked in the archive copy, since
    those are per-machine (field report 2026-08-07: that's the whole fix
    — client IDENTITY roams, per-machine PATHS never do). See
    services/shared_state_sync.py for why client_configs.json /
    summary_templates.json never rode along with the session-JSON
    archive copy in the first place, and the JSON/dict safety gate that
    keeps a half-synced archive file from clobbering a good local one.

    Same convergence rule as the Designated Folder reconciler: the
    enqueue-on-process path is the fast lane, this is what makes a
    missed one a delay instead of a permanent gap. Idempotent — a fully
    archived library queues nothing.

    The comparison itself lives in services/archive_reconcile.py
    (pending_session_ids), shared with GET /sessions/archive-status so
    the "P pending" the status endpoint reports and what Sync now
    actually queues can never drift apart (field report 2026-08-07,
    bug 3 — see that module's docstring for the original bug).
    """
    global _LAST_SANITIZE_CLEARED
    archive = _session_archive_dir()
    if not archive:
        return 0
    src_dir = Path(svc.settings.recordings_dir)

    cleared = shared_state_sync.sanitize_local_paths(str(src_dir))
    _LAST_SANITIZE_CLEARED = cleared
    if cleared:
        logger.info(
            f"Shared state sanitize cleared {len(cleared)} foreign "
            f"folder path(s): {cleared}")
        _reset_shared_state_services()

    pulled = shared_state_sync.pull(str(src_dir), archive)
    if pulled:
        logger.info(f"Shared state pulled from archive: {pulled}")
        _reset_shared_state_services()
    pushed = shared_state_sync.push(str(src_dir), archive)
    if pushed:
        logger.info(f"Shared state pushed to archive: {pushed}")

    pending = archive_reconcile.pending_session_ids(src_dir, archive)
    for sid in pending:
        _EXPORT_WORKER.enqueue(sid, copy_audio=False)
    queued = len(pending)
    if queued:
        logger.info(f"Archive reconcile queued {queued} session(s) -> {archive}")
    return queued


def _archive_status_report() -> dict:
    """Snapshot of the roaming Session Archive: what's configured, how
    many session JSONs are on each side, and how many still owe a copy.

    Pure inspection — stats files, never copies — so it's cheap enough
    to call on every poll from the Settings UI. Shares its convergence
    rule with _reconcile_archive() via services/archive_reconcile.py;
    see that module's docstring for why a disconnected/unreachable
    archive folder must read as "everything pending", never "all
    present".
    """
    archive = _session_archive_dir()
    src_dir = Path(svc.settings.recordings_dir)
    folder_present = bool(archive) and Path(archive).expanduser().is_dir()
    sessions_local = len(archive_reconcile.local_session_ids(src_dir))
    sessions_in_archive = len(archive_reconcile.archived_session_ids(archive))
    pending = len(archive_reconcile.pending_session_ids(src_dir, archive))

    shared_state = shared_state_sync.status(str(src_dir), archive)
    # Surface the last sanitize_local_paths() outcome (field report
    # 2026-08-07: the Mac was re-queuing exports against `G:\My Drive\...`
    # paths roamed in from Windows) on the client_configs.json row so the
    # Settings card can tell the user their config was healed, instead of
    # the fix happening silently. Additive key on an existing row — the
    # per-file dict shape everything else reads is untouched when nothing
    # was cleared (empty list, same as "no reason" elsewhere in this dict).
    if CLIENT_CONFIGS_FILE in shared_state:
        shared_state[CLIENT_CONFIGS_FILE]["sanitized_cleared"] = list(
            _LAST_SANITIZE_CLEARED)

    return {
        "folder": archive or "",
        "folder_present": folder_present,
        "sessions_in_archive": sessions_in_archive,
        "sessions_local": sessions_local,
        "pending": pending,
        # client_configs.json / summary_templates.json roaming status
        # (field report 2026-08-07) — additive key, existing keys above
        # are untouched so the frontend's current reads keep working.
        "shared_state": shared_state,
    }


def _reconcile_all_clients() -> None:
    """Background sweep across every configured client. Runs once at
    startup so a folder set on another device, an interrupted export, or
    a trigger that predates this machinery heals without the user having
    to notice and click anything."""
    try:
        if not svc.client_cfg_svc:
            return
        names = [
            (cfg.display_name or key)
            for key, cfg in svc.client_cfg_svc.get_all().items()
        ]
    except Exception as e:
        logger.warning(f"Startup reconcile skipped (config unreadable): {e}")
        return
    total = 0
    for name in names:
        try:
            total += _reconcile_client(name).get("queued", 0)
        except Exception as e:
            logger.warning(f"Reconcile of '{name}' failed: {e}")
    if total:
        logger.info(f"Startup reconcile queued {total} session export(s)")


# ── Client configs (per-client designated export folder) ──────────────
@app.get("/clients/config")
async def get_client_configs():
    svc.load_settings()
    def _do():
        return {
            name: {
                "export_folder": cfg.export_folder,
                "knowledge_folder": cfg.knowledge_folder,
                "display_name": cfg.display_name or name,
            }
            for name, cfg in svc.client_cfg_svc.get_all().items()
        }
    try:
        return await asyncio.to_thread(_do)
    except CloudFileNotReadyError as e:
        # The synced client_configs.json hasn't downloaded to this
        # device yet. 503 + the actionable message beats silently
        # returning {} (which read as "you have no clients").
        raise HTTPException(status_code=503, detail=str(e))


class ClientConfigDTO(BaseModel):
    # Optional + None-default (not "") so the endpoint can tell "field
    # omitted, leave it alone" apart from "field explicitly cleared".
    # LMA gap analysis 2026-08-07: the Designated Folder card and the
    # new Knowledge Folder card each PUT only the one field they own —
    # if this endpoint rebuilt ClientConfig from just the payload (the
    # pre-knowledge_folder behavior), saving a Designated Folder would
    # silently wipe out whatever Knowledge Folder was already saved and
    # vice versa.
    export_folder: Optional[str] = None
    knowledge_folder: Optional[str] = None


@app.put("/clients/config/{client_name}")
async def put_client_config(client_name: str, payload: ClientConfigDTO):
    svc.load_settings()
    existing = svc.client_cfg_svc.get(client_name) or ClientConfig()

    folder = existing.export_folder
    if payload.export_folder is not None:
        folder = payload.export_folder.strip()
        # Validate a non-empty folder path so the user catches typos up
        # front rather than at the next recording when nothing shows up
        # there. A Designated Folder is an EXPORT target, so creating it
        # is correct — there's nothing there yet to lose.
        if folder:
            p = Path(folder).expanduser()
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Can't create or access '{folder}': {e}",
                )
            folder = str(p)

    knowledge_folder = existing.knowledge_folder
    if payload.knowledge_folder is not None:
        knowledge_folder = payload.knowledge_folder.strip()
        # Unlike export_folder, a Knowledge Folder is the user's
        # EXISTING documents (SOWs, discovery notes, requirements
        # docs) — mkdir-ing it on a typo would silently create an empty
        # folder and mask the mistake instead of surfacing it. 400 with
        # a clear message so the user fixes the path instead.
        if knowledge_folder:
            p = Path(knowledge_folder).expanduser()
            if not p.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Knowledge folder '{knowledge_folder}' doesn't "
                        f"exist or isn't a directory."
                    ),
                )
            knowledge_folder = str(p)

    def _do():
        svc.client_cfg_svc.set(
            client_name,
            ClientConfig(
                export_folder=folder,
                knowledge_folder=knowledge_folder,
                display_name=existing.display_name,
            ),
        )
        # Backfill. Setting a Designated Folder used to apply only to
        # meetings recorded AFTER the change, so a user who filed a
        # back-catalogue found the folder holding a fraction of it
        # (2026-08-07). Reconciling here mirrors everything already
        # tagged to this client. Runs on the worker queue, so a slow or
        # offline Drive mount never blocks the response.
        return _reconcile_client(client_name)
    report = await asyncio.to_thread(_do)
    return {
        "ok": True,
        "export_folder": folder,
        "knowledge_folder": knowledge_folder,
        "queued": report.get("queued", 0),
        "mirrored": report.get("mirrored", 0),
        "exportable": report.get("exportable", 0),
    }


@app.get("/clients/{client_name}/export-status")
async def get_client_export_status(client_name: str):
    """How much of this client's library has reached its Designated
    Folder. Powers the "N of M mirrored" readout so a silent export
    failure can't hide the way it did before 2026-08-07."""
    svc.load_settings()
    try:
        return await asyncio.to_thread(_client_export_report, client_name)
    except CloudFileNotReadyError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/clients/{client_name}/reconcile")
async def reconcile_client_exports(client_name: str):
    """Queue an export for every meeting of this client whose artifacts
    are missing from the Designated Folder. Idempotent — the Sync now
    button. Safe to hammer; a fully-mirrored client queues nothing."""
    svc.load_settings()
    try:
        return await asyncio.to_thread(_reconcile_client, client_name)
    except CloudFileNotReadyError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _knowledge_embed_fn():
    """Resolve the shared local embedding path (core.embeddings), the
    exact one SearchService/session indexing already uses, so document
    chunks and transcript chunks land in the same vector space.

    Returns None when sentence-transformers isn't installed — the
    caller turns that into a 503 rather than a stack trace, matching
    how /search/semantic and /sessions/{id}/embed degrade (LMA gap
    analysis 2026-08-07: a document reindex is exactly the kind of
    "ran once, silently did nothing" failure mode those field reports
    are about, so this is loud instead of silent).
    """
    from core.embeddings import embed_texts, is_available
    if not is_available():
        return None
    return embed_texts


@app.post("/clients/{client_name}/knowledge/reindex")
async def reindex_client_knowledge(client_name: str):
    """Extract, chunk, and embed every supported document in this
    client's Knowledge Folder, then drop stale entries for documents
    that no longer exist. Invalidates the search cache afterward so the
    new chunks are searchable / citable in Q&A immediately."""
    svc.load_settings()
    cfg = svc.client_cfg_svc.get(client_name) if svc.client_cfg_svc else None
    folder = (cfg.knowledge_folder if cfg else "") or ""
    if not folder:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No knowledge folder configured for '{client_name}'. "
                "Set one from the client's Knowledge Folder card first."
            ),
        )
    if not Path(folder).expanduser().is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Knowledge folder '{folder}' doesn't exist or isn't a directory.",
        )

    embed_fn = _knowledge_embed_fn()
    if embed_fn is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Semantic embedding isn't available (sentence-transformers "
                "not installed) — install it and restart to index documents."
            ),
        )

    def _do():
        recordings_dir = svc.session_svc.recordings_dir
        report = document_service.index_folder(
            folder, client_name, embed_fn, recordings_dir)
        removed = document_service.remove_stale(
            folder, client_name, recordings_dir)
        report["removed_stale"] = removed
        return report

    try:
        report = await asyncio.to_thread(_do)
    except Exception as e:
        logger.exception(f"Knowledge reindex failed for '{client_name}'")
        raise HTTPException(status_code=500, detail=str(e))

    if svc.search_svc:
        await asyncio.to_thread(svc.search_svc.invalidate)
    return report


@app.get("/clients/{client_name}/knowledge")
async def get_client_knowledge_status(client_name: str):
    """Status without reindexing — configured folder, whether it's
    currently reachable, and how much of this client's document index
    is on disk. Cheap: reads doc_index JSON sidecars, no embedding, and
    never touches a legacy .pkl (see services/document_service.py) —
    those are ignored here exactly like anywhere else in the app."""
    svc.load_settings()
    cfg = svc.client_cfg_svc.get(client_name) if svc.client_cfg_svc else None
    folder = (cfg.knowledge_folder if cfg else "") or ""

    def _do():
        indexed_docs = 0
        total_chunks = 0
        if svc.session_svc:
            doc_dir = Path(svc.session_svc.recordings_dir) / "doc_index"
            if doc_dir.is_dir():
                import json as _json
                for f in doc_dir.glob("doc_*.json"):
                    try:
                        payload = _json.loads(f.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if (payload.get("client") or "") != client_name:
                        continue
                    indexed_docs += 1
                    total_chunks += len(payload.get("chunks") or [])
        return {
            "client": client_name,
            "knowledge_folder": folder,
            "folder_present": bool(folder) and Path(folder).expanduser().is_dir(),
            "indexed_documents": indexed_docs,
            "total_chunks": total_chunks,
        }

    return await asyncio.to_thread(_do)


@app.post("/sessions/{session_id}/export")
async def export_session(session_id: str):
    """
    Manually export a session's artifacts. Routes to the session's
    client's designated folder if one is set, otherwise falls back to
    the recordings dir.
    """
    svc.load_settings()
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    target = _client_export_folder(session)

    def _do():
        # Text artifacts only — small (KB) and safe to write
        # synchronously. v2.19+: the raw WAV never goes to a network
        # folder from any path (the Drive-stall rule).
        return svc.export_svc.export_all(
            session, target_dir=target, copy_audio=False)

    paths = await asyncio.to_thread(_do)
    return {"ok": True, "target_dir": target or svc.settings.recordings_dir,
            "paths": paths}


# ── System / filesystem helpers ──────────────────────────────────────
class OpenFolderRequest(BaseModel):
    # Keys: "recordings" opens the configured recordings dir, "client"
    # opens the designated folder for a client, or explicit "path".
    kind: str = "recordings"
    client: Optional[str] = None
    path: Optional[str] = None


def _resolve_within_app_data_dir(raw_path: str) -> Optional[Path]:
    """Resolve `raw_path` if it sits under the app's own data directory.

    Diagnostics zips, logs and config live here — everything the app
    writes for itself, as opposed to the user's recordings. The v2.70.0
    containment allowed only the session scan roots and client export
    folders, so "Show" beside a freshly written diagnostics zip was
    refused by the app's own guard (field report, 2026-08-27). A
    directory the app writes into is a directory it may reveal.

    Read through the module global rather than captured at import so a
    test (and a future relocation) can point it elsewhere.
    """
    try:
        resolved = Path(raw_path).expanduser().resolve()
        root = Path(USER_DATA_DIR).expanduser().resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
        return resolved
    except ValueError:
        return None


def _resolve_within_client_export_folders(raw_path: str) -> Optional[Path]:
    """Resolve `raw_path` if it sits under a configured client export
    folder. Client folders live outside the scan roots (they're
    per-client Drive folders), but they are still app-configured
    destinations — a path inside one is a path the app itself wrote to.
    Returns None on any failure or non-containment."""
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except OSError:
        return None
    try:
        configs = (svc.client_cfg_svc.get_all() or {}).values() \
            if svc.client_cfg_svc else []
    except Exception:  # noqa: BLE001 — an unreadable config store must
        return None    # fail closed, not open
    for cfg in configs:
        folder = getattr(cfg, "export_folder", "") or ""
        if not folder:
            continue
        try:
            resolved.relative_to(Path(folder).expanduser().resolve())
            return resolved
        except (ValueError, OSError):
            continue
    return None


@app.post("/system/open-folder")
async def open_folder(req: OpenFolderRequest):
    """
    Opens a folder in Windows Explorer. Uses os.startfile (ShellExecute
    under the hood) so there's no console flash — unlike the old
    powershell-spawning shortcut path.
    """
    svc.load_settings()
    target: Optional[str] = None
    if req.kind == "recordings":
        target = svc.settings.recordings_dir
    elif req.kind == "client":
        if not req.client:
            raise HTTPException(status_code=400, detail="client required")
        cfg = svc.client_cfg_svc.get(req.client)
        if not cfg or not cfg.export_folder:
            raise HTTPException(
                status_code=404,
                detail=f"No designated folder set for '{req.client}'")
        target = cfg.export_folder
    elif req.kind == "path":
        target = req.path
    if not target:
        raise HTTPException(status_code=400, detail="path required")
    p = Path(target)
    if req.kind == "path":
        # An arbitrary caller-supplied path used to be opened as-is —
        # and CREATED if missing, an "open folder" call that wrote to
        # any location the request named. os.startfile is ShellExecute:
        # pointed at a file, it runs the handler. The only real caller
        # (the "show in folder" button for a backend-produced export)
        # only ever needs paths inside the app's own roots, so contain
        # to those plus configured client export folders — the same
        # posture as the audio/screenshot endpoints.
        resolved = _resolve_within_scan_roots(target)
        if resolved is None:
            resolved = _resolve_within_app_data_dir(target)
        if resolved is None:
            resolved = _resolve_within_client_export_folders(target)
        if resolved is None:
            logger.warning(
                "open-folder: refusing path outside recordings/archive/"
                "app-data/client-export roots: %r", target)
            raise HTTPException(
                status_code=400,
                detail="Path is outside the app's folders")
        if not resolved.exists():
            # Opening must never create. A missing-but-contained path
            # means the export it came from is gone — say so.
            raise HTTPException(status_code=404,
                                detail="Folder no longer exists")
        p = resolved
    elif not p.exists():
        # recordings/client dirs are app-owned; creating them on first
        # open is the fresh-install behavior the UI depends on.
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            # `open` on macOS reveals the folder in Finder. Run as a
            # detached subprocess so we don't block the request, and
            # ignore exit codes — `open` returns 1 for "already open"
            # which isn't an error from the user's POV.
            subprocess.Popen(["/usr/bin/open", str(p)],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            # Linux: best-effort xdg-open, with a webbrowser fallback for
            # systems where xdg-utils isn't installed.
            try:
                subprocess.Popen(["xdg-open", str(p)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                import webbrowser
                webbrowser.open(p.as_uri())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open folder: {e}")
    return {"ok": True, "path": str(p)}


class ImportSessionRequest(BaseModel):
    file_path: str
    display_name: str = ""
    client: str = ""
    project: str = ""


@app.post("/sessions/import")
async def import_session(req: ImportSessionRequest):
    """
    Import an audio file sitting somewhere on disk as a new session. The
    file is copied into the recordings directory and a session JSON is
    written. Transcription/summary are NOT run automatically — the user
    triggers those from the session detail dialog like any other session.
    """
    svc.load_settings()

    def _do():
        session = svc.session_svc.import_from_file(
            source_path=req.file_path,
            display_name=req.display_name,
            client=req.client,
            project=req.project,
        )
        # No enqueue here: v2.19+ never copies the raw WAV to a network
        # folder. Once the user processes this imported session, the
        # transcript/summary/extractions will enqueue themselves.
        return session.session_id

    try:
        session_id = await asyncio.to_thread(_do)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Import session failed")
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "session_id": session_id}


# ── Finalize-in-progress three-way state (field repro 2026-08-14) ────
#
# Before this, /sessions/{id}/process (and every other endpoint that
# reads a session's audio) had exactly one failure mode for "the WAV
# isn't at audio_path yet": a RuntimeError claiming the file "may have
# been moved, deleted, or not yet synced down from the cloud" — a 500,
# logged at ERROR. That message is only true for a THIRD case; the
# other two are normal, temporary, and honestly reportable:
#
#   1. finalize still running   -> 409, not an error, don't log ERROR.
#   2. finalize failed          -> report the recorded reason, not a
#                                   generic "missing" claim.
#   3. genuinely no finalize in flight and no audio -> the existing
#                                   RuntimeError-based message is
#                                   accurate here; leave it alone.
#
# See Session.finalize_status / recording_service.stop_recording (where
# the state is set/cleared) and services/recovery_service.py (where a
# backend restart mid-finalize is resolved instead of left stuck).
def _finalize_status_detail(session: "Session") -> Optional[tuple[int, str]]:
    """Return ``(status_code, detail)`` if ``session`` is currently
    finalizing (or queued behind another finalize) or its last finalize
    failed — the caller should raise HTTPException with these
    immediately, before touching the audio file at all. Returns None
    when it's safe to proceed to the normal "does the audio file exist"
    checks (case 3 above)."""
    status = getattr(session, "finalize_status", None)
    if status in ("finalizing", "queued"):
        elapsed_s = 0.0
        started = getattr(session, "finalize_started_at", None)
        if started:
            elapsed_s = max(0.0, (datetime.now() - started).total_seconds())
        mins, secs = divmod(int(elapsed_s), 60)
        elapsed_str = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
        if status == "queued":
            # SERIALIZED FINALIZE (see utils/finalize_gate.py): another
            # finalize already holds the one process-wide slot, so this
            # one hasn't even started its own subprocess yet. Say so
            # explicitly — "still being finalized" would read as
            # progress that isn't actually happening, and the elapsed
            # time here counts the WAIT, not finalize work.
            detail = (
                f"This recording is waiting behind another finalize job "
                f"that's currently running (queued for {elapsed_str}). "
                f"This is normal — no data has been lost, and this one "
                f"will start as soon as the other finishes. Please wait "
                f"and try again in a bit; there's no need to keep "
                f"retrying."
            )
            return (409, detail)
        aec_note = ""
        # Prefer what THIS finalize actually started with over the
        # current setting. They differ whenever the user toggles echo
        # cancellation while a finalize is in flight, and the setting
        # then describes the next run rather than the one the user is
        # waiting on. `finalize_aec_requested` is stamped before the
        # finalize stub is written, so it is available for exactly the
        # window this message is shown in; the live setting stays as
        # the fallback for sessions written before the field existed.
        aec_running = getattr(session, "finalize_aec_requested", None)
        if aec_running is None:
            aec_running = bool(
                svc.settings
                and getattr(svc.settings, "echo_cancellation_enabled", False))
        if aec_running:
            aec_note = (
                " Echo cancellation is enabled, which can make this step "
                "take several minutes for longer meetings."
            )
        detail = (
            f"This recording is still being finalized (running for "
            f"{elapsed_str}).{aec_note} This is normal — no data has "
            f"been lost. Please wait and try again in a bit; there's no "
            f"need to keep retrying."
        )
        return (409, detail)
    if status == "failed":
        reason = getattr(session, "finalize_error", None) or "unknown error"
        detail = f"Finalizing this recording's audio failed: {reason}"
        return (422, detail)
    return None


def _raise_if_finalizing(session: "Session") -> None:
    """Raise HTTPException for cases 1/2 above; no-op (case 3, or a
    session with no finalize history at all) otherwise."""
    result = _finalize_status_detail(session)
    if result is not None:
        code, detail = result
        raise HTTPException(status_code=code, detail=detail)


def _raise_if_finalizing_dict(data: dict) -> None:
    """Same as ``_raise_if_finalizing`` but for callers that only have
    the raw session dict (e.g. SessionService.load(), not load_full())
    — the audio and screenshot-serving endpoints use the raw dict to
    avoid the cost of rebuilding a full Session for a file stream."""
    status = data.get("finalize_status")
    if status not in ("finalizing", "queued", "failed"):
        return
    fake = Session(session_id=data.get("session_id", ""))
    fake.finalize_status = status
    fake.finalize_error = data.get("finalize_error")
    started = data.get("finalize_started_at")
    if started:
        try:
            fake.finalize_started_at = datetime.fromisoformat(started)
        except ValueError:
            pass
    _raise_if_finalizing(fake)


@app.post("/sessions/{session_id}/process")
async def process_session(session_id: str):
    svc.load_settings()
    # In-flight indicator for /recording/status — see Services._begin_
    # processing. `finally` guarantees this releases on every exit path:
    # the fast 400s below, a 404, the transcribe/diarize failure branch,
    # AND an exception raised anywhere in between (network blip mid-
    # export, a crash inside an asyncio.create_task's awaited setup,
    # etc). Without the `finally` this would be the exact bug it fixes,
    # just moved from `current_status` to a new flag.
    svc._begin_processing()
    try:
        # Fail fast with a helpful message BEFORE trying to load models — if
        # API keys aren't set, models can't load and the user sees a cryptic
        # "Process failed" toast. Show them where to fix it instead.
        if not svc.settings or not svc.settings.is_configured:
            missing = []
            if not (svc.settings and svc.settings.anthropic_api_key):
                missing.append("Anthropic API key (get at console.anthropic.com)")
            if not (svc.settings and svc.settings.hf_token):
                missing.append(
                    "HuggingFace token (get at huggingface.co/settings/tokens, "
                    "then accept model terms at huggingface.co/pyannote/speaker-"
                    "diarization-3.1 and huggingface.co/pyannote/segmentation-3.0)"
                )
            raise HTTPException(
                status_code=400,
                detail=(
                    "API keys not configured. Open Settings → paste the "
                    "required tokens → Save. Missing: " + "; ".join(missing)
                ),
            )
        # ensure_models_loaded is blocking (imports torch etc.) — thread it
        await asyncio.to_thread(svc.ensure_models_loaded)
        session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        # Finalize still running or already known to have failed — tell
        # the truth about which, instead of letting the "audio file
        # missing" RuntimeError further down claim it was moved/deleted/
        # not synced. See _finalize_status_detail's module comment.
        # Still recording: the WAV is being streamed to the temp capture
        # dir and is only merged to session.audio_path on stop, so the
        # file genuinely isn't there yet. Without this guard we fall
        # through to _ensure_audio_available / copy_audio_to_local_for_
        # processing, whose "missing — moved, deleted, or not yet synced
        # down from the cloud" RuntimeError names three causes that all
        # imply the recording is lost, and omits the real one: not
        # written yet. Same reasoning as _raise_if_finalizing directly
        # below, one window earlier. Field repro 2026-08-25.
        _rec = svc.recording_svc
        if (
            _rec is not None
            and _rec.is_recording
            and _rec.current_session is not None
            and _rec.current_session.session_id == session_id
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This session is still recording. Stop the recording "
                    "first — its audio is written to disk on stop, and "
                    "then it can be processed."
                ),
            )
        _raise_if_finalizing(session)
        # Serialize the shared-state transcribe/diarize stage — see
        # _PROCESSING_LOCK. Without this, a manual re-process here racing a
        # background auto-process cross-contaminates session transcripts.
        try:
            async with _PROCESSING_LOCK:
                # DATA-LOSS FIX (2026-06-15): pass session EXPLICITLY into
                # process_session(). The old `set_session(...)` + parameter-
                # less process_session() pair mutated the shared
                # recording_svc._session reference, which a concurrent
                # start_recording would then reassign — and the in-flight
                # process_session would write segments onto the WRONG
                # session object. set_session() still exists for legitimate
                # external-session callers (recovery), but the processing
                # path doesn't need it now that process_session takes the
                # session as a parameter. svc.current_session also stays —
                # it's used by status / live-transcript endpoints that
                # genuinely want "the actively-recording session".
                svc.current_session = session
                result = await svc.recording_svc.process_session(session)
            # Auto-name speakers from explicit introductions / direct-address
            # hand-offs and persist their voiceprints to the known-speakers
            # store so the next meeting auto-matches them. Best-effort — a
            # failure here must not block saving the processed session.
            try:
                await _auto_identify_and_save_speakers(result)
            except Exception as e:
                logger.warning(f"auto speaker identification skipped: {e}")
            await asyncio.to_thread(svc.session_svc.save, result)
            _auto_export_to_client(result, copy_audio=False)
            # Build the semantic-search index entry for this session in the
            # background. Non-fatal: if sentence-transformers is missing or
            # the index pass fails, the session is still saved and full-text
            # search keeps working — only the semantic search "knows" about
            # one fewer session until the user re-runs the backfill.
            if svc.search_svc:
                asyncio.create_task(asyncio.to_thread(
                    svc.search_svc.index_session, result.session_id))
            # Extract commitments in the background too (Claude pass over
            # the transcript). Same fire-and-forget pattern as semantic
            # indexing — if the LLM is unavailable or the model returns
            # bad JSON, the commitments tracker just shows fewer entries.
            asyncio.create_task(_auto_extract_commitments(result))
            return {"ok": True, "segments": len(result.segments),
                    "speakers": len(result.speakers)}
        except Exception as e:
            logger.exception("Process failed")
            raise HTTPException(status_code=500, detail=str(e))
    finally:
        svc._end_processing()


# Exponential-backoff retry for LLM calls that hit 429 / rate-limit
# responses. v2.9.0 shipped without this and a single Anthropic rate
# burst would cascade-fail every subsequent extractor in process_full
# (summary, action_items, decisions, requirements all in the same
# minute bucket). With this, each extractor gets three attempts at
# 2s / 8s / 30s before giving up.
#
# Detection is intentionally broad — the Anthropic SDK and OpenAI SDK
# raise different exception types but both surface "429" or "rate" in
# the message. Keep this textual rather than catching specific
# exception classes; the alternative is a fragile import-time SDK
# detection that breaks the moment a new provider is added.
async def _llm_call_with_retry(coro_factory, op_name: str,
                                max_attempts: int = 3):
    delays = [2.0, 8.0, 30.0]
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except Exception as e:
            msg = str(e).lower()
            is_rate_limit = (
                "429" in msg
                or "rate limit" in msg
                or "ratelimit" in msg
                or "too many requests" in msg
            )
            last_err = e
            if not is_rate_limit or attempt == max_attempts - 1:
                # Not a rate-limit error, or out of attempts — propagate.
                raise
            delay = delays[min(attempt, len(delays) - 1)]
            logger.warning(
                f"{op_name}: rate-limited, retrying in {delay:.0f}s "
                f"(attempt {attempt + 2}/{max_attempts}): {e}")
            await asyncio.sleep(delay)
    # Unreachable; mypy/typing comfort.
    if last_err:
        raise last_err
    raise RuntimeError(f"{op_name}: unreachable retry exit")


def _meeting_date(session) -> str:
    """The session's own start date, for the summarizer's date anchor.

    Without this the prompt carries no real-world date and a bare
    relative reference in the transcript ("come October") gets a year
    invented for it — see `core._precision.date_anchor`. Returns "" for
    a session with no start time, which makes every summarizer path fall
    back to the un-anchored prompt rather than anchoring on a wrong date.
    """
    started = getattr(session, "started_at", None)
    try:
        return started.isoformat() if started else ""
    except AttributeError:
        return str(started or "")


async def _run_extraction(session_id: str, extractor_name: str, field_name: str,
                           export_fn_name: str, extra_arg=None):
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400,
                            detail="Anthropic API key required")
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # A session mid-finalize (or whose finalize just failed) has no
    # transcript yet for a specific, honest reason — surface that
    # instead of the generic "no transcript" 400 below.
    _raise_if_finalizing(session)
    if not session.segments:
        raise HTTPException(status_code=400,
                            detail="Session has no transcript (run /process first)")
    transcript = session.full_transcript()
    user_notes = session.notes or ""
    try:
        method = getattr(svc.summarizer, extractor_name)
        # Wrap the actual LLM call in the retry helper. Coro factory
        # rebuilds the coroutine on each retry — coroutines can't be
        # awaited twice.
        meeting_date = _meeting_date(session)

        async def _invoke():
            if extra_arg is not None:
                return await method(transcript, extra_arg, notes=user_notes,
                                    meeting_date=meeting_date)
            return await method(transcript, notes=user_notes,
                                meeting_date=meeting_date)
        result = await _llm_call_with_retry(_invoke, extractor_name)
        setattr(session, field_name, result)
        await asyncio.to_thread(svc.session_svc.save, session)
        try:
            export_fn = getattr(svc.export_svc, export_fn_name)
            await asyncio.to_thread(export_fn, session)
        except Exception as ex:
            logger.warning(f"Export failed: {ex}")
        _auto_export_to_client(session, copy_audio=False)
        return {"ok": True, field_name: result}
    except Exception as e:
        logger.exception(f"{extractor_name} failed")
        raise HTTPException(status_code=500, detail=str(e))


class TemplateRequest(BaseModel):
    template: str = "General"


def _copilot_observations_blob(session) -> str:
    """Roll the session's live-copilot ticks into a deduplicated bullet
    blob for the summarizer. Returns empty string when there are no
    ticks (no co-pilot enabled, or no useful suggestions emitted).

    Dedupe is normalized-substring-aware: an earlier tick that said
    'Ask about VPC peering' won't repeat later as 'ask about vpc
    peering' — same observation surfaces once. We preserve original
    casing from the first occurrence.

    The section headers carry provenance on purpose. They used to read
    "Clarifying questions raised" / "Risks flagged" / "Follow-ups
    suggested" — passive phrasings that read as though a PARTICIPANT
    raised or flagged them. They didn't: every line here is the
    co-pilot model's own generated suggestion. A summary duly reported
    four "open routing questions raised by co-pilot" of which three
    were never said by anyone. Each header now names the co-pilot as
    the author, so the framing can't launder speculation into record
    even before the summarizer's own provenance rules apply."""
    ticks = list(getattr(session, "copilot_ticks", []) or [])
    if not ticks:
        return ""
    sections = (
        ("clarifying_questions",
         "Questions the AI co-pilot suggested asking "
         "(generated, not asked by anyone)"),
        ("risks",
         "Risks the AI co-pilot generated (not raised by anyone)"),
        ("follow_ups",
         "Follow-ups the AI co-pilot proposed (not agreed by anyone)"),
    )
    out_lines: list[str] = []
    for key, header in sections:
        seen: dict[str, str] = {}
        for tick in ticks:
            for item in (tick.get(key) or []):
                if not isinstance(item, str):
                    continue
                norm = " ".join(item.split()).lower()
                if norm and norm not in seen:
                    seen[norm] = item.strip()
        if seen:
            out_lines.append(f"{header}:")
            for original in seen.values():
                out_lines.append(f"  • {original}")
            out_lines.append("")
    return "\n".join(out_lines).rstrip()


@app.post("/sessions/{session_id}/summarize")
async def summarize_session(session_id: str, req: TemplateRequest):
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="Anthropic API key required")
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _raise_if_finalizing(session)
    if not session.segments:
        raise HTTPException(status_code=400, detail="Session has no transcript")
    try:
        # Resolve the template name to its current prompt via the template
        # service. Users can edit default prompts or add their own, so we
        # can't bake a prompt into the summarizer anymore.
        prompt_text = await asyncio.to_thread(
            svc.template_svc.get_prompt, req.template)
        async def _summarize():
            return await svc.summarizer.summarize(
                session.full_transcript(),
                prompt=prompt_text,
                notes=session.notes or "",
                template_name=req.template,
                image_paths=list(session.screenshots or []),
                copilot_observations=_copilot_observations_blob(session),
                meeting_date=_meeting_date(session),
            )
        result = await _llm_call_with_retry(_summarize, "summarize")
        session.summary = result
        session.template = req.template
        await asyncio.to_thread(svc.session_svc.save, session)
        try:
            await asyncio.to_thread(svc.export_svc.export_summary, session)
        except Exception:
            pass
        _auto_export_to_client(session, copy_audio=False)
        return {"ok": True, "summary": result}
    except Exception as e:
        logger.exception("Summarize failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions/{session_id}/action-items")
async def action_items(session_id: str):
    return await _run_extraction(
        session_id, "extract_action_items", "action_items",
        "export_action_items")


@app.post("/sessions/{session_id}/requirements")
async def requirements(session_id: str):
    return await _run_extraction(
        session_id, "extract_requirements", "requirements",
        "export_requirements")


@app.post("/sessions/{session_id}/decisions")
async def decisions(session_id: str):
    return await _run_extraction(
        session_id, "extract_decisions", "decisions", "export_decisions")


@app.post("/sessions/{session_id}/structured")
async def structured(session_id: str):
    """Opt-in structured (re)extraction. The auto pipeline already does
    this for new sessions; this endpoint lets the UI backfill a legacy
    session on demand without a bulk, token-burning migration."""
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="AI provider not configured")
    try:
        counts = await _extract_structured_and_save(session_id)
        return {"ok": True, "counts": counts}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Structured extraction failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/engagements/{client}/register")
async def engagement_register(client: str, project: str = ""):
    """The engagement-level roll-up: structured records from every
    session for this client (optionally scoped to a project), deduped
    with provenance. Computed on demand from session JSONs."""
    svc.load_settings()
    if not svc.engagement_svc:
        raise HTTPException(status_code=400, detail="Service not ready")
    try:
        register = await asyncio.to_thread(
            svc.engagement_svc.build_register, client, project)
        # Merge the user's manual overlay (status, sponsor, milestone,
        # notes) into the auto-rolled register. Frontend gets one
        # cohesive payload — separate object so the auto-rolled fields
        # stay unambiguously distinct.
        if svc.engagement_overlay_svc:
            overlay = await asyncio.to_thread(
                svc.engagement_overlay_svc.get, client, project)
            register["overlay"] = overlay.to_dict()
        return {"ok": True, "register": register}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Engagement register build failed")
        raise HTTPException(status_code=500, detail=str(e))


class EngagementOverlayRequest(BaseModel):
    project: str = ""
    status: str = ""
    exec_sponsor: str = ""
    next_milestone: str = ""
    notes: str = ""


@app.put("/engagements/{client}/overlay")
async def engagement_overlay_put(client: str, req: EngagementOverlayRequest):
    """Replace the user's manual overlay for an engagement scope.
    Empty strings clear the corresponding field. Always returns the
    canonical stored overlay so the UI can sync timestamps."""
    if not svc.engagement_overlay_svc:
        raise HTTPException(status_code=503, detail="Overlay service not initialized")
    try:
        overlay = await asyncio.to_thread(
            svc.engagement_overlay_svc.put,
            client, req.project,
            req.status, req.exec_sponsor, req.next_milestone, req.notes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "overlay": overlay.to_dict()}


class PortalBindRequest(BaseModel):
    client: str
    project: str
    # THE way to bind: the connection block pasted verbatim as the
    # portal hands it over ({"portal","api","opportunity","customerId",
    # "editToken"}). It carries the edit token, so it gets the same
    # never-echoed treatment the token itself does.
    connection: str = ""
    # Manual fields, used only when no connection block is pasted.
    customer_id: str = ""
    opportunity_name: str = ""
    parent_name: str = ""
    # The edit token. Accepted in this request body, stored in the OS
    # keychain, and NEVER echoed back: no response, log line or error
    # from this API carries it. Paste-once — the Cognito picker flow
    # can replace this later without changing the wire format.
    edit_token: str = ""


class PortalScopeRequest(BaseModel):
    client: str
    project: str


@app.get("/portal/bindings")
async def portal_bindings():
    """Every project→opportunity binding, tokenless by construction —
    tokens never enter the bindings JSON, so there is nothing here to
    redact."""
    if not svc.portal_push_svc:
        return {}
    # token_present is computed per machine, never persisted — the
    # bindings file roams between the user's machines (it lives in the
    # recordings dir); each machine's keychain does not.
    return svc.portal_push_svc.bindings_with_token_state()


@app.post("/portal/bind")
async def portal_bind(req: PortalBindRequest):
    if not svc.portal_push_svc:
        raise HTTPException(status_code=503, detail="portal service not ready")
    try:
        fields = dict(
            customer_id=req.customer_id,
            opportunity_name=req.opportunity_name,
            parent_name=req.parent_name,
            edit_token=req.edit_token,
        )
        if (req.connection or "").strip():
            from services.portal_push_service import parse_connection
            conn = parse_connection(req.connection)
            fields.update(
                customer_id=conn["customer_id"],
                edit_token=conn["edit_token"],
                api_base=conn["api_base"],
                portal_url=conn["portal_url"],
                # The block's opportunity name wins unless the user
                # typed one alongside the paste.
                opportunity_name=(req.opportunity_name.strip()
                                  or conn["opportunity_name"]),
            )
        binding = await asyncio.to_thread(
            svc.portal_push_svc.bind,
            (req.client or "").strip().lower(),
            (req.project or "").strip().lower(),
            **fields,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # Keychain refusal — the binding was NOT created; saying so
        # beats a binding that pushes tokenless forever.
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "binding": binding}


@app.post("/portal/unbind")
async def portal_unbind(req: PortalScopeRequest):
    if not svc.portal_push_svc:
        raise HTTPException(status_code=503, detail="portal service not ready")
    existed = await asyncio.to_thread(
        svc.portal_push_svc.unbind,
        (req.client or "").strip().lower(),
        (req.project or "").strip().lower())
    return {"ok": True, "existed": existed}


@app.post("/portal/sync")
async def portal_sync(req: PortalScopeRequest):
    """Manual 'sync now'. Regenerates the register (which itself
    enqueues a background push via the register-written hook) and then
    pushes SYNCHRONOUSLY once so the caller gets the portal's actual
    answer — added/updated counts on success, and the specific failure
    class otherwise. Ingest is idempotent, so the background push this
    also triggers adds nothing on top."""
    if not (svc.portal_push_svc and svc.engagement_svc):
        raise HTTPException(status_code=503, detail="portal service not ready")
    client = (req.client or "").strip().lower()
    project = (req.project or "").strip().lower()
    try:
        register = await asyncio.to_thread(
            svc.engagement_svc.build_register, client, project)
        result = await asyncio.to_thread(
            svc.portal_push_svc.push, client, project)
    except PortalBindingBroken as e:
        raise HTTPException(status_code=409, detail=(
            f"The portal rejected this project's edit token — the binding "
            f"is marked broken and automatic pushes have stopped. Re-bind "
            f"the project to resume. ({e})"))
    except PortalPermanent as e:
        raise HTTPException(status_code=422, detail=str(e))
    except PortalTransient as e:
        raise HTTPException(status_code=502, detail=(
            f"The portal is unreachable right now; the push will retry "
            f"automatically in the background. ({e})"))
    # WHAT WE SENT, ALONGSIDE WHAT THE PORTAL DID WITH IT.
    #
    # "0 added, 0 updated" is three completely different situations
    # wearing one sentence: the register was empty and we sent nothing;
    # the portal already had exactly this and correctly no-op'd
    # (ingest is idempotent by contract); or the scope is wrong and we
    # pushed some other project's register. The user cannot act on any
    # of them without knowing which — the same "an unreadable result
    # rendered as a result that isn't there" defect this app keeps
    # paying for.
    #
    # So the response carries the register's own shape. An empty
    # register is now a statement, not a silence.
    reg = register if isinstance(register, dict) else {}
    sent = {
        "session_count": int(reg.get("session_count") or 0),
        "action_items": len(reg.get("action_items") or []),
        "decisions": len(reg.get("decisions") or []),
        "requirements": len(reg.get("requirements") or []),
        "open_questions": len(reg.get("open_questions") or []),
    }
    sent["total"] = (sent["action_items"] + sent["decisions"]
                     + sent["requirements"] + sent["open_questions"])
    return {"ok": True, "sent": sent, **(result or {})}


@app.get("/engagements/known-statuses")
async def engagement_known_statuses():
    """Canonical list of engagement status values. Frontend uses
    this to render the status dropdown — keeps wire format and UI
    options in sync without baking the enum into the frontend."""
    return {"statuses": KNOWN_STATUSES}


@app.post("/engagements/{client}/export")
async def engagement_export(client: str, project: str = ""):
    """Render the engagement register to a stable .xlsx in the client's
    export folder (falls back to the recordings dir). Overwrites in
    place; human-entered Status/Notes carry forward across runs."""
    svc.load_settings()
    if not svc.engagement_svc:
        raise HTTPException(status_code=400, detail="Service not ready")
    try:
        from services.engagement_export_service import (
            export_register_workbook,
        )

        register = await asyncio.to_thread(
            svc.engagement_svc.build_register, client, project)

        cfg = svc.client_cfg_svc.get(client) if svc.client_cfg_svc else None
        dest_dir = (
            cfg.export_folder if cfg and cfg.export_folder
            else str(svc.session_svc.recordings_dir)
        )
        label = register.get("client") or client
        suffix = f" - {project}" if project else ""
        filename = f"{label} - Engagement Register{suffix}.xlsx"

        result = await asyncio.to_thread(
            export_register_workbook, register, dest_dir, filename)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Engagement export failed")
        raise HTTPException(status_code=500, detail=str(e))


class ProcessFullRequest(BaseModel):
    template: str = "General"
    follow_up_drafts: bool = False
    # Re-run every extractor even when the inputs are unchanged. The
    # skip is the default because it is almost always right; this is
    # the escape hatch for "I want it regenerated anyway".
    force: bool = False


@app.post("/sessions/{session_id}/process_full")
async def process_full(session_id: str, req: ProcessFullRequest):
    """
    One-shot pipeline: transcribe + diarize + summary + action items +
    decisions + requirements. Used by auto_process_after_stop so SAs don't
    have to click four separate extract buttons per session.

    Each extraction is best-effort and wrapped in its own try — if Claude
    rate-limits or times out on one extraction, the others still run and
    the session is saved with partial results. The response lists which
    stages succeeded so the UI can show a toast with the exact state.
    """
    svc.load_settings()
    # In-flight indicator for /recording/status — see the docstring
    # on Services._end_processing for why this MUST be released in a
    # `finally`, not just on the success path. process_full is also
    # the function the backend auto-process-after-stop path calls
    # (_auto_process_session -> process_full, with its own retry
    # loop), so this one indicator covers both the manual "Process
    # full" button and the automatic post-stop pipeline.
    svc._begin_processing()
    try:
        if not svc.settings or not svc.settings.is_configured:
            raise HTTPException(
                status_code=400,
                detail="API keys not configured. Open Settings → save tokens → retry.",
            )

        await asyncio.to_thread(svc.ensure_models_loaded)

        stages: dict[str, str] = {}

        # 1. Transcribe + diarize (only if not already done). Serialized
        # under _PROCESSING_LOCK because this stage mutates the shared
        # RecordingService._session / svc.current_session — two overlapping
        # processings would otherwise cross-contaminate each other's
        # transcripts. Double-checked: re-load inside the lock so we don't
        # re-transcribe a session another job just finished while we waited.
        session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        _raise_if_finalizing(session)
        if not session.segments:
            async with _PROCESSING_LOCK:
                session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
                if not session:
                    raise HTTPException(status_code=404, detail="Session not found")
                _raise_if_finalizing(session)
                if not session.segments:
                    # DATA-LOSS FIX (2026-06-15): see the long-form comment in
                    # the manual /sessions/{id}/process handler. Pass session
                    # by parameter so a concurrent start_recording cannot
                    # alias recording_svc._session out from under us.
                    svc.current_session = session
                    try:
                        session = await svc.recording_svc.process_session(session)
                        try:
                            await _auto_identify_and_save_speakers(session)
                        except Exception as e:
                            logger.warning(f"auto speaker identification skipped: {e}")
                        await asyncio.to_thread(svc.session_svc.save, session)
                        stages["transcribe_diarize"] = "ok"
                    except Exception as e:
                        logger.exception("process_full: transcribe/diarize failed")
                        stages["transcribe_diarize"] = f"failed: {e}"
                        return {"ok": False, "stages": stages}
                else:
                    stages["transcribe_diarize"] = "skipped (already processed)"
        else:
            stages["transcribe_diarize"] = "skipped (already processed)"

        # 2-6. Run the Claude extractions against ONE loaded session, then
        # save ONCE.
        #
        # The previous version ran five extractions via asyncio.gather where
        # each one independently did load_full -> setattr(one field) ->
        # save(whole session). Because they all loaded the same base
        # concurrently and each saved the entire object back, they CLOBBERED
        # each other — the last writer won and silently nulled the other
        # fields. Observed in the wild: summary/action_items/decisions/
        # requirements all came back null while only the structured records
        # survived (it saved last). Now the extractions only COMPUTE; we
        # apply every result to a single session object and write it once.
        from models.extraction import STRUCTURED_FIELDS, stamp_records

        session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
        if not session or not session.segments:
            # No transcript to extract from (transcribe failed/empty).
            return {"ok": False, "stages": stages}
        transcript = session.full_transcript()
        notes = session.notes or ""
        images = list(session.screenshots or [])
        meeting_date = _meeting_date(session)

        async def _do_summary():
            prompt_text = await asyncio.to_thread(
                svc.template_svc.get_prompt, req.template)
            return await _llm_call_with_retry(
                lambda: svc.summarizer.summarize(
                    transcript, prompt=prompt_text, notes=notes,
                    template_name=req.template, image_paths=images,
                    copilot_observations=_copilot_observations_blob(session),
                    meeting_date=meeting_date),
                "summarize")

        async def _do_markdown(method_name):
            method = getattr(svc.summarizer, method_name)
            return await _llm_call_with_retry(
                lambda: method(transcript, notes=notes, image_paths=images,
                               meeting_date=meeting_date),
                method_name)

        async def _do_structured():
            parsed = await svc.summarizer.extract_structured(
                transcript, notes=notes, image_paths=images,
                meeting_date=meeting_date)
            created_at = session.started_at.isoformat() if session.started_at else ""
            return stamp_records(parsed, session.session_id, created_at)

        # NOTHING CHANGED SINCE THE LAST RUN -> DON'T PAY FOR IT AGAIN.
        #
        # Reprocessing re-ran all five extractors on every session
        # regardless of whether their inputs had moved. The August 2026
        # token export showed what that costs: the reprocessing days ran
        # 4-8x a normal day, nearly all of it regenerating byte-identical
        # text. The fingerprint covers transcript + notes + template +
        # EXTRACTOR_PROMPT_VERSION, so a prompt edit still forces a real
        # re-run — it just stops being the default for everything.
        from core.prompt_version import extraction_fingerprint

        fingerprint = extraction_fingerprint(transcript, notes, req.template)
        already = (getattr(session, "extraction_fingerprint", "") or "")
        if (already and already == fingerprint and not req.force
                and session.summary):
            stages["extract"] = "skipped (inputs unchanged since last run)"
            logger.info(
                "process_full: %s inputs unchanged — skipped 5 LLM calls",
                session_id)
            return {"ok": True, "stages": stages, "skipped": True}

        # THE FIRST CALL WARMS THE CACHE; THE REST READ IT.
        #
        # These five used to run in one gather. Prompt caching writes on
        # the first request and is only readable once that write lands,
        # so five concurrent calls all raced it and every one paid the
        # full input price — the cache would have reported 0 reads and
        # looked exactly like the bug it was meant to fix. Summary goes
        # first, alone; the other four then share its cached transcript.
        try:
            summary_r = await _do_summary()
        except Exception as e:  # noqa: BLE001
            # Every extraction here is best-effort: one failing must not
            # cancel the rest (see this function's docstring). Moving
            # summary out of the gather to warm the cache silently
            # dropped that guarantee, because a bare await propagates.
            # Captured to match exactly what return_exceptions=True did.
            summary_r = e
        ai_r, dec_r, req_r, struct_r = await asyncio.gather(
            _do_markdown("extract_action_items"),
            _do_markdown("extract_decisions"),
            _do_markdown("extract_requirements"),
            _do_structured(),
            return_exceptions=True,
        )

        def _apply(field: str, value, label: str):
            if isinstance(value, Exception):
                logger.warning(f"process_full: {label} failed: {value}")
                stages[label] = f"failed: {value}"
            else:
                setattr(session, field, value)
                stages[label] = "ok"

        _apply("summary", summary_r, "summary")
        if not isinstance(summary_r, Exception):
            session.template = req.template
        _apply("action_items", ai_r, "action_items")
        _apply("decisions", dec_r, "decisions")
        _apply("requirements", req_r, "requirements")
        if isinstance(struct_r, Exception):
            logger.warning(f"process_full: structured failed: {struct_r}")
            stages["structured"] = f"failed: {struct_r}"
        else:
            for key, (_cls, attr) in STRUCTURED_FIELDS.items():
                setattr(session, attr, struct_r.get(key, []))
            stages["structured"] = "ok"

        # Stamp the fingerprint only when the run genuinely succeeded.
        # A partial run (one extractor rate-limited, the rest fine) must
        # NOT record "these inputs are done" — that would make the next
        # reprocess skip the very session that still needs finishing,
        # and the skip would be indistinguishable from success.
        if all(not isinstance(r, Exception)
               for r in (summary_r, ai_r, dec_r, req_r, struct_r)):
            session.extraction_fingerprint = fingerprint
        else:
            session.extraction_fingerprint = ""

        # A cache that silently isn't hitting looks exactly like no cache
        # at all — the August export's `cache_read = 0` was the only sign
        # anything was wrong, and nothing in the app was reporting it.
        # One line per run so the answer is in the log, not in a rebuild.
        try:
            cs = getattr(svc.summarizer, "cache_stats", None)
            if cs and cs.get("calls"):
                billed = (cs["write"] * 1.25 + cs["read"] * 0.1
                          + cs["uncached"])
                full = cs["read"] + cs["write"] + cs["uncached"]
                logger.info(
                    "process_full: prompt cache — %d calls, read=%d "
                    "write=%d uncached=%d (~%d%% of uncached input cost)",
                    cs["calls"], cs["read"], cs["write"], cs["uncached"],
                    round(billed / full * 100) if full else 100)
        except Exception as e:  # noqa: BLE001 — must never fail a run
            # Swallowed on purpose, but not silently: a bare `pass` here
            # would hide the accounting breaking, and the whole point of
            # this block is that a broken cache must not be invisible.
            logger.debug("process_full: cache accounting failed: %s", e)

        # Single write with every successful field applied.
        await asyncio.to_thread(svc.session_svc.save, session)

        # 6. Optional follow-up email drafts — only when requested explicitly.
        if req.follow_up_drafts:
            try:
                from services.follow_up_email import draft_follow_up_emails
                from services.follow_up_owners import ARTIFACT_COMPOSE_LINK
                result = await asyncio.to_thread(
                    draft_follow_up_emails, svc, session_id)
                # `state` distinguishes "no action items" from "action
                # items we could not read" from "all group-owned" — a
                # zero here used to be indistinguishable from all three.
                # `unaddressed` is the same idea one level on: a non-zero
                # count is not automatically a good outcome, because a
                # draft with no recipient cannot be sent.
                #
                # The noun comes from `artifact`, not from this line: a
                # compose link (calendar_source "extension" — no mail
                # client contacted, nothing written to any mailbox) is
                # not a draft, and this stage string is read back by a
                # human deciding whether to go looking in Outlook.
                noun = ("compose links"
                        if result.artifact == ARTIFACT_COMPOSE_LINK
                        else "drafts")
                stages["follow_up_drafts"] = (
                    f"ok ({result.created} {noun}, state={result.state}"
                    + (f", source={result.source}" if result.source else "")
                    + (f", unaddressed={result.unaddressed}"
                       if result.unaddressed else "")
                    + (f", unverified={result.unverified}"
                       if result.unverified else "")
                    + ")"
                )
            except Exception as e:
                logger.exception("process_full: follow_up_drafts failed")
                stages["follow_up_drafts"] = f"failed: {e}"

        # Auto-index for semantic search (mirrors what /process does for the
        # one-shot path). Fire-and-forget; missing semantic index never
        # blocks the user's main flow. Catches the auto_process_after_stop
        # path which previously skipped indexing entirely.
        if svc.search_svc:
            asyncio.create_task(asyncio.to_thread(
                svc.search_svc.index_session, session_id))
        # Same for commitments — auto_process_after_stop now auto-mines
        # commitments too. Need a fresh session load because the local
        # `session` variable above may not reflect the latest extractions.
        if svc.commitments_svc:
            async def _do_commitments():
                try:
                    fresh = await asyncio.to_thread(
                        svc.session_svc.load_full, session_id)
                    if fresh:
                        await _auto_extract_commitments(fresh)
                except Exception as e:
                    logger.exception(f"process_full commitments failed: {e}")
            asyncio.create_task(_do_commitments())

        # Keep the engagement register warm: a freshly processed session
        # has new structured records, so recompute its client's roll-up.
        # Fire-and-forget — a stale register never blocks processing.
        if svc.engagement_svc:
            async def _do_register():
                try:
                    fresh = await asyncio.to_thread(
                        svc.session_svc.load_full, session_id)
                    if fresh and (fresh.client or "").strip():
                        await asyncio.to_thread(
                            svc.engagement_svc.build_register,
                            fresh.client, fresh.project or "")
                except Exception as e:
                    logger.exception(f"process_full register refresh failed: {e}")
            asyncio.create_task(_do_register())

        # Clear any prior auto-process failure badge — a successful run (manual
        # or auto retry) means the session is no longer in a failed state.
        try:
            fresh = await asyncio.to_thread(svc.session_svc.load_full, session_id)
            if fresh and fresh.processing_error:
                fresh.processing_error = None
                await asyncio.to_thread(svc.session_svc.save, fresh)
        except Exception as e:
            logger.warning(f"could not clear processing_error on {session_id}: {e}")

        return {"ok": True, "stages": stages}
    finally:
        svc._end_processing()


async def _extract_and_save(
    session_id: str, method_name: str, field_name: str,
    template: str = "General",
):
    """Helper used by process_full — runs one extraction and persists it."""
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session or not session.segments:
        raise RuntimeError("no transcript")
    transcript = session.full_transcript()
    notes = session.notes or ""
    method = getattr(svc.summarizer, method_name)
    if method_name == "summarize":
        # Resolve the template name to its current prompt the same way
        # the standalone /sessions/{id}/summarize endpoint does — the
        # summarizer no longer owns template storage. Passing
        # `template=<name>` directly was a vestige of the old API.
        prompt_text = await asyncio.to_thread(
            svc.template_svc.get_prompt, template)
        # Wrap the LLM call in retry-on-429 — see _llm_call_with_retry.
        async def _summarize():
            return await method(
                transcript, prompt=prompt_text,
                notes=notes, template_name=template,
                image_paths=list(session.screenshots or []),
                copilot_observations=_copilot_observations_blob(session),
                meeting_date=_meeting_date(session))
        result = await _llm_call_with_retry(_summarize, method_name)
        session.template = template
    else:
        # All four markdown extractors now accept image_paths so
        # screenshots inform action items / decisions / requirements
        # the same way they inform the summary. Older builds passed
        # only the transcript; this widening is backwards-compatible
        # with the Summarizer signatures (image_paths default = None).
        async def _extract():
            return await method(
                transcript, notes=notes,
                image_paths=list(session.screenshots or []),
                meeting_date=_meeting_date(session),
            )
        result = await _llm_call_with_retry(_extract, method_name)
    setattr(session, field_name, result)
    await asyncio.to_thread(svc.session_svc.save, session)


async def _extract_structured_and_save(session_id: str) -> dict:
    """Run the single structured extraction and persist the typed
    records onto the session. Returns per-type counts for the response
    / stage log. Independent of the markdown extractors — those still
    populate the per-session UI; this feeds the engagement layer."""
    from models.extraction import STRUCTURED_FIELDS, stamp_records

    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if session is None:
        raise FileNotFoundError("session not found")
    if not session.segments:
        # Audio exists but was never transcribed. The bulk backfill walks
        # every session, so this is expected, not an error — skip with
        # zero counts instead of failing the whole sweep.
        logger.info("structured: %s has no transcript — skipped", session_id)
        return {k: 0 for k in STRUCTURED_FIELDS}
    parsed = await svc.summarizer.extract_structured(
        session.full_transcript(),
        notes=session.notes or "",
        image_paths=list(session.screenshots or []),
        meeting_date=_meeting_date(session))
    created_at = session.started_at.isoformat() if session.started_at else ""
    stamped = stamp_records(parsed, session.session_id, created_at)
    counts: dict = {}
    for key, (_cls, attr) in STRUCTURED_FIELDS.items():
        recs = stamped.get(key, [])
        setattr(session, attr, recs)
        counts[key] = len(recs)
    await asyncio.to_thread(svc.session_svc.save, session)
    return counts


class FollowUpDraftsRequest(BaseModel):
    # Optional override for the sender's tone / context
    tone: str = "friendly-professional"


@app.post("/sessions/{session_id}/follow_up_drafts")
async def create_follow_up_drafts(session_id: str, req: FollowUpDraftsRequest):
    """Create per-attendee Outlook email drafts with their action items."""
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="Anthropic API key required")
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _raise_if_finalizing(session)
    try:
        from services.follow_up_email import draft_follow_up_emails
        result = await asyncio.to_thread(
            draft_follow_up_emails, svc, session_id, tone=req.tone)
        # `state` + `message` let the UI say WHICH empty case happened.
        # `drafts_created == 0` used to be the only signal, so "we could
        # not parse the action items" was reported to the user as
        # "Claude didn't attribute any items to a specific person".
        return {"ok": True, **result.to_dict()}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Follow-up drafts failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Semantic search ──────────────────────────────────────────────────


class SemanticSearchRequest(BaseModel):
    query: str
    top_k: int = 10
    client: Optional[str] = None
    project: Optional[str] = None


@app.post("/search/semantic")
async def semantic_search(req: SemanticSearchRequest):
    """Run a vector search across every indexed session's chunks.
    Returns up to top_k hits ordered by cosine similarity. Filters
    (client / project) narrow the candidate pool BEFORE ranking so a
    scoped search isn't drowned out by closer matches in other clients."""
    svc.load_settings()
    if not svc.search_svc:
        raise HTTPException(status_code=500, detail="Search service unavailable")
    if not (req.query or "").strip():
        return {"results": [], "query": req.query}
    try:
        results = await asyncio.to_thread(
            svc.search_svc.search,
            req.query,
            max(1, min(50, req.top_k)),
            req.client,
            req.project,
        )
        return {"results": results, "query": req.query}
    except Exception as e:
        logger.exception("Semantic search failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/index/status")
async def search_index_status():
    """How many sessions are currently in the semantic index, vs total
    session count. Drives the Settings backfill UI's progress hint."""
    svc.load_settings()
    if not svc.search_svc:
        return {"available": False, "total_sessions": 0,
                "indexed_sessions": 0, "model_id": ""}
    return await asyncio.to_thread(svc.search_svc.index_status)


@app.post("/sessions/{session_id}/embed")
async def embed_session(session_id: str):
    """Embed (or re-embed) one session's transcript for semantic search.
    Idempotent — running twice produces the same result, the second
    write just overwrites the existing sidecar. Used by the backfill
    UI in Settings to walk every session that's missing an index entry."""
    svc.load_settings()
    if not svc.search_svc:
        raise HTTPException(status_code=500, detail="Search service unavailable")
    try:
        ok = await asyncio.to_thread(svc.search_svc.index_session, session_id)
    except Exception as e:
        logger.exception("Embed failed")
        raise HTTPException(status_code=500, detail=str(e))
    if not ok:
        # Session has no segments yet OR sentence-transformers isn't
        # installed. 200 with embedded=False so callers can keep walking
        # through a backfill list rather than treating one no-op as fatal.
        return {"embedded": False, "session_id": session_id}
    return {"embedded": True, "session_id": session_id}


@app.post("/search/index/backfill")
async def search_index_backfill(limit: int = 50):
    """Embed up to `limit` sessions that don't have an index entry yet.

    Designed to be called repeatedly from the UI in batches so the user
    sees progress. Each call processes whichever sessions are still
    missing — when indexed_sessions == total_sessions, the UI knows
    backfill is done.

    Capped by `limit` so the FastAPI request doesn't sit on the event
    loop for several minutes if the user has 100s of unindexed sessions.
    """
    svc.load_settings()
    if not svc.search_svc:
        raise HTTPException(status_code=500, detail="Search service unavailable")

    def _backfill_one_batch():
        recordings_dir = svc.search_svc._session_service.recordings_dir
        all_sessions = svc.search_svc._session_service.list_sessions()
        # Only embed processed sessions — sessions without segments would
        # produce no chunks and waste a model load.
        candidates = [
            s for s in all_sessions
            if s.get("has_transcript")
            # A session with only a legacy .embeddings.pkl (pre-migration,
            # never loaded — see services/search_service.py) has no .npz
            # here and is correctly treated as still needing a rebuild.
            and not (recordings_dir / f"session_{s['session_id']}.embeddings.npz").exists()
        ]
        embedded: list[str] = []
        for s in candidates[:limit]:
            try:
                if svc.search_svc.index_session(s["session_id"]):
                    embedded.append(s["session_id"])
            except Exception as e:
                logger.warning(f"Backfill failed for {s['session_id']}: {e}")
        return {
            "embedded": embedded,
            "embedded_count": len(embedded),
            "remaining": max(0, len(candidates) - len(embedded)),
        }

    try:
        return await asyncio.to_thread(_backfill_one_batch)
    except Exception as e:
        logger.exception("Backfill failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Commitments ──────────────────────────────────────────────────────


@app.get("/commitments")
async def list_commitments(
    client: Optional[str] = None,
    project: Optional[str] = None,
    status: Optional[str] = None,  # comma-separated: "active", "awaiting", "delivered", "dismissed", "overdue"
    owner: Optional[str] = None,
    side: Optional[str] = None,    # "internal" | "customer" | "unknown"
):
    """Aggregated commitment list across every session, with optional
    filters. status accepts a comma-separated list and supports two
    synthetic values:
      - "active"  → expands to awaiting + overdue
      - "overdue" → awaiting commitments past their due_date_iso
    """
    svc.load_settings()
    if not svc.commitments_svc:
        return {"commitments": []}
    status_list = (
        [s.strip() for s in status.split(",") if s.strip()]
        if status else None
    )
    # owner filtering resolves multi-owner strings ("Mark/Sam") and
    # confirmed aliases ("Samantha" -> "Sam") — see owner_service.py.
    alias_index = load_alias_index(svc.owner_alias_store)
    items = await asyncio.to_thread(
        svc.commitments_svc.list_all,
        client or None,
        project or None,
        status_list,
        owner or None,
        side or None,
        alias_index,
    )
    return {"commitments": items}


class CommitmentStatusUpdate(BaseModel):
    status: str        # "awaiting" | "delivered" | "dismissed"
    note: str = ""


@app.patch("/commitments/{commitment_id}")
async def update_commitment(commitment_id: str, req: CommitmentStatusUpdate):
    """Mark a commitment delivered, dismissed, or restore to awaiting.
    Returns 404 when the commitment_id doesn't match any session's
    sidecar — usually means the underlying session was deleted while
    the user had the page open."""
    svc.load_settings()
    if not svc.commitments_svc:
        raise HTTPException(status_code=500,
                            detail="Commitments service unavailable")
    if req.status not in {"awaiting", "delivered", "dismissed"}:
        raise HTTPException(
            status_code=400,
            detail="status must be awaiting / delivered / dismissed")
    updated = await asyncio.to_thread(
        svc.commitments_svc.update_status,
        commitment_id, req.status, req.note,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Commitment not found")
    return updated


@app.post("/sessions/{session_id}/extract-commitments")
async def extract_session_commitments(session_id: str):
    """Manually re-run commitment extraction over a session. Used by
    the Sessions detail dialog when the user has corrected speaker
    labels or edited segments and wants the tracker to re-mine the
    transcript with the cleaner data.

    Replaces all existing commitments for the session — re-running
    isn't meant to merge with prior output, it supersedes it."""
    svc.load_settings()
    if not svc.commitments_svc:
        raise HTTPException(status_code=500,
                            detail="Commitments service unavailable")
    if not svc.summarizer:
        raise HTTPException(status_code=400,
                            detail="AI provider not configured")
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    commits = await extract_commitments_from_session(
        svc.summarizer, session,
        customer_hint=session.client or "",
    )
    await asyncio.to_thread(
        svc.commitments_svc.replace_session_commitments,
        session_id, commits)
    return {"ok": True, "extracted": len(commits)}


# ── Owner grouping (Follow Ups + Commitments owner normalisation) ────
#
# See services/owner_service.py for the split/normalise/suggest rules.
# This section is the HTTP surface over its OwnerAliasStore: list /
# create / edit / delete confirmed merges, plus the suggestion feed the
# management UI shows for the judgement-call tier the user must
# explicitly accept.
#
# Decisions also carry an "Owner" bullet field (decisions-view.tsx),
# but confirmed aliases apply uniformly to any raw owner string passed
# through resolve_owners() regardless of which view reads it — nothing
# here is Follow-Ups-specific. Only the *suggestion* feed below is
# scoped to Follow Ups + Commitments (the two item types explicitly in
# scope for this pass); Decisions' owner field isn't scanned for new
# suggestions yet.


def _gather_raw_owner_strings() -> List[str]:
    """Collect every raw owner string across Follow Ups (action-item
    markdown) and Commitments, for suggest_groups()/aggregate_raw_owners().
    One entry per item, exactly as extracted — splitting/normalising
    happens downstream in owner_service.py, not here."""
    from services.insights_service import InsightsService
    out: List[str] = []
    try:
        sessions = svc.session_svc.list_sessions() if svc.session_svc else []
    except Exception as e:
        logger.warning(f"Owner suggestions: session list failed: {e}")
        sessions = []
    for s in sessions:
        md = s.get("action_items") or ""
        if not md:
            continue
        for item in InsightsService._parse_follow_ups(md):
            owner = (item.get("owner") or "").strip()
            if owner:
                out.append(owner)
    try:
        commitments = svc.commitments_svc.list_all() if svc.commitments_svc else []
    except Exception as e:
        logger.warning(f"Owner suggestions: commitments list failed: {e}")
        commitments = []
    for c in commitments:
        owner = (c.get("owner") or "").strip()
        if owner:
            out.append(owner)
    return out


def _alias_to_dict(a) -> dict:
    return {"id": a.id, "canonical": a.canonical, "members": a.members}


@app.get("/owners/aliases")
async def list_owner_aliases():
    """Confirmed owner merges — the tier-3 groups the user has already
    accepted (or created manually). Each `members` entry is a tier-2
    normalised key (lowercase, org-suffix/punctuation stripped)."""
    svc.load_settings()
    if not svc.owner_alias_store:
        return {"aliases": []}
    aliases = await asyncio.to_thread(svc.owner_alias_store.list_all)
    return {"aliases": [_alias_to_dict(a) for a in aliases]}


class OwnerAliasCreate(BaseModel):
    canonical: str
    members: List[str]


@app.post("/owners/aliases")
async def create_owner_alias(req: OwnerAliasCreate):
    """Manual merge, or accepting a suggested group. `members` may be
    raw display strings or tier-2 keys — the store lowercases them, so
    the frontend can pass whatever it already has on hand."""
    svc.load_settings()
    if not svc.owner_alias_store:
        raise HTTPException(status_code=500,
                             detail="Owner alias store unavailable")
    try:
        alias = await asyncio.to_thread(
            svc.owner_alias_store.create, req.canonical, req.members)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _alias_to_dict(alias)


class OwnerAliasUpdate(BaseModel):
    canonical: Optional[str] = None
    add_members: Optional[List[str]] = None
    remove_members: Optional[List[str]] = None


@app.patch("/owners/aliases/{alias_id}")
async def update_owner_alias(alias_id: str, req: OwnerAliasUpdate):
    """Rename a group, merge more names into it, or split (remove) a
    member out of it — removing the last member deletes the group,
    which is how a full split back to "ungrouped" is expressed."""
    svc.load_settings()
    if not svc.owner_alias_store:
        raise HTTPException(status_code=500,
                             detail="Owner alias store unavailable")
    alias = await asyncio.to_thread(
        svc.owner_alias_store.update, alias_id,
        req.canonical, req.add_members, req.remove_members,
    )
    if alias is None:
        return {"deleted": True, "id": alias_id}
    return _alias_to_dict(alias)


@app.delete("/owners/aliases/{alias_id}")
async def delete_owner_alias(alias_id: str):
    """Fully ungroup — reverses create() exactly, restoring every
    member to its own unaliased (tier-2-normalised) entry."""
    svc.load_settings()
    if not svc.owner_alias_store:
        raise HTTPException(status_code=500,
                             detail="Owner alias store unavailable")
    deleted = await asyncio.to_thread(svc.owner_alias_store.delete, alias_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Alias not found")
    return {"ok": True}


@app.get("/owners/suggestions")
async def get_owner_suggestions():
    """Judgement-call merge candidates (first-token match, nickname/
    prefix relationships) across every raw owner string seen in Follow
    Ups + Commitments — advisory only, never applied without the user
    accepting via POST /owners/aliases."""
    svc.load_settings()

    def _do():
        raw_strings = _gather_raw_owner_strings()
        counts, display = aggregate_raw_owners(raw_strings)
        grouped_keys = (
            svc.owner_alias_store.member_keys()
            if svc.owner_alias_store else set()
        )
        rejected = (
            svc.owner_alias_store.rejected_pairs()
            if svc.owner_alias_store else []
        )
        groups = suggest_groups(counts, display, grouped_keys, rejected)
        return {
            "groups": [
                {
                    "group_id": g.group_id,
                    "suggested_canonical": g.suggested_canonical,
                    "members": [
                        {"key": m.key, "display": m.display, "count": m.count}
                        for m in g.members
                    ],
                }
                for g in groups
            ]
        }
    return await asyncio.to_thread(_do)


class OwnerSuggestionReject(BaseModel):
    a: str
    b: str


@app.post("/owners/suggestions/reject")
async def reject_owner_suggestion(req: OwnerSuggestionReject):
    """Dismiss a suggested pair so it stops resurfacing. Bookkeeping
    only — never groups anything, only suppresses future suggestions."""
    svc.load_settings()
    if not svc.owner_alias_store:
        raise HTTPException(status_code=500,
                             detail="Owner alias store unavailable")
    await asyncio.to_thread(svc.owner_alias_store.reject, req.a, req.b)
    return {"ok": True}


# ── Item status overlays (follow-up done, decision lifecycle) ────────

class ItemStatusUpdate(BaseModel):
    """Patch a single follow-up or decision's status. The frontend hashes
    the parsed item text with the same normalization as
    item_status_service.compute_item_hash and sends the digest back."""
    type: str  # "follow_up" | "decision"
    item_hash: str
    done: Optional[bool] = None         # follow_up only
    status: Optional[str] = None        # decision only


@app.get("/insights/summary")
async def insights_summary(
    since: Optional[str] = None,
    until: Optional[str] = None,
    client: Optional[str] = None,
):
    """Cross-meeting analytics for the Insights view.

    Query params:
      - since / until: ISO 8601 datetimes bounding the window (both
        optional; absence means no bound on that end).
      - client: when set, scope time-allocation and open-loops to a
        single client. Recurring topics ignore this param and always
        return per-client buckets.

    The response shape is consumed by src/components/insights-view.tsx.
    Computation is bounded by the number of session JSONs on disk
    (sub-1k for the foreseeable future), so we keep this synchronous —
    no caching, no warmup.
    """
    svc.load_settings()
    if not svc.insights_svc:
        raise HTTPException(status_code=503, detail="Insights service not ready")
    def _parse(s: Optional[str]) -> Optional[datetime]:
        # Session `started_at` is stored as naive local time (see
        # SessionService), so any tz-aware input from the frontend
        # (toISOString() emits a trailing 'Z') must be converted to
        # local time and stripped of tzinfo to be comparable.
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid ISO datetime: {s!r}")
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    since_dt = _parse(since)
    until_dt = _parse(until)
    return await asyncio.to_thread(
        svc.insights_svc.summary,
        since_dt, until_dt, client)


@app.get("/item-status")
async def list_all_item_status():
    """Return every session's overrides in one shot, keyed by
    session_id. The cross-session FollowUps / Decisions views call this
    once on mount so they can render check-state without per-row
    requests."""
    svc.load_settings()
    if not svc.item_status_svc:
        return {"sessions": {}}
    data = await asyncio.to_thread(svc.item_status_svc.list_all)
    return {"sessions": data}


@app.get("/sessions/{session_id}/item-status")
async def get_item_status(session_id: str):
    svc.load_settings()
    if not svc.item_status_svc:
        return {"follow_ups": {}, "decisions": {}}
    return await asyncio.to_thread(svc.item_status_svc.get, session_id)


@app.patch("/sessions/{session_id}/item-status")
async def patch_item_status(session_id: str, req: ItemStatusUpdate):
    svc.load_settings()
    if not svc.item_status_svc:
        raise HTTPException(status_code=500,
                            detail="Item-status service unavailable")
    if req.type == "follow_up":
        if req.done is None:
            raise HTTPException(status_code=400,
                                detail="follow_up update requires `done`")
        try:
            doc = await asyncio.to_thread(
                svc.item_status_svc.set_follow_up,
                session_id, req.item_hash, req.done,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return doc
    if req.type == "decision":
        if not req.status:
            raise HTTPException(status_code=400,
                                detail="decision update requires `status`")
        if req.status not in VALID_DECISION_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {sorted(VALID_DECISION_STATUSES)}")
        try:
            doc = await asyncio.to_thread(
                svc.item_status_svc.set_decision,
                session_id, req.item_hash, req.status,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return doc
    raise HTTPException(
        status_code=400, detail="type must be follow_up or decision")


# ── Cross-meeting Q&A ────────────────────────────────────────────────


class QARequest(BaseModel):
    query: str
    top_k: int = 8
    client: Optional[str] = None
    project: Optional[str] = None


class QAInlineRequest(BaseModel):
    """Used by the in-call search panel's 'this call (AI)' mode. The
    `context` is the live-transcript text the frontend has accumulated
    so far; we hand it straight to Claude with no semantic retrieval."""
    query: str
    context: str


@app.post("/qa/stream")
async def qa_stream(req: QARequest):
    """SSE endpoint that streams a Claude (or OpenAI-compatible) answer
    to the user's question, grounded in chunks retrieved from the
    semantic search index.

    Event sequence delivered to the client:

      event: sources
      data: [{session_id, display_name, similarity, ...}, ...]

      data: <text fragment>
      data: <text fragment>
      ...

      event: done
      data:

    The `sources` event fires before the first text fragment so the
    UI can render the citation panel while Claude is still typing. A
    final `done` event signals normal completion. Errors come through
    as an `event: error` so the client can show a clear failure state
    instead of just hanging on a closed stream.
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    svc.load_settings()
    if not svc.qa_svc or not svc.qa_svc.is_ready:
        raise HTTPException(
            status_code=409,
            detail=("Q&A needs both the semantic-search index and an AI "
                    "provider configured. Save an Anthropic / OpenRouter / "
                    "Ollama key in Settings → AI Provider, then try again."),
        )
    if not (req.query or "").strip():
        raise HTTPException(status_code=400, detail="query cannot be empty")

    # Retrieve up front (synchronous, fast) so the `sources` event can
    # fire immediately when the SSE stream opens. If the retrieval
    # finds nothing, QAService.stream_answer emits a "no material"
    # answer instead of calling the LLM at all.
    sources = await asyncio.to_thread(
        svc.qa_svc.retrieve,
        req.query,
        max(1, min(20, req.top_k)),
        req.client,
        req.project,
    )

    async def event_stream():
        try:
            yield ": connected\n\n"
            # Source list event — the UI builds its citation panel from
            # this without having to parse anything out of the answer text.
            yield "event: sources\n"
            yield "data: " + _json.dumps(sources) + "\n\n"
            # Stream the answer text
            async for fragment in svc.qa_svc.stream_answer(req.query, sources):
                # Each fragment becomes a single `data:` line. JSON-encode
                # so embedded newlines / quotes don't break SSE framing.
                yield "data: " + _json.dumps({"text": fragment}) + "\n\n"
            yield "event: done\ndata: \n\n"
        except Exception as e:
            logger.exception(f"QA stream failed: {e}")
            yield "event: error\n"
            yield "data: " + _json.dumps({"error": str(e)}) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/qa/inline-stream")
async def qa_inline_stream(req: QAInlineRequest):
    """SSE endpoint for the in-call 'ask about this call' mode.

    Mirrors /qa/stream's framing (data: text fragments, then `done` /
    `error` events) but skips the `sources` event and the retrieval
    step — the only context is the live-transcript blob the frontend
    sends in the request body. Faster (no embedding lookup, no source
    formatting) and cheaper (smaller prompt) than /qa/stream.
    """
    from fastapi.responses import StreamingResponse
    import json as _json

    svc.load_settings()
    if not svc.qa_svc or svc.summarizer is None:
        raise HTTPException(
            status_code=409,
            detail=("AI search needs a provider configured. Save an "
                    "Anthropic / OpenRouter / Ollama / Groq / Gemini "
                    "key in Settings → AI Provider, then try again."),
        )
    if not (req.query or "").strip():
        raise HTTPException(status_code=400, detail="query cannot be empty")

    async def event_stream():
        try:
            yield ": connected\n\n"
            async for fragment in svc.qa_svc.stream_inline_answer(
                req.query, req.context,
            ):
                yield "data: " + _json.dumps({"text": fragment}) + "\n\n"
            yield "event: done\ndata: \n\n"
        except Exception as e:
            logger.exception(f"QA inline stream failed: {e}")
            yield "event: error\n"
            yield "data: " + _json.dumps({"error": str(e)}) + "\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class PrepBriefRequest(BaseModel):
    subject: str
    client: str = ""
    project: str = ""
    # Free-text from the user — pasted exec ask, agenda notes, recent
    # email thread, anything the meeting history wouldn't capture.
    # Optional; older clients omit it and the brief behaves exactly as
    # before.
    user_context: str = ""


class PrepBriefFromMeetingRequest(BaseModel):
    """Body for the click-from-calendar-tile prep-brief flow.

    The frontend resolves client/project from attendee email domains
    (mirroring the existing useMeeting() auto-tag logic) before calling
    this endpoint, so we get them as direct fields rather than having
    to recompute. attendees + scheduled_start_iso are kept so the
    Claude prompt can name the meeting and the people in the room
    explicitly — that nudges the brief toward person-specific
    context."""
    subject: str
    attendees: list[str] = []
    scheduled_start_iso: str = ""
    scheduled_end_iso: str = ""
    client: str = ""
    project: str = ""
    # The invite body/agenda, when the user opened the meeting's
    # detail. Optional — older callers / meetings without a body just
    # omit it and the brief behaves exactly as before.
    body: str = ""
    # Free-text from the user — see PrepBriefRequest. Allows the SA to
    # feed authoritative context the calendar invite and meeting history
    # can't capture.
    user_context: str = ""


@app.post("/prep-brief/from-meeting")
async def prep_brief_from_meeting(req: PrepBriefFromMeetingRequest):
    """Generate a meeting-specific prep brief for a calendar entry.

    Context comes from two independent sources, budgeted separately
    (see services/prep_brief_context.py): the most recent sessions in
    scope, and semantically-retrieved excerpts from this client's
    Knowledge Folder. Documents never come out of the session budget —
    recent calls stay the spine of the brief.

    Returns:
        {
          "markdown": str,            # The brief itself (markdown body)
          "referenced_sessions": [    # Sessions Claude COULD cite —
            {"session_id", "display_name", "started_at"},  # frontend
            ...                       # uses these to render
          ],                          # click-to-jump on the [id]
          "referenced_documents": [   # Knowledge-Folder documents
            {"doc_name", "doc_path",  # Claude COULD cite, as
             "chunk_count",           # [DOC: <doc_name>]. Empty list
             "similarity"},           # when the client has no
            ...                       # Knowledge Folder — which reads
          ],                          # identically to the old shape.
          "related_count": int,       # citations.
          "document_count": int,      # distinct documents referenced.
          "identified_client": str,   # echoed back so the modal can
          "identified_project": str,  # show the resolved scope.
          "last_meeting_at": str|null # ISO of the most recent prior
        }                             # session in scope (header info).
    """
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400,
                            detail="AI provider not configured")

    sessions = svc.session_svc.list_sessions()
    related = []
    for s in sessions:
        if not s.get("has_summary") and not s.get("has_transcript"):
            continue
        if req.client and (s.get("client") or "") != req.client:
            continue
        if req.project and (s.get("project") or "") != req.project:
            continue
        related.append(s)

    # Sort by started_at desc so the brief sees the most recent context
    # first — the LLM prioritises the head of its context. Cap at
    # MAX_CONTEXT_SESSIONS (8) to keep this half of the prompt under
    # 6-8 KB, comfortably inside any provider's context limit. Document
    # retrieval below has its own separate budget and never eats into
    # this one.
    related.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    related = related[:_PREP_MAX_SESSIONS]

    if not related:
        # No client-scoped material AND no project-scoped material —
        # fall back to the most recent processed sessions across the
        # entire corpus. Less precise but still gives the user
        # something actionable rather than a blank brief.
        related = [s for s in sessions
                   if s.get("has_summary")][:_PREP_MAX_FALLBACK_SESSIONS]

    # Knowledge-Folder retrieval. Gated on a resolved client: documents
    # carry a client and nothing else, so without one there is no way to
    # keep another account's SOW out of this brief — an unscoped pull
    # would be actively harmful, not merely noisy. Retrieval-only; this
    # never indexes or touches the source folder. Off-thread because
    # SearchService is CPU-bound numpy plus a sentence-transformers
    # encode. Returns [] on every failure path, so a missing folder,
    # empty index, disconnected Drive or absent embedding model all
    # land on "brief exactly as it was before".
    doc_hits = await asyncio.to_thread(
        _prep_retrieve_documents,
        svc.search_svc,
        req.client,
        req.subject,
        req.project,
        req.body or "",
        req.user_context,
    )

    if not related and not doc_hits:
        return {
            "markdown": (
                "_No prior meetings with summaries are available yet. "
                "Process a few sessions and the brief will have material "
                "to work from._"
            ),
            "referenced_sessions": [],
            "referenced_documents": [],
            "related_count": 0,
            "document_count": 0,
            "identified_client": req.client,
            "identified_project": req.project,
            "last_meeting_at": None,
        }

    # Build the prior_notes blob with session_id headers Claude can
    # cite back. The frontend's [id] regex matcher pulls these out
    # later for click-to-jump rendering.
    parts = []
    for s in related:
        sid = s.get("session_id") or ""
        date = (s.get("started_at") or "")[:10]
        title = s.get("display_name") or "Untitled meeting"
        block = [f"### [{sid}] {title}  ({date})"]
        if s.get("summary"):
            block.append(f"**Summary:**\n{s['summary']}")
        if s.get("action_items"):
            block.append(f"**Action Items:**\n{s['action_items']}")
        if s.get("decisions"):
            block.append(f"**Decisions:**\n{s['decisions']}")
        parts.append("\n\n".join(block))

    # Open commitments for this scope — the brief's "Open commitments"
    # section pulls directly from the tracker rather than relying on
    # Claude to re-derive them from action_items markdown. Cleaner +
    # more accurate, since each entry already has a verbatim quote
    # and resolved due date that survived the original extraction.
    if svc.commitments_svc:
        try:
            open_commits = await asyncio.to_thread(
                svc.commitments_svc.list_all,
                req.client or None,
                req.project or None,
                ["active"],   # awaiting + overdue
                None,
                None,
            )
        except Exception as e:
            logger.warning(f"Open-commitments lookup failed: {e}")
            open_commits = []
    else:
        open_commits = []

    if open_commits:
        commit_lines = []
        for c in open_commits[:20]:  # safety cap
            sid = c.get("session_id") or ""
            owner = c.get("owner", "") or "Unknown"
            desc = c.get("description", "") or ""
            due = c.get("due_date_iso") or "no deadline"
            overdue = " (OVERDUE)" if c.get("is_overdue") else ""
            commit_lines.append(
                f"- [{sid}] {owner}: {desc} (due {due}){overdue}"
            )
        parts.append(
            "### Open commitments still outstanding\n"
            + "\n".join(commit_lines)
        )

    prior_notes = "\n\n---\n\n".join(parts)
    if not prior_notes:
        # Reachable only on the new "documents but no sessions" path —
        # a client whose Knowledge Folder is indexed but who has no
        # processed meetings yet. Say so explicitly rather than handing
        # the model an empty section it might hallucinate into.
        prior_notes = "(No prior meetings with this client yet.)"

    # Friendly time string for the prompt — Claude does better with
    # natural-language dates than ISO timestamps.
    when_blob = req.scheduled_start_iso or "(time not specified)"
    if req.scheduled_start_iso:
        try:
            dt = datetime.fromisoformat(req.scheduled_start_iso)
            when_blob = dt.strftime("%A %b %d at %I:%M %p")
        except ValueError:
            pass

    try:
        markdown = await svc.summarizer.meeting_prep_brief_from_calendar(
            upcoming_subject=req.subject,
            upcoming_attendees=req.attendees,
            upcoming_when=when_blob,
            identified_client=req.client,
            identified_project=req.project,
            prior_notes=prior_notes,
            agenda=req.body or "",
            user_context=req.user_context,
            # "" when nothing was retrieved — the summarizer then emits
            # the exact prompt it emitted before documents existed.
            document_notes=_prep_format_documents(doc_hits),
            # A brief is written NOW about a meeting in the FUTURE, so
            # today is the anchor a relative reference in the prior
            # notes or the agenda resolves against.
            today_iso=datetime.now().date().isoformat(),
        )
    except Exception as e:
        logger.exception("Calendar prep brief failed")
        raise HTTPException(status_code=500, detail=str(e))

    ref_docs = _prep_referenced_documents(doc_hits)
    return {
        "markdown": markdown,
        "referenced_sessions": [
            {
                "session_id": s.get("session_id"),
                "display_name": s.get("display_name") or "Untitled meeting",
                "started_at": s.get("started_at"),
            }
            for s in related
        ],
        # Document equivalent of referenced_sessions, so the UI can show
        # which Knowledge-Folder files the brief was allowed to draw on
        # and render `[DOC: <name>]` citations as document chips rather
        # than as broken session links. Empty list when the client has
        # no indexed folder.
        "referenced_documents": ref_docs,
        "related_count": len(related),
        "document_count": len(ref_docs),
        "identified_client": req.client,
        "identified_project": req.project,
        "last_meeting_at": (related[0].get("started_at") if related else None),
    }


@app.post("/prep-brief")
async def prep_brief(req: PrepBriefRequest):
    """Manual prep brief (Prep Brief tab): subject + optional
    client/project scope.

    Same two-source context as /prep-brief/from-meeting — recent
    in-scope sessions plus retrieved Knowledge-Folder excerpts — with a
    thinner query, since this entry point has no invite body and no
    attendees to work from (see services/prep_brief_context.py)."""
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="Anthropic API key required")
    sessions = svc.session_svc.list_sessions()
    # Filter: when client+project are both set we AND them (project always
    # belongs to a client). When only one is set, filter by that alone.
    related = []
    for s in sessions:
        if req.client and s.get("client") != req.client:
            continue
        if req.project and s.get("project") != req.project:
            continue
        if req.client or req.project:
            related.append(s)
    if not related:
        # Fallback: use the 8 most recent processed sessions
        related = [s for s in sessions
                   if s.get("has_summary")][:_PREP_MAX_SESSIONS]

    # Knowledge-Folder retrieval, keyed on the client exactly as in
    # /prep-brief/from-meeting. Deliberately NOT run for the unscoped
    # fallback: with no client there is nothing to filter documents by,
    # and pulling another account's SOW into this brief would be worse
    # than no documents at all. Never raises — [] on every failure.
    doc_hits = await asyncio.to_thread(
        _prep_retrieve_documents,
        svc.search_svc,
        req.client,
        req.subject,
        req.project,
        "",                 # no invite body on this entry point
        req.user_context,
    )

    if not related and not doc_hits:
        return {"brief": "No prior meetings with summaries found to brief from.",
                "related_count": 0,
                "referenced_documents": [],
                "document_count": 0}

    # Build context blob
    parts = []
    for s in related[:_PREP_MAX_SESSIONS]:
        block = [f"### {s.get('display_name', 'Meeting')} "
                 f"({(s.get('started_at') or '')[:10]})"]
        if s.get("summary"):
            block.append(f"**Summary:**\n{s['summary']}")
        if s.get("action_items"):
            block.append(f"**Action Items:**\n{s['action_items']}")
        if s.get("decisions"):
            block.append(f"**Decisions:**\n{s['decisions']}")
        parts.append("\n\n".join(block))
    prior_notes = "\n\n---\n\n".join(parts)
    if not prior_notes:
        prior_notes = "(No prior meetings with this client yet.)"

    ref_docs = _prep_referenced_documents(doc_hits)
    try:
        brief = await svc.summarizer.meeting_prep_brief(
            prior_notes, req.subject, user_context=req.user_context,
            # "" when nothing was retrieved — the summarizer then emits
            # the exact prompt it emitted before documents existed.
            document_notes=_prep_format_documents(doc_hits),
            # See the from-meeting endpoint: today anchors any relative
            # date the prior notes carry.
            today_iso=datetime.now().date().isoformat())
        return {"brief": brief,
                "related_count": len(related),
                "referenced_documents": ref_docs,
                "document_count": len(ref_docs)}
    except Exception as e:
        logger.exception("Prep brief failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Retention ────────────────────────────────────────────────────────
@app.get("/retention/stats")
async def retention_stats():
    s = svc.load_settings()
    # folder_stats walks the recordings dir (stat() per file). Off-loop
    # so a large library can't block the event loop / trip the Tauri
    # backend watchdog.
    return await asyncio.to_thread(folder_stats, s.recordings_dir)


@app.post("/retention/cleanup")
async def retention_cleanup(processed_days: int = 7, unprocessed_days: int = 30):
    s = svc.load_settings()
    # The cleanup walks every session JSON and unlinks WAVs — seconds to
    # minutes on a big library. Running it synchronously in this async
    # handler blocked the whole event loop, so the Tauri shell's backend
    # watchdog saw the server as unresponsive, killed + respawned it, and
    # the in-flight request died as a "failed to fetch" in the UI. Run it
    # in a worker thread instead.
    return await asyncio.to_thread(
        run_retention_cleanup,
        s.recordings_dir,
        processed_days=processed_days,
        unprocessed_days=unprocessed_days,
        client_export_dirs=_client_export_dirs(),
    )


def _scan_ghost_sessions(recordings_dir: str) -> list[dict]:
    """Enumerate session_*.json files whose audio_path target doesn't
    exist on disk.

    "Ghost sessions" are JSON stubs that v2.11.1's start-of-recording
    write left behind when the backend crashed mid-recording or
    mid-finalize. Each one shows up in the Sessions list and will fail
    to process (no WAV to transcribe). Field repro 2026-06-26: 69 of
    them accumulated on one machine over a few weeks.

    Returns ``[{session_id, display_name, json_path, json_mtime_iso,
    age_days, audio_path}]`` sorted oldest-first so a UI cleanup picker
    can show the worst offenders at the top."""
    from pathlib import Path as _P
    rec_dir = _P(recordings_dir)
    if not rec_dir.exists():
        return []
    now = datetime.now()
    out: list[dict] = []
    for json_path in rec_dir.glob("session_*.json"):
        # Sidecars (session_<id>.commitments.json etc.) share the glob;
        # the dotted stem distinguishes them.
        if json_path.stem.count(".") > 0:
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            # Unreadable JSON is its own problem; skip — retention
            # cleanup handles those separately.
            continue
        audio_path = data.get("audio_path") or ""
        # A populated audio_path that doesn't exist on disk is the
        # ghost-session signature. (An empty audio_path means the
        # session was created but never paired with a WAV — also a
        # ghost, but the recover_orphans path handles those.)
        if not audio_path or _P(audio_path).exists():
            continue
        try:
            mtime = datetime.fromtimestamp(json_path.stat().st_mtime)
        except OSError:
            mtime = now
        out.append({
            "session_id": data.get("session_id") or
                          json_path.stem.replace("session_", ""),
            "display_name": data.get("display_name") or "",
            "json_path": str(json_path),
            "json_mtime_iso": mtime.isoformat(),
            "age_days": (now - mtime).days,
            "audio_path": audio_path,
        })
    out.sort(key=lambda r: r["age_days"], reverse=True)
    return out


# Stubs older than this auto-purge at backend startup. 14 days picks a
# point well past any plausible "OneDrive is still syncing this back"
# window without surprising a user who paused syncing for a long
# vacation. Tunable via settings if needed; default matches the
# retention story (processed_days=7 → kept; ghost stubs → kept 2x as
# long since they're cheap to clean up if we're wrong).
_GHOST_AUTO_PURGE_AGE_DAYS = 14


@app.get("/ghost-sessions")
async def list_ghost_sessions():
    """List session JSONs whose audio file is missing on disk. Returned
    list is sorted oldest first so the UI can show "delete the worst
    offenders" without sorting. See ``_scan_ghost_sessions``."""
    s = svc.load_settings()
    items = await asyncio.to_thread(
        _scan_ghost_sessions, s.recordings_dir)
    return {
        "count": len(items),
        "auto_purge_age_days": _GHOST_AUTO_PURGE_AGE_DAYS,
        "items": items,
    }


class GhostSessionDeleteRequest(BaseModel):
    # Either: explicit list of session_ids the UI picked,
    # OR: a min_age_days threshold (everything older than this).
    session_ids: Optional[list[str]] = None
    min_age_days: Optional[int] = None


@app.delete("/ghost-sessions")
async def delete_ghost_sessions(req: GhostSessionDeleteRequest):
    """Bulk-delete ghost session JSONs (and their sidecar files).
    Refuses to touch a session JSON whose audio_path EXISTS on disk —
    that would be deleting a real recording, not a ghost.

    Accepts either an explicit ``session_ids`` list (what the UI picker
    sends) or a ``min_age_days`` cutoff (the auto-purge path uses 14)."""
    s = svc.load_settings()
    candidates = await asyncio.to_thread(
        _scan_ghost_sessions, s.recordings_dir)

    if req.session_ids:
        wanted = set(req.session_ids)
        candidates = [c for c in candidates if c["session_id"] in wanted]
    elif req.min_age_days is not None:
        candidates = [
            c for c in candidates if c["age_days"] >= int(req.min_age_days)
        ]
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either session_ids or min_age_days",
        )

    from pathlib import Path as _P
    deleted: list[str] = []
    errors: list[dict] = []
    for c in candidates:
        json_path = _P(c["json_path"])
        sid = c["session_id"]
        # Defence in depth: re-check audio_path doesn't exist now (the
        # scan was on a worker thread; a parallel sync could have
        # hydrated it). Refuse the delete if the WAV actually exists.
        audio = c.get("audio_path") or ""
        if audio and _P(audio).exists():
            errors.append({
                "session_id": sid,
                "error": "audio file exists now — not deleting",
            })
            continue
        # Delete the JSON + every sidecar (session_<id>.commitments.json,
        # session_<id>.item_status.json, etc.)
        stem = f"session_{sid}"
        try:
            for sidecar in json_path.parent.glob(f"{stem}.*"):
                try:
                    sidecar.unlink()
                except OSError as e:
                    errors.append({
                        "session_id": sid,
                        "error": f"sidecar {sidecar.name}: {e}",
                    })
            deleted.append(sid)
        except Exception as e:
            errors.append({"session_id": sid, "error": str(e)})

    logger.info(
        f"Deleted {len(deleted)} ghost session(s); "
        f"{len(errors)} error(s)")
    return {"deleted": deleted, "errors": errors}


def _client_export_dirs() -> list[str]:
    """Folders retention should sweep for orphaned recorder copies.

    v2.19+ never writes the raw WAV to a network folder (only derived
    text artifacts go there, and text is tiny + doesn't need retention
    sweeping), so we only enumerate explicit Designated Folders that
    predate this change and may still hold copies from older builds.
    New copies from v2.19+ never leave local disk, so this list is
    only meaningful for cleaning up legacy WAVs."""
    try:
        if not svc.client_cfg_svc:
            return []
        return [
            cfg.export_folder
            for cfg in svc.client_cfg_svc.get_all().values()
            if getattr(cfg, "export_folder", "")
        ]
    except Exception as e:
        logger.warning(f"Could not enumerate client export dirs: {e}")
        return []


# Re-check roughly twice a day. Retention is day-granular so this is
# plenty; the first pass runs ~1 min after startup so a freshly-opened
# app reclaims space without waiting out the whole interval.
RETENTION_INTERVAL_S = 12 * 3600


async def _retention_loop():
    """Background auto-retention. The `retention_enabled` setting existed
    and was persisted, but nothing ever consumed it — so 'automatic
    cleanup' silently never happened. This loop is that missing piece.

    Reads settings fresh from disk each cycle so toggling the setting
    (or changing the day thresholds) takes effect on the next pass
    without an app restart."""
    await asyncio.sleep(60)  # let startup prewarm settle first
    try:
        while True:
            try:
                s = Settings.from_env()
                if (s.retention_enabled
                        and (s.retention_processed_days > 0
                             or s.retention_unprocessed_days > 0)):
                    res = await asyncio.to_thread(
                        run_retention_cleanup,
                        s.recordings_dir,
                        processed_days=s.retention_processed_days,
                        unprocessed_days=s.retention_unprocessed_days,
                        client_export_dirs=_client_export_dirs(),
                    )
                    if res.get("deleted_count"):
                        logger.info(
                            f"Auto-retention: deleted "
                            f"{res['deleted_count']} file(s), freed "
                            f"{res.get('bytes_freed', 0)} bytes")
            except Exception as e:
                logger.warning(f"Auto-retention pass failed: {e}")
            await asyncio.sleep(RETENTION_INTERVAL_S)
    except asyncio.CancelledError:
        raise


# ── Templates ────────────────────────────────────────────────────────
class TemplateDTO(BaseModel):
    name: str
    prompt: str
    is_default: bool = False
    # Only populated for defaults; null for user-created templates.
    default_prompt: Optional[str] = None


@app.get("/templates")
async def get_templates():
    """
    Full template list with prompts. The frontend Record view + Session
    Detail use only the `name` field for the dropdown; the Settings page
    Templates editor renders the full entry.
    """
    svc.load_settings()
    def _do():
        return [
            {
                "name": t.name,
                "prompt": t.prompt,
                "is_default": t.is_default,
                "default_prompt": t.default_prompt,
            }
            for t in svc.template_svc.list_all()
        ]
    return await asyncio.to_thread(_do)


class TemplateUpsertRequest(BaseModel):
    prompt: str


@app.put("/templates/{name}")
async def put_template(name: str, req: TemplateUpsertRequest):
    svc.load_settings()
    def _do():
        return svc.template_svc.upsert(name, req.prompt)
    try:
        t = await asyncio.to_thread(_do)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "name": t.name,
        "prompt": t.prompt,
        "is_default": t.is_default,
        "default_prompt": t.default_prompt,
    }


@app.delete("/templates/{name}")
async def delete_template(name: str):
    svc.load_settings()
    await asyncio.to_thread(svc.template_svc.delete, name)
    return {"ok": True}


@app.post("/templates/{name}/reset")
async def reset_template(name: str):
    """Restore a default template's prompt to its shipped text."""
    svc.load_settings()
    def _do():
        return svc.template_svc.reset(name)
    t = await asyncio.to_thread(_do)
    if not t:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' isn't a default template — nothing to reset to.",
        )
    return {
        "name": t.name,
        "prompt": t.prompt,
        "is_default": t.is_default,
        "default_prompt": t.default_prompt,
    }


# ── Co-Pilot mode + meeting-type library ─────────────────────────────
#
# Same CRUD shape as /templates above. Two parallel resources because
# modes and meeting types compose multiplicatively (3 modes × 7 types
# = 21 working combinations from 10 editable prompts).

def _mode_dict(m) -> dict:
    return {
        "name": m.name, "prompt": m.prompt,
        "is_default": m.is_default, "default_prompt": m.default_prompt,
    }


class CoPilotPromptUpsertRequest(BaseModel):
    prompt: str


@app.get("/copilot/modes")
async def get_copilot_modes():
    svc.load_settings()
    if not svc.copilot_mode_svc:
        return []
    return await asyncio.to_thread(
        lambda: [_mode_dict(m) for m in svc.copilot_mode_svc.list_all()])


@app.put("/copilot/modes/{name}")
async def put_copilot_mode(name: str, req: CoPilotPromptUpsertRequest):
    svc.load_settings()
    if not svc.copilot_mode_svc:
        raise HTTPException(status_code=503, detail="Mode service not initialized")
    try:
        m = await asyncio.to_thread(svc.copilot_mode_svc.upsert, name, req.prompt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _mode_dict(m)


@app.delete("/copilot/modes/{name}")
async def delete_copilot_mode(name: str):
    svc.load_settings()
    if not svc.copilot_mode_svc:
        raise HTTPException(status_code=503, detail="Mode service not initialized")
    await asyncio.to_thread(svc.copilot_mode_svc.delete, name)
    return {"ok": True}


@app.post("/copilot/modes/{name}/reset")
async def reset_copilot_mode(name: str):
    svc.load_settings()
    if not svc.copilot_mode_svc:
        raise HTTPException(status_code=503, detail="Mode service not initialized")
    m = await asyncio.to_thread(svc.copilot_mode_svc.reset, name)
    if not m:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' isn't a default mode — nothing to reset to.")
    return _mode_dict(m)


@app.get("/copilot/meeting-types")
async def get_copilot_meeting_types():
    svc.load_settings()
    if not svc.copilot_meeting_type_svc:
        return []
    return await asyncio.to_thread(
        lambda: [_mode_dict(m) for m in svc.copilot_meeting_type_svc.list_all()])


@app.put("/copilot/meeting-types/{name}")
async def put_copilot_meeting_type(name: str, req: CoPilotPromptUpsertRequest):
    svc.load_settings()
    if not svc.copilot_meeting_type_svc:
        raise HTTPException(status_code=503, detail="Meeting-type service not initialized")
    try:
        m = await asyncio.to_thread(
            svc.copilot_meeting_type_svc.upsert, name, req.prompt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _mode_dict(m)


@app.delete("/copilot/meeting-types/{name}")
async def delete_copilot_meeting_type(name: str):
    svc.load_settings()
    if not svc.copilot_meeting_type_svc:
        raise HTTPException(status_code=503, detail="Meeting-type service not initialized")
    await asyncio.to_thread(svc.copilot_meeting_type_svc.delete, name)
    return {"ok": True}


@app.post("/copilot/meeting-types/{name}/reset")
async def reset_copilot_meeting_type(name: str):
    svc.load_settings()
    if not svc.copilot_meeting_type_svc:
        raise HTTPException(status_code=503, detail="Meeting-type service not initialized")
    m = await asyncio.to_thread(svc.copilot_meeting_type_svc.reset, name)
    if not m:
        raise HTTPException(
            status_code=400,
            detail=f"'{name}' isn't a default meeting type — nothing to reset to.")
    return _mode_dict(m)


# ── Lightweight setter for the active mode/type ──────────────────────
# Mirrors /settings/live-copilot — flips just these two fields without
# rebuilding RecordingService (which the full POST /settings does),
# so the panel dropdown can change them mid-recording without orphaning
# capture threads.

class CoPilotActiveModeRequest(BaseModel):
    mode: Optional[str] = None
    meeting_type: Optional[str] = None


@app.post("/settings/copilot-active")
async def set_copilot_active(req: CoPilotActiveModeRequest):
    """Update the active co-pilot mode + meeting type. Either field
    optional — pass only the one you're changing. Persists to
    config.env so the choice survives restarts."""
    # Local import — `dataclasses` isn't imported at module level.
    # The sibling `/settings/live-copilot` endpoint imports it the
    # same way (line 2155). v2.9.0 shipped without this import here,
    # producing a NameError on every mid-recording mode/type change.
    import dataclasses
    s = svc.load_settings()
    new_mode = (req.mode or s.live_copilot_mode or "SA").strip()
    new_type = (req.meeting_type or s.live_copilot_meeting_type or "General").strip()
    Settings.save_to_env(
        anthropic_api_key=s.anthropic_api_key,
        hf_token=s.hf_token,
        whisper_model=s.whisper_model,
        max_speakers=s.max_speakers,
        recordings_dir=s.recordings_dir,
        email_to=s.email_to,
        claude_model=s.claude_model,
        notify_minutes_before=s.notify_minutes_before,
        auto_process_after_stop=s.auto_process_after_stop,
        launch_on_startup=s.launch_on_startup,
        auto_follow_up_email=s.auto_follow_up_email,
        retention_enabled=s.retention_enabled,
        retention_processed_days=s.retention_processed_days,
        retention_unprocessed_days=s.retention_unprocessed_days,
        ai_provider=s.ai_provider,
        openai_api_key=s.openai_api_key,
        openai_base_url=s.openai_base_url,
        live_transcription_enabled=s.live_transcription_enabled,
        auto_screenshot_interval_minutes=s.auto_screenshot_interval_minutes,
        silence_warn_min=s.silence_warn_min,
        silence_stop_min=s.silence_stop_min,
        overrun_warn_min=s.overrun_warn_min,
        overrun_stop_min=s.overrun_stop_min,
        hard_cap_hours=s.hard_cap_hours,
        auto_record_enabled=s.auto_record_enabled,
        live_copilot_enabled=s.live_copilot_enabled,
        live_ai_provider=s.live_ai_provider,
        live_claude_model=s.live_claude_model,
        live_openai_api_key=s.live_openai_api_key,
        live_openai_base_url=s.live_openai_base_url,
        live_anthropic_api_key=s.live_anthropic_api_key,
        live_copilot_mode=new_mode,
        live_copilot_meeting_type=new_type,
        live_copilot_wide_interval_sec=s.live_copilot_wide_interval_sec,
        live_copilot_hot_interval_sec=s.live_copilot_hot_interval_sec,
        copilot_custom_context=s.copilot_custom_context,
        today_view_enabled=s.today_view_enabled,
        auto_prep_brief_enabled=s.auto_prep_brief_enabled,
        auto_prep_brief_lead_min=s.auto_prep_brief_lead_min,
        cloud_mirror_dir=s.cloud_mirror_dir,
        session_archive_dir=s.session_archive_dir,
        live_vad_enabled=s.live_vad_enabled,
        live_speaker_split_enabled=s.live_speaker_split_enabled,
        diarization_device=s.diarization_device,
        audio_mix_format_lookup_enabled=s.audio_mix_format_lookup_enabled,
        echo_cancellation_enabled=s.echo_cancellation_enabled,
        session_index_enabled=s.session_index_enabled,
        channel_attribution_enabled=s.channel_attribution_enabled,
        calendar_source=s.calendar_source,
    )
    svc.settings = dataclasses.replace(
        s, live_copilot_mode=new_mode, live_copilot_meeting_type=new_type)
    return {"mode": new_mode, "meeting_type": new_type}


# ── Daily Briefing (Today view) ─────────────────────────────────────
#
# The user runs a Microsoft 365 Copilot scheduled prompt every morning
# that produces a free-form briefing (priorities, today's agenda,
# items awaiting response, FYI). M365 Copilot exposes no API surface
# for scheduled-prompt output, so the integration is intentionally
# manual: user copies the output text → pastes here → LLM parses it
# into structured JSON → DailyBriefingService stores one parsed file
# per calendar date.
#
# Re-importing the same date merges done-state from the prior import
# so a mid-morning re-paste doesn't wipe action items the user has
# already checked off.

class BriefingImportRequest(BaseModel):
    text: str
    date: Optional[str] = None  # YYYY-MM-DD; defaults to today


class BriefingActionUpdateRequest(BaseModel):
    done: bool


@app.post("/briefing/import")
async def import_daily_briefing(req: BriefingImportRequest):
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503, detail="Briefing service not initialized")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty briefing text")
    if len(text) > 50000:
        # Generous cap — a real briefing is a few KB at most; anything
        # this large is either pasted email chain or a mistake.
        raise HTTPException(status_code=400, detail="Briefing too large (>50KB)")

    # Use the MAIN summarizer (Anthropic / quality model), not the live
    # co-pilot one. Briefing parsing is a single ~2k-token call a few
    # times a day — same quality bar as post-meeting summaries — so it
    # belongs on the main provider, not the live tick model (which is
    # often a cheap Ollama / OpenRouter free-tier model picked because
    # ticks fire constantly). Fall back to live_summarizer only if the
    # user hasn't configured a main provider at all.
    summ = svc.summarizer or svc.live_summarizer
    if summ is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "No AI provider configured. Set an API key + model in "
                "Settings (or configure a Live Co-Pilot provider) "
                "before importing a briefing."
            ))
    try:
        parsed = await summ.parse_daily_briefing(text)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    stored = await asyncio.to_thread(
        svc.daily_briefing_svc.save_parsed, parsed, text, req.date)
    return stored


# ── Chrome-extension import (replaces the doomed Playwright path).
#
# Background: the Playwright-driven OWA scrape (services/outlook_web_
# scraper.py) ran headed Chrome via a persistent profile and hit
# Microsoft's enterprise-tenant automation detection every time. No
# combination of stealth flags or off-screen positioning worked —
# Microsoft binds session tokens to the original browser identity, and
# Playwright's Chrome is a different identity from the user's normal
# Chrome regardless of any flag we pass. Result: every Sync attempt
# bounced to login.microsoftonline.com and produced an empty brief.
#
# The fix: a small Chrome extension running in the USER'S real Chrome
# (chrome-extension/ at repo root). The user clicks the extension's
# toolbar icon, the extension opens OWA + Teams tabs in that same
# Chrome (Microsoft trusts it because it IS the user's real browser),
# extracts the [role=main] inner text, POSTs the result here.
#
# We then run the SAME LLM-parser pipeline the manual paste-import
# uses. Same storage shape, same Today-tab rendering, same action-
# item toggling — only the data path changes.

class ExtensionImportRequest(BaseModel):
    owa_text: Optional[str] = ""
    teams_text: Optional[str] = ""
    # v1.1 of the extension also captures the focused inbox and the
    # Teams Chat surface so the brief reflects "what's actually waiting
    # for me" — most needs_response items live in email, and Chat
    # surfaces unread DMs that the Activity feed doesn't catch.
    inbox_text: Optional[str] = ""
    chat_text: Optional[str] = ""
    date: Optional[str] = None  # YYYY-MM-DD; defaults to today
    # v1.2: structured calendar events the extension parsed CLIENT-SIDE
    # out of Outlook Web's own aria-label accessibility strings (week
    # view, current + next — see chrome-extension/background.js
    # parseMeetingLabel / extractEventsFromCandidates). No LLM on this
    # path. Each item: {subject, start, end, location?, organizer?,
    # join_url?, attendees?}. When present this is the PREFERRED source
    # for the Record tab's calendar store (see below); an un-upgraded
    # extension simply never sends it and the old owa_text -> LLM ->
    # events_from_briefing path is used exactly as before.
    calendar_events: Optional[List[Dict[str, Any]]] = None
    # Last-resort fallback text for the calendar ONLY, sent when the
    # extension's structured DOM scan found nothing (Outlook Web
    # redesign, etc). Distinct from owa_text (which is the DAY view,
    # feeds the Today-tab narrative, and is always LLM-parsed +
    # persisted as today's briefing) — calendar_text is the WEEK
    # view's raw text and, when used, is parsed WITHOUT touching the
    # saved briefing (see the calendar-only branch below).
    calendar_text: Optional[str] = ""
    # Self-reported extension version — chrome.runtime.getManifest()
    # .version, added in 1.2.0 (see chrome-extension/background.js).
    # Absent on an un-upgraded extension; recorded as such rather than
    # assumed current — see ExtensionCalendarService.
    # record_extension_version / extension_bundle_service.
    # extension_version_status.
    extension_version: Optional[str] = None
    # Counts and booleans describing what the extension's calendar
    # RESPONSE RECORDER did on this capture — whether it registered,
    # whether it installed, how many responses it saw, how many held a
    # meeting, and how many events actually gained attendees / body /
    # join URL from them. No URL, subject, attendee or body text.
    #
    # Stored so the diagnostics bundle can answer WHICH failure a
    # field-empty report is. v1.7 computed these and kept them inside
    # the extension, so an empty attendee list looked identical whether
    # the recorder never installed, saw nothing, or saw traffic it
    # could not read — three causes needing three different fixes.
    # Free-form dict rather than a model: it is opaque diagnostic
    # payload, and a schema here would reject a newer extension's extra
    # counter rather than pass it through.
    capture_diag: Optional[Dict[str, Any]] = None


# Two calendar-parse paths (events_from_structured / events_from_briefing
# — see services/extension_calendar_service.py) produce IDENTICALLY
# shaped output, so neither the stored JSON nor a bare "kept N events"
# log line reveals which one ran. That ambiguity directly caused
# several wrong diagnoses (field report chain culminating 2026-08-14).
# These two helpers format the extra "Extension calendar: path=..."
# line every import branch below emits, so a single grep
# (`grep "Extension calendar: path="`) always answers "which path, how
# many raw, how many kept, how many dropped and why".

def _extension_calendar_fallback_note(fallback_reason: Optional[str]) -> str:
    """Human-readable clause explaining WHY a briefing-fallback import
    fell back — distinct wording for "extension sent no calendar_events
    key at all" (absent — an old extension, or a capture that threw
    before building a payload) vs. "extension sent an empty list"
    (empty — a current extension whose structured DOM scan ran and
    found zero candidates). Those mean different things and must not
    collapse into one message."""
    if fallback_reason == "absent":
        return "extension sent no structured events"
    if fallback_reason == "empty":
        return "extension's structured scan found zero events"
    return fallback_reason or ""


def _extension_calendar_dropped_detail(stats: Dict[str, Any]) -> str:
    """" (reason: N, reason2: M)" for every non-zero dropped_* key in a
    stats dict from events_from_structured/events_from_briefing, or ""
    if nothing was dropped. Pure formatting, never raises."""
    parts = []
    for key, value in (stats or {}).items():
        if key.startswith("dropped_") and value:
            label = key[len("dropped_"):].replace("_", " ")
            parts.append(f"{label}: {value}")
    return f" ({', '.join(parts)})" if parts else ""


def _emit_calendar_import_event(
    log_path: str,
    stats: Dict[str, Any],
    dropped: int,
    fallback_reason: Optional[str],
    extension_version: Optional[str],
    *,
    calendar_only: bool,
) -> None:
    """Structured twin of the ``Extension calendar: path=...`` line.

    Counts and reason codes only. Deliberately NOT emitted: any event
    title, organiser, attendee or join URL — the whole payload this
    endpoint receives is meeting content, and the only thing a
    diagnosis has ever needed from it is which parser ran and how many
    events survived it.
    """
    try:
        drop_reasons = {
            k[len("dropped_"):]: int(v)
            for k, v in (stats or {}).items()
            if k.startswith("dropped_") and v
        }
        events.emit(
            events.CALENDAR_IMPORT,
            path=log_path,
            calendar_only=bool(calendar_only),
            raw=int((stats or {}).get("raw") or 0),
            kept=int((stats or {}).get("kept") or 0),
            dropped=int(dropped or 0),
            drop_reasons=drop_reasons,
            fallback_reason=fallback_reason,
            extension_version=extension_version,
        )
    except Exception:
        pass


@app.post("/briefing/extension-import")
async def import_briefing_from_extension(req: ExtensionImportRequest):
    """Receives scraped OWA + Teams text from the Chrome extension,
    runs it through the same parse_daily_briefing pipeline /briefing/
    import uses, stores as today's briefing.

    The extension scrapes in the user's REAL Chrome — Microsoft's
    automation detection doesn't fire there — so the request lands
    with real, populated content. If both blobs are empty we 400; if
    only Teams is empty we publish an OWA-only brief (same fallback
    shape the v2.15.x Playwright path used).

    v1.2: also accepts `calendar_events` (structured, client-parsed —
    see ExtensionImportRequest) as a THIRD, preferred input alongside
    the four narrative text fields. A request carrying ONLY calendar
    data (the periodic calendar-refresh alarm in background.js — see
    its header comment) takes a fast path below that updates the
    Record tab's calendar store and returns WITHOUT touching today's
    saved briefing or spending an LLM call — that alarm fires every
    30 minutes and must not silently overwrite the day's real greeting
    / top_priority / needs_response with a partial calendar-only
    parse."""
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503,
                            detail="Briefing service not initialized")

    owa_text = (req.owa_text or "").strip()
    teams_text = (req.teams_text or "").strip()
    inbox_text = (req.inbox_text or "").strip()
    chat_text = (req.chat_text or "").strip()
    calendar_events_in = [e for e in (req.calendar_events or [])
                          if isinstance(e, dict)]
    calendar_text = (req.calendar_text or "").strip()
    narrative_present = any((owa_text, teams_text, inbox_text, chat_text))

    # Record the extension's self-reported version on EVERY POST that
    # reaches here, regardless of which branch below runs or whether it
    # ultimately produces anything usable — a stale extension that
    # fails to produce anything is exactly the case this needs to catch.
    # Best-effort: must never fail the import it's piggybacking on.
    if svc.extension_calendar_svc:
        try:
            await asyncio.to_thread(
                svc.extension_calendar_svc.record_extension_version,
                req.extension_version)
            if req.capture_diag is not None:
                await asyncio.to_thread(
                    svc.extension_calendar_svc.record_capture_diag,
                    req.capture_diag)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Extension version bookkeeping failed: {e}")

    # ── Calendar-only fast path ──────────────────────────────────────
    # No narrative text at all: this is the calendar-refresh alarm, not
    # a full "Capture & Send". Update ONLY the calendar store.
    if not narrative_present and (calendar_events_in or calendar_text):
        if not svc.extension_calendar_svc:
            raise HTTPException(status_code=503,
                                detail="Calendar store not initialized")
        stats: Dict[str, Any] = {}
        fallback_reason = None
        if calendar_events_in:
            events = events_from_structured(calendar_events_in, stats=stats)
            path = "structured"
        else:
            # Structured extraction found nothing client-side — last-
            # resort LLM fallback, but scoped to events only: we parse
            # it the same way a real briefing would be parsed, then
            # discard everything except the agenda. Today's saved
            # briefing (greeting/top_priority/needs_response/fyi) is
            # never touched by this branch.
            #
            # WHY it fell back matters and is logged below: "absent"
            # (req.calendar_events was never sent — an old extension,
            # or a capture that threw before building a payload) and
            # "empty" (the extension DID run its structured scan and
            # found zero candidates this time) point at completely
            # different problems and were previously indistinguishable
            # from server-side state alone (field report chain
            # culminating 2026-08-14).
            fallback_reason = describe_structured_source(req.calendar_events)
            summ = svc.summarizer or svc.live_summarizer
            if summ is None:
                raise HTTPException(
                    status_code=400,
                    detail=("No AI provider configured. Set an API key + "
                            "model in Settings before importing a briefing."))
            try:
                parsed_fallback = await summ.parse_daily_briefing(
                    calendar_text, req.date or "")
            except RuntimeError as e:
                raise HTTPException(status_code=502,
                                    detail=f"LLM parse failed: {e}")
            events = events_from_briefing(
                parsed_fallback, req.date or parsed_fallback.get("date"),
                stats=stats)
            path = "text-fallback"

        dropped = stats.get("raw", len(events)) - stats.get("kept", len(events))
        kept = await asyncio.to_thread(
            svc.extension_calendar_svc.replace_all, events, None, {
                "path": path,
                "raw": stats.get("raw", len(events)),
                "kept": stats.get("kept", len(events)),
                "dropped": dropped,
                "fallback_reason": fallback_reason,
            })
        logger.info(
            f"Calendar-only extension capture ({path}): "
            f"{len(events)} parsed -> {len(kept)} kept")
        log_path = "structured" if path == "structured" else "briefing-fallback"
        fallback_note = f" ({_extension_calendar_fallback_note(fallback_reason)})" \
            if fallback_reason else ""
        logger.info(
            f"Extension calendar: path={log_path}{fallback_note} "
            f"raw={stats.get('raw', 0)} kept={stats.get('kept', 0)} "
            f"dropped={dropped}{_extension_calendar_dropped_detail(stats)}")
        _emit_calendar_import_event(
            log_path, stats, dropped, fallback_reason,
            req.extension_version, calendar_only=True)
        return {
            "ok": True,
            "path": path,
            "parsed_events": len(events),
            "kept_events": len(kept),
        }

    if not (narrative_present or calendar_events_in or calendar_text):
        raise HTTPException(status_code=400,
                            detail="No content sent from extension")

    # ── The calendar lands BEFORE the briefing can fail ──────────────
    #
    # Structured events are parsed CLIENT-side. They need no LLM, and
    # the fast path above already proves the backend knows that. What
    # the full-capture path did was check the LLM gate first — so a
    # Capture & Send with no API key configured, or with an LLM that
    # threw mid-parse, discarded a calendar the extension had just
    # spent ninety seconds opening panes to build (proven end to end
    # against a real extension + real backend, 2026-08-21).
    #
    # The briefing failing is not the calendar failing. Storing here
    # is idempotent with respect to everything below: the briefing
    # branch neither reads nor rewrites the calendar store.
    calendar_stored: Optional[Dict[str, Any]] = None
    if calendar_events_in and svc.extension_calendar_svc:
        cal_stats: Dict[str, Any] = {}
        cal_events = events_from_structured(calendar_events_in,
                                            stats=cal_stats)
        cal_dropped = (cal_stats.get("raw", len(cal_events))
                       - cal_stats.get("kept", len(cal_events)))
        cal_kept = await asyncio.to_thread(
            svc.extension_calendar_svc.replace_all, cal_events, None, {
                "path": "structured",
                "raw": cal_stats.get("raw", len(cal_events)),
                "kept": cal_stats.get("kept", len(cal_events)),
                "dropped": cal_dropped,
                "fallback_reason": None,
            })
        logger.info(
            f"Extension calendar (full capture): {len(cal_events)} parsed "
            f"-> {len(cal_kept)} kept")
        _emit_calendar_import_event(
            "structured", cal_stats, cal_dropped, None,
            req.extension_version, calendar_only=False)
        calendar_stored = {
            "parsed_events": len(cal_events),
            "kept_events": len(cal_kept),
        }

    summ = svc.summarizer or svc.live_summarizer
    if summ is None:
        raise HTTPException(
            status_code=400,
            detail=("No AI provider configured. Set an API key + model "
                    "in Settings before importing a briefing."))

    # Stitch all four labeled sections into one blob for the LLM.
    # v1.1 adds Inbox + Chat (was OWA + Teams only). The LLM-parse
    # prompt treats labeled sections as topic hints, so adding more
    # context here directly improves needs_response / fyi extraction.
    blob_parts = []
    if owa_text:
        blob_parts.append("=== Today's Outlook Calendar ===\n\n" + owa_text)
    if teams_text:
        blob_parts.append(
            "=== Today's Teams Activity (mentions, replies, missed calls) ===\n\n"
            + teams_text)
    if inbox_text:
        # Inbox is the highest-signal needs_response source. Email
        # threads waiting on the user are the canonical "needs response"
        # items in real work.
        blob_parts.append(
            "=== Today's Outlook Focused Inbox (emails — likely needs_response items) ===\n\n"
            + inbox_text)
    if chat_text:
        blob_parts.append(
            "=== Today's Teams Chat (active 1:1 / group conversations) ===\n\n"
            + chat_text)
    blob = "\n\n".join(blob_parts)

    if len(blob.encode("utf-8")) > 50_000:
        # Generous cap — see /briefing/import for rationale.
        raise HTTPException(status_code=400,
                            detail="Briefing too large (>50KB)")

    try:
        parsed = await summ.parse_daily_briefing(blob, req.date or "")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"LLM parse failed: {e}")

    if isinstance(parsed, dict):
        parsed["source"] = "chrome-extension"

    stored = await asyncio.to_thread(
        svc.daily_briefing_svc.save_parsed, parsed, blob, req.date)

    # Second consumer of the same request: the Record tab's Upcoming
    # Meetings panel. Until v2.22.2 the extension's calendar data dead-
    # ended in the Today tab's briefing, so a meeting visible in Outlook
    # Web but absent from local Outlook (the exact case the extension
    # exists to cover — IT policy blocks COM/Graph) could never be used
    # to start a recording. We persist the structured events here and
    # /calendar/upcoming merges them with the local source (field report
    # 2026-08-11). Best-effort: a failure to persist must not fail the
    # briefing import, which is the primary purpose of this endpoint.
    #
    # v1.2: prefer the extension's own structured events (no LLM, no
    # regex-over-text-blob) whenever a manual "Capture & Send" included
    # them alongside the narrative capture. Only fall back to deriving
    # events from the LLM-parsed briefing agenda for an un-upgraded
    # extension that never sends calendar_events at all.
    ext_kept = 0
    ext_log_path = "unavailable"  # svc.extension_calendar_svc missing, or update failed
    if calendar_stored is not None:
        # Already stored above, before the briefing gate — storing the
        # same events twice is pure waste, and the pre-gate write is
        # the one that survives a briefing failure.
        ext_kept = calendar_stored["kept_events"]
        ext_log_path = "structured"
    elif svc.extension_calendar_svc:
        try:
            calendar_stats: Dict[str, Any] = {}
            fallback_reason = None
            if calendar_events_in:
                events = events_from_structured(calendar_events_in, stats=calendar_stats)
                path = "structured"
            else:
                # See the calendar-only fast path above for why "absent"
                # vs. "empty" matters — an un-upgraded extension that
                # never sends calendar_events at all vs. a current one
                # whose structured scan ran and found nothing.
                fallback_reason = describe_structured_source(req.calendar_events)
                events = events_from_briefing(
                    stored, stored.get("date"), stats=calendar_stats)
                path = "text-fallback"

            dropped = (calendar_stats.get("raw", len(events))
                      - calendar_stats.get("kept", len(events)))
            kept_list = await asyncio.to_thread(
                svc.extension_calendar_svc.replace_all, events, None, {
                    "path": path,
                    "raw": calendar_stats.get("raw", len(events)),
                    "kept": calendar_stats.get("kept", len(events)),
                    "dropped": dropped,
                    "fallback_reason": fallback_reason,
                })
            ext_kept = len(kept_list)
            ext_log_path = "structured" if path == "structured" else "briefing-fallback"
            fallback_note = (f" ({_extension_calendar_fallback_note(fallback_reason)})"
                             if fallback_reason else "")
            logger.info(
                f"Extension calendar: path={ext_log_path}{fallback_note} "
                f"raw={calendar_stats.get('raw', 0)} "
                f"kept={calendar_stats.get('kept', 0)} dropped={dropped}"
                f"{_extension_calendar_dropped_detail(calendar_stats)}")
            _emit_calendar_import_event(
                ext_log_path, calendar_stats, dropped, fallback_reason,
                req.extension_version, calendar_only=False)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Extension calendar store update failed ({e}); "
                f"briefing import itself succeeded.")

    logger.info(
        f"Chrome-extension import: OWA={len(owa_text)} chars, "
        f"Teams={len(teams_text)} chars, "
        f"Inbox={len(inbox_text)} chars, Chat={len(chat_text)} chars → "
        f"agenda={len(stored.get('agenda', []))}, "
        f"needs_response={len(stored.get('needs_response', []))}, "
        f"fyi={len(stored.get('fyi', []))}, "
        f"calendar_events={ext_kept}, path={ext_log_path}")
    return stored


# ── Ship-the-extension-in-the-app (see AGENTS.md build item #2/#3) ──────
#
# The Chrome extension used to be a separate zip on the GitHub releases
# page: the user had to find the release, download it, locate their
# unpacked-extension folder, replace files, and reload — for every
# release that touched the extension. v2.28.0 shipping a NEW extension
# version with no way to detect the old one was still installed is what
# forced this: see services/extension_bundle_service.py.
#
# The app now carries chrome-extension/ inside its own runtime bundle
# (zip-bundle.py) and can write it out on demand to a STABLE folder
# under the user's app data dir — same folder every release, so
# updating is "click Install/Update, click Reload in Chrome" instead of
# a file hunt.

@app.get("/calendar/capture-diagnostics")
async def calendar_capture_diagnostics():
    """The last capture's own counters, for the Record tab.

    These numbers have existed since v2.47.0 and were reachable ONLY by
    generating a diagnostics zip and sending it to someone who could
    read it. That is why every round of "the join link still is not
    there" cost a file transfer and a day: the app knew how many panes
    it opened and how many join-shaped URLs it found in them, and had
    no way to say so.

    {} means no capture has reported since the field existed — which is
    NOT "a capture ran and found nothing". The two render differently.
    """
    if not svc.extension_calendar_svc:
        return {"available": False, "diag": {}}
    try:
        diag = await asyncio.to_thread(
            svc.extension_calendar_svc.last_capture_diag)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"capture diagnostics unavailable: {e}")
        return {"available": False, "diag": {}}
    return {"available": bool(diag), "diag": diag or {}}


@app.get("/extension/info")
async def extension_info():
    """Bundled vs. last-seen Chrome extension version, for the Settings
    Chrome Extension card. Never 500s: a dev checkout without a
    zip-bundle build (bundled_version None) and a store that has never
    seen a POST are both legitimate, reportable states, not errors."""
    svc.load_settings()
    bundled = bundled_extension_version()
    last_seen_version = None
    last_seen_at = None
    if svc.extension_calendar_svc:
        try:
            status = await asyncio.to_thread(
                svc.extension_calendar_svc.capture_status)
            last_seen_version = status.get("last_seen_version")
            last_seen_at = status.get("last_seen_version_at")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Extension status unavailable: {e}")
    return {
        "bundled_version": bundled,
        "last_seen_version": last_seen_version,
        "last_seen_at": last_seen_at,
        "status": extension_version_status(bundled, last_seen_version, last_seen_at),
        "install_path": str(extension_export_dir()),
    }


@app.post("/extension/install")
async def install_extension_files():
    """Write/refresh the bundled extension into its stable install
    folder (see extension_bundle_service.export_dir — NEVER changes
    between releases). A failure partway through never leaves a
    half-written folder presented as success; see export_extension_
    files's atomically-ish swap."""
    try:
        written = await asyncio.to_thread(export_extension_files)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Extension export failed")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")
    return {
        "ok": True,
        "path": str(extension_export_dir()),
        "files": written,
        "file_count": len(written),
    }


@app.get("/integrations/mcp/status")
async def mcp_status():
    """Can this machine launch the MCP server, and with which two paths?

    The Settings "AI assistant access" card renders entirely from this.
    Every field is answerable without the user knowing anything about
    their install, which is the point: before v2.72 the card asked them
    to paste `/absolute/path/to/mcp-server/.venv/bin/python`, a file
    that does not exist on a machine that installed the app instead of
    cloning the repo.

    Never 500s. `bundled: false` (this build carries no mcp-server/) is
    a state to report, not an error.
    """
    return await asyncio.to_thread(mcp_bundle_service.status)


@app.post("/integrations/mcp/install")
async def mcp_install():
    """Install the MCP SDK into the app's own venv, on demand.

    On demand rather than at first launch deliberately — see
    mcp_bundle_service's module docstring: the bootstrap install is the
    one place where a resolution failure bricks the app before it
    starts, and this repo has shipped that bug. Here the worst case is
    a card that says pip failed, on a working app.

    A pip failure is reported as ok=false WITH pip's own output rather
    than raised as a 500: the reason (offline, blocked index, conflict)
    is in those lines and nowhere else.
    """
    result = await asyncio.to_thread(mcp_bundle_service.install_sdk)
    return {**result, "status": mcp_bundle_service.status()}


@app.get("/briefing/today")
async def get_today_briefing():
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503, detail="Briefing service not initialized")
    try:
        data = await asyncio.to_thread(svc.daily_briefing_svc.get, None)
    except BriefingUnreadableError as e:
        # Present-but-unreadable must NOT look like "no briefing today".
        # Returning {} here made the Today tab fall back to its
        # first-run import screen, so a transient read failure looked
        # exactly like the day's briefing had been lost.
        raise HTTPException(
            status_code=503,
            detail=f"Today's briefing is temporarily unreadable: {e}")
    return data or {}


@app.get("/briefing/{date}")
async def get_briefing_by_date(date: str):
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503, detail="Briefing service not initialized")
    try:
        data = await asyncio.to_thread(svc.daily_briefing_svc.get, date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BriefingUnreadableError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Briefing for {date} is temporarily unreadable: {e}")
    return data or {}


@app.patch("/briefing/{date}/actions/{action_id}")
async def patch_briefing_action(date: str, action_id: str,
                                 req: BriefingActionUpdateRequest):
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503, detail="Briefing service not initialized")
    try:
        updated = await asyncio.to_thread(
            svc.daily_briefing_svc.set_action_status,
            date, action_id, bool(req.done))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"No briefing for {date}")
    return updated


# ── Outlook Web sync — drives the user's installed Chrome to scrape
#    today's calendar from outlook.office.com, then feeds the resulting
#    text through the same parser the manual "Import briefing" paste
#    uses. Two endpoints:
#
#    POST /briefing/signin  → spawns a HEADED Chrome window so the user
#                              can sign in or re-MFA. Persistent profile
#                              dir keeps cookies across launches; weekly
#                              MFA re-auth is the expected cadence given
#                              typical M365 conditional-access policies.
#    POST /briefing/sync    → runs the scrape headlessly against the
#                              persistent profile, parses + stores the
#                              briefing. Returns 423 LOCKED if the
#                              session expired so the UI knows to prompt
#                              re-sign-in (vs. a generic 500).
#
#    Concurrency: serialized via a single asyncio.Lock. Two clicks on
#    Sync Now while a scrape is mid-flight would otherwise both try to
#    open Chrome against the same user-data-dir; Chrome locks that dir
#    so the second open errors loudly. Lock makes the second click a
#    no-op-after-wait instead.

# One per backend process; Sync and Signin share it so the user can't
# trigger a sync while the sign-in window is open against the same
# profile dir (Chrome would refuse the lock).
_outlook_web_lock = asyncio.Lock()


@app.post("/briefing/signin")
async def signin_to_outlook_web():
    """Open the headed Chrome window pointing at OWA's day view so the
    user can sign in (or re-MFA). Blocks until the user closes the
    window. Returns {"ok": true} on clean close."""
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503,
                            detail="Briefing service not initialized")
    if _outlook_web_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another sign-in or sync is already running.")
    # v2.15.0+: profile lives in USER_DATA_DIR (LOCAL-only) instead of
    # recordings_dir (often cloud-synced). Cloud-synced profile caused
    # cookie-handoff failures between the headed sign-in and the
    # subsequent headless scrape — see profile_dir_for() docstring.
    from config.settings import USER_DATA_DIR
    async with _outlook_web_lock:
        try:
            await open_signin_window(USER_DATA_DIR)
        except OutlookScraperUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))
        except OutlookScraperError as e:
            raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


@app.post("/briefing/sync")
async def sync_briefing_from_outlook_web():
    """Scrape OWA's day view via the persistent profile, parse the
    resulting text with the same LLM pipeline manual Import uses, and
    store as today's briefing. Returns the stored DailyBriefing JSON
    (same shape as /briefing/import).

    Status codes:
      200 → success, returns DailyBriefing
      423 LOCKED → session expired; UI prompts sign-in
      503 → Playwright / Chrome not available on this machine
      502 → scrape ran but extraction or LLM-parse failed
    """
    svc.load_settings()
    if not svc.daily_briefing_svc:
        raise HTTPException(status_code=503,
                            detail="Briefing service not initialized")
    summ = svc.summarizer or svc.live_summarizer
    if summ is None:
        raise HTTPException(
            status_code=400,
            detail=("No AI provider configured. Set an API key + model in "
                    "Settings before syncing the briefing."))
    if _outlook_web_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="Another sign-in or sync is already running.")

    # v2.15.0+: profile is LOCAL-only at USER_DATA_DIR/web-session/.
    # See profile_dir_for() for why; v2.14.0's recordings_dir-based
    # path broke on cloud-synced volumes.
    from config.settings import USER_DATA_DIR
    async with _outlook_web_lock:
        try:
            owa_text = await scrape_today_briefing_text(USER_DATA_DIR)
        except OutlookAuthExpired as e:
            # 423 Locked is the cleanest semantic for "auth state
            # expired, you must re-sign-in before this can succeed."
            raise HTTPException(status_code=423, detail=str(e))
        except OutlookScraperUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))
        except OutlookScraperError as e:
            raise HTTPException(status_code=502, detail=str(e))
        # Teams Activity scrape runs against the SAME profile right
        # after OWA. ALL Teams failures (including auth-expired) are
        # non-fatal — the brief still publishes OWA-only. v2.15.0
        # propagated Teams' OutlookAuthExpired as a 423, assuming
        # stale cookies for Teams meant stale cookies for OWA. That
        # was wrong: Teams Web has its own OAuth dance on top of M365
        # SSO (the v2/?clientType=desktop URL bounces through
        # login.microsoftonline.com on first visit even when OWA's
        # already authenticated, and may also hit "Stay signed in?"
        # interstitials). The fix in v2.15.1 catches that here AND
        # extends open_signin_window to also visit Teams so the
        # one-time interactive dance gets handled during sign-in,
        # not silently during sync.
        try:
            teams_text = await scrape_today_teams_text(USER_DATA_DIR)
        except OutlookAuthExpired as e:
            # Distinct log level so users tailing backend.log can see
            # "Teams needs sign-in" before they're confused about
            # why Teams section is missing.
            logger.warning(
                f"Teams needs an interactive sign-in (OWA was fine); "
                f"OWA-only brief. Click Sign in to Microsoft once and "
                f"complete Teams' auth dance in the second tab. {e}")
            teams_text = ""
        except OutlookScraperUnavailable:
            logger.warning("Teams scrape unavailable; OWA-only brief.")
            teams_text = ""
        except OutlookScraperError as e:
            logger.warning(f"Teams scrape failed; OWA-only brief: {e}")
            teams_text = ""

    blob = format_for_briefing_parser(owa_text, teams_text=teams_text)
    try:
        parsed = await summ.parse_daily_briefing(blob)
    except RuntimeError as e:
        raise HTTPException(status_code=502,
                            detail=f"LLM parse failed: {e}")

    # Mark the source so the UI can show "Synced from Outlook"
    # provenance distinct from manual paste imports.
    if isinstance(parsed, dict):
        parsed["source"] = "outlook-web-sync"

    stored = await asyncio.to_thread(
        svc.daily_briefing_svc.save_parsed, parsed, blob, None)
    return stored


# ── Domain terminology glossary ─────────────────────────────────────
#
# Biases Whisper toward the user's jargon (initial_prompt) and corrects
# known mis-hears post-transcription. Seeded with a curated SA / CCaaS /
# cloud / sales vocabulary; fully user-editable.

class TerminologyUpdateRequest(BaseModel):
    terms: List[str] = []
    corrections: Dict[str, str] = {}


@app.get("/terminology")
async def get_terminology():
    svc.load_settings()
    if not svc.terminology_svc:
        return {"terms": [], "corrections": {}}
    return await asyncio.to_thread(svc.terminology_svc.get_all)


@app.put("/terminology")
async def put_terminology(req: TerminologyUpdateRequest):
    svc.load_settings()
    if not svc.terminology_svc:
        raise HTTPException(status_code=503, detail="Terminology service not initialized")
    return await asyncio.to_thread(
        svc.terminology_svc.set_all, req.terms, req.corrections)


@app.post("/terminology/reset")
async def reset_terminology():
    svc.load_settings()
    if not svc.terminology_svc:
        raise HTTPException(status_code=503, detail="Terminology service not initialized")
    return await asyncio.to_thread(svc.terminology_svc.reset)


# ── Diagnostics ─────────────────────────────────────────────────────
#
# One endpoint that surfaces the health signals we've repeatedly had to
# dig out of backend.log by hand: is the live co-pilot's model reachable,
# is the main AI provider configured, are mic + loopback visible, is the
# recordings dir writable, and a tail of the log itself. Powers the
# Settings → Diagnostics panel so support questions don't require
# PowerShell archaeology.

def _probe_http(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Best-effort GET to check an HTTP endpoint is alive. Returns
    (reachable, detail). Uses stdlib urllib so there's no dependency on
    the LLM SDK's HTTP client being importable here."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # A 4xx still means something is listening — endpoint is up.
        return True, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _gather_diagnostics() -> dict:
    checks: list[dict] = []

    def add(cid: str, label: str, status: str, detail: str = ""):
        checks.append({"id": cid, "label": label,
                       "status": status, "detail": detail})

    s = svc.settings

    # 1. Recordings dir — exists + writable (OneDrive KFM bites here).
    try:
        from pathlib import Path as _P
        rd = _P(s.recordings_dir) if s and s.recordings_dir else None
        if not rd:
            add("recordings_dir", "Recordings folder", "error", "Not configured")
        elif not rd.exists():
            add("recordings_dir", "Recordings folder", "error",
                f"Does not exist: {rd}")
        else:
            probe = rd / ".write_test.tmp"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                add("recordings_dir", "Recordings folder", "ok", str(rd))
            except Exception as e:
                add("recordings_dir", "Recordings folder", "error",
                    f"Not writable ({type(e).__name__}): {rd}")
    except Exception as e:
        add("recordings_dir", "Recordings folder", "error", str(e))

    # 2. Main AI provider configured.
    try:
        if s and s.is_configured:
            provider = (s.ai_provider or "anthropic")
            model = s.claude_model if provider != "openai" else (s.openai_base_url or "openai-compatible")
            add("main_provider", "AI provider (summaries/extractions)",
                "ok", f"{provider} · {model}")
        else:
            add("main_provider", "AI provider (summaries/extractions)",
                "error", "Not configured — set keys in Settings")
    except Exception as e:
        add("main_provider", "AI provider", "error", str(e))

    # 3. Live Co-Pilot model reachability — THE one that silently failed
    #    the webinar (Ollama wasn't running).
    try:
        if not s or not s.live_copilot_enabled:
            add("copilot_model", "Live Co-Pilot model", "info",
                "Live Co-Pilot disabled")
        else:
            provider = (s.live_ai_provider or "").strip().lower()
            if provider and provider != "anthropic":
                base = (s.live_openai_base_url or "").strip().rstrip("/")
                model = (s.live_claude_model or "").strip()
                if not base:
                    add("copilot_model", "Live Co-Pilot model", "warn",
                        "OpenAI-compatible provider with no base URL set")
                else:
                    # OpenAI-compatible servers (incl. Ollama) expose /models.
                    ok, detail = _probe_http(f"{base}/models")
                    if ok:
                        add("copilot_model", "Live Co-Pilot model", "ok",
                            f"{model or 'model'} reachable at {base}")
                    else:
                        add("copilot_model", "Live Co-Pilot model", "error",
                            f"Can't reach {base} ({detail}). "
                            f"If this is Ollama, is it running?")
            else:
                # Anthropic path — can't cheaply ping; just confirm a key.
                key = (s.live_anthropic_api_key or s.anthropic_api_key or "").strip()
                add("copilot_model", "Live Co-Pilot model",
                    "ok" if key else "error",
                    f"Anthropic · {s.live_claude_model or s.claude_model}"
                    if key else "No Anthropic key configured")
    except Exception as e:
        add("copilot_model", "Live Co-Pilot model", "error", str(e))

    # 4. Audio devices.
    try:
        ins = list_input_devices()
        outs = list_output_devices()
        if ins:
            add("mic", "Microphone input", "ok",
                f"{len(ins)} input device(s) available")
        else:
            add("mic", "Microphone input", "error", "No input devices found")
        # Loopback = output device used for system-audio capture.
        if outs:
            add("loopback", "System-audio (loopback)", "ok",
                f"{len(outs)} output device(s) available")
        else:
            add("loopback", "System-audio (loopback)", "warn",
                "No output devices found for loopback")
    except Exception as e:
        add("mic", "Audio devices", "error", str(e))

    # 5. Models loaded.
    try:
        loaded = bool(svc.recording_svc and svc.recording_svc.can_process)
        add("models", "Transcription + diarization models",
            "ok" if loaded else "warn",
            "Loaded" if loaded
            else "Not loaded yet (load on first Process, ~200MB one-time)")
    except Exception as e:
        add("models", "Models", "warn", str(e))

    # 6. Recording state.
    try:
        rec = bool(svc.recording_svc and svc.recording_svc.is_recording)
        add("recording", "Recording state", "info",
            "Recording in progress" if rec else "Idle")
    except Exception:
        pass

    # Log tail — last lines of backend.log so the user never has to open
    # PowerShell to read it.
    log_tail = ""
    try:
        from config.settings import USER_DATA_DIR
        log_path = USER_DATA_DIR / "backend.log"
        if log_path.exists():
            # Read the tail without slurping a 40MB file into memory.
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                back = min(size, 64 * 1024)  # last 64KB is plenty for ~150 lines
                f.seek(size - back)
                chunk = f.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()[-150:]
            log_tail = "\n".join(lines)
    except Exception as e:
        log_tail = f"(could not read backend.log: {e})"

    # Crash tail — last ~100 lines of crash.log (faulthandler output).
    # Separate from log_tail on purpose: this is the ONLY surface that
    # survives a 0xC0000005 access violation, and it is empty on a
    # healthy machine. Getting it in front of the user via Settings →
    # Diagnostics (rather than "open %LOCALAPPDATA% in Explorer") is
    # what turns the recurring Windows crash into something diagnosable
    # from a copy-paste (field report 2026-08-11).
    crash_tail = ""
    try:
        from utils.crash_log import read_crash_tail
        crash_tail = read_crash_tail(max_lines=100)
    except Exception as e:  # noqa: BLE001
        crash_tail = f"(could not read crash.log: {e})"

    # Recency, not existence — crash.log is append-only and never
    # deleted (utils/crash_log.py), so its mere presence would make the
    # Settings "backend crashed" banner permanent even for a bug fixed
    # and released weeks ago. Only the timestamp of the MOST RECENT
    # crash and a threshold decide whether to warn; the raw tail above
    # stays available regardless so the history is still there to
    # diagnose from.
    last_crash_at: Optional[str] = None
    crash_is_recent = False
    try:
        from utils.crash_log import last_crash_time, is_recent_crash
        crash_ts = last_crash_time()
        if crash_ts is not None:
            last_crash_at = crash_ts.isoformat()
            crash_is_recent = is_recent_crash(crash_ts)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"crash recency check failed: {e}")

    return {
        "checks": checks,
        "log_tail": log_tail,
        "crash_tail": crash_tail,
        "last_crash_at": last_crash_at,
        "crash_is_recent": crash_is_recent,
    }


@app.get("/diagnostics")
async def get_diagnostics():
    svc.load_settings()
    return await asyncio.to_thread(_gather_diagnostics)


@app.get("/diagnostics/export/preview")
async def diagnostics_export_preview():
    """What an export WOULD contain, without writing one.

    The Settings card renders this before the user clicks, so nobody
    ever finds out what they shared after they have already shared it.
    """
    return {
        "members": diagnostics_bundle.preview_members(),
        "descriptions": diagnostics_bundle.MEMBER_DESCRIPTIONS,
        "excluded": diagnostics_bundle.EXCLUDED_STATEMENT,
    }


@app.post("/diagnostics/export")
async def diagnostics_export():
    """Write a single support zip and report exactly what went into it.

    Replaces the five hand-written ``.bat`` scripts it used to take to
    get this same material off a user's machine. Everything in it is
    counts, versions, enums and log tails; the settings snapshot is
    allow-list redacted (see utils/diagnostics_bundle.py).
    """
    svc.load_settings()
    try:
        result = await asyncio.to_thread(
            diagnostics_bundle.build_diagnostics_zip,
            settings=svc.settings,
        )
    except Exception as e:
        logger.exception(f"Diagnostics export failed: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Could not build the diagnostics zip: {e}")
    logger.info(
        f"Diagnostics export written: {result['filename']} "
        f"({result['bytes']} bytes, {len(result['members'])} members)")
    return result


class LLMTestRequest(BaseModel):
    # "main" (default) → svc.summarizer; "live" → svc.live_summarizer.
    # The Live Co-Pilot Settings card uses scope="live" to probe its
    # own provider config without touching the main summarizer.
    scope: Optional[str] = "main"


@app.post("/diagnostics/llm-test")
async def diagnose_llm_connection(req: Optional[LLMTestRequest] = None):
    """Fire a tiny chat completion against the configured AI provider so
    the UI can give the user actionable "is my key / base URL / model
    actually reachable" feedback BEFORE the next summary fails.

    Common failure modes this surfaces directly:
      - Wrong base_url (Ollama 11434 not running, Gemini compat URL
        misspelled, etc.) → connection-refused / 404 / DNS error.
      - Bad API key → 401/403 from the provider.
      - Wrong model id → 404 from the provider with a clear message.
      - Provider reachable but returning unexpected payloads → caught
        and surfaced as "responded but didn't return a chat
        completion."

    ``scope`` in the request body picks which summarizer to test —
    "main" (default) uses ``svc.summarizer``, "live" uses
    ``svc.live_summarizer`` so the Live Co-Pilot card can verify its
    own provider config in isolation from the main path.

    Doesn't touch settings — purely a read against whatever's currently
    configured. ~1 token in/out so it's nearly free against any
    rate-limited backend. ~10s timeout so a stuck endpoint can't hang
    the diagnostics page indefinitely."""
    s = svc.load_settings()
    use_live = ((req and req.scope) or "main").lower() == "live"
    if use_live:
        summarizer = svc.live_summarizer or svc.summarizer
        provider_name = (s.live_ai_provider or s.ai_provider or "anthropic")
        model_name = (s.live_claude_model or s.claude_model or "")
    else:
        summarizer = svc.summarizer
        provider_name = s.ai_provider or "anthropic"
        model_name = s.claude_model or ""
    if not summarizer:
        return {
            "ok": False,
            "provider": provider_name,
            "model": model_name,
            "scope": "live" if use_live else "main",
            "latency_ms": 0,
            "error": (
                "Live summarizer not initialized — save Settings (with the "
                "Live Co-Pilot override enabled), then try again."
                if use_live else
                "Summarizer not initialized — set up an AI provider in "
                "Settings, save, then try again."
            ),
        }
    import time as _t
    t0 = _t.monotonic()
    try:
        # Single-message chat completion. The summarizer wrapper handles
        # both Anthropic and OpenAI-compat surfaces, so this exercises
        # the same code path summary/extract uses without needing two
        # bespoke probes.
        reply = await asyncio.wait_for(
            summarizer._chat(
                "Reply with the single word OK.",
                max_tokens=8,
            ),
            timeout=10.0,
        )
        latency_ms = int((_t.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "provider": provider_name,
            "model": model_name,
            "scope": "live" if use_live else "main",
            "latency_ms": latency_ms,
            "reply": (reply or "").strip()[:80],
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "provider": provider_name,
            "model": model_name,
            "scope": "live" if use_live else "main",
            "latency_ms": int((_t.monotonic() - t0) * 1000),
            "error": (
                "Provider didn't respond within 10s. The endpoint may "
                "be unreachable, the model may be cold-loading (Ollama), "
                "or the URL may be wrong."
            ),
        }
    except Exception as e:
        # Anthropic/OpenAI client exceptions stringify into useful
        # diagnostics ("401 Unauthorized", "404 model not found",
        # "Connection refused"). Surface the raw message verbatim — it's
        # what an operator would want to see.
        return {
            "ok": False,
            "provider": provider_name,
            "model": model_name,
            "scope": "live" if use_live else "main",
            "latency_ms": int((_t.monotonic() - t0) * 1000),
            "error": f"{type(e).__name__}: {e}",
        }


# ── Backend-driven watchdog tick ────────────────────────────────────
#
# CRITICAL: previously the watchdog only ticked when the frontend
# polled /recording/status. If no UI was driving the polls (orphan
# backend, frontend crashed, network blip), the watchdog NEVER fired
# and recordings ran indefinitely. The 4h17m orphan-recording incident
# happened in exactly this state — there was nothing polling /status
# for the orphan backend, so its watchdog never evaluated hard cap,
# silence, or overrun.
#
# Now we fire the watchdog from an asyncio task on a 1-second
# cadence regardless of whether anyone's polling. Same logic the
# /recording/status handler uses, just on a backend-owned timer.
async def _watchdog_loop():
    while True:
        try:
            await asyncio.sleep(1.0)
            rec = svc.recording_svc
            if rec is None or not rec.is_recording:
                continue
            decision = await asyncio.to_thread(rec.watchdog_tick)
            if decision.get("should_auto_stop"):
                reason = decision.get("reason", "?")
                logger.info(
                    f"Watchdog (timer-driven) auto-stopping recording: "
                    f"{reason}")
                try:
                    session = await asyncio.to_thread(_stop_recording_sync)
                    # Auto-process the watchdog-stopped session too — this
                    # is the path that silently skipped processing before
                    # (auto-recorded meeting that auto-stops on silence /
                    # overrun never went through the frontend stop hook).
                    _maybe_auto_process(session)
                except Exception as e:
                    logger.exception(
                        f"Watchdog auto-stop raised: {e}")
        except Exception as e:
            logger.warning(f"Watchdog tick failed: {e}")


# ── Auto pre-meeting brief loop ─────────────────────────────────────
#
# Generates a prep brief shortly before each calendar meeting so it's
# ready when the user needs it. Backend-driven (not gated on the Record
# view being open) per the lesson from the auto-process bug. The brief is
# cached; the frontend polls /prep-brief/auto/pending to fire the native
# "ready" notification.
async def _auto_prep_brief_loop():
    # First tick after a short delay so the app finishes booting.
    await asyncio.sleep(20.0)
    while True:
        try:
            await asyncio.sleep(60.0)
            s = svc.settings
            if not s or not getattr(s, "auto_prep_brief_enabled", False):
                continue
            if not svc.summarizer or not svc.prep_brief_cache_svc:
                continue
            lead = max(1, int(getattr(s, "auto_prep_brief_lead_min", 10) or 10))

            # Upcoming meetings in the next 24 hours. The signature was
            # briefly extended to (hours, include_resource_calendars) on
            # a branch, but the underlying calendar_service.get_upcoming_meetings
            # still takes just `hours_ahead`. Passing two args silently
            # broke auto-brief on every tick ("takes from 0 to 1 positional
            # arguments but 2 were given"). Resource-calendar filtering
            # already happens inside the calendar backend.
            try:
                meetings = await asyncio.to_thread(
                    get_upcoming_meetings, 24)
            except Exception as e:
                logger.warning(f"[auto-brief] calendar fetch failed: {e}")
                continue

            now = datetime.now()
            for m in meetings or []:
                try:
                    subject = (m.get("subject") or "").strip()
                    start_iso = m.get("start") or ""
                    if not subject or not start_iso:
                        continue
                    try:
                        start_dt = datetime.fromisoformat(start_iso)
                    except ValueError:
                        continue
                    mins_until = (start_dt - now).total_seconds() / 60.0
                    # In the lead window and not already started.
                    if mins_until <= 0 or mins_until > lead:
                        continue
                    # Cache key stays subject+start — i.e. one entry per
                    # meeting OCCURRENCE — even though the brief now
                    # also depends on Knowledge-Folder content that can
                    # be re-indexed at any time. Deliberate: this cache
                    # is a "have we notified about this meeting yet?"
                    # marker, and the markdown it stores is never
                    # rendered (page.tsx reads only key/subject/
                    # minutes_before to fire the notification; opening
                    # the brief re-hits /prep-brief/from-meeting, which
                    # re-retrieves documents live). Mixing an index
                    # fingerprint into the key would therefore buy no
                    # freshness the user can see, while making a mid-
                    # window re-index fire a duplicate notification for
                    # the same meeting. The generate→consume window is
                    # bounded by auto_prep_brief_lead_min anyway
                    # (default 10 min). document_count is stored below
                    # so a cached entry is still self-describing.
                    key = _prep_meeting_key(subject, start_iso)
                    if svc.prep_brief_cache_svc.has(key):
                        continue  # already briefed this occurrence

                    logger.info(
                        f"[auto-brief] generating brief for '{subject}' "
                        f"(~{int(mins_until)} min out)")
                    # client/project used to be hard-coded empty here,
                    # because attendee-domain → client resolution lived
                    # only in the frontend (meeting-brief-modal.tsx) and
                    # this loop had no account scope. The consequence
                    # was that auto briefs fell back to corpus-wide
                    # recent sessions instead of client-scoped history,
                    # and retrieved NO Knowledge Folder documents at all
                    # (document retrieval is gated on a resolved client,
                    # since documents can only be filtered by client).
                    # The resolver now lives server-side, so wire it in.
                    # Non-fatal by construction — an unresolved meeting
                    # produces "" / "" and the brief degrades to exactly
                    # the old behaviour rather than failing.
                    resolution = await asyncio.to_thread(
                        _resolve_client_for_meeting,
                        subject, list(m.get("attendees") or []))
                    req = PrepBriefFromMeetingRequest(
                        subject=subject,
                        attendees=list(m.get("attendees") or []),
                        scheduled_start_iso=start_iso,
                        scheduled_end_iso=m.get("end") or "",
                        client=resolution.get("client") or "",
                        project=resolution.get("project") or "",
                        body="",
                        user_context="",
                    )
                    try:
                        result = await prep_brief_from_meeting(req)
                    except Exception as e:
                        logger.warning(
                            f"[auto-brief] generation failed for "
                            f"'{subject}': {e}")
                        continue
                    await asyncio.to_thread(
                        svc.prep_brief_cache_svc.put,
                        {
                            "key": key,
                            "subject": subject,
                            "start_iso": start_iso,
                            "markdown": result.get("markdown", ""),
                            "related_count": result.get("related_count", 0),
                            "document_count": result.get("document_count", 0),
                            "minutes_before": int(mins_until),
                        },
                    )
                    # One generation per tick keeps LLM load gentle; the
                    # next tick (60s) picks up any other in-window meeting.
                    break
                except Exception as e:
                    logger.warning(f"[auto-brief] per-meeting error: {e}")
        except Exception as e:
            logger.warning(f"[auto-brief] loop tick failed: {e}")


@app.get("/prep-brief/auto")
async def get_auto_prep_briefs():
    svc.load_settings()
    if not svc.prep_brief_cache_svc:
        return []
    return await asyncio.to_thread(svc.prep_brief_cache_svc.list_today)


@app.get("/prep-brief/auto/pending")
async def get_pending_auto_prep_briefs():
    svc.load_settings()
    if not svc.prep_brief_cache_svc:
        return []
    return await asyncio.to_thread(svc.prep_brief_cache_svc.pending_notifications)


@app.post("/prep-brief/auto/{key}/notified")
async def mark_auto_prep_brief_notified(key: str):
    svc.load_settings()
    if not svc.prep_brief_cache_svc:
        raise HTTPException(status_code=503, detail="Prep-brief cache not initialized")
    await asyncio.to_thread(svc.prep_brief_cache_svc.mark_notified, key)
    return {"ok": True}


# ── Parent-PID deadman switch ───────────────────────────────────────
#
# CRITICAL SAFETY LAYER. If the Tauri shell that spawned us dies
# (force-quit, crash, BSOD, Windows kills the tree weirdly), this
# backend must NOT continue recording silently in the background. We
# poll the parent PID every 5 seconds — if it goes missing we cleanly
# stop any active recording and exit.
#
# This prevents the v2.9.0 incident where an orphan backend recorded
# 4h17m of audio across multiple meetings because Tauri couldn't kill
# it on shell exit.
#
# Trip wire is the env var MEETING_RECORDER_PARENT_PID set by the
# Tauri spawn in lib.rs. When the var is absent (e.g. running server.py
# standalone for dev), the watchdog no-ops.
async def _parent_pid_watchdog():
    raw = os.environ.get("MEETING_RECORDER_PARENT_PID", "").strip()
    if not raw:
        logger.info("Parent-PID watchdog disabled (no PID env var)")
        return
    try:
        parent_pid = int(raw)
    except ValueError:
        logger.warning(
            f"Parent-PID watchdog: invalid PID '{raw}', disabling")
        return
    logger.info(f"Parent-PID watchdog armed (parent={parent_pid})")
    poll_interval = 5.0
    while True:
        try:
            await asyncio.sleep(poll_interval)
            if not _pid_alive(parent_pid):
                logger.critical(
                    f"Parent process {parent_pid} died. "
                    f"Stopping any active recording and exiting NOW.")
                # Best-effort stop the recording so the WAV file is
                # finalized cleanly rather than left as a `_recording_*`
                # orphan. The auto-recover routine on next launch would
                # pick it up, but a clean stop is better.
                try:
                    if svc.recording_svc and svc.recording_svc.is_recording:
                        await asyncio.to_thread(
                            svc.recording_svc.stop_recording)
                except Exception as e:
                    logger.exception(
                        f"Parent-PID watchdog: clean stop failed: {e}")
                # Force-exit. os._exit() bypasses any pending tasks
                # (we explicitly DO NOT want graceful shutdown here —
                # the parent is gone, there's nothing left to serve).
                os._exit(0)
        except Exception as e:
            logger.warning(f"Parent-PID watchdog tick failed: {e}")


def _pid_alive(pid: int) -> bool:
    """Return True if a process with PID exists. Cross-platform."""
    if sys.platform == "win32":
        # Windows: use OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION
        # so we don't need elevated rights. Returns NULL handle on
        # missing PID. Avoids spawning a wmic / Get-Process child.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            # Check if it's still running (exit code 259 = STILL_ACTIVE)
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(ok) and exit_code.value == 259
        except Exception:
            return True  # fail open — don't kill ourselves on uncertainty
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists, just no permission to signal
        except Exception:
            return True


# ── Startup ──────────────────────────────────────────────────────────
_BACKEND_STARTED_MONOTONIC = time.monotonic()


def _emit_backend_start_event() -> None:
    """``backend.start`` plus, when there is one, ``backend.prior_crash``.

    The pairing is the point: the Rust watchdog respawns the backend
    after a native crash, so a restart with no user action looks
    identical in backend.log to a normal launch. Two adjacent lines in
    events.jsonl — a start, and a prior-crash with an age in days —
    turn the 0xC0000005 restart loop into something countable.
    """
    try:
        import platform as _pf
        events.emit(
            events.BACKEND_START,
            app_version=diagnostics_bundle.app_version(),
            python_version=_pf.python_version(),
            os_name=_pf.system(),
            os_release=_pf.release(),
            machine=_pf.machine(),
        )
    except Exception:
        pass
    try:
        from utils.crash_log import last_crash_time, is_recent_crash
        ts = last_crash_time()
        if ts is not None:
            age_days = max(
                0.0, (datetime.now() - ts).total_seconds() / 86400.0)
            events.emit(
                events.BACKEND_PRIOR_CRASH,
                age_days=age_days,
                recent=bool(is_recent_crash(ts)),
            )
    except Exception:
        pass


@app.on_event("startup")
async def startup():
    try:
        svc.load_settings()
        logger.info("Backend started")
    except Exception as e:
        logger.warning(f"Settings not yet configured: {e}")

    _emit_backend_start_event()

    # PARENT-PID WATCHDOG — first thing after settings. If our Tauri
    # shell dies, we exit within ~5 seconds.
    try:
        asyncio.create_task(_parent_pid_watchdog())
    except Exception as e:
        logger.error(f"Could not start parent-PID watchdog: {e}")

    # RECORDING WATCHDOG — runs on its own timer regardless of whether
    # the frontend is polling. Without this, an orphan backend (or one
    # whose UI has crashed) records forever because nothing evaluates
    # the auto-stop conditions.
    try:
        asyncio.create_task(_watchdog_loop())
    except Exception as e:
        logger.error(f"Could not start recording watchdog loop: {e}")

    # Start the calendar-driven auto-recorder if the user left it on
    # last session. Safe no-op when settings load failed above.
    try:
        _ensure_auto_record_service()
    except Exception as e:
        logger.warning(f"AutoRecordService bootstrap failed: {e}")

    # Designated-Folder reconciliation sweep. Heals anything the
    # enqueue-on-mutation fast path missed: a folder set on another
    # device, an export that exhausted its retries while the mount was
    # offline, or meetings tagged by a build that predates this. Runs
    # off the startup path (to_thread) because it stats files on what
    # may be a cloud mount.
    async def _startup_reconcile() -> None:
        # Let the backend finish coming up first — reconciliation is
        # convergence, not urgency, and the export worker's own retry
        # schedule already covers the fast path.
        await asyncio.sleep(20)
        try:
            await asyncio.to_thread(_reconcile_all_clients)
        except Exception as e:
            logger.warning(f"Startup reconcile failed: {e}")
        try:
            await asyncio.to_thread(_reconcile_archive)
        except Exception as e:
            logger.warning(f"Startup archive reconcile failed: {e}")

    try:
        asyncio.create_task(_startup_reconcile())
    except Exception as e:
        logger.warning(f"Startup reconcile bootstrap failed: {e}")

    # Automatic old-audio cleanup. Previously the retention_enabled
    # setting was saved but never acted on; this task is what makes it
    # real. Independent of settings success above — it reads config
    # fresh each cycle and no-ops while disabled.
    try:
        asyncio.create_task(_retention_loop())
    except Exception as e:
        logger.warning(f"Auto-retention bootstrap failed: {e}")

    # Auto pre-meeting brief generator. Backend timer (not frontend-poll
    # driven) so briefs are ready even if the Record view isn't open.
    # No-ops while the setting is off.
    try:
        asyncio.create_task(_auto_prep_brief_loop())
    except Exception as e:
        logger.warning(f"Auto prep-brief bootstrap failed: {e}")

    # Crash recovery: if a previous run died mid-`/recording/stop`, merge
    # the orphan `_recording_*.wav` / `_loopback_*.wav` temp files into
    # real sessions so they appear in the Session Browser. Off-loop so a
    # slow merge on a big recording can't block the HTTP server coming up.
    def _recover_orphans():
        try:
            if svc.settings is None or svc.session_svc is None:
                return
            results = recover_orphans(
                recordings_dir=svc.settings.recordings_dir,
                session_svc=svc.session_svc,
            )
            recovered = [r for r in results if r.get("status") == "recovered"]
            if recovered:
                logger.info(
                    f"Crash recovery: merged {len(recovered)} orphan "
                    f"recording(s) on startup"
                )
        except Exception as e:
            logger.exception(f"Crash recovery pass failed: {e}")

    def _audit_ghost_sessions():
        """Scan every session JSON in recordings_dir and flag the ones
        whose referenced audio_path doesn't exist on disk. Phantom
        sessions accumulated during v2.9.0's orphan-process incident —
        a session.json was written but the recording aborted before the
        WAV was finalized. The next process_full call fails with a
        cryptic 'no transcript' error.

        v2.12.0 extends this to AUTO-PURGE ghost stubs older than
        ``_GHOST_AUTO_PURGE_AGE_DAYS`` (14 days). Field repro 2026-06-26:
        69 ghosts accumulated on one machine — v2.11.1's JSON-first
        writes add one every time the backend crashes mid-recording or
        mid-finalize. Auto-purge keeps the list bounded; younger ghosts
        stay until the next user-driven cleanup (the new
        DELETE /ghost-sessions endpoint surfaces them in the UI)."""
        try:
            if svc.settings is None or svc.session_svc is None:
                return
            items = _scan_ghost_sessions(svc.settings.recordings_dir)
            if not items:
                return

            # Auto-purge the elderly. Each entry has age_days computed
            # against now by _scan_ghost_sessions.
            from pathlib import Path as _P
            old = [c for c in items if c["age_days"] >= _GHOST_AUTO_PURGE_AGE_DAYS]
            purged: list[str] = []
            for c in old:
                stem = f"session_{c['session_id']}"
                base = _P(c["json_path"]).parent
                try:
                    for sidecar in base.glob(f"{stem}.*"):
                        try:
                            sidecar.unlink()
                        except OSError:
                            pass
                    purged.append(c["session_id"])
                except Exception:
                    pass
            if purged:
                logger.info(
                    f"GHOST_SESSIONS: auto-purged {len(purged)} stub(s) "
                    f"older than {_GHOST_AUTO_PURGE_AGE_DAYS} days: "
                    f"{', '.join(purged[:10])}"
                    f"{' …' if len(purged) > 10 else ''}")

            still_present = [c for c in items if c["session_id"] not in set(purged)]
            if still_present:
                ids = [c["session_id"] for c in still_present]
                logger.warning(
                    f"GHOST_SESSIONS: {len(still_present)} session(s) have a "
                    f"session.json but no audio file on disk: "
                    f"{', '.join(ids[:10])}"
                    f"{' …' if len(still_present) > 10 else ''}. "
                    f"These will fail to process. Visit Settings → "
                    f"Cleanup or call DELETE /ghost-sessions to remove.")
        except Exception as e:
            logger.exception(f"Ghost session audit failed: {e}")

    # Pre-warm the slow stuff in background threads so the first frontend
    # request doesn't pay the latency. These populate module-level caches.
    import threading as _t

    # Resume auto-processing orphaned by a mid-processing backend death.
    # A segfault (the Windows 0xC0000005 class) kills the process with
    # no exception handler — the in-memory retry loop, dedup set, and
    # "Transcribing…" status all vanish, and before this pass nothing
    # ever picked the session back up: it sat unprocessed forever after
    # the UI said it was processing. Runs AFTER crash recovery (which
    # may finalize the very session that needs processing). Poison-pill
    # capped: a session that repeatedly crashes the backend gets a
    # visible processing_error instead of an infinite boot-crash loop.
    _loop = asyncio.get_running_loop()

    def _resume_orphaned_auto_process():
        try:
            if svc.settings is None or svc.session_svc is None:
                return
            rows = svc.session_svc.list_sessions()
            for row in rows:
                if row.get("has_transcript"):
                    continue  # processing completed; stale marker is moot
                sid = row.get("session_id") or ""
                if not sid or sid in _auto_processed_sessions:
                    continue
                session = svc.session_svc.load_full(sid)
                marker = getattr(session, "auto_process_pending", None) \
                    if session else None
                if not isinstance(marker, dict):
                    continue
                verdict = _auto_process_resume_decision(marker, datetime.now())
                if verdict == "give_up":
                    logger.error(
                        f"[auto-process] session {sid} crashed the backend "
                        f"{marker.get('resumes')}+ times mid-processing — "
                        f"giving up (run Process manually)")
                    _stamp_processing_error(
                        sid,
                        "Processing crashed repeatedly (likely during "
                        "transcription). Try Process manually; if it keeps "
                        "failing, the audio file may trigger a native bug.")
                    _stamp_auto_process_pending(sid, None)
                    continue
                if verdict == "stale":
                    logger.warning(
                        f"[auto-process] session {sid} has a stale pending "
                        f"marker (> {_AUTO_PROCESS_RESUME_MAX_AGE_H}h) — "
                        f"not auto-resuming")
                    _stamp_processing_error(
                        sid, "Auto-processing was interrupted and is too old "
                             "to auto-resume — run Process manually.")
                    _stamp_auto_process_pending(sid, None)
                    continue
                # resume
                resumes = int(marker.get("resumes", 0)) + 1
                _auto_processed_sessions.add(sid)
                _stamp_auto_process_pending(sid, {**marker, "resumes": resumes})
                logger.info(
                    f"[auto-process] resuming session {sid} after backend "
                    f"restart (crash-resume {resumes}/"
                    f"{_AUTO_PROCESS_MAX_CRASH_RESUMES})")
                asyncio.run_coroutine_threadsafe(
                    _auto_process_session(
                        sid,
                        str(marker.get("template") or "General"),
                        bool(marker.get("follow_up", False)),
                    ),
                    _loop,
                )
        except Exception as e:
            logger.exception(f"[auto-process] resume pass failed: {e}")

    def _recover_then_resume():
        _recover_orphans()
        _resume_orphaned_auto_process()

    _t.Thread(target=_recover_then_resume, daemon=True).start()
    _t.Thread(target=_audit_ghost_sessions, daemon=True).start()

    def _prewarm_audio():
        # Skipped in safe mode: this is the other half of the PortAudio
        # collision that crash-looped the backend, and it is pure
        # optimisation — the device lists are rebuilt on demand, just
        # more slowly the first time.
        if want_safe_mode():
            logger.warning("SAFE MODE: skipping audio device pre-warm.")
            return
        try:
            from core.audio_capture import list_input_devices, list_output_devices
            t0 = time.time()
            list_input_devices()
            list_output_devices()
            logger.info(f"Audio device cache warmed in {time.time()-t0:.1f}s")
        except Exception as e:
            logger.warning(f"Audio device pre-warm failed: {e}")

    # NOTE: We intentionally do NOT pre-warm the calendar here.
    # Outlook COM occasionally hangs for 30-60s on the first call (usually
    # waiting on Exchange). If that hang happens in the pre-warm thread it
    # holds the Outlook lock, making the first user-triggered Refresh also
    # appear to hang. Better UX is: first calendar fetch runs on-demand
    # when the user first opens the Record view or the CalendarMonitor
    # fires. If that fetch hangs, the frontend's own timeout handling +
    # the in-flight dedup short wait keep the UI responsive.

    _t.Thread(target=_prewarm_audio, daemon=True).start()

    def _backfill_search_index():
        """Walk every processed session that doesn't have an embedding
        sidecar yet and index it. Runs once at startup, in the
        background, so the user never has to click 'Index N sessions'
        manually — it just happens.

        Cheap: each session takes ~0.1-1s on CPU MiniLM. A user with
        100 unindexed sessions sees the count tick down over ~1 minute
        in Settings → Semantic Index without lifting a finger.

        We avoid logging the per-session loop at INFO level — only the
        start/end of the run, and only when there's actual work to do.
        Otherwise this would spam backend.log on every launch even when
        the index is already complete.
        """
        try:
            if svc.search_svc is None:
                return
            # Quick gate before doing anything: only do this work if the
            # AI provider is actually configured. Otherwise the embedding
            # backend hasn't been used and there's nothing meaningful
            # to index against.
            recordings_dir = svc.search_svc._session_service.recordings_dir
            all_sessions = svc.search_svc._session_service.list_sessions()
            missing = [
                s for s in all_sessions
                if s.get("has_transcript")
                # A session with only a legacy .embeddings.pkl
                # (pre-migration, never loaded — see
                # services/search_service.py) has no .npz here and is
                # correctly treated as still needing a rebuild.
                and not (recordings_dir / f"session_{s['session_id']}.embeddings.npz").exists()
            ]
            if not missing:
                return
            logger.info(
                f"Background search-index backfill: {len(missing)} "
                f"processed session(s) need embeddings.")
            indexed = 0
            for s in missing:
                try:
                    if svc.search_svc.index_session(s["session_id"]):
                        indexed += 1
                except Exception as e:
                    logger.warning(
                        f"Backfill failed for {s['session_id']}: {e}")
            logger.info(
                f"Background search-index backfill: indexed {indexed} "
                f"of {len(missing)} session(s).")
        except Exception as e:
            # Never let this background pass take the backend down —
            # search index isn't critical for app usability.
            logger.exception(f"Background backfill aborted: {e}")

    _t.Thread(target=_backfill_search_index, daemon=True).start()


@app.on_event("shutdown")
async def _shutdown_event_log():
    """``backend.stop`` — the counterpart that makes ``backend.start``
    readable.

    A start with no preceding stop is an unclean exit: a native crash,
    a force-quit, or the parent-PID watchdog firing. That distinction
    currently requires eyeballing timestamps in backend.log against
    rust.log, and it is the first question asked whenever a recording
    goes missing. This handler does not run on a 0xC0000005 — that is
    exactly what makes its absence informative."""
    try:
        events.emit(
            events.BACKEND_STOP,
            uptime_s=max(0.0, time.monotonic() - _BACKEND_STARTED_MONOTONIC),
            recording_active=bool(
                svc.recording_svc and svc.recording_svc.is_recording),
        )
    except Exception:
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # The Tauri shell picks a free port at startup and hands it down via
    # MEETING_RECORDER_PORT. Falls back to 17645 only when running this
    # file standalone (manual `python server.py` for debugging).
    _port_env = os.environ.get("MEETING_RECORDER_PORT", "").strip()
    try:
        _port = int(_port_env) if _port_env else 17645
    except ValueError:
        logger.warning(
            f"Invalid MEETING_RECORDER_PORT={_port_env!r}; falling back to 17645")
        _port = 17645
    logger.info(f"Backend listening on 127.0.0.1:{_port}")
    # The ?token= query channel (EventSource/<audio>/<img> can't set
    # headers) would otherwise print the persisted auth token into every
    # access line — and backend.log's tail ships in diagnostics bundles.
    # Logger-level filters survive uvicorn's own logging setup, so
    # installing before run() is sufficient.
    from utils.access_log_redaction import install_access_log_redaction
    install_access_log_redaction()
    uvicorn.run(app, host="127.0.0.1", port=_port, log_level="info")
