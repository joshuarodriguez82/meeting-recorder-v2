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

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import Settings
from core.audio_capture import list_input_devices, list_output_devices
from core.summarizer import MEETING_TEMPLATES
from models.session import Session
from services.calendar_service import (
    get_todays_meetings, get_upcoming_meetings, is_outlook_available,
)
from services.export_service import ExportService
from services.recording_service import RecordingService
from services.retention_service import cleanup as run_retention_cleanup, folder_stats
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
        self.recording_svc: Optional[RecordingService] = None
        self.transcription: Optional[TranscriptionEngine] = None
        self.diarization: Optional[DiarizationEngine] = None
        self.summarizer: Optional[Summarizer] = None
        self.current_session: Optional[Session] = None
        self.models_ready = False
        self.models_loading = False
        self.models_error: Optional[str] = None
        self.record_started_at: Optional[datetime] = None

    def load_settings(self) -> Settings:
        if self.settings is None:
            self.settings = Settings.from_env()
            self.session_svc = SessionService(self.settings.recordings_dir)
            self.export_svc = ExportService(self.settings.recordings_dir)
            self.recording_svc = RecordingService(
                settings=self.settings,
                on_status=lambda msg: logger.info(f"[rec] {msg}"),
            )
            if self.settings.anthropic_api_key:
                # Lazy import Summarizer (pulls in anthropic SDK)
                global Summarizer
                if Summarizer is None:
                    from core.summarizer import Summarizer as _Summarizer
                    Summarizer = _Summarizer
                self.summarizer = Summarizer(
                    self.settings.anthropic_api_key,
                    model=self.settings.claude_model)
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

    if result["nvidia"]:
        result["recommended"] = "cuda"
    elif result["amd"] or result["intel"]:
        result["recommended"] = "directml"
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
        args = ["torch-directml"]
        post = None
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown backend '{req.backend}'. Use cpu, cuda, or directml.",
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
async def get_calendar_upcoming(hours: int = 36, refresh: bool = False):
    """
    Meetings from now through N hours ahead.
    Default 36h covers the rest of today + all of tomorrow.
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


@app.patch("/sessions/{session_id}")
async def patch_session(session_id: str, req: SessionPatchRequest):
    """Update editable session metadata (name, tags, template)."""
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


@app.post("/sessions/{session_id}/process")
async def process_session(session_id: str):
    svc.load_settings()
    # Fail fast with a helpful message BEFORE trying to load models — if
    # API keys aren't set, models can't load and the user sees a cryptic
    # "Process failed" toast. Show them where to fix it instead.
    if not svc.settings or not svc.settings.is_configured:
        raise HTTPException(
            status_code=400,
            detail=(
                "API keys not configured. Open Settings → paste your "
                "Anthropic API key and HuggingFace token → Save, then "
                "accept the pyannote model terms at "
                "huggingface.co/pyannote/speaker-diarization-3.1 and "
                "huggingface.co/pyannote/segmentation-3.0."
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
    try:
        method = getattr(svc.summarizer, extractor_name)
        if extra_arg is not None:
            result = await method(transcript, extra_arg)
        else:
            result = await method(transcript)
        setattr(session, field_name, result)
        await asyncio.to_thread(svc.session_svc.save, session)
        try:
            export_fn = getattr(svc.export_svc, export_fn_name)
            await asyncio.to_thread(export_fn, session)
        except Exception as ex:
            logger.warning(f"Export failed: {ex}")
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
        result = await svc.summarizer.summarize(session.full_transcript(),
                                                  template=req.template)
        session.summary = result
        session.template = req.template
        await asyncio.to_thread(svc.session_svc.save, session)
        try:
            await asyncio.to_thread(svc.export_svc.export_summary, session)
        except Exception:
            pass
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
@app.get("/templates")
async def get_templates():
    return list(MEETING_TEMPLATES.keys())


# ── Startup ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        svc.load_settings()
        logger.info("Backend started")
    except Exception as e:
        logger.warning(f"Settings not yet configured: {e}")

    # Pre-warm the slow stuff in background threads so the first frontend
    # request doesn't pay the latency. These populate module-level caches.
    import threading as _t

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
