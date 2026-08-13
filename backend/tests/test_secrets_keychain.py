"""Keychain-vs-config.env plaintext hardening (security review item 1 & 2).

save_to_env() now prefers the OS keychain for ANTHROPIC_API_KEY, HF_TOKEN,
OPENAI_API_KEY, LIVE_ANTHROPIC_API_KEY and LIVE_OPENAI_API_KEY: when the
keychain accepts (and verifiably holds) the value, config.env gets a blank
line for that key instead of the plaintext. When the keychain is
unavailable, config.env keeps the plaintext as a fallback so a key is
never silently lost.

These tests fake the keychain with an in-memory dict (real `keyring` isn't
installed in the CI venv — see requirements*.txt vs this repo's CI venv,
which intentionally only carries `numpy scipy soundfile pytest fastapi`)
so the round trip can be exercised deterministically without a real OS
credential store.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Make `import config.settings` succeed regardless of prior test modules'
# sys.modules state — see test_session_archive_settings.py's docstring for
# why this has to be defensive rather than trusting collection order.
sys.modules.setdefault("dotenv", MagicMock())

from config import settings as settings_mod  # noqa: E402
from config import secrets as secrets_mod  # noqa: E402


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


class _FakeKeychain:
    """In-memory stand-in for the OS keychain, wired in place of the real
    `keyring`-backed helpers in config.secrets."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.write_calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def set_secret(self, name: str, value: str) -> bool:
        self.write_calls.append((name, value))
        if value:
            self.store[name] = value
        else:
            self.store.pop(name, None)
        return True

    def get_secret(self, name: str):
        return self.store.get(name)


def _install_fake_keychain(monkeypatch) -> _FakeKeychain:
    fake = _FakeKeychain()
    monkeypatch.setattr(settings_mod._secrets, "available", fake.available)
    monkeypatch.setattr(settings_mod._secrets, "set_secret", fake.set_secret)
    monkeypatch.setattr(settings_mod._secrets, "get_secret", fake.get_secret)
    # migrate_from_env is a no-op here since config.env starts empty, but
    # patch it through the fake store too so behaviour stays consistent
    # if a test later seeds file_values.
    def _fake_migrate(env_values: dict) -> dict:
        migrated = {}
        for k in secrets_mod.SECRET_KEYS:
            v = (env_values.get(k) or "").strip()
            if not v or fake.get_secret(k):
                continue
            if fake.set_secret(k, v):
                migrated[k] = True
        return migrated
    monkeypatch.setattr(settings_mod._secrets, "migrate_from_env", _fake_migrate)
    return fake


def _make_kwargs(**overrides) -> dict:
    base = dict(
        anthropic_api_key="",
        hf_token="",
        whisper_model="base",
        max_speakers=10,
        recordings_dir="",
    )
    base.update(overrides)
    return base


# ── item 1: primary secrets prefer the keychain, blank config.env ──────

def test_keychain_round_trip_blanks_env_and_still_loads(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)
    fake = _install_fake_keychain(monkeypatch)

    secret_value = "sk-ant-supersecret-12345"
    settings_mod.Settings.save_to_env(**_make_kwargs(
        anthropic_api_key=secret_value,
        recordings_dir=str(tmp_path / "recordings"),
    ))

    # config.env must not contain the plaintext value anywhere.
    raw = env_path.read_text(encoding="utf-8")
    assert secret_value not in raw
    assert "ANTHROPIC_API_KEY=\n" in raw

    # The keychain actually has it.
    assert fake.store["ANTHROPIC_API_KEY"] == secret_value

    # And the value still loads correctly through the normal path.
    loaded = settings_mod.Settings.from_env()
    assert loaded.anthropic_api_key == secret_value


def test_keychain_round_trip_all_primary_secret_keys(tmp_path, monkeypatch):
    """HF_TOKEN and OPENAI_API_KEY get the same treatment as
    ANTHROPIC_API_KEY."""
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)
    fake = _install_fake_keychain(monkeypatch)

    settings_mod.Settings.save_to_env(**_make_kwargs(
        anthropic_api_key="anthropic-secret",
        hf_token="hf-secret",
        openai_api_key="openai-secret",
        recordings_dir=str(tmp_path / "recordings"),
    ))

    raw = env_path.read_text(encoding="utf-8")
    for secret in ("anthropic-secret", "hf-secret", "openai-secret"):
        assert secret not in raw

    loaded = settings_mod.Settings.from_env()
    assert loaded.anthropic_api_key == "anthropic-secret"
    assert loaded.hf_token == "hf-secret"
    assert loaded.openai_api_key == "openai-secret"


# ── item 2: LIVE_* keys go through the same keychain path ──────────────

def test_live_keys_round_trip_through_keychain(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)
    fake = _install_fake_keychain(monkeypatch)

    settings_mod.Settings.save_to_env(**_make_kwargs(
        recordings_dir=str(tmp_path / "recordings"),
        live_openai_api_key="live-openai-secret",
        live_anthropic_api_key="live-anthropic-secret",
    ))

    raw = env_path.read_text(encoding="utf-8")
    assert "live-openai-secret" not in raw
    assert "live-anthropic-secret" not in raw
    assert fake.store["LIVE_OPENAI_API_KEY"] == "live-openai-secret"
    assert fake.store["LIVE_ANTHROPIC_API_KEY"] == "live-anthropic-secret"

    loaded = settings_mod.Settings.from_env()
    assert loaded.live_openai_api_key == "live-openai-secret"
    assert loaded.live_anthropic_api_key == "live-anthropic-secret"


def test_live_keys_are_in_secret_keys_tuple():
    assert "LIVE_OPENAI_API_KEY" in secrets_mod.SECRET_KEYS
    assert "LIVE_ANTHROPIC_API_KEY" in secrets_mod.SECRET_KEYS


# ── keychain-unavailable fallback (item 1) ──────────────────────────────

def test_keychain_unavailable_falls_back_to_plaintext(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)

    # No fake keychain installed: config.secrets.available() reflects the
    # real environment, where `keyring` is not installed in the CI venv
    # (see requirements*.txt vs the CI venv's deliberately narrow deps).
    assert settings_mod._secrets.available() is False

    secret_value = "sk-ant-plaintext-fallback"
    settings_mod.Settings.save_to_env(**_make_kwargs(
        anthropic_api_key=secret_value,
        recordings_dir=str(tmp_path / "recordings"),
    ))

    raw = env_path.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={secret_value}" in raw

    loaded = settings_mod.Settings.from_env()
    assert loaded.anthropic_api_key == secret_value


def test_keychain_write_failure_falls_back_to_plaintext(tmp_path, monkeypatch):
    """set_secret() reporting failure (or a readback mismatch) must not
    lose the key — config.env keeps the plaintext copy."""
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)

    monkeypatch.setattr(settings_mod._secrets, "available", lambda: True)
    monkeypatch.setattr(settings_mod._secrets, "set_secret", lambda k, v: False)
    monkeypatch.setattr(settings_mod._secrets, "get_secret", lambda k: None)
    monkeypatch.setattr(settings_mod._secrets, "migrate_from_env", lambda ev: {})

    secret_value = "sk-ant-write-failed"
    settings_mod.Settings.save_to_env(**_make_kwargs(
        anthropic_api_key=secret_value,
        recordings_dir=str(tmp_path / "recordings"),
    ))

    raw = env_path.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={secret_value}" in raw

    loaded = settings_mod.Settings.from_env()
    assert loaded.anthropic_api_key == secret_value
