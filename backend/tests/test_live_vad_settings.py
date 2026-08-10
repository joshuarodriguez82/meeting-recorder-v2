"""
Settings plumbing for `live_vad_enabled`.

Field report 2026-08-10 (Zoom notetaker parity): mirrors
test_session_archive_settings.py's round-trip pattern — that file's own
docstring explains why session_archive_dir needed this kind of pinning
(a setting that existed only as an os.getenv() read, with no
Settings-dataclass field and no save_to_env plumbing, silently reset on
every settings save). live_vad_enabled is a NEW field from day one, so
these tests exist to make sure it was wired through EVERY layer
(Settings.save_to_env -> config.env -> Settings.from_env) rather than
just added to the dataclass and forgotten in save_to_env's signature —
exactly the class of bug the AGENTS.md instructions call out by name.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("dotenv", MagicMock())

from config import settings as settings_mod  # noqa: E402


def _real_dotenv_values(path) -> dict:
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v
    except OSError:
        pass
    return out


def _patch_dotenv(monkeypatch) -> None:
    monkeypatch.setattr(settings_mod, "dotenv_values", _real_dotenv_values)
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **kw: True)


def _isolate_env_path(monkeypatch, tmp_path: Path) -> Path:
    env_path = tmp_path / "config.env"
    fallback_path = tmp_path / "dev-fallback.env"
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(settings_mod, "_resolve_env_path", lambda: env_path)

    real_write = settings_mod.Settings._write_env_file

    def _redirect_write(target: Path, content: str) -> bool:
        if target != env_path:
            target = fallback_path
        return real_write(target, content)

    monkeypatch.setattr(
        settings_mod.Settings, "_write_env_file", staticmethod(_redirect_write))
    return env_path


def test_live_vad_enabled_is_a_dataclass_field():
    assert "live_vad_enabled" in settings_mod.Settings.__dataclass_fields__


def test_live_vad_enabled_defaults_true_on_fresh_install(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    # No config.env has ever been written — from_env must still resolve
    # a default rather than raising or defaulting to False.
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_vad_enabled is True


def test_live_vad_enabled_true_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        live_vad_enabled=True,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_vad_enabled is True


def test_live_vad_enabled_false_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        live_vad_enabled=False,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_vad_enabled is False


def test_live_vad_enabled_survives_alongside_other_settings(tmp_path, monkeypatch):
    """Guards against the exact bug class the session-archive field hit:
    a setting present in the dataclass but silently dropped by a
    save_to_env call site that forgot to pass it through — here we
    exercise save_to_env with a broad mix of other fields set to
    confirm live_vad_enabled isn't accidentally coupled to any of
    them."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="sk-test", hf_token="hf-test", whisper_model="small",
        max_speakers=6, recordings_dir=str(tmp_path / "recordings"),
        live_transcription_enabled=True,
        live_copilot_enabled=True,
        session_archive_dir=str(tmp_path / "archive"),
        live_vad_enabled=False,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_vad_enabled is False
    assert loaded.session_archive_dir == str(tmp_path / "archive")
    assert loaded.live_copilot_enabled is True
