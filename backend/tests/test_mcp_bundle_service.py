"""
Turning the MCP server on from inside the app.

The gap this covers: v2.71.0 shipped an "AI assistant access" card that
told the user to `cd mcp-server`, build a venv and paste
``/absolute/path/to/mcp-server/.venv/bin/python``. Anyone who INSTALLED
the app rather than cloning the repo has no ``mcp-server/`` directory at
all, so those instructions could not be followed on the machine the card
was displayed on. The fix ships ``mcp-server/`` inside the runtime
bundle (zip-bundle.py) and resolves the two things the user cannot know
— which interpreter to launch and where the launcher lives — from the
running backend itself.

Covers:
  - locating the bundled ``mcp-server/`` in BOTH layouts: packaged
    (sibling of server.py inside the extracted runtime) and dev
    checkout (sibling of backend/ at the repo root), and returning
    None — never raising — when neither exists.
  - refusing a directory that exists but has no importable package in
    it, because "found it" and "it works" must not be the same answer.
  - preferring a console ``python.exe`` over the ``pythonw.exe`` the
    backend itself runs under on Windows: an MCP client speaks stdio to
    whatever we name here.
  - status() reporting bundled/installed independently, so "the files
    aren't here" and "the SDK isn't installed yet" are distinguishable
    in the UI rather than collapsing into one dead end.

Pure filesystem + pure functions. No pip is ever run from the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from services import mcp_bundle_service as mbs


def _make_package(root: Path) -> Path:
    """A minimally credible bundled mcp-server/ tree."""
    pkg = root / "meeting_recorder_mcp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "server.py").write_text("", encoding="utf-8")
    (root / mbs.LAUNCHER_FILENAME).write_text("", encoding="utf-8")
    return root


class TestFindBundledMcpDir:
    def test_packaged_layout_sibling_of_server_py(self, tmp_path: Path):
        runtime = tmp_path / "runtime"
        (runtime / "services").mkdir(parents=True)
        _make_package(runtime / "mcp-server")
        assert mbs.find_bundled_mcp_dir(runtime) == runtime / "mcp-server"

    def test_dev_checkout_layout_sibling_of_backend(self, tmp_path: Path):
        backend = tmp_path / "backend"
        backend.mkdir()
        _make_package(tmp_path / "mcp-server")
        assert mbs.find_bundled_mcp_dir(backend) == tmp_path / "mcp-server"

    def test_packaged_layout_wins_over_repo_root(self, tmp_path: Path):
        """Both can exist when a release binary is run from a checkout.
        The extracted runtime is the copy that matches the running
        backend, so it must be the one we hand out."""
        backend = tmp_path / "backend"
        backend.mkdir()
        _make_package(backend / "mcp-server")
        _make_package(tmp_path / "mcp-server")
        assert mbs.find_bundled_mcp_dir(backend) == backend / "mcp-server"

    def test_missing_returns_none(self, tmp_path: Path):
        assert mbs.find_bundled_mcp_dir(tmp_path / "nowhere") is None

    def test_directory_without_package_is_not_accepted(self, tmp_path: Path):
        """An empty or partially-extracted mcp-server/ must read as
        absent. Handing back a path whose launcher does not import
        produces a client that fails with no diagnosable error."""
        backend = tmp_path / "backend"
        backend.mkdir()
        (tmp_path / "mcp-server").mkdir()
        assert mbs.find_bundled_mcp_dir(backend) is None

    def test_package_without_launcher_is_not_accepted(self, tmp_path: Path):
        backend = tmp_path / "backend"
        backend.mkdir()
        root = tmp_path / "mcp-server"
        pkg = root / "meeting_recorder_mcp"
        pkg.mkdir(parents=True)
        (pkg / "server.py").write_text("", encoding="utf-8")
        assert mbs.find_bundled_mcp_dir(backend) is None


class TestLauncherPython:
    def test_prefers_console_python_over_pythonw(self, tmp_path: Path):
        """On Windows the backend runs under pythonw.exe (no console).
        An MCP client launches the server and talks stdio to it, so we
        name the console interpreter from the same venv when there is
        one."""
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        (scripts / "pythonw.exe").write_text("", encoding="utf-8")
        (scripts / "python.exe").write_text("", encoding="utf-8")
        assert mbs.client_interpreter(scripts / "pythonw.exe") == scripts / "python.exe"

    def test_keeps_the_interpreter_when_there_is_no_console_sibling(
        self, tmp_path: Path
    ):
        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        (scripts / "pythonw.exe").write_text("", encoding="utf-8")
        assert mbs.client_interpreter(scripts / "pythonw.exe") == scripts / "pythonw.exe"

    def test_posix_interpreter_is_returned_unchanged(self, tmp_path: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        py = bin_dir / "python3"
        py.write_text("", encoding="utf-8")
        assert mbs.client_interpreter(py) == py


class TestStatus:
    def test_reports_bundled_and_installed_separately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runtime = tmp_path / "runtime"
        (runtime / "services").mkdir(parents=True)
        _make_package(runtime / "mcp-server")
        monkeypatch.setattr(mbs, "sdk_installed", lambda: False)
        st = mbs.status(runtime)
        assert st["bundled"] is True
        assert st["installed"] is False
        assert st["ready"] is False
        assert st["launcher"] == str(runtime / "mcp-server" / mbs.LAUNCHER_FILENAME)
        assert st["python"] == str(mbs.client_interpreter(Path(sys.executable)))

    def test_ready_only_when_both_halves_are_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runtime = tmp_path / "runtime"
        (runtime / "services").mkdir(parents=True)
        _make_package(runtime / "mcp-server")
        monkeypatch.setattr(mbs, "sdk_installed", lambda: True)
        assert mbs.status(runtime)["ready"] is True

    def test_not_bundled_is_a_reportable_state_not_an_error(self, tmp_path: Path):
        """A dev checkout that was never run through zip-bundle.py, and
        a corrupted extraction, both land here. Neither may raise."""
        st = mbs.status(tmp_path / "nowhere")
        assert st["bundled"] is False
        assert st["ready"] is False
        assert st["launcher"] is None
