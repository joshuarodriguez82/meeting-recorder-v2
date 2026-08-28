"""
What the runtime bundle actually contains.

This is the guard for a failure mode the suite had no way to see:
`zip-bundle.py` and the code that reads its output are in different
languages, in different directories, and nothing connected them. v2.71.0
shipped a Settings card that told users to launch
``mcp-server/run_mcp_server.py`` from their install — while zip-bundle.py
had never packed ``mcp-server/`` at all. Both halves were individually
correct and the product was broken.

So this test runs the real packaging script and then asks the real
consumer — ``services.mcp_bundle_service`` — to find what it needs in
the extracted result. If someone removes a directory from
``EXTRA_ROOT_DIRS``, renames the launcher, or moves the runtime layout,
this fails at PR time rather than on a delivery consultant's laptop.

The zip is written to a tmp path, never over the repo's own
backend-bundle.zip.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from services import mcp_bundle_service as mbs

REPO_ROOT = Path(__file__).resolve().parents[2]
ZIP_SCRIPT = REPO_ROOT / "zip-bundle.py"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    """Build the real bundle once for this module."""
    out = tmp_path_factory.mktemp("bundle") / "backend-bundle.zip"
    proc = subprocess.run(
        [sys.executable, str(ZIP_SCRIPT), str(out)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file(), (
        "zip-bundle.py ignored its output argument and wrote elsewhere:\n"
        + proc.stdout + proc.stderr
    )
    return out


@pytest.fixture(scope="module")
def extracted(bundle: Path, tmp_path_factory) -> Path:
    """The bundle unpacked the way the Tauri shell unpacks it: flat,
    into <data_root>/runtime/. See ensure_runtime_extracted."""
    runtime = tmp_path_factory.mktemp("runtime")
    with zipfile.ZipFile(bundle) as z:
        z.extractall(runtime)
    return runtime


def test_server_py_is_at_the_root_of_the_bundle(extracted: Path):
    """The shell's essentials check keys off exactly this."""
    assert (extracted / "server.py").is_file()


def test_requirements_and_constraints_ship(extracted: Path):
    """First-launch venv bootstrap reads these out of the extracted
    runtime; without the constraints file it silently degrades to a
    floating resolve, which is how fresh installs broke in v2.8.0."""
    for name in ("requirements-cpu.txt", "requirements-mac.txt",
                 "constraints-cpu.txt", "constraints-mac.txt"):
        assert (extracted / name).is_file(), name


def test_the_mcp_server_is_where_the_backend_looks_for_it(extracted: Path):
    """The whole point: the consumer finds it, not just 'a file exists'."""
    found = mbs.find_bundled_mcp_dir(extracted)
    assert found == extracted / "mcp-server"
    assert (found / mbs.LAUNCHER_FILENAME).is_file()
    assert (found / "meeting_recorder_mcp" / "server.py").is_file()


def test_the_chrome_extension_still_ships(extracted: Path):
    """Same packaging path, older tenant — a regression in the walk
    would take both out together."""
    assert (extracted / "chrome-extension" / "manifest.json").is_file()


def test_mcp_tests_are_not_shipped(extracted: Path):
    """run_mcp_server.py puts its own directory on sys.path, so a
    shipped tests/__init__.py becomes an importable top-level `tests`
    package inside the AI client's process."""
    assert not (extracted / "mcp-server" / "tests").exists()


def test_no_bytecode_or_tool_caches_ship(bundle: Path):
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
    junk = [n for n in names
            if "__pycache__" in n or ".pytest_cache" in n or n.endswith(".pyc")]
    assert junk == [], junk


def test_the_version_marker_is_stamped(extracted: Path):
    """Every exported diagnostics bundle carried "app_version": null
    until this marker existed — the runtime dir has no tauri.conf.json
    to read a version out of."""
    marker = extracted / "app_version.txt"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip()
