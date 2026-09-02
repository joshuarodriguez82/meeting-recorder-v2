"""
Diarization survives a GPU that fails at inference time.

THE GAP (finding 6 of the 2026-09-02 pipeline audit)
----------------------------------------------------
``_resolve_device`` probes CUDA, then MPS, then CPU, and
``DiarizationEngine.__init__`` falls back to CPU if ``pipeline.to(device)``
raises. That covers a device that rejects the model at LOAD.

It does not cover a device that fails during the actual pass. That is
the documented MPS failure mode — some pyannote layers have no MPS
kernel and only find out when they run — and it is also what an
out-of-memory CUDA card does on a long meeting. Either way
``self._pipeline(audio_path)`` raised, ``diarize`` turned it into
``RuntimeError("Diarization failed …")``, and the entire processing run
died with it.

What the app offered instead was a Settings dropdown reading "CPU —
avoids a known GPU conflict", which asks the user to perform the
fallback by hand, after a crash, from a panel served by the process that
crashed.

THE RULE
--------
One retry, on CPU, only when the first attempt was not already on CPU.
Never a second retry: if CPU fails too, the audio is the problem and
retrying is just a slower way to reach the same error.

The v2.24 Windows access-violation that motivated the manual setting is
still marked "working hypothesis" in the module docstring and is closed
nowhere. This fix is correct either way, which is the point of doing it
rather than continuing to reason about the crash.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from core.diarization import DiarizationEngine


class _FakeTorchDevice:
    def __init__(self, kind):
        self.type = kind

    def __eq__(self, other):
        return getattr(other, "type", None) == self.type

    def __repr__(self):
        return f"device({self.type})"


class _Pipeline:
    """A pyannote pipeline that fails on the first N calls.

    Records which device it was on for each attempt, because "retried"
    is only half the requirement — retrying on the SAME device would
    reproduce the failure and look identical from the outside.
    """

    def __init__(self, failures=1, exc=None):
        self._remaining = failures
        self._exc = exc or RuntimeError("MPS backend out of memory")
        self.device = _FakeTorchDevice("mps")
        self.attempts = []

    def to(self, device):
        self.device = device
        return self

    def __call__(self, audio_path, **kwargs):
        self.attempts.append(self.device.type)
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return _Diarization()


class _Diarization:
    def itertracks(self, yield_label=False):
        yield (types.SimpleNamespace(start=0.0, end=1.0), None, "SPEAKER_00")


# Never opened: the fake pipeline ignores it entirely. A real path here
# would only invite someone to think this test touches a file.
_FAKE_AUDIO = "(no such recording)"


def _engine(pipeline, device_kind="mps"):
    """A DiarizationEngine wired to a fake pipeline without loading
    pyannote — __init__ downloads a gated model, which no test can do."""
    eng = DiarizationEngine.__new__(DiarizationEngine)
    eng._pipeline = pipeline
    eng._max_speakers = 8
    eng._device = _FakeTorchDevice(device_kind)
    return eng


@pytest.fixture(autouse=True)
def _stub_torch(monkeypatch):
    """core.diarization imports torch lazily inside the retry path."""
    torch = types.ModuleType("torch")
    torch.device = _FakeTorchDevice
    monkeypatch.setitem(sys.modules, "torch", torch)
    yield


def test_a_gpu_failure_retries_on_cpu_and_succeeds():
    """The gap this closes: the run used to die here."""
    pipeline = _Pipeline(failures=1)
    engine = _engine(pipeline)

    turns = asyncio.run(engine.diarize(_FAKE_AUDIO))

    assert turns, "the retry produced no turns"
    assert pipeline.attempts == ["mps", "cpu"], (
        f"expected one GPU attempt then one CPU retry, got "
        f"{pipeline.attempts}")


def test_the_retry_actually_moves_the_pipeline_to_cpu():
    """Retrying on the same device reproduces the failure and would look
    identical from the outside — assert the move, not just the retry."""
    pipeline = _Pipeline(failures=1)
    asyncio.run(_engine(pipeline).diarize(_FAKE_AUDIO))
    assert pipeline.device.type == "cpu"


def test_a_cpu_failure_is_not_retried():
    """CPU is the fallback. If it fails, the audio is the problem, and a
    second identical attempt is a slower path to the same error."""
    pipeline = _Pipeline(failures=99)
    engine = _engine(pipeline, device_kind="cpu")
    pipeline.device = _FakeTorchDevice("cpu")

    with pytest.raises(RuntimeError):
        asyncio.run(engine.diarize(_FAKE_AUDIO))

    assert pipeline.attempts == ["cpu"]


def test_a_failure_on_both_devices_still_raises():
    """Degrading must not become swallowing. A run that cannot diarize
    has to fail loudly enough for the pipeline model to mark it — see
    test_pipeline_failure_is_visible.py."""
    pipeline = _Pipeline(failures=99)
    with pytest.raises(RuntimeError):
        asyncio.run(_engine(pipeline).diarize(_FAKE_AUDIO))
    assert pipeline.attempts == ["mps", "cpu"]


def test_the_error_names_the_original_failure_not_just_the_retry():
    """"Diarization failed" tells the user nothing. The reason the GPU
    fell over is the actionable part."""
    pipeline = _Pipeline(failures=99,
                         exc=RuntimeError("CUDA out of memory"))
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        asyncio.run(_engine(pipeline).diarize(_FAKE_AUDIO))


def test_a_working_gpu_is_left_alone():
    """No retry, no device move, no behaviour change for the machines
    where this already worked."""
    pipeline = _Pipeline(failures=0)
    asyncio.run(_engine(pipeline).diarize(_FAKE_AUDIO))
    assert pipeline.attempts == ["mps"]
    assert pipeline.device.type == "mps"
