"""Diagnostics routes.

One endpoint that surfaces the health signals we've repeatedly had to
dig out of backend.log by hand: is the live co-pilot's model reachable,
is the main AI provider configured, are mic + loopback visible, is the
recordings dir writable, and a tail of the log itself. Powers the
Settings → Diagnostics panel so support questions don't require
PowerShell archaeology.

Extracted verbatim from server.py (router split). Same paths, same
handlers, same behavior — see routers/__init__.py.
"""

import asyncio
import os
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from core.audio_capture import list_input_devices, list_output_devices
from server import svc

router = APIRouter()


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

    return {"checks": checks, "log_tail": log_tail}


@router.get("/diagnostics")
async def get_diagnostics():
    svc.load_settings()
    return await asyncio.to_thread(_gather_diagnostics)


class LLMTestRequest(BaseModel):
    # "main" (default) → svc.summarizer; "live" → svc.live_summarizer.
    # The Live Co-Pilot Settings card uses scope="live" to probe its
    # own provider config without touching the main summarizer.
    scope: Optional[str] = "main"


@router.post("/diagnostics/llm-test")
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
