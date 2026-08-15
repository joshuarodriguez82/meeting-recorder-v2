"""
Coverage for utils/ml_memory.py — the shared "release PyTorch's caching
allocator after a batch of model work" helper.

The CI venv genuinely has no torch installed (see conftest.py's scope
note: "No FastAPI endpoints, no ML models, no network"), which is
exactly the environment this module has to degrade gracefully in
(requirements-cpu.txt installs, or any backend that never pulled in the
ML extras) — so most of these tests exercise the REAL absent-torch
path, not a simulation. One test additionally simulates torch being
present via sys.modules to prove the CUDA/MPS call shape without
needing a GPU in CI.
"""

import gc
import importlib
import sys
import types


def _fresh_ml_memory_module():
    """Re-import utils.ml_memory from scratch so its module-level
    `try: import torch` guard runs fresh under whatever sys.modules
    state the test has set up."""
    sys.modules.pop("utils.ml_memory", None)
    return importlib.import_module("utils.ml_memory")


def test_import_safe_when_torch_genuinely_absent():
    """The real CI environment: torch is not installed at all. Import
    must not raise, and torch must resolve to None inside the module."""
    # Force a clean re-import regardless of any prior test's sys.modules
    # mutations, with torch guaranteed absent for this import.
    saved_torch = sys.modules.pop("torch", None)
    try:
        mod = _fresh_ml_memory_module()
        assert mod.torch is None
    finally:
        if saved_torch is not None:
            sys.modules["torch"] = saved_torch


def test_cleanup_ml_memory_is_noop_when_torch_absent(monkeypatch):
    """cleanup_ml_memory() must be a safe, silent no-op (beyond a plain
    gc.collect()) when torch is missing — never an ImportError, never
    any other exception."""
    saved_torch = sys.modules.pop("torch", None)
    try:
        mod = _fresh_ml_memory_module()
        assert mod.torch is None
        calls = []
        monkeypatch.setattr(gc, "collect", lambda: calls.append("collected") or 0)
        mod.cleanup_ml_memory()  # must not raise
        assert calls == ["collected"]
    finally:
        if saved_torch is not None:
            sys.modules["torch"] = saved_torch
        else:
            sys.modules.pop("torch", None)


def test_cleanup_ml_memory_noop_survives_broken_torch_install():
    """A partially-broken torch install (import raises something other
    than ImportError, e.g. a truncated C-extension — see
    tests/_app_import.py's documented failure mode) must ALSO degrade
    to a no-op, not propagate. The module's import guard catches
    `Exception`, not just `ImportError`, specifically for this case."""
    import importlib.abc
    import importlib.machinery

    class _ExplodingTorchLoader(importlib.abc.Loader):
        def create_module(self, spec):
            return None  # use default module creation

        def exec_module(self, module):
            raise RuntimeError("simulated: corrupt torch C-extension")

    class _ExplodingTorchFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "torch":
                return importlib.machinery.ModuleSpec(
                    name, _ExplodingTorchLoader())
            return None

    sys.modules.pop("torch", None)
    finder = _ExplodingTorchFinder()
    sys.meta_path.insert(0, finder)
    try:
        mod = _fresh_ml_memory_module()
        assert mod.torch is None
        mod.cleanup_ml_memory()  # must not raise
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop("torch", None)


def test_cleanup_ml_memory_calls_cuda_path_when_available():
    """With a stubbed torch that reports CUDA available, cleanup must
    call empty_cache() AND ipc_collect() — the CUDA-only pair — and
    must NOT touch the MPS branch."""
    fake_torch = types.ModuleType("torch")
    calls = []
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: calls.append("cuda_empty_cache"),
        ipc_collect=lambda: calls.append("cuda_ipc_collect"),
    )
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(
            is_available=lambda: (_ for _ in ()).throw(
                AssertionError("must not check MPS when CUDA is available")),
            empty_cache=lambda: calls.append("mps_empty_cache"),
        )
    )
    sys.modules["torch"] = fake_torch
    try:
        mod = _fresh_ml_memory_module()
        assert mod.torch is fake_torch
        mod.cleanup_ml_memory()
        assert calls == ["cuda_empty_cache", "cuda_ipc_collect"]
    finally:
        sys.modules.pop("torch", None)


def test_cleanup_ml_memory_calls_mps_path_when_cuda_unavailable():
    """CUDA unavailable, MPS (Apple Silicon) available -> only the MPS
    branch runs, and ipc_collect (CUDA-only) is never touched."""
    fake_torch = types.ModuleType("torch")
    calls = []
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: False,
        empty_cache=lambda: calls.append("cuda_empty_cache"),
        ipc_collect=lambda: calls.append("cuda_ipc_collect"),
    )
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(
            is_available=lambda: True,
        )
    )
    fake_torch.mps = types.SimpleNamespace(
        empty_cache=lambda: calls.append("mps_empty_cache"),
    )
    sys.modules["torch"] = fake_torch
    try:
        mod = _fresh_ml_memory_module()
        assert mod.torch is fake_torch
        mod.cleanup_ml_memory()
        assert calls == ["mps_empty_cache"]
    finally:
        sys.modules.pop("torch", None)


def test_cleanup_ml_memory_swallows_driver_errors():
    """A GPU driver hiccup inside empty_cache() must never propagate out
    of cleanup_ml_memory() — callers rely on this running safely inside
    a `finally:` block."""
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: (_ for _ in ()).throw(RuntimeError("simulated driver error")),
        ipc_collect=lambda: None,
    )
    fake_torch.backends = types.SimpleNamespace(mps=None)
    sys.modules["torch"] = fake_torch
    try:
        mod = _fresh_ml_memory_module()
        mod.cleanup_ml_memory()  # must not raise
    finally:
        sys.modules.pop("torch", None)
