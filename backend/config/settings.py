"""
Application configuration loaded from environment variables.
All secrets are sourced from .env — never hardcoded.

Per-user config + recordings live under a writable per-platform location
so the bundled backend (inside the installed app directory, which is
read-only for non-admin users) never needs to write anywhere outside the
user's profile:

  Windows: %LOCALAPPDATA%\\MeetingRecorder
  macOS:   ~/Library/Application Support/MeetingRecorder
  Linux:   $XDG_CONFIG_HOME/MeetingRecorder (or ~/.config/MeetingRecorder)
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from dotenv import dotenv_values, load_dotenv

from . import secrets as _secrets


def _user_data_dir() -> Path:
    """The canonical writable directory for this user's data.

    Notes:
      - Windows: uses LOCALAPPDATA (non-roaming), NOT APPDATA. Corporate
        environments enable OneDrive Known Folder Move which redirects
        %APPDATA% into OneDrive; sync causes stale reads, file locks
        mid-write, and intermittent 'failed to fetch' errors. LOCALAPPDATA
        is per-machine and never redirected.
      - macOS: Library/Application Support is the canonical Apple-blessed
        spot for app data that doesn't need to be user-visible. It's
        included in Time Machine backups (good for transcripts/sessions)
        but not in iCloud Drive sync.
    """
    if os.name == "nt":
        base = (os.getenv("LOCALAPPDATA")
                or os.getenv("APPDATA")
                or os.getenv("USERPROFILE")
                or str(Path.home()))
        d = Path(base) / "MeetingRecorder"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "MeetingRecorder"
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        d = Path(base) / "MeetingRecorder"
    d.mkdir(parents=True, exist_ok=True)
    return d


USER_DATA_DIR = _user_data_dir()
ENV_PATH = USER_DATA_DIR / "config.env"


def _resolve_env_path() -> Path:
    """
    Resolve config.env path at call time, in priority order. Prefers whichever
    file actually exists on disk — this means a spawn env with LOCALAPPDATA
    stripped still finds the user's saved settings under APPDATA (Roaming) or
    the dev fallback, rather than defaulting to a fresh install.
    """
    candidates = []
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            v = os.getenv(var)
            if v:
                candidates.append(Path(v) / "MeetingRecorder" / "config.env")
        candidates.append(Path.home() / "MeetingRecorder" / "config.env")
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support"
            / "MeetingRecorder" / "config.env")
        # Some users prefer XDG-style on Mac; respect it as a fallback.
        v = os.getenv("XDG_CONFIG_HOME")
        if v:
            candidates.append(Path(v) / "MeetingRecorder" / "config.env")
    else:
        v = os.getenv("XDG_CONFIG_HOME")
        if v:
            candidates.append(Path(v) / "MeetingRecorder" / "config.env")
        candidates.append(Path.home() / ".config" / "MeetingRecorder" / "config.env")
    # Dev fallback — sibling .env next to the backend source tree
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    # None exists yet — return the primary so save_to_env creates it there.
    return ENV_PATH

# Migration: pre-v2.1.2 stored config.env under %APPDATA% (Roaming),
# which OneDrive Known Folder Move redirects on corporate laptops and
# causes sync conflicts. If the old config is still there and the new
# one isn't, seed the new location so users keep their API keys.
_OLD_ROAMING_ENV = None
if os.name == "nt":
    _roaming = os.getenv("APPDATA")
    if _roaming:
        _OLD_ROAMING_ENV = Path(_roaming) / "MeetingRecorder" / "config.env"
if (_OLD_ROAMING_ENV and _OLD_ROAMING_ENV.exists()
        and _OLD_ROAMING_ENV.resolve() != ENV_PATH.resolve()
        and not ENV_PATH.exists()):
    try:
        ENV_PATH.write_text(
            _OLD_ROAMING_ENV.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass

# Dev fallback: if config.env still doesn't exist but backend/.env does,
# seed from it so existing developers don't have to reconfigure.
_LEGACY_ENV = Path(__file__).resolve().parent.parent / ".env"
if not ENV_PATH.exists() and _LEGACY_ENV.exists():
    try:
        ENV_PATH.write_text(_LEGACY_ENV.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass

load_dotenv(dotenv_path=ENV_PATH, override=True)


# Model ids the Anthropic API rejects with 404 not_found, mapped to the
# canonical id that resolves. claude-3-5-haiku-latest shipped in the
# settings dropdown and got persisted into users' config.env; the
# "-latest" alias is not resolvable for 3.5 Haiku. Heal it on read so
# every consumer (summarizer + the direct client-suggestion call) uses
# the working id without each user having to re-pick the model.
_DEAD_MODEL_ALIASES = {
    "claude-3-5-haiku-latest": "claude-3-5-haiku-20241022",
}


def _normalize_model(model: str) -> str:
    return _DEAD_MODEL_ALIASES.get((model or "").strip(), model)


@dataclass(frozen=True)
class Settings:
    """Immutable application settings resolved at startup."""

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
    # Whether to run the streaming live-transcription pipeline during
    # recording. When False the recording itself is unaffected — we just
    # don't spin up the LiveTranscriber thread or its 16 kHz resample,
    # which saves CPU on long calls. The canonical post-stop transcript
    # always runs regardless.
    live_transcription_enabled: bool
    # ── Auto-stop watchdog ────────────────────────────────────────
    # Warn (and optionally auto-stop) when a recording has been silent
    # for too long, run past its scheduled end time, or exceeded the
    # hard cap. All values are minutes except hard_cap_hours. 0
    # disables a given trigger; the hard cap is always-on.
    silence_warn_min: int           # warn at N min of silence; 0 = off
    silence_stop_min: int           # auto-stop at N min of silence; 0 = off
    overrun_warn_min: int           # warn N min after scheduled end; 0 = off
    overrun_stop_min: int           # auto-stop N min after scheduled end; 0 = off
    hard_cap_hours: int             # never let a recording exceed this; 0 = off
    # Which LLM family powers summaries/extractions.
    #   "anthropic"         → native Anthropic SDK, uses anthropic_api_key +
    #                         claude_model (e.g. claude-haiku-4-5)
    #   "openai"            → OpenAI-compatible endpoint (OpenRouter, Ollama,
    #                         LM Studio, vLLM) via openai_base_url +
    #                         openai_api_key + claude_model (reused as the
    #                         model id — e.g. "meta-llama/llama-3.3-70b-
    #                         instruct:free" on OpenRouter, "llama3" on Ollama)
    ai_provider: str
    openai_api_key: str
    openai_base_url: str
    # When True, a background loop watches the connected calendar and
    # auto-starts a recording at each qualifying event's scheduled start
    # time (filtered to non-all-day events with a conference link).
    # Manual recordings always win — auto-start is a no-op while
    # `recording_svc.is_recording` is True.
    auto_record_enabled: bool
    # When True, the Record view shows a "Live Co-Pilot" panel that calls
    # the LLM every ~45s with the recent transcript and surfaces three
    # short lists (clarifying questions / risks / suggested follow-ups).
    # Opt-in by default — it costs LLM calls during every recording.
    live_copilot_enabled: bool
    # Optional separate LLM for the live co-pilot ticks. When
    # `live_ai_provider` is empty (default) the co-pilot reuses the main
    # provider — same client, same key, same model as post-meeting
    # summaries. When it's set, we build a second Summarizer with these
    # fields so the live side can run on something cheap or free (local
    # Ollama, a free OpenRouter model) while the post-meeting summary
    # stays on the main provider. `live_anthropic_api_key` is only used
    # when the override provider is "anthropic"; otherwise the main
    # `anthropic_api_key` is reused.
    live_ai_provider: str
    live_claude_model: str
    live_openai_api_key: str
    live_openai_base_url: str
    live_anthropic_api_key: str
    # Active co-pilot persona + meeting-type modifier names. Both resolve
    # through the CoPilotMode / CoPilotMeetingType services, which seed
    # editable defaults. Defaults: SA persona, General type.
    live_copilot_mode: str
    live_copilot_meeting_type: str
    # Free-text the SA pins per-engagement as authoritative role / topic
    # framing for the co-pilot. Appended to every coach_tick prompt.
    # Empty by default — the baked-in SA-flavored prompt runs as-is.
    copilot_custom_context: str

    @classmethod
    def from_env(cls) -> "Settings":
        """
        Load settings directly from the on-disk config.env every call.

        Why not just trust os.environ / load_dotenv at import time: some
        Tauri spawn contexts deliver Python a partial/stale environment
        where ANTHROPIC_API_KEY leaks in from the parent but none of the
        other KEY=VALUE pairs do. When load_dotenv then sees those vars
        already present some combinations refuse to override, and the
        backend boots with defaults for everything except the one key —
        exactly the "app opens with 0 sessions" symptom.

        Reading the file directly (not through os.environ at all) removes
        the whole class of inherited-env bugs. load_dotenv still runs as
        a side effect so child processes (subprocess.run) see the same
        values.
        """
        env_path = _resolve_env_path()
        file_values: dict = {}
        if env_path.exists():
            try:
                file_values = dotenv_values(str(env_path)) or {}
            except Exception:
                file_values = {}
            # Also populate os.environ so subprocesses inherit.
            load_dotenv(dotenv_path=env_path, override=True)

        # One-shot migration: copy any secrets still living in config.env
        # into the OS keychain. After this point get_secret() returns the
        # value; on the next save we blank the env line so the file no
        # longer holds plaintext credentials.
        _secrets.migrate_from_env(file_values)

        def _get(key: str, default: str = "") -> str:
            # config.env is authoritative (save_to_env always writes it).
            # A non-empty file value wins so a stale/unreadable keychain
            # entry can never shadow the real key — that shadowing is the
            # "401 invalid x-api-key" bug. Keychain is only consulted when
            # the file has nothing (pre-migration / externally cleared).
            v = file_values.get(key)
            if v is not None and v != "":
                return v
            if key in _secrets.SECRET_KEYS:
                kc = _secrets.get_secret(key)
                if kc:
                    return kc
            return os.getenv(key, default)

        def _get_int(key: str, default: int) -> int:
            try:
                return int(_get(key, str(default)))
            except ValueError:
                return default

        def _get_bool(key: str, default: bool) -> bool:
            v = _get(key, "true" if default else "false").lower()
            return v == "true"

        return cls._build_from_source(_get, _get_int, _get_bool)

    @classmethod
    def _build_from_source(cls, _get, _get_int, _get_bool) -> "Settings":
        return cls(
            anthropic_api_key=_get("ANTHROPIC_API_KEY", ""),
            hf_token=_get("HF_TOKEN", ""),
            whisper_model=_get("WHISPER_MODEL", "base"),
            max_speakers=_get_int("MAX_SPEAKERS", 10),
            # Default recordings dir is %LOCALAPPDATA%\MeetingRecorder\recordings.
            # Users can override via RECORDINGS_DIR in config.env but shouldn't
            # need to — the default just works on a fresh install.
            recordings_dir=_get(
                "RECORDINGS_DIR", str(USER_DATA_DIR / "recordings")),
            email_to=_get("EMAIL_TO", ""),
            claude_model=_normalize_model(
                _get("CLAUDE_MODEL", "claude-haiku-4-5")),
            notify_minutes_before=_get_int("NOTIFY_MINUTES_BEFORE", 2),
            auto_process_after_stop=_get_bool("AUTO_PROCESS_AFTER_STOP", True),
            launch_on_startup=_get_bool("LAUNCH_ON_STARTUP", False),
            auto_follow_up_email=_get_bool("AUTO_FOLLOW_UP_EMAIL", False),
            retention_enabled=_get_bool("RETENTION_ENABLED", False),
            retention_processed_days=_get_int("RETENTION_PROCESSED_DAYS", 7),
            retention_unprocessed_days=_get_int("RETENTION_UNPROCESSED_DAYS", 30),
            ai_provider=_get("AI_PROVIDER", "anthropic"),
            openai_api_key=_get("OPENAI_API_KEY", ""),
            openai_base_url=_get("OPENAI_BASE_URL", ""),
            # Default ON — feature is opt-OUT, since most users will want
            # the live preview. Explicit "false" in config.env disables.
            live_transcription_enabled=_get_bool(
                "LIVE_TRANSCRIPTION_ENABLED", True),
            # Auto-stop defaults: warnings on, auto-stops opt-in, 4h hard cap.
            # The user reported real "I forgot the recording was still going
            # for hours" pain — these defaults catch the common case while
            # leaving the more aggressive auto-stop behaviour off until the
            # user explicitly opts in.
            silence_warn_min=_get_int("SILENCE_WARN_MIN", 5),
            silence_stop_min=_get_int("SILENCE_STOP_MIN", 0),
            overrun_warn_min=_get_int("OVERRUN_WARN_MIN", 5),
            overrun_stop_min=_get_int("OVERRUN_STOP_MIN", 0),
            hard_cap_hours=_get_int("HARD_CAP_HOURS", 4),
            auto_record_enabled=_get_bool("AUTO_RECORD_ENABLED", False),
            live_copilot_enabled=_get_bool("LIVE_COPILOT_ENABLED", False),
            live_ai_provider=_get("LIVE_AI_PROVIDER", ""),
            live_claude_model=_get("LIVE_CLAUDE_MODEL", ""),
            live_openai_api_key=_get("LIVE_OPENAI_API_KEY", ""),
            live_openai_base_url=_get("LIVE_OPENAI_BASE_URL", ""),
            live_anthropic_api_key=_get("LIVE_ANTHROPIC_API_KEY", ""),
            live_copilot_mode=_get("LIVE_COPILOT_MODE", "SA"),
            live_copilot_meeting_type=_get(
                "LIVE_COPILOT_MEETING_TYPE", "General"),
            # Mirrors the escape in save() — `\n` literal on disk →
            # real newlines in the runtime value.
            copilot_custom_context=_get(
                "COPILOT_CUSTOM_CONTEXT", "").replace("\\n", "\n"),
        )

    @property
    def is_configured(self) -> bool:
        """
        True if required keys are set for the active provider.

        HF token is always required (pyannote speaker diarization). The LLM
        credential depends on provider: Anthropic needs anthropic_api_key;
        OpenAI-compatible needs openai_api_key unless the user has set up
        a local Ollama endpoint, in which case no real key is needed (the
        SDK just needs something non-empty, and we supply a placeholder).
        """
        if not self.hf_token:
            return False
        if self.ai_provider == "openai":
            # Ollama / LocalAI endpoints don't need a real key — detect by
            # URL so we don't block the user on a field they don't need.
            if self.openai_api_key:
                return True
            base = (self.openai_base_url or "").lower()
            return "localhost" in base or "127.0.0.1" in base
        return bool(self.anthropic_api_key)

    @staticmethod
    def _write_env_file(target: Path, content: str) -> bool:
        """Atomic-ish write of config.env to `target`. Returns True on success."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
            return True
        except Exception:
            return False

    @staticmethod
    def save_to_env(
        anthropic_api_key: str,
        hf_token: str,
        whisper_model: str,
        max_speakers: int,
        recordings_dir: str,
        email_to: str = "",
        claude_model: str = "claude-haiku-4-5",
        notify_minutes_before: int = 2,
        auto_process_after_stop: bool = True,
        launch_on_startup: bool = False,
        auto_follow_up_email: bool = False,
        retention_enabled: bool = False,
        retention_processed_days: int = 7,
        retention_unprocessed_days: int = 30,
        ai_provider: str = "anthropic",
        openai_api_key: str = "",
        openai_base_url: str = "",
        live_transcription_enabled: bool = True,
        silence_warn_min: int = 5,
        silence_stop_min: int = 0,
        overrun_warn_min: int = 5,
        overrun_stop_min: int = 0,
        hard_cap_hours: int = 4,
        auto_record_enabled: bool = False,
        live_copilot_enabled: bool = False,
        live_ai_provider: str = "",
        live_claude_model: str = "",
        live_openai_api_key: str = "",
        live_openai_base_url: str = "",
        live_anthropic_api_key: str = "",
        live_copilot_mode: str = "SA",
        live_copilot_meeting_type: str = "General",
        copilot_custom_context: str = "",
    ) -> None:
        """Write settings back to the .env file.

        Secrets (Anthropic / HF / OpenAI keys) are mirrored into the OS
        keychain when available, but ALSO always kept in config.env as
        the durable source of truth. Earlier builds blanked the env line
        on a successful keychain write; that lost the key whenever the
        keychain entry later became unreadable — e.g. an unsigned macOS
        app rebuilt with a new ad-hoc signature, or a Windows Credential
        Manager entry written under a different context — producing a
        hard "401 invalid x-api-key" with no fallback. The plaintext
        fallback is the lesser evil versus silently losing the key on
        every upgrade.
        """
        # Mirror secrets into the keychain best-effort. We no longer
        # blank the file on success — config.env stays authoritative so
        # the key survives the keychain entry becoming unreadable across
        # an upgrade / re-sign / different-user context.
        _secrets.set_secret("ANTHROPIC_API_KEY", anthropic_api_key)
        _secrets.set_secret("HF_TOKEN", hf_token)
        _secrets.set_secret("OPENAI_API_KEY", openai_api_key)
        env_anthropic = anthropic_api_key
        env_hf        = hf_token
        env_openai    = openai_api_key

        content = (
            f"ANTHROPIC_API_KEY={env_anthropic}\n"
            f"HF_TOKEN={env_hf}\n"
            f"WHISPER_MODEL={whisper_model}\n"
            f"MAX_SPEAKERS={max_speakers}\n"
            f"RECORDINGS_DIR={recordings_dir}\n"
            f"EMAIL_TO={email_to}\n"
            f"CLAUDE_MODEL={claude_model}\n"
            f"NOTIFY_MINUTES_BEFORE={notify_minutes_before}\n"
            f"AUTO_PROCESS_AFTER_STOP={'true' if auto_process_after_stop else 'false'}\n"
            f"LAUNCH_ON_STARTUP={'true' if launch_on_startup else 'false'}\n"
            f"AUTO_FOLLOW_UP_EMAIL={'true' if auto_follow_up_email else 'false'}\n"
            f"RETENTION_ENABLED={'true' if retention_enabled else 'false'}\n"
            f"RETENTION_PROCESSED_DAYS={retention_processed_days}\n"
            f"RETENTION_UNPROCESSED_DAYS={retention_unprocessed_days}\n"
            f"AI_PROVIDER={ai_provider}\n"
            f"OPENAI_API_KEY={env_openai}\n"
            f"OPENAI_BASE_URL={openai_base_url}\n"
            f"LIVE_TRANSCRIPTION_ENABLED={'true' if live_transcription_enabled else 'false'}\n"
            f"SILENCE_WARN_MIN={silence_warn_min}\n"
            f"SILENCE_STOP_MIN={silence_stop_min}\n"
            f"OVERRUN_WARN_MIN={overrun_warn_min}\n"
            f"OVERRUN_STOP_MIN={overrun_stop_min}\n"
            f"HARD_CAP_HOURS={hard_cap_hours}\n"
            f"AUTO_RECORD_ENABLED={'true' if auto_record_enabled else 'false'}\n"
            f"LIVE_COPILOT_ENABLED={'true' if live_copilot_enabled else 'false'}\n"
            f"LIVE_AI_PROVIDER={live_ai_provider}\n"
            f"LIVE_CLAUDE_MODEL={live_claude_model}\n"
            f"LIVE_OPENAI_API_KEY={live_openai_api_key}\n"
            f"LIVE_OPENAI_BASE_URL={live_openai_base_url}\n"
            f"LIVE_ANTHROPIC_API_KEY={live_anthropic_api_key}\n"
            f"LIVE_COPILOT_MODE={live_copilot_mode}\n"
            f"LIVE_COPILOT_MEETING_TYPE={live_copilot_meeting_type}\n"
            # Newlines in copilot_custom_context would break the .env line
            # format — escape them to literal \n so the value round-trips
            # cleanly through dotenv. from_env unescapes on read.
            f"COPILOT_CUSTOM_CONTEXT={(copilot_custom_context or '').replace(chr(10), '\\n').replace(chr(13), '')}\n"
        )
        # Write to the canonical LOCALAPPDATA location first. In rare cases
        # a Tauri-spawned Python child cannot open files under LOCALAPPDATA
        # (OneDrive KFM, filter drivers, some AV products — CreateFileW
        # returns ERROR_FILE_NOT_FOUND on files that exist from the user's
        # shell view). Mirror the write to the backend/ .env dev fallback
        # so the next backend spawn still finds the settings via
        # _resolve_env_path() even when the canonical path is unreachable
        # from the child process.
        Settings._write_env_file(ENV_PATH, content)
        Settings._write_env_file(
            Path(__file__).resolve().parent.parent / ".env",
            content,
        )