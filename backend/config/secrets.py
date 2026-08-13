"""
OS-keychain credential storage for the sensitive settings:
ANTHROPIC_API_KEY, HF_TOKEN, OPENAI_API_KEY, LIVE_ANTHROPIC_API_KEY,
LIVE_OPENAI_API_KEY.

Backend per platform (auto-detected by the `keyring` package):
  Windows: Windows Credential Manager
  macOS:   Keychain
  Linux:   Secret Service (GNOME Keyring / KWallet) — best-effort

If `keyring` is unavailable or its backend is broken (locked keychain,
headless Linux without a daemon), every helper falls back to a no-op
that leaves the value in config.env. The settings layer stays functional
in that case — security degrades, app does not.

First-time migration: when get_secret(name) is called and the keychain
has no entry but the legacy config.env has a non-empty value, the value
is copied into the keychain and BLANKED from the file on the next save.
This is a one-shot migration; subsequent reads come from the keychain.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

SERVICE_NAME = "MeetingRecorder"
SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "OPENAI_API_KEY",
    "LIVE_ANTHROPIC_API_KEY",
    "LIVE_OPENAI_API_KEY",
)

try:
    import keyring  # type: ignore
    import keyring.errors  # type: ignore
    _KEYRING_OK = True
except Exception as e:  # pragma: no cover — missing dep / broken backend
    keyring = None  # type: ignore
    _KEYRING_OK = False
    log.info("keyring unavailable, falling back to config.env: %s", e)


def available() -> bool:
    """True if the OS keychain is usable on this machine."""
    return _KEYRING_OK


def get_secret(name: str) -> Optional[str]:
    """Read a secret from the keychain. Returns None on miss or failure."""
    if not _KEYRING_OK:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except Exception as e:
        log.warning("keyring read failed for %s: %s", name, e)
        return None


def set_secret(name: str, value: str) -> bool:
    """
    Write a secret to the keychain. Empty value means delete.
    Returns True if the keychain accepted the write.
    """
    if not _KEYRING_OK:
        return False
    try:
        if value:
            keyring.set_password(SERVICE_NAME, name, value)
        else:
            try:
                keyring.delete_password(SERVICE_NAME, name)
            except keyring.errors.PasswordDeleteError:
                pass  # already absent
        return True
    except Exception as e:
        log.warning("keyring write failed for %s: %s", name, e)
        return False


def migrate_from_env(env_values: dict) -> dict:
    """
    One-shot migration: for each secret key in env_values that has a
    non-empty value AND no keychain entry yet, copy it into the keychain.
    Returns a dict of {key: True} for keys that were successfully migrated
    (caller should blank these in config.env on the next save). Migration
    failures are silent — caller keeps the env value as a fallback.
    """
    migrated: dict = {}
    if not _KEYRING_OK:
        return migrated
    for k in SECRET_KEYS:
        v = (env_values.get(k) or "").strip()
        if not v:
            continue
        if get_secret(k):
            continue  # keychain already has it
        if set_secret(k, v):
            migrated[k] = True
            log.info("migrated %s into OS keychain", k)
    return migrated
