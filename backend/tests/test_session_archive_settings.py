"""Settings plumbing for the Session Archive folder (SESSION_ARCHIVE_DIR).

Field report 2026-08-07: this setting existed ONLY as an os.getenv()
read in server.py — no Settings dataclass field, no Settings UI, no way
to set it short of hand-editing config.env or a process env var. These
tests pin:

  1. session_archive_dir survives a write-then-read of config.env
     (Settings.save_to_env -> Settings.from_env).
  2. server._session_archive_dir() prefers the Settings value over the
     raw env var, and falls back to the env var when the Settings value
     is empty (so anyone already setting it by hand keeps working).
  3. _archive_status_report() counts are correct for a populated tmp
     folder layout.

config.settings imports `dotenv`, which isn't in the CI test env (see
AGENTS.md / conftest.py). Other test files in this suite (or server.py
via _app_import) may already have stubbed `dotenv` as a bare MagicMock
by the time this module runs, and pytest's collection order is not
something to depend on — so rather than trust whatever's already in
sys.modules["dotenv"], every test here explicitly monkeypatches the two
names config.settings actually calls (`dotenv_values`, `load_dotenv`)
with real, minimal implementations for the duration of the test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Make `import config.settings` succeed regardless of whether a prior
# test module already stubbed `dotenv` — see module docstring. Mirrors
# the pattern in test_coach_tick.py.
sys.modules.setdefault("dotenv", MagicMock())

from config import settings as settings_mod  # noqa: E402
from _app_import import import_app  # noqa: E402


def _real_dotenv_values(path) -> dict:
    """Minimal real KEY=VALUE parser — good enough for config.env's
    flat, unquoted format, and unlike a MagicMock stub it actually
    round-trips values instead of returning more MagicMocks."""
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
    """Redirect config.env reads AND writes to tmp_path, including the
    dev-fallback write save_to_env() always also performs — without
    this the test would clobber the real repo's (gitignored) backend/.env.
    """
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


# ── round trip through config.env ───────────────────────────────────

def test_session_archive_dir_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        session_archive_dir="/Users/sampleuser/Google Drive/MRv2 Archive",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.session_archive_dir == "/Users/sampleuser/Google Drive/MRv2 Archive"


def test_session_archive_dir_default_is_empty_string(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.session_archive_dir == ""


def test_settings_importable_and_constructible():
    """Guards the Python 3.11-incompatible f-string landmine noted in
    AGENTS.md: `python -c "from config.settings import Settings"` must
    work under the CI venv. Also confirms session_archive_dir is a
    proper dataclass field (no missing-argument TypeError from adding
    it without updating every construction site)."""
    assert "session_archive_dir" in settings_mod.Settings.__dataclass_fields__


# ── server._session_archive_dir() preference order ──────────────────

def test_session_archive_dir_prefers_settings_over_env(monkeypatch):
    app = import_app()
    assert app is not None
    import server

    monkeypatch.setenv("SESSION_ARCHIVE_DIR", "/env/fallback")
    monkeypatch.setattr(
        server.svc, "settings",
        SimpleNamespace(session_archive_dir="/from/settings-ui"))
    assert server._session_archive_dir() == "/from/settings-ui"


def test_session_archive_dir_falls_back_to_env_when_settings_empty(monkeypatch):
    import_app()
    import server

    monkeypatch.setenv("SESSION_ARCHIVE_DIR", "/env/fallback")
    monkeypatch.setattr(
        server.svc, "settings", SimpleNamespace(session_archive_dir=""))
    assert server._session_archive_dir() == "/env/fallback"


def test_session_archive_dir_falls_back_to_env_when_settings_unset(monkeypatch):
    """svc.settings is None before the first load_settings() call —
    must not raise, must read the env var."""
    import_app()
    import server

    monkeypatch.setenv("SESSION_ARCHIVE_DIR", "/env/fallback")
    monkeypatch.setattr(server.svc, "settings", None)
    assert server._session_archive_dir() == "/env/fallback"


def test_session_archive_dir_empty_when_neither_configured(monkeypatch):
    import_app()
    import server

    monkeypatch.delenv("SESSION_ARCHIVE_DIR", raising=False)
    monkeypatch.setattr(server.svc, "settings", None)
    assert server._session_archive_dir() == ""


# ── _archive_status_report() ────────────────────────────────────────

def test_archive_status_report_counts(tmp_path, monkeypatch):
    import_app()
    import server

    recordings = tmp_path / "recordings"
    archive = tmp_path / "archive"
    (recordings).mkdir()
    (archive).mkdir()

    # 3 local sessions, 1 already archived (fresh), 1 stale in the
    # archive, 1 missing entirely -> 2 pending.
    for name, mtime in (("a", 1000), ("b", 1000), ("c", 1000)):
        p = recordings / f"session_{name}.json"
        p.write_text("{}", encoding="utf-8")
        import os
        os.utime(p, (mtime, mtime))

    import os
    a_archived = archive / "session_a.json"
    a_archived.write_text("{}", encoding="utf-8")
    os.utime(a_archived, (2000, 2000))  # fresh copy

    b_archived = archive / "session_b.json"
    b_archived.write_text("{}", encoding="utf-8")
    os.utime(b_archived, (500, 500))  # stale copy -> still pending

    monkeypatch.setattr(server.svc, "settings", SimpleNamespace(
        recordings_dir=str(recordings), session_archive_dir=str(archive)))
    monkeypatch.delenv("SESSION_ARCHIVE_DIR", raising=False)

    report = server._archive_status_report()
    assert report["folder"] == str(archive)
    assert report["folder_present"] is True
    assert report["sessions_local"] == 3
    assert report["sessions_in_archive"] == 2  # a and b have files there
    assert report["pending"] == 2  # b (stale) and c (missing)


def test_archive_status_report_unreachable_folder_is_all_pending(tmp_path, monkeypatch):
    import_app()
    import server

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "session_a.json").write_text("{}", encoding="utf-8")
    (recordings / "session_b.json").write_text("{}", encoding="utf-8")

    gone = tmp_path / "not-mounted" / "archive"
    monkeypatch.setattr(server.svc, "settings", SimpleNamespace(
        recordings_dir=str(recordings), session_archive_dir=str(gone)))
    monkeypatch.delenv("SESSION_ARCHIVE_DIR", raising=False)

    report = server._archive_status_report()
    assert report["folder_present"] is False
    assert report["sessions_local"] == 2
    assert report["sessions_in_archive"] == 0
    assert report["pending"] == 2  # never "all present"


def test_archive_status_report_not_configured(tmp_path, monkeypatch):
    import_app()
    import server

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "session_a.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(server.svc, "settings", SimpleNamespace(
        recordings_dir=str(recordings), session_archive_dir=""))
    monkeypatch.delenv("SESSION_ARCHIVE_DIR", raising=False)

    report = server._archive_status_report()
    assert report["folder"] == ""
    assert report["folder_present"] is False
    assert report["sessions_local"] == 1
    assert report["sessions_in_archive"] == 0
    assert report["pending"] == 0
