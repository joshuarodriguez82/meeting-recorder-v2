"""
Shipping the Chrome extension inside the app, and telling a current
copy apart from a stale one (AGENTS.md build items #2/#3: "the app
cannot detect the mismatch" was the actual v2.28.0 field bug — the
extension never reported its version and nothing recorded one).

Covers:
  - reading the bundled version when the extension directory is
    present, and degrading clearly (None, never a raise) when it
    isn't — a dev checkout without the zip-bundle build must not 500.
  - export_extension_files copying every bundled file and reporting
    them, atomically-ish: a failure partway through must leave the
    PRIOR install untouched, never a half-written folder presented as
    a success.
  - export_dir() being stable across calls — the whole point of a
    "load unpacked once" workflow.
  - extension_version_status's distinct states.

No optional deps: pure filesystem + pure functions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.extension_bundle_service import (
    bundled_extension_version,
    export_dir,
    export_extension_files,
    extension_version_status,
    find_bundled_extension_dir,
)

REAL_FILES = {
    "background.js": "// background",
    "popup.html": "<html>popup</html>",
    "popup.js": "// popup",
    "options.html": "<html>options</html>",
    "options.js": "// options",
}


def _make_bundle(root: Path, version: str = "1.2.0", files=None,
                 backend_subdir: str = "runtime") -> Path:
    """Build a fake ``<backend_dir>/chrome-extension/`` tree under
    ``root`` and return the backend_dir to pass as ``backend_dir=``."""
    backend_dir = root / backend_subdir
    ext_dir = backend_dir / "chrome-extension"
    ext_dir.mkdir(parents=True)
    (ext_dir / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": version}),
        encoding="utf-8")
    for name, content in (files or REAL_FILES).items():
        (ext_dir / name).write_text(content, encoding="utf-8")
    return backend_dir


# ── locate / read the bundled version ───────────────────────────────

def test_find_bundled_extension_dir_present_alongside_server(tmp_path):
    backend_dir = _make_bundle(tmp_path)
    assert find_bundled_extension_dir(backend_dir) == backend_dir / "chrome-extension"


def test_find_bundled_extension_dir_absent_returns_none_not_raise(tmp_path):
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    assert find_bundled_extension_dir(backend_dir) is None


def test_find_bundled_extension_dir_checks_dev_checkout_sibling(tmp_path):
    # Dev-checkout layout: chrome-extension/ lives next to backend/,
    # not inside it (only the PACKAGED runtime bundles it as a child).
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    ext_dir = tmp_path / "chrome-extension"
    ext_dir.mkdir()
    (ext_dir / "manifest.json").write_text(
        json.dumps({"version": "1.2.0"}), encoding="utf-8")
    assert find_bundled_extension_dir(backend_dir) == ext_dir


def test_find_bundled_extension_dir_requires_a_manifest(tmp_path):
    # A directory that merely happens to be named chrome-extension/ but
    # has no manifest.json isn't a real bundle -- must not be picked up.
    backend_dir = tmp_path / "runtime"
    (backend_dir / "chrome-extension").mkdir(parents=True)
    assert find_bundled_extension_dir(backend_dir) is None


def test_bundled_extension_version_reads_manifest(tmp_path):
    backend_dir = _make_bundle(tmp_path, version="1.2.0")
    assert bundled_extension_version(backend_dir) == "1.2.0"


def test_bundled_extension_version_none_when_dir_absent(tmp_path):
    backend_dir = tmp_path / "empty"
    backend_dir.mkdir()
    assert bundled_extension_version(backend_dir) is None


def test_bundled_extension_version_none_on_malformed_manifest(tmp_path):
    backend_dir = tmp_path / "runtime"
    ext_dir = backend_dir / "chrome-extension"
    ext_dir.mkdir(parents=True)
    (ext_dir / "manifest.json").write_text("not json", encoding="utf-8")
    assert bundled_extension_version(backend_dir) is None


def test_bundled_extension_version_none_when_version_key_missing(tmp_path):
    backend_dir = tmp_path / "runtime"
    ext_dir = backend_dir / "chrome-extension"
    ext_dir.mkdir(parents=True)
    (ext_dir / "manifest.json").write_text(
        json.dumps({"manifest_version": 3}), encoding="utf-8")
    assert bundled_extension_version(backend_dir) is None


# ── export to the stable install folder ─────────────────────────────

def test_export_extension_files_writes_every_file_and_reports_them(tmp_path):
    backend_dir = _make_bundle(tmp_path)
    dest = tmp_path / "installed" / "chrome-extension"

    written = export_extension_files(dest=dest, backend_dir=backend_dir)

    expected = sorted(["manifest.json", *REAL_FILES.keys()])
    assert written == expected
    for rel in expected:
        assert (dest / rel).is_file(), f"{rel} missing from export"
    assert json.loads((dest / "manifest.json").read_text())["version"] == "1.2.0"


def test_export_extension_files_raises_clearly_when_no_bundle(tmp_path):
    backend_dir = tmp_path / "empty"
    backend_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        export_extension_files(dest=tmp_path / "out", backend_dir=backend_dir)


def test_export_extension_files_updates_an_existing_install_in_place(tmp_path):
    backend_dir_v1 = _make_bundle(tmp_path / "v1", version="1.1.0")
    dest = tmp_path / "installed" / "chrome-extension"
    export_extension_files(dest=dest, backend_dir=backend_dir_v1)
    assert json.loads((dest / "manifest.json").read_text())["version"] == "1.1.0"

    backend_dir_v2 = _make_bundle(tmp_path / "v2", version="1.2.0")
    written = export_extension_files(dest=dest, backend_dir=backend_dir_v2)

    # SAME path, contents replaced -- this is the entire point of a
    # stable install folder: "Install / Update" just rewrites it.
    assert json.loads((dest / "manifest.json").read_text())["version"] == "1.2.0"
    assert written == sorted(["manifest.json", *REAL_FILES.keys()])


def test_export_extension_files_failure_partway_leaves_prior_install_intact(
        tmp_path, monkeypatch):
    backend_dir_v1 = _make_bundle(tmp_path / "v1", version="1.2.0")
    dest = tmp_path / "installed" / "chrome-extension"

    # A real, successful install first, so `dest` holds a known-good
    # prior copy.
    export_extension_files(dest=dest, backend_dir=backend_dir_v1)
    prior_manifest = (dest / "manifest.json").read_text(encoding="utf-8")
    assert json.loads(prior_manifest)["version"] == "1.2.0"

    # A "new release" whose copy dies partway through.
    backend_dir_v2 = _make_bundle(tmp_path / "v2", version="1.3.0")

    import services.extension_bundle_service as mod
    real_copy2 = mod.shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full (simulated)")
        return real_copy2(src, dst)

    monkeypatch.setattr(mod.shutil, "copy2", flaky_copy2)

    with pytest.raises(OSError):
        export_extension_files(dest=dest, backend_dir=backend_dir_v2)

    # The failure must not have touched the real destination at all --
    # still the old, complete v1.2.0 copy, not a half-written v1.3.0
    # tree presented as if it were installed.
    assert (dest / "manifest.json").read_text(encoding="utf-8") == prior_manifest

    # No stray temp directory left next to it either.
    leftovers = [p for p in dest.parent.iterdir()
                if p.name.startswith(".chrome-extension-export-")]
    assert leftovers == []


def test_export_dir_is_stable_across_calls():
    first = export_dir()
    second = export_dir()
    assert first == second
    assert first.name == "chrome-extension"


# ── version mismatch classification ─────────────────────────────────

def test_status_never_posted_is_its_own_state():
    assert extension_version_status("1.2.0", None, None) == "never_posted"


def test_status_posted_without_a_version_is_unknown_not_current():
    assert extension_version_status(
        "1.2.0", None, "2026-08-14T09:00:00") == "unknown_version"


def test_status_older_than_bundled_flags_an_update():
    assert extension_version_status(
        "1.2.0", "1.1.0", "2026-08-14T09:00:00") == "update_available"


def test_status_equal_to_bundled_does_not_flag_an_update():
    assert extension_version_status(
        "1.2.0", "1.2.0", "2026-08-14T09:00:00") == "up_to_date"


def test_status_newer_than_bundled_counts_as_up_to_date():
    assert extension_version_status(
        "1.2.0", "1.3.0", "2026-08-14T09:00:00") == "up_to_date"


def test_status_unknown_when_bundled_version_unavailable():
    assert extension_version_status(
        None, "1.2.0", "2026-08-14T09:00:00") == "unknown"


def test_status_unparseable_last_seen_version_is_unknown_version():
    assert extension_version_status(
        "1.2.0", "not-a-version", "2026-08-14T09:00:00") == "unknown_version"
