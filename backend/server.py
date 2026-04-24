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
from services.session_service import SessionService
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
            self.recording_svc = RecordingService(
                settings=self.settings,
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
    )


@app.post("/settings")
async def save_settings(payload: SettingsDTO):
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
    )
    # Force reload
    svc.settings = None
    svc.models_ready = False
    svc.load_settings()
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
    """Best-effort GPU probe via the Windows registry — NO subprocess.

    Previous versions shelled out to PowerShell (Get-CimInstance). On
    corporate laptops with AppLocker / SentinelOne / CrowdStrike /
    Zscaler that subprocess call was being killed and taking the whole
    backend down with it. Reading DisplayAdapters directly from the
    registry is the same information, involves no child process, and
    works in locked-down environments.
    """
    global _GPU_DETECTION_CACHE
    if _GPU_DETECTION_CACHE is not None:
        return _GPU_DETECTION_CACHE

    result = {"nvidia": False, "amd": False, "intel": False, "gpus": []}
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
    try:
        # start_recording can take a couple seconds opening audio streams —
        # run off the event loop so /recording/status stays responsive
        session = await asyncio.to_thread(_start_recording_sync, req)
        return {"session_id": session.session_id}
    except Exception as e:
        logger.exception("Start recording failed")
        raise HTTPException(status_code=500, detail=str(e))


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


@app.patch("/sessions/{session_id}/speakers/{speaker_id}")
async def rename_speaker(session_id: str, speaker_id: str, req: SpeakerRenameRequest):
    """Rename a speaker on a session — updates the Speaker.display_name."""
    svc.load_settings()
    session = svc.session_svc.load_full(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if speaker_id not in session.speakers:
        raise HTTPException(status_code=404, detail="Speaker not on this session")
    session.rename_speaker(speaker_id, req.display_name.strip())
    svc.session_svc.save(session)
    return {"ok": True, "speaker_id": speaker_id,
            "display_name": session.speakers[speaker_id].display_name}


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
    """
    folder = _client_export_folder(session)
    if not folder:
        return
    try:
        svc.export_svc.export_all(
            session, target_dir=folder, copy_audio=copy_audio)
        logger.info(f"Auto-exported session {session.session_id} to {folder}")
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
        else:
            # macOS / Linux fallback for completeness (not the primary target).
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
