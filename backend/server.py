"""
FastAPI sidecar server for the Tauri frontend.
Exposes the existing Python services as HTTP endpoints.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import Settings
from core.audio_capture import list_input_devices, list_output_devices
from models.session import Session
from services.calendar_service import get_todays_meetings, is_outlook_available
from services.export_service import ExportService
from services.retention_service import cleanup as run_retention_cleanup, folder_stats
from services.session_service import SessionService
from utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="Meeting Recorder Backend", version="2.0.0")

# Allow the Tauri dev server + production webview to hit the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Lazy service initialization ──────────────────────────────────────
class Services:
    def __init__(self):
        self.settings: Optional[Settings] = None
        self.session_svc: Optional[SessionService] = None
        self.export_svc: Optional[ExportService] = None
        self.transcription = None
        self.diarization = None
        self.summarizer = None
        self.recording_svc = None
        self.current_session: Optional[Session] = None

    def load_settings(self) -> Settings:
        if self.settings is None:
            self.settings = Settings.from_env()
            self.session_svc = SessionService(self.settings.recordings_dir)
            self.export_svc = ExportService(self.settings.recordings_dir)
        return self.settings


svc = Services()


# ── Models ───────────────────────────────────────────────────────────
class SettingsResponse(BaseModel):
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


# ── Health ───────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


# ── Settings ─────────────────────────────────────────────────────────
@app.get("/settings", response_model=SettingsResponse)
async def get_settings():
    s = svc.load_settings()
    return SettingsResponse(
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
async def save_settings(payload: SettingsResponse):
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
    svc.settings = None  # force reload
    return {"ok": True}


# ── Audio devices ────────────────────────────────────────────────────
@app.get("/audio/devices")
async def get_audio_devices():
    return {
        "input": list_input_devices(),
        "output": list_output_devices(),
    }


# ── Calendar ─────────────────────────────────────────────────────────
@app.get("/calendar/today")
async def get_calendar_today():
    try:
        meetings = get_todays_meetings()
        # datetime -> ISO string for JSON
        return [{
            **m,
            "start": m["start"].isoformat() if hasattr(m["start"], "isoformat") else m["start"],
            "end": m["end"].isoformat() if hasattr(m["end"], "isoformat") else m["end"],
        } for m in meetings]
    except Exception as e:
        logger.exception("Calendar fetch failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/calendar/available")
async def calendar_available():
    return {"available": is_outlook_available()}


# ── Sessions ─────────────────────────────────────────────────────────
@app.get("/sessions")
async def list_sessions():
    svc.load_settings()
    return svc.session_svc.list_sessions()


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    svc.load_settings()
    data = svc.session_svc.load(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="Session not found")
    return data


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    svc.load_settings()
    svc.session_svc.delete(session_id)
    return {"ok": True}


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


# ── Startup ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    try:
        svc.load_settings()
        logger.info("Backend started")
    except Exception as e:
        logger.warning(f"Settings not yet configured: {e}")


if __name__ == "__main__":
    # Run on port 17645 (arbitrary; unlikely to clash)
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="127.0.0.1", port=17645, log_level="info")
