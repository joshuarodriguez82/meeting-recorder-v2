"""
Application configuration loaded from environment variables.
All secrets are sourced from .env — never hardcoded.

For portability, config + recordings live under %APPDATA%\\MeetingRecorder
on Windows, so the bundled backend (inside the installed app directory,
which is read-only for non-admin users) never needs to write anywhere
outside the user's profile.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import dotenv_values, load_dotenv


def _user_data_dir() -> Path:
    """
    %LOCALAPPDATA%\\MeetingRecorder — the canonical writable directory.

    IMPORTANT: we use LOCALAPPDATA, not APPDATA (Roaming). Corporate
    environments like TTEC enable OneDrive Known Folder Move which
    redirects %APPDATA% into OneDrive; sync causes stale reads, file
    locks mid-write, and intermittent 'failed to fetch' errors from
    the frontend when config/recordings are being synced in the
    background. LOCALAPPDATA is per-machine and never redirected.
    """
    if os.name == "nt":
        base = (os.getenv("LOCALAPPDATA")
                or os.getenv("APPDATA")
                or os.getenv("USERPROFILE")
                or str(Path.home()))
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

        def _get(key: str, default: str = "") -> str:
            v = file_values.get(key)
            if v is not None and v != "":
                return v
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
            claude_model=_get("CLAUDE_MODEL", "claude-haiku-4-5"),
            notify_minutes_before=_get_int("NOTIFY_MINUTES_BEFORE", 2),
            auto_process_after_stop=_get_bool("AUTO_PROCESS_AFTER_STOP", False),
            launch_on_startup=_get_bool("LAUNCH_ON_STARTUP", False),
            auto_follow_up_email=_get_bool("AUTO_FOLLOW_UP_EMAIL", False),
            retention_enabled=_get_bool("RETENTION_ENABLED", False),
            retention_processed_days=_get_int("RETENTION_PROCESSED_DAYS", 7),
            retention_unprocessed_days=_get_int("RETENTION_UNPROCESSED_DAYS", 30),
        )

    @property
    def is_configured(self) -> bool:
        """True if both required API keys are set."""
        return bool(self.anthropic_api_key) and bool(self.hf_token)

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
        auto_process_after_stop: bool = False,
        launch_on_startup: bool = False,
        auto_follow_up_email: bool = False,
        retention_enabled: bool = False,
        retention_processed_days: int = 7,
        retention_unprocessed_days: int = 30,
    ) -> None:
        """Write settings back to the .env file."""
        content = (
            f"ANTHROPIC_API_KEY={anthropic_api_key}\n"
            f"HF_TOKEN={hf_token}\n"
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