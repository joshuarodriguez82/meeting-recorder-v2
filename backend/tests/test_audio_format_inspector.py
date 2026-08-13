"""
Tests for core/audio_format_inspector.py's subprocess isolation, result
cache, and kill switch.

Context: every confirmed STATUS_ACCESS_VIOLATION crash tracked across
v2.23.2 / v2.25.0 implicates pycaw/comtypes, called from this module's
get_device_mix_format(). As of v2.25.1 that function never runs pycaw
in the backend process — it spawns scripts/get_mix_format.py as a
child process, following the same pattern as
services/recording_service._run_finalize_subprocess. This test module
never imports pycaw/comtypes/pythoncom (not present in the CI venv) —
it exercises the subprocess-plumbing, caching, and kill-switch logic
by monkeypatching subprocess.run and the module's own platform check.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import audio_format_inspector as afi  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with an empty mix-format cache and ends by
    clearing it again, so tests can't leak state into each other."""
    afi.invalidate_mix_format_cache()
    yield
    afi.invalidate_mix_format_cache()


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ──────────────────────────────────────────────────────────────────────
# _get_device_mix_format_subprocess: degrade to None on every failure
# mode, following recording_service._run_finalize_subprocess's style.
# ──────────────────────────────────────────────────────────────────────

def test_subprocess_success_parses_result_line(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(
            returncode=0,
            stdout='RESULT {"sample_rate": 48000, "bits_per_sample": 24, "channels": 2}\n',
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = afi._get_device_mix_format_subprocess("Speakers (Realtek)", "output")
    assert result == {"sample_rate": 48000, "bits_per_sample": 24, "channels": 2}


def test_subprocess_success_with_null_result_is_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="RESULT null\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Nonexistent Device", "input") is None


def test_subprocess_nonzero_exit_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=1, stderr="boom")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_native_crash_exit_code_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        # e.g. 0xC0000005 on Windows, or a negative signal number on POSIX.
        return _FakeCompletedProcess(returncode=-1073741819, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_timeout_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_spawn_oserror_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("no such file")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_garbage_stdout_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="not json at all\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_malformed_json_result_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="RESULT {not valid json\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_non_dict_result_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="RESULT [1, 2, 3]\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


def test_subprocess_exit_zero_no_result_line_degrades_to_none(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="just some log noise\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert afi._get_device_mix_format_subprocess("Mic", "input") is None


# ──────────────────────────────────────────────────────────────────────
# get_device_mix_format: platform gate, kill switch, and caching.
# ──────────────────────────────────────────────────────────────────────

def test_non_windows_returns_none_without_spawning_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess",
                         lambda *a, **k: calls.append(1) or {"sample_rate": 1})
    monkeypatch.setattr(afi.sys, "platform", "linux")
    assert afi.get_device_mix_format("Mic", "input") is None
    assert calls == []


def test_kill_switch_disabled_returns_none_without_spawning_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess",
                         lambda *a, **k: calls.append(1) or {"sample_rate": 1})
    monkeypatch.setattr(afi.sys, "platform", "win32")
    assert afi.get_device_mix_format("Mic", "input", enabled=False) is None
    assert calls == []


def test_cache_returns_memoized_value_without_reinvoking_lookup(monkeypatch):
    call_count = {"n": 0}

    def fake_subprocess_lookup(device_name, kind):
        call_count["n"] += 1
        return {"sample_rate": 48000, "bits_per_sample": 24, "channels": 2}

    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess", fake_subprocess_lookup)
    monkeypatch.setattr(afi.sys, "platform", "win32")

    first = afi.get_device_mix_format("Speakers (Realtek)", "output")
    second = afi.get_device_mix_format("Speakers (Realtek)", "output")

    assert first == {"sample_rate": 48000, "bits_per_sample": 24, "channels": 2}
    assert second == first
    assert call_count["n"] == 1, "second call should be served from cache"


def test_cache_memoizes_negative_results_too(monkeypatch):
    call_count = {"n": 0}

    def fake_subprocess_lookup(device_name, kind):
        call_count["n"] += 1
        return None

    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess", fake_subprocess_lookup)
    monkeypatch.setattr(afi.sys, "platform", "win32")

    assert afi.get_device_mix_format("Ghost Device", "input") is None
    assert afi.get_device_mix_format("Ghost Device", "input") is None
    assert call_count["n"] == 1, "a cached None must not re-invoke the lookup"


def test_cache_is_keyed_per_device_and_kind(monkeypatch):
    seen = []

    def fake_subprocess_lookup(device_name, kind):
        seen.append((device_name, kind))
        return {"sample_rate": 16000, "bits_per_sample": 16, "channels": 1}

    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess", fake_subprocess_lookup)
    monkeypatch.setattr(afi.sys, "platform", "win32")

    afi.get_device_mix_format("Mic A", "input")
    afi.get_device_mix_format("Mic B", "input")
    afi.get_device_mix_format("Mic A", "output")

    assert len(seen) == 3, "distinct (device, kind) pairs must not share a cache entry"


def test_invalidate_mix_format_cache_clears_memoized_entries(monkeypatch):
    call_count = {"n": 0}

    def fake_subprocess_lookup(device_name, kind):
        call_count["n"] += 1
        return {"sample_rate": 44100, "bits_per_sample": 16, "channels": 2}

    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess", fake_subprocess_lookup)
    monkeypatch.setattr(afi.sys, "platform", "win32")

    afi.get_device_mix_format("Mic", "input")
    assert call_count["n"] == 1

    afi.invalidate_mix_format_cache()

    afi.get_device_mix_format("Mic", "input")
    assert call_count["n"] == 2, "invalidation must force a fresh lookup"


def test_audio_capture_invalidate_device_cache_also_clears_mix_format_cache(monkeypatch):
    """core.audio_capture.invalidate_device_cache() must hang off the
    same invalidation — a device-list change should also drop any
    cached mix-format lookups (see core/audio_capture.py)."""
    from core import audio_capture

    call_count = {"n": 0}

    def fake_subprocess_lookup(device_name, kind):
        call_count["n"] += 1
        return {"sample_rate": 44100, "bits_per_sample": 16, "channels": 2}

    monkeypatch.setattr(afi, "_get_device_mix_format_subprocess", fake_subprocess_lookup)
    monkeypatch.setattr(afi.sys, "platform", "win32")

    afi.get_device_mix_format("Mic", "input")
    assert call_count["n"] == 1

    audio_capture.invalidate_device_cache()

    afi.get_device_mix_format("Mic", "input")
    assert call_count["n"] == 2, (
        "audio_capture.invalidate_device_cache() should also clear "
        "audio_format_inspector's mix-format cache"
    )
