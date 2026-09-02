"""Diarization device selection (Settings.diarization_device).

Field report 2026-08-11 (0xC0000005 after recording stop): the backend
process was observed dying with STATUS_ACCESS_VIOLATION 3-4s after every
recording stop, no Python traceback (native crash). Working hypothesis:
during a recording, LiveTranscriber holds a faster-whisper (CTranslate2,
its own bundled cuDNN) model resident on CUDA; on stop, auto-process
loads pyannote via PyTorch (a *different* bundled cuDNN) and moves it
onto CUDA too — two CUDA/cuDNN runtimes alive in one process at once.
`diarization_device` makes the pyannote device user-selectable so that
hypothesis can be tested/worked around without guessing.

These tests cover two independent layers, both without importing torch
or pyannote (forbidden in this suite — see AGENTS.md / conftest.py):

  1. core.diarization._resolve_device — pure device-selection logic. It
     takes a torch-*like* object as a parameter rather than importing
     torch itself, so it's fully testable with a small fake stand-in.
     core.diarization only imports torch/pyannote lazily inside
     DiarizationEngine.__init__, so importing the module itself is safe.
  2. config.settings.Settings — diarization_device round-trips through
     config.env (save_to_env -> from_env), including the case where
     save_to_env is called WITHOUT diarization_device explicitly (the
     exact bug class flagged in AGENTS.md: a save_to_env call site that
     forgot to pass a field silently blanks it), and garbage values
     self-heal to "auto" on read.
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.modules.setdefault("dotenv", MagicMock())

from config import settings as settings_mod  # noqa: E402
from core.diarization import _resolve_device  # noqa: E402


# ── Fake torch stand-in ───────────────────────────────────────────────

class _FakeDevice:
    """Minimal stand-in for torch.device — only `.type` is used by the
    caller (DiarizationEngine's Pipeline.to() fallback path)."""

    def __init__(self, type_: str):
        self.type = type_

    def __repr__(self):
        return f"device(type={self.type!r})"

    def __eq__(self, other):
        return isinstance(other, _FakeDevice) and other.type == self.type


class _FakeCuda:
    def __init__(self, available: bool):
        self._available = available

    def is_available(self):
        return self._available


class _FakeMps:
    def __init__(self, available: bool):
        self._available = available

    def is_available(self):
        return self._available


class _FakeBackends:
    def __init__(self, mps=None):
        self.mps = mps


class FakeTorch:
    """Stands in for the `torch` module argument _resolve_device takes.
    Deliberately does NOT touch sys.modules["torch"] — _resolve_device
    is a plain function that receives its torch dependency as an
    argument, so no import-time stubbing is needed at all."""

    def __init__(self, cuda_available: bool = False, mps_available=None):
        self.cuda = _FakeCuda(cuda_available)
        mps = _FakeMps(mps_available) if mps_available is not None else None
        self.backends = _FakeBackends(mps=mps)

    def device(self, type_: str) -> _FakeDevice:
        return _FakeDevice(type_)


# ── _resolve_device ────────────────────────────────────────────────────

def test_cpu_forces_cpu_even_when_cuda_available():
    """"cpu" must never probe cuda/mps at all — even a torch stub that
    claims CUDA is available must not change the outcome."""
    fake = FakeTorch(cuda_available=True, mps_available=True)
    device, label = _resolve_device("cpu", fake)
    assert device.type == "cpu"
    assert label == "CPU"


def test_auto_with_cuda_available_picks_cuda():
    """Today's behavior, preserved: auto + CUDA available -> CUDA."""
    fake = FakeTorch(cuda_available=True)
    device, label = _resolve_device("auto", fake)
    assert device.type == "cuda"
    assert label == "GPU (CUDA)"


def test_auto_with_no_cuda_and_no_mps_picks_cpu():
    fake = FakeTorch(cuda_available=False, mps_available=False)
    device, label = _resolve_device("auto", fake)
    assert device.type == "cpu"
    assert label == "CPU"


def test_auto_with_no_cuda_but_mps_available_picks_mps():
    fake = FakeTorch(cuda_available=False, mps_available=True)
    device, label = _resolve_device("auto", fake)
    assert device.type == "mps"
    assert label == "GPU (MPS, Apple Silicon)"


def test_cuda_setting_with_no_cuda_available_falls_back_to_cpu_and_warns(caplog):
    """Forcing "cuda" on a machine with no GPU must fall back to CPU
    with a warning — never crash. This is the inverse workaround: a
    user testing the crash hypothesis force-picks "cuda" but happens to
    be on a laptop with no NVIDIA GPU; the app must still boot."""
    fake = FakeTorch(cuda_available=False)
    with caplog.at_level(logging.WARNING):
        device, label = _resolve_device("cuda", fake)  # must not raise
    assert device.type == "cpu"
    assert label == "CPU"
    assert any(
        "cuda" in rec.message.lower() and "fall" in rec.message.lower()
        for rec in caplog.records
    ), f"expected a fallback warning, got: {[r.message for r in caplog.records]}"


def test_cuda_setting_with_cuda_available_picks_cuda():
    fake = FakeTorch(cuda_available=True)
    device, label = _resolve_device("cuda", fake)
    assert device.type == "cuda"
    assert label == "GPU (CUDA)"


def test_unknown_setting_falls_back_to_auto_behavior():
    """A garbage value reaching _resolve_device directly (bypassing
    Settings' own normalization) must behave like "auto", not raise."""
    fake = FakeTorch(cuda_available=True)
    device, label = _resolve_device("bogus-value", fake)
    assert device.type == "cuda"  # auto behavior: cuda available -> cuda


# ── Settings round-trip ─────────────────────────────────────────────────

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


def test_diarization_device_is_a_dataclass_field():
    assert "diarization_device" in settings_mod.Settings.__dataclass_fields__


def test_diarization_device_defaults_auto_on_fresh_install(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    loaded = settings_mod.Settings.from_env()
    assert loaded.diarization_device == "auto"


def test_diarization_device_cpu_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        diarization_device="cpu",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.diarization_device == "cpu"


def test_diarization_device_cuda_round_trips_through_config_env(tmp_path, monkeypatch):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        diarization_device="cuda",
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.diarization_device == "cuda"


def test_diarization_device_garbage_value_falls_back_to_auto(tmp_path, monkeypatch):
    """Directly write garbage to config.env (simulating a hand-edit or a
    value from a since-removed option) and confirm from_env self-heals
    to "auto" rather than raising or propagating the garbage."""
    _patch_dotenv(monkeypatch)
    env_path = _isolate_env_path(monkeypatch, tmp_path)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("DIARIZATION_DEVICE=directml-please\n", encoding="utf-8")

    loaded = settings_mod.Settings.from_env()
    assert loaded.diarization_device == "auto"


def test_diarization_device_defaults_to_auto_when_save_to_env_omits_it(
    tmp_path, monkeypatch
):
    """Regression guard for the exact bug class AGENTS.md calls out by
    name: a save_to_env call site that forgot to pass a new field
    silently blanks it. Calling save_to_env WITHOUT diarization_device
    (as an old/forgetful call site would) must still leave the field at
    its documented default ("auto") on the next read, not an empty
    string or a crash."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.diarization_device == "auto"


def test_settings_round_trips_with_all_fields_preserved(tmp_path, monkeypatch):
    """Broader regression guard than any single field: build a Settings
    with every field set to a distinctive non-default value, save it via
    save_to_env(**dataclasses.asdict(...)), reload via from_env, and
    assert every field survives unchanged. This is the same class of bug
    a missed diarization_device (or any other field) at a save_to_env
    call site produces — a field present in the dataclass but dropped by
    one particular caller — generalized to catch it for ALL fields at
    once rather than one at a time.
    """
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)

    fields = settings_mod.Settings.__dataclass_fields__
    distinctive: dict = {}
    for name, f in fields.items():
        if f.type == "str" or f.type is str:
            distinctive[name] = f"distinctive-{name}"
        elif f.type == "int" or f.type is int:
            distinctive[name] = 12345
        elif f.type == "bool" or f.type is bool:
            distinctive[name] = True
        else:
            distinctive[name] = f"distinctive-{name}"

    # Fields with real semantic constraints that would otherwise get
    # clobbered by the generic string/int/bool fill above.
    distinctive["recordings_dir"] = str(tmp_path / "recordings")
    distinctive["max_speakers"] = 7
    distinctive["diarization_device"] = "cpu"
    # Same self-healing normalization as diarization_device (see
    # config/settings.py's _normalize_calendar_source) — an unrecognized
    # value falls back to "auto" rather than round-tripping verbatim.
    distinctive["calendar_source"] = "extension"
    # Also self-healing (_normalize_language): "auto" or a 2-3 letter
    # code, anything else falls back to "en". A generic
    # "distinctive-whisper_language" placeholder is not a language.
    distinctive["whisper_language"] = "it"

    settings_mod.Settings.save_to_env(**distinctive)
    loaded = settings_mod.Settings.from_env()

    for name in fields:
        if name == "claude_model":
            # from_env runs dead-model-alias healing on this field only;
            # the distinctive placeholder isn't a known alias so it's
            # unaffected, but assert via getattr for clarity anyway.
            assert getattr(loaded, name) == distinctive[name]
            continue
        assert getattr(loaded, name) == distinctive[name], (
            f"field {name!r} did not round-trip: "
            f"wrote {distinctive[name]!r}, read back {getattr(loaded, name)!r}")
