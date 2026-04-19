"""
Application configuration loaded from environment variables.
All secrets are sourced from .env — never hardcoded.
"""

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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
        Load settings from environment variables.
        Missing API keys default to empty strings — the user can set them
        via the in-app Settings dialog without blocking app startup.
        """
        return cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            hf_token=os.getenv("HF_TOKEN", ""),
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            max_speakers=int(os.getenv("MAX_SPEAKERS", "10")),
            recordings_dir=os.getenv("RECORDINGS_DIR", "recordings"),
            email_to=os.getenv("EMAIL_TO", ""),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5"),
            notify_minutes_before=int(os.getenv("NOTIFY_MINUTES_BEFORE", "2")),
            auto_process_after_stop=os.getenv("AUTO_PROCESS_AFTER_STOP", "false").lower() == "true",
            launch_on_startup=os.getenv("LAUNCH_ON_STARTUP", "false").lower() == "true",
            auto_follow_up_email=os.getenv("AUTO_FOLLOW_UP_EMAIL", "false").lower() == "true",
            retention_enabled=os.getenv("RETENTION_ENABLED", "false").lower() == "true",
            retention_processed_days=int(os.getenv("RETENTION_PROCESSED_DAYS", "7")),
            retention_unprocessed_days=int(os.getenv("RETENTION_UNPROCESSED_DAYS", "30")),
        )

    @property
    def is_configured(self) -> bool:
        """True if both required API keys are set."""
        return bool(self.anthropic_api_key) and bool(self.hf_token)

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
        ENV_PATH.write_text(content, encoding="utf-8")