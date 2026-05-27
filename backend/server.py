"""
FastAPI sidecar server for the Tauri frontend.
Exposes the Python services as HTTP endpoints.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Set CWD to this file's directory so relative paths (like "recordings/")
# resolve consistently regardless of how the server was launched.
os.chdir(Path(__file__).resolve().parent)
# Also ensure backend dir is on sys.path so `config`, `services`, etc.
# import cleanly even if launched with an odd CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    """Import-test critical packages. If any are missing, re-run
    `pip install -r requirements-*.txt` to repair the venv in place."""
    import importlib

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
        try:
            importlib.import_module(module)
        except ImportError:
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

from config.settings import Settings
from core.audio_capture import list_input_devices, list_output_devices
from services.template_service import TemplateService
from services.copilot_mode_service import CoPilotModeService
from services.copilot_meeting_type_service import CoPilotMeetingTypeService
from models.session import Session
from services.calendar_service import (
    get_todays_meetings, get_upcoming_meetings, is_outlook_available,
)
from services._cloud_sync import CloudFileNotReadyError
from services.client_config_service import ClientConfig, ClientConfigService
from services.engagement_service import EngagementService
from services.export_service import ExportService
from services.recording_service import RecordingService
from services.retention_service import cleanup as run_retention_cleanup, folder_stats
from services.recovery_service import recover_orphans
from services.commitments_service import (
    CommitmentsService, extract_commitments_from_session,
)
from services.item_status_service import (
    ItemStatusService, VALID_DECISION_STATUSES,
)
from services.qa_service import QAService
from services.auto_record_blocklist_service import AutoRecordBlocklistService
from services.search_service import SearchService
from services.session_service import SessionService
from services.speaker_profile_service import (
    SpeakerProfile, SpeakerProfileService,
)
from utils.logger import get_logger

# Heavy ML imports deferred to avoid blocking startup. These load torch +
# pyannote which take several seconds. Imported lazily inside
# ensure_models_loaded() so the API is reachable within ~500ms of launch.
TranscriptionEngine = None  # type: ignore
DiarizationEngine = None  # type: ignore
Summarizer = None  # type: ignore

logger = get_logger(__name__)

app = FastAPI(title="Meeting Recorder Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
        self.session_svc: Optional[SessionService] = None
        self.export_svc: Optional[ExportService] = None
        self.client_cfg_svc: Optional[ClientConfigService] = None
        self.engagement_svc: Optional[EngagementService] = None
        self.template_svc: Optional[TemplateService] = None
        self.copilot_mode_svc: Optional[CoPilotModeService] = None
        self.copilot_meeting_type_svc: Optional[CoPilotMeetingTypeService] = None
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
        self.record_started_at: Optional[datetime] = None
        # Latest status message from the recording/processing pipeline,
        # surfaced to the frontend via /recording/status so the user can
        # see "Transcribing…", "Identifying speakers…" while the long
        # POST /process call is blocking. Previously this signal only
        # went to the log file, so the UI had no way to show progress.
        self.current_status: str = ""

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
        if self.settings is None:
            self.settings = Settings.from_env()
            self.session_svc = SessionService(self.settings.recordings_dir)
            self.export_svc = ExportService(self.settings.recordings_dir)
            # Per-client configs and user-authored templates live ALONGSIDE
            # the recordings dir so they sync with the user's session
            # library — point RECORDINGS_DIR at a cloud-synced folder
            # (iCloud / OneDrive) and clients + templates roam across
            # devices automatically. Migration on first v2.4 launch (or on
            # a recordings_dir change) copies the file from the legacy
            # USER_DATA_DIR location, leaving the old copy as a fallback
            # in case the user downgrades.
            from config.settings import USER_DATA_DIR
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
            # CommitmentsService is built BEFORE the engagement service
            # because the engagement register pulls open / outstanding
            # commitment counts via it. Sidecar JSONs next to session
            # pickles; no state of its own.
            self.commitments_svc = CommitmentsService(self.session_svc)
            # Pure aggregator over session JSONs + client configs +
            # commitments — no state of its own, so it's safe to build
            # eagerly here.
            self.engagement_svc = EngagementService(
                self.session_svc, self.client_cfg_svc, self.commitments_svc)
            self.template_svc = TemplateService(_recordings_dir)
            # Co-Pilot mode + meeting-type libraries. Same shape as
            # TemplateService — seeds defaults on first launch, user
            # can edit / reset / delete from Settings.
            self.copilot_mode_svc = CoPilotModeService(_recordings_dir)
            self.copilot_meeting_type_svc = CoPilotMeetingTypeService(_recordings_dir)
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
        return self.settings

    def ensure_models_loaded(self):
        """Blocking: load transcription + diarization engines if not loaded."""
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
            self.diarization = DiarizationEngine(s.hf_token, s.max_speakers)
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
    live_anthropic_api_key: str = ""
    # Active co-pilot persona + meeting-type modifier. Names resolve
    # through CoPilotModeService / CoPilotMeetingTypeService; the
    # prompts themselves are edited in Settings as their own library.
    # Defaults match what those services seed: SA persona, General type.
    live_copilot_mode: str = "SA"
    live_copilot_meeting_type: str = "General"
    # Free-text context the SA pins for the live co-pilot — appended to
    # every coach_tick prompt as authoritative role / topic framing.
    # Examples: "Current engagement is a Genesys → Connect migration for
    # a healthcare client, ~800 agents, focus on PHI compliance." Empty
    # by default so the baked-in SA-flavored prompt runs as-is.
    copilot_custom_context: str = ""


class StartRecordingRequest(BaseModel):
    mic_device_index: Optional[int] = None
    output_device_index: Optional[int] = None
    meeting_name: str = ""
    template: str = "General"
    client: str = ""
    project: str = ""
    attendees: list[str] = []
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
    current_status: str = ""
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
        live_copilot_mode=s.live_copilot_mode,
        live_copilot_meeting_type=s.live_copilot_meeting_type,
        copilot_custom_context=s.copilot_custom_context,
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
        live_anthropic_api_key=payload.live_anthropic_api_key or "",
        live_copilot_mode=(payload.live_copilot_mode or "").strip() or "SA",
        live_copilot_meeting_type=(payload.live_copilot_meeting_type or "").strip() or "General",
        copilot_custom_context=(payload.copilot_custom_context or "").strip(),
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
        for _filename in ("client_configs.json", "summary_templates.json"):
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


# ── Calendar ─────────────────────────────────────────────────────────
def _serialize_meetings(meetings):
    return [{
        **m,
        "start": m["start"].isoformat() if hasattr(m["start"], "isoformat") else m["start"],
        "end": m["end"].isoformat() if hasattr(m["end"], "isoformat") else m["end"],
    } for m in meetings]


@app.get("/calendar/today")
async def get_calendar_today():
    """Today's meetings (date-based, doesn't cross midnight)."""
    try:
        meetings = await asyncio.to_thread(get_todays_meetings)
        return _serialize_meetings(meetings)
    except Exception as e:
        logger.exception("Calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


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
    """
    try:
        if refresh:
            from services.calendar_service import invalidate_calendar_cache
            invalidate_calendar_cache()
        try:
            meetings = await asyncio.wait_for(
                asyncio.to_thread(get_upcoming_meetings, hours),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Calendar fetch ({hours}h) exceeded 45s — returning empty. "
                f"Outlook/Exchange likely slow to respond. Retry in a moment.")
            return []
        return _serialize_meetings(meetings)
    except Exception as e:
        logger.exception("Upcoming calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/calendar/available")
async def calendar_available():
    return await asyncio.to_thread(is_outlook_available)


@app.get("/calendar/meeting-detail")
async def calendar_meeting_detail(subject: str, start: str):
    """Lazy detail for one calendar invite — agenda/body, attendees, and
    a parsed one-click join link. Fetched on demand (per meeting) so the
    bulk calendar list stays fast: pulling Outlook bodies for every
    meeting in the window would blow the 15s COM budget."""
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

    # Tick the auto-stop watchdog every poll (frontend polls /status
    # every 1s while recording — that's the heartbeat we use for
    # condition evaluation, no separate background task needed).
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
                    await asyncio.to_thread(_stop_recording_sync)
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
    return RecordingStatus(
        is_recording=is_rec,
        session_id=session_id,
        started_at=started_iso,
        duration_s=duration_s,
        models_ready=svc.models_ready,
        models_loading=svc.models_loading,
        models_error=svc.models_error,
        current_status=svc.current_status,
        warnings=warnings,
        auto_record_subject=(svc.auto_record_subject if is_rec else None),
        auto_record_skip_reason=skip_reason,
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
    session.attendees = req.attendees or []
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

    req = StartRecordingRequest(
        meeting_name=name,
        attendees=list(meeting.get("attendees") or []),
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
    # Whisper ready when the first window completes.
    if (svc.settings and svc.settings.is_configured
            and not svc.models_ready and not svc.models_loading):
        threading.Thread(target=svc.ensure_models_loaded, daemon=True).start()
    _start_recording_sync(req)


def _ensure_auto_record_service() -> None:
    """Lazily build the AutoRecordService once settings exist, and
    start/stop its loop to match the current `auto_record_enabled` flag.
    Safe to call from any HTTP handler — it's idempotent."""
    from services.auto_record_service import AutoRecordService
    from services import calendar_service
    if svc.auto_record_svc is None:
        svc.auto_record_svc = AutoRecordService(
            get_upcoming_meetings=calendar_service.get_upcoming_meetings,
            get_todays_meetings=calendar_service.get_todays_meetings,
            is_recording=lambda: bool(
                svc.recording_svc and svc.recording_svc.is_recording),
            start_recording=_auto_record_start,
            is_enabled=lambda: bool(
                svc.settings and svc.settings.auto_record_enabled),
            is_blocked=lambda m: bool(
                svc.auto_record_blocklist_svc
                and svc.auto_record_blocklist_svc.is_blocked(m)),
        )
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
    if (svc.settings and svc.settings.is_configured
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
    session. It's persisted with the session JSON on stop, then fed to
    the summarizer as visual context."""
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
    return {"ok": True, "count": count}


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

    segments = transcriber.recent_segments(last_seconds=600.0)
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
    result = await coach.coach_tick(
        segments=segments, meeting_name=meeting_name,
        custom_context=custom_context, prior_ticks=prior_ticks,
        mode_name=mode_name, mode_prompt=mode_prompt,
        meeting_type_name=type_name, meeting_type_prompt=type_prompt,
    )
    payload = {
        "clarifying_questions": result.get("clarifying_questions", []),
        "risks": result.get("risks", []),
        "follow_ups": result.get("follow_ups", []),
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
        copilot_custom_context=s.copilot_custom_context,
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


def _stop_recording_sync():
    session = svc.recording_svc.stop_recording()
    if session:
        svc.current_session = session
        svc.session_svc.save(session)
        # Copy the fresh WAV into the client's designated folder right
        # away so the user doesn't have to wait for transcription to
        # finish before seeing the file in Explorer.
        _auto_export_to_client(session, copy_audio=True)
    return session


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
    # Drop the session's semantic-search sidecar too — otherwise stale
    # entries linger in the in-memory index and the next search would
    # surface chunks pointing at a session that no longer exists.
    if svc.search_svc:
        await asyncio.to_thread(
            svc.search_svc.delete_session_index, session_id)
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


@app.get("/sessions/{session_id}/audio")
async def get_session_audio(session_id: str):
    """Stream the session's WAV file so the UI can play it in an <audio> element."""
    from fastapi.responses import FileResponse
    from pathlib import Path as _P
    svc.load_settings()
    data = svc.session_svc.load(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    audio_path = data.get("audio_path")
    if not audio_path or not _P(audio_path).exists():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(audio_path, media_type="audio/wav", filename=_P(audio_path).name)


@app.get("/sessions/{session_id}/screenshots/{index}")
async def get_session_screenshot(session_id: str, index: int):
    """Serve one screenshot the user captured during this session, by
    its position in the session's screenshots list. Serving by index
    (rather than an arbitrary path) means we only ever hand back files
    the session actually recorded — no path-traversal surface."""
    from fastapi.responses import FileResponse
    from pathlib import Path as _P
    svc.load_settings()
    data = svc.session_svc.load(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    shots = list(data.get("screenshots") or [])
    if index < 0 or index >= len(shots):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    path = _P(shots[index])
    if not path.is_file():
        raise HTTPException(status_code=404,
                            detail="Screenshot file missing on disk")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media, filename=path.name)


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
    if req.project is not None:
        session.project = req.project
    if req.template is not None:
        session.template = req.template
    if req.notes is not None:
        session.notes = req.notes
    svc.session_svc.save(session)
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

    Mirrors the create/link/refine logic of the manual rename endpoint.
    Best-effort: returns the number of speakers named; logs and continues
    on any failure."""
    if not svc.summarizer or not svc.speaker_profile_svc:
        return 0
    if not session.segments or not session.speakers:
        return 0
    try:
        mapping = await svc.summarizer.identify_speakers(
            session.full_transcript())
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
        if req.project is not None:
            session.project = req.project
        svc.session_svc.save(session)
        updated += 1
    return {"updated": updated}


class SuggestTaggingRequest(BaseModel):
    client: str
    project: str = ""


@app.post("/clients/suggest-tagging")
async def suggest_tagging(req: SuggestTaggingRequest):
    """
    Use Claude to suggest which untagged sessions likely belong to a client.
    Returns [{session_id, display_name, confidence, reason}].
    """
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="Anthropic API key required")

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
        "Only include items with confidence >= 0.5.\n\n"
        "Meetings:\n" + "\n".join(candidate_lines)
    )

    try:
        import anthropic, json
        client_anthropic = anthropic.AsyncAnthropic(
            api_key=svc.settings.anthropic_api_key)
        msg = await client_anthropic.messages.create(
            model=svc.settings.claude_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
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


def _client_export_folder(session: Session) -> Optional[str]:
    """Return the user-designated folder for this session's client, or None."""
    if not session.client or not svc.client_cfg_svc:
        return None
    cfg = svc.client_cfg_svc.get(session.client)
    if cfg and cfg.export_folder:
        return cfg.export_folder
    return None


def _auto_export_to_client(session: Session, copy_audio: bool = False) -> None:
    """
    If this session's client has a designated export folder, drop every
    available artifact there. Called after any step that adds new
    content (processing, summarize, action items, decisions, requirements,
    and on stop_recording for the audio copy). Best-effort — never blocks
    the main flow on an export failure.

    Audio copies are recorded on the session's `exported_audio_paths` so
    retention can clean them up later. We don't track the text artifacts
    (transcript.txt, summary.txt, etc.) — they're KB-sized, keeping them
    as the archival copy is exactly the point of the Designated Folder.
    """
    folder = _client_export_folder(session)
    if not folder:
        return
    try:
        paths = svc.export_svc.export_all(
            session, target_dir=folder, copy_audio=copy_audio)
        logger.info(f"Auto-exported session {session.session_id} to {folder}")
        # Track audio copies so retention can reach them later.
        AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac")
        new_audio = [p for p in paths if p.lower().endswith(AUDIO_EXTS)]
        if new_audio:
            existing = set(session.exported_audio_paths)
            for p in new_audio:
                if p not in existing:
                    session.exported_audio_paths.append(p)
                    existing.add(p)
            # Persist the list so a crash between here and the next save
            # doesn't orphan the copy from retention's view.
            try:
                svc.session_svc.save(session)
            except Exception as save_err:
                logger.warning(
                    f"Could not persist exported_audio_paths for "
                    f"{session.session_id}: {save_err}")
    except Exception as e:
        logger.warning(
            f"Auto-export to '{folder}' failed for session "
            f"{session.session_id}: {e}")


# ── Client configs (per-client designated export folder) ──────────────
@app.get("/clients/config")
async def get_client_configs():
    svc.load_settings()
    def _do():
        return {
            name: {
                "export_folder": cfg.export_folder,
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
    export_folder: str = ""


@app.put("/clients/config/{client_name}")
async def put_client_config(client_name: str, payload: ClientConfigDTO):
    svc.load_settings()
    folder = payload.export_folder.strip()
    # Validate a non-empty folder path so the user catches typos up front
    # rather than at the next recording when nothing shows up there.
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
    def _do():
        svc.client_cfg_svc.set(
            client_name, ClientConfig(export_folder=folder))
    await asyncio.to_thread(_do)
    return {"ok": True, "export_folder": folder}


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
        return svc.export_svc.export_all(
            session, target_dir=target, copy_audio=bool(target))

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
    if not p.exists():
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
        # If the client has a designated folder, copy the audio over now
        # so the user sees it there immediately. Transcripts etc. will
        # follow once processing runs.
        _auto_export_to_client(session, copy_audio=True)
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


@app.post("/sessions/{session_id}/process")
async def process_session(session_id: str):
    svc.load_settings()
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
    svc.recording_svc.set_session(session)
    svc.current_session = session
    try:
        result = await svc.recording_svc.process_session()
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


async def _run_extraction(session_id: str, extractor_name: str, field_name: str,
                           export_fn_name: str, extra_arg=None):
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400,
                            detail="Anthropic API key required")
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.segments:
        raise HTTPException(status_code=400,
                            detail="Session has no transcript (run /process first)")
    transcript = session.full_transcript()
    user_notes = session.notes or ""
    try:
        method = getattr(svc.summarizer, extractor_name)
        if extra_arg is not None:
            result = await method(transcript, extra_arg, notes=user_notes)
        else:
            result = await method(transcript, notes=user_notes)
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
    casing from the first occurrence."""
    ticks = list(getattr(session, "copilot_ticks", []) or [])
    if not ticks:
        return ""
    sections = (
        ("clarifying_questions", "Clarifying questions raised"),
        ("risks", "Risks flagged"),
        ("follow_ups", "Follow-ups suggested"),
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
    if not session or not session.segments:
        raise HTTPException(status_code=400, detail="Session has no transcript")
    try:
        # Resolve the template name to its current prompt via the template
        # service. Users can edit default prompts or add their own, so we
        # can't bake a prompt into the summarizer anymore.
        prompt_text = await asyncio.to_thread(
            svc.template_svc.get_prompt, req.template)
        result = await svc.summarizer.summarize(
            session.full_transcript(),
            prompt=prompt_text,
            notes=session.notes or "",
            template_name=req.template,
            image_paths=list(session.screenshots or []),
            copilot_observations=_copilot_observations_blob(session),
        )
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
        return {"ok": True, "register": register}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Engagement register build failed")
        raise HTTPException(status_code=500, detail=str(e))


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
    if not svc.settings or not svc.settings.is_configured:
        raise HTTPException(
            status_code=400,
            detail="API keys not configured. Open Settings → save tokens → retry.",
        )

    await asyncio.to_thread(svc.ensure_models_loaded)

    stages: dict[str, str] = {}

    # 1. Transcribe + diarize (only if not already done)
    session = await asyncio.to_thread(svc.session_svc.load_full, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.segments:
        svc.recording_svc.set_session(session)
        svc.current_session = session
        try:
            session = await svc.recording_svc.process_session()
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

    # 2-5. Run the four Claude extractions in parallel — independent failures.
    async def _safe(coro, label):
        try:
            await coro
            stages[label] = "ok"
        except Exception as e:
            logger.exception(f"process_full: {label} failed")
            stages[label] = f"failed: {e}"

    await asyncio.gather(
        _safe(_extract_and_save(
            session_id, "summarize", "summary",
            template=req.template), "summary"),
        _safe(_extract_and_save(
            session_id, "extract_action_items", "action_items"), "action_items"),
        _safe(_extract_and_save(
            session_id, "extract_decisions", "decisions"), "decisions"),
        _safe(_extract_and_save(
            session_id, "extract_requirements", "requirements"), "requirements"),
        _safe(_extract_structured_and_save(session_id), "structured"),
    )

    # 6. Optional follow-up email drafts — only when requested explicitly.
    if req.follow_up_drafts:
        try:
            from services.follow_up_email import draft_follow_up_emails
            count = await asyncio.to_thread(
                draft_follow_up_emails, svc, session_id)
            stages["follow_up_drafts"] = f"ok ({count} drafts)"
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

    return {"ok": True, "stages": stages}


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
        result = await method(
            transcript, prompt=prompt_text,
            notes=notes, template_name=template,
            image_paths=list(session.screenshots or []),
            copilot_observations=_copilot_observations_blob(session))
        session.template = template
    else:
        # All four markdown extractors now accept image_paths so
        # screenshots inform action items / decisions / requirements
        # the same way they inform the summary. Older builds passed
        # only the transcript; this widening is backwards-compatible
        # with the Summarizer signatures (image_paths default = None).
        result = await method(
            transcript, notes=notes,
            image_paths=list(session.screenshots or []),
        )
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
        image_paths=list(session.screenshots or []))
    created_at = session.started_at.isoformat() if session.started_at else ""
    stamped = stamp_records(parsed, session.session_id, created_at)
    counts: dict = {}
    for key, (_cls, attr) in STRUCTURED_FIELDS.items():
        recs = stamped.get(key, [])
        setattr(session, attr, recs)
        counts[key] = len(recs)
    await asyncio.to_thread(svc.session_svc.save, session)
    return counts


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


class FollowUpDraftsRequest(BaseModel):
    # Optional override for the sender's tone / context
    tone: str = "friendly-professional"


@app.post("/sessions/{session_id}/follow_up_drafts")
async def create_follow_up_drafts(session_id: str, req: FollowUpDraftsRequest):
    """Create per-attendee Outlook email drafts with their action items."""
    svc.load_settings()
    if not svc.summarizer:
        raise HTTPException(status_code=400, detail="Anthropic API key required")
    try:
        from services.follow_up_email import draft_follow_up_emails
        count = await asyncio.to_thread(
            draft_follow_up_emails, svc, session_id, tone=req.tone)
        return {"ok": True, "drafts_created": count}
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
    write just overwrites the existing pickle. Used by the backfill
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
            and not (recordings_dir / f"session_{s['session_id']}.embeddings.pkl").exists()
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
    items = await asyncio.to_thread(
        svc.commitments_svc.list_all,
        client or None,
        project or None,
        status_list,
        owner or None,
        side or None,
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
    Returns:
        {
          "markdown": str,            # The brief itself (markdown body)
          "referenced_sessions": [    # Sessions Claude COULD cite —
            {"session_id", "display_name", "started_at"},  # frontend
            ...                       # uses these to render
          ],                          # click-to-jump on the [id]
          "related_count": int,       # citations.
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
    # first — the LLM prioritises the head of its context. Cap at 8 to
    # keep the prompt under 6-8 KB which is comfortably under any
    # provider's context limit.
    related.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    related = related[:8]

    if not related:
        # No client-scoped material AND no project-scoped material —
        # fall back to the most recent processed sessions across the
        # entire corpus. Less precise but still gives the user
        # something actionable rather than a blank brief.
        related = [s for s in sessions if s.get("has_summary")][:5]

    if not related:
        return {
            "markdown": (
                "_No prior meetings with summaries are available yet. "
                "Process a few sessions and the brief will have material "
                "to work from._"
            ),
            "referenced_sessions": [],
            "related_count": 0,
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
        )
    except Exception as e:
        logger.exception("Calendar prep brief failed")
        raise HTTPException(status_code=500, detail=str(e))

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
        "related_count": len(related),
        "identified_client": req.client,
        "identified_project": req.project,
        "last_meeting_at": (related[0].get("started_at") if related else None),
    }


@app.post("/prep-brief")
async def prep_brief(req: PrepBriefRequest):
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
        related = [s for s in sessions if s.get("has_summary")][:8]
    if not related:
        return {"brief": "No prior meetings with summaries found to brief from.",
                "related_count": 0}

    # Build context blob
    parts = []
    for s in related[:8]:
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

    try:
        brief = await svc.summarizer.meeting_prep_brief(
            prior_notes, req.subject, user_context=req.user_context)
        return {"brief": brief, "related_count": len(related)}
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


def _client_export_dirs() -> list[str]:
    """Configured per-client Designated Folders, so retention can sweep
    untracked recorder copies that orphaned in them."""
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
        copilot_custom_context=s.copilot_custom_context,
    )
    svc.settings = dataclasses.replace(
        s, live_copilot_mode=new_mode, live_copilot_meeting_type=new_type)
    return {"mode": new_mode, "meeting_type": new_type}


# ── Startup ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        svc.load_settings()
        logger.info("Backend started")
    except Exception as e:
        logger.warning(f"Settings not yet configured: {e}")

    # Start the calendar-driven auto-recorder if the user left it on
    # last session. Safe no-op when settings load failed above.
    try:
        _ensure_auto_record_service()
    except Exception as e:
        logger.warning(f"AutoRecordService bootstrap failed: {e}")

    # Automatic old-audio cleanup. Previously the retention_enabled
    # setting was saved but never acted on; this task is what makes it
    # real. Independent of settings success above — it reads config
    # fresh each cycle and no-ops while disabled.
    try:
        asyncio.create_task(_retention_loop())
    except Exception as e:
        logger.warning(f"Auto-retention bootstrap failed: {e}")

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

    # Pre-warm the slow stuff in background threads so the first frontend
    # request doesn't pay the latency. These populate module-level caches.
    import threading as _t

    _t.Thread(target=_recover_orphans, daemon=True).start()

    def _prewarm_audio():
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
                and not (recordings_dir / f"session_{s['session_id']}.embeddings.pkl").exists()
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
    uvicorn.run(app, host="127.0.0.1", port=_port, log_level="info")
