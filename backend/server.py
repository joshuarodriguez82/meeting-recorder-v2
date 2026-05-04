"""
FastAPI sidecar server for the Tauri frontend.
Exposes the Python services as HTTP endpoints.
"""

import asyncio
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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import Settings
from core.audio_capture import list_input_devices, list_output_devices
from services.template_service import TemplateService
from models.session import Session
from services.calendar_service import (
    get_todays_meetings, get_upcoming_meetings, is_outlook_available,
)
from services.client_config_service import ClientConfig, ClientConfigService
from services.export_service import ExportService
from services.recording_service import RecordingService
from services.retention_service import cleanup as run_retention_cleanup, folder_stats
from services.recovery_service import recover_orphans
from services.qa_service import QAService
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


# ── Lazy service container ──────────────────────────────────────────
class Services:
    def __init__(self):
        self.settings: Optional[Settings] = None
        self.session_svc: Optional[SessionService] = None
        self.export_svc: Optional[ExportService] = None
        self.client_cfg_svc: Optional[ClientConfigService] = None
        self.template_svc: Optional[TemplateService] = None
        self.recording_svc: Optional[RecordingService] = None
        self.speaker_profile_svc: Optional[SpeakerProfileService] = None
        self.search_svc: Optional[SearchService] = None
        self.qa_svc: Optional[QAService] = None
        self.transcription: Optional[TranscriptionEngine] = None
        self.diarization: Optional[DiarizationEngine] = None
        self.summarizer: Optional[Summarizer] = None
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
            # Per-client configs live next to config.env / logs so they
            # roam with the user profile, not under `recordings/`.
            from config.settings import USER_DATA_DIR
            self.client_cfg_svc = ClientConfigService(USER_DATA_DIR)
            self.template_svc = TemplateService(USER_DATA_DIR)
            self.speaker_profile_svc = SpeakerProfileService(USER_DATA_DIR)
            # SearchService stays a thin wrapper around session_service —
            # session embeddings live next to session JSONs, so it just
            # needs that handle. Lazy index load happens on first search.
            self.search_svc = SearchService(self.session_svc)
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


class StartRecordingRequest(BaseModel):
    mic_device_index: Optional[int] = None
    output_device_index: Optional[int] = None
    meeting_name: str = ""
    template: str = "General"
    client: str = ""
    project: str = ""
    attendees: list[str] = []


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


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


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
    )


@app.post("/settings")
async def save_settings(payload: SettingsDTO):
    # Capture the previous launch_on_startup value BEFORE writing the new
    # one — we only call into the OS to install/remove the auto-launch
    # entry when it actually changes. Otherwise every Save Settings click
    # would hammer the LaunchAgent / Startup folder.
    prev_launch = bool(svc.settings.launch_on_startup) if svc.settings else False

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
    )
    # Force reload
    svc.settings = None
    svc.models_ready = False
    svc.load_settings()

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
        v = torch.__version__  # e.g. "2.2.2+cpu", "2.2.2+cu121"
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
            "torch==2.2.2", "torchaudio==2.2.2",
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
            "--index-url", "https://download.pytorch.org/whl/cu121",
            "--force-reinstall", "--no-deps",
            "torch==2.2.2", "torchaudio==2.2.2",
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

    Wrapped in a 15s asyncio timeout so a hung Outlook COM call never
    leaves the frontend with a dead fetch. On timeout we return [] and
    let the user Refresh again; the underlying thread finishes at its
    own pace and populates the cache for next time.
    """
    try:
        if refresh:
            from services.calendar_service import invalidate_calendar_cache
            invalidate_calendar_cache()
        try:
            meetings = await asyncio.wait_for(
                asyncio.to_thread(get_upcoming_meetings, hours),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"Calendar fetch ({hours}h) exceeded 15s — returning empty. "
                f"Outlook/Exchange likely slow to respond. Retry in a moment.")
            return []
        return _serialize_meetings(meetings)
    except Exception as e:
        logger.exception("Upcoming calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/calendar/available")
async def calendar_available():
    return await asyncio.to_thread(is_outlook_available)


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
    return RecordingStatus(
        is_recording=is_rec,
        session_id=session_id,
        started_at=started_iso,
        duration_s=duration_s,
        models_ready=svc.models_ready,
        models_loading=svc.models_loading,
        models_error=svc.models_error,
        current_status=svc.current_status,
    )


def _start_recording_sync(req: StartRecordingRequest):
    session = svc.recording_svc.start_recording(
        mic_device_index=req.mic_device_index,
        output_device_index=req.output_device_index,
    )
    session.display_name = req.meeting_name or ""
    session.template = req.template or "General"
    session.client = req.client or ""
    session.project = req.project or ""
    session.attendees = req.attendees or []
    svc.current_session = session
    svc.record_started_at = datetime.now()
    return session


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
        return {"session_id": session.session_id}
    except Exception as e:
        logger.exception("Start recording failed")
        raise HTTPException(status_code=500, detail=str(e))


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
            name: {"export_folder": cfg.export_folder}
            for name, cfg in svc.client_cfg_svc.get_all().items()
        }
    return await asyncio.to_thread(_do)


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
        result = await method(transcript, template=template, notes=notes)
        session.template = template
    else:
        result = await method(transcript, notes=notes)
    setattr(session, field_name, result)
    await asyncio.to_thread(svc.session_svc.save, session)


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


# ── Cross-meeting Q&A ────────────────────────────────────────────────


class QARequest(BaseModel):
    query: str
    top_k: int = 8
    client: Optional[str] = None
    project: Optional[str] = None


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


class PrepBriefRequest(BaseModel):
    subject: str
    client: str = ""
    project: str = ""


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
        brief = await svc.summarizer.meeting_prep_brief(prior_notes, req.subject)
        return {"brief": brief, "related_count": len(related)}
    except Exception as e:
        logger.exception("Prep brief failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Retention ────────────────────────────────────────────────────────
@app.get("/retention/stats")
async def retention_stats():
    s = svc.load_settings()
    return folder_stats(s.recordings_dir)


@app.post("/retention/cleanup")
async def retention_cleanup(processed_days: int = 7, unprocessed_days: int = 30):
    s = svc.load_settings()
    return run_retention_cleanup(
        s.recordings_dir,
        processed_days=processed_days,
        unprocessed_days=unprocessed_days,
    )


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


# ── Startup ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        svc.load_settings()
        logger.info("Backend started")
    except Exception as e:
        logger.warning(f"Settings not yet configured: {e}")

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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=17645, log_level="info")
