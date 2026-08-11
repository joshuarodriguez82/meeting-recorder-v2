"""
Settings plumbing for `live_speaker_split_enabled` — the kill switch for
live per-speaker labelling of the far-end ("them") stream.

Field report 2026-08-11: on a real 2-person call the live splitter gave
ONE continuous speaker eight identities (SPEAKER 1,2,3,4,5,6,7,9) and
attached a saved colleague's real NAME to the wrong person. The
thresholds in core/live_speakers.py were retuned hard toward merging,
but a user whose calls still label badly needs a way to switch the
feature off entirely and get the old, plain, never-wrong "them" back.

Mirrors test_live_vad_settings.py's round-trip pattern for exactly the
reason that file's docstring gives: a field can be added to the
dataclass and silently dropped by a save_to_env CALL SITE that forgot
to pass it through — there are three of them in server.py, and a missed
one blanks the field on the next save. These tests pin every layer
(save_to_env -> config.env -> from_env) plus the all-fields-preserved
case that catches the dropped-call-site bug class.
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


def test_live_speaker_split_enabled_is_a_dataclass_field():
    assert ("live_speaker_split_enabled"
            in settings_mod.Settings.__dataclass_fields__)


def test_defaults_true_on_fresh_install(tmp_path, monkeypatch):
    """Default ON — the feature stays on for everyone; the switch is an
    escape hatch, not an opt-in."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_speaker_split_enabled is True


def test_true_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        live_speaker_split_enabled=True,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_speaker_split_enabled is True


def test_false_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        live_speaker_split_enabled=False,
    )
    # The env line itself must exist — a field the writer forgets isn't
    # merely defaulted on read, it's unrecoverable.
    assert "LIVE_SPEAKER_SPLIT_ENABLED=false" in env_path.read_text(
        encoding="utf-8")
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_speaker_split_enabled is False


def test_survives_alongside_other_settings(tmp_path, monkeypatch):
    """All-fields-preserved guard: exercise save_to_env with a broad mix
    of other fields set and confirm nothing gets coupled or blanked."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="sk-test", hf_token="hf-test", whisper_model="small",
        max_speakers=6, recordings_dir=str(tmp_path / "recordings"),
        email_to="a@b.com",
        live_transcription_enabled=True,
        live_copilot_enabled=True,
        live_copilot_mode="SA",
        live_copilot_meeting_type="General",
        copilot_custom_context="line one\nline two",
        session_archive_dir=str(tmp_path / "archive"),
        cloud_mirror_dir=str(tmp_path / "mirror"),
        live_vad_enabled=False,
        live_speaker_split_enabled=False,
        diarization_device="cpu",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.live_speaker_split_enabled is False
    # Neighbours in the same write must be untouched.
    assert loaded.live_vad_enabled is False
    assert loaded.diarization_device == "cpu"
    assert loaded.session_archive_dir == str(tmp_path / "archive")
    assert loaded.cloud_mirror_dir == str(tmp_path / "mirror")
    assert loaded.live_copilot_enabled is True
    assert loaded.anthropic_api_key == "sk-test"
    assert loaded.copilot_custom_context == "line one\nline two"


def test_a_save_that_omits_the_flag_defaults_it_back_on(tmp_path, monkeypatch):
    """save_to_env's default is True, so a CALL SITE that forgets to
    pass the flag re-enables it rather than blanking it into an
    un-parseable value. Documents the failure mode the three server.py
    call sites are audited against."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        live_speaker_split_enabled=False,
    )
    assert settings_mod.Settings.from_env().live_speaker_split_enabled is False

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
    )
    assert settings_mod.Settings.from_env().live_speaker_split_enabled is True
