"""/system/open-folder must not open or create arbitrary paths.

The endpoint's `kind:"path"` variant took any path the request named,
CREATED it if missing (mkdir -p as a side effect of an "open folder"
call), and handed it to the OS opener — `os.startfile` on Windows is
ShellExecute, which runs the default handler for whatever the path is.
It sits behind the auth token, so this was least-privilege hygiene
rather than an open door — but the only real caller (settings-view's
"show in folder" for a diagnostics export) only ever needs paths the
backend itself produced, inside its own roots.

Pinned properties:

  - kind:"path" resolves through the same containment the audio and
    screenshot endpoints use (scan roots), plus configured client
    export folders — the two places backend-produced paths live;
  - anything outside → 400, and the OS opener is NEVER invoked;
  - the mkdir side effect is gone for kind:"path": opening a folder
    must not create it, anywhere, and a missing-but-contained path is
    a 404 rather than a silent mkdir+open;
  - kind:"recordings" and kind:"client" keep their existing behavior,
    including mkdir (those directories are app-owned).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from _app_import import import_app

import_app()
import server  # noqa: E402


@pytest.fixture
def rig(monkeypatch, tmp_path: Path):
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    client_folder = tmp_path / "drive" / "AcmeExports"
    client_folder.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    monkeypatch.setattr(server.svc, "load_settings",
                        lambda: SimpleNamespace(recordings_dir=str(recordings)))
    server.svc.settings = SimpleNamespace(recordings_dir=str(recordings))
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(scan_roots=lambda: [recordings.resolve()]))
    monkeypatch.setattr(server.svc, "client_cfg_svc", SimpleNamespace(
        get=lambda name: (SimpleNamespace(export_folder=str(client_folder))
                          if name == "Acme" else None),
        get_all=lambda: {"Acme": SimpleNamespace(export_folder=str(client_folder))},
    ))

    opened: list[str] = []

    def fake_popen(argv, **_kw):
        opened.append(argv[-1])
        return SimpleNamespace(pid=1)

    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(server.os, "startfile", lambda p: opened.append(p),
                        raising=False)
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.append(u))
    return SimpleNamespace(recordings=recordings, client_folder=client_folder,
                           outside=outside, opened=opened, tmp=tmp_path)


def _call(**kw):
    return asyncio.run(server.open_folder(server.OpenFolderRequest(**kw)))


def test_path_outside_all_roots_is_refused_and_never_opened(rig):
    with pytest.raises(HTTPException) as e:
        _call(kind="path", path=str(rig.outside))
    assert e.value.status_code == 400
    assert rig.opened == []


def test_refused_path_is_not_created_either(rig):
    """The old code mkdir -p'd the arbitrary path before refusing was
    even possible — an 'open folder' call that WRITES to the filesystem
    at any location the request names."""
    target = rig.tmp / "made-by-attacker" / "deep"
    with pytest.raises(HTTPException):
        _call(kind="path", path=str(target))
    assert not target.exists()
    assert not target.parent.exists()


def test_traversal_out_of_a_root_is_refused(rig):
    sneaky = str(rig.recordings / ".." / "elsewhere")
    with pytest.raises(HTTPException) as e:
        _call(kind="path", path=sneaky)
    assert e.value.status_code == 400
    assert rig.opened == []


def test_path_inside_recordings_root_opens(rig):
    sub = rig.recordings / "diagnostics"
    sub.mkdir()
    result = _call(kind="path", path=str(sub))
    assert result["ok"] is True
    assert rig.opened and Path(rig.opened[0]).resolve() == sub.resolve()


def test_path_inside_a_client_export_folder_opens(rig):
    result = _call(kind="path", path=str(rig.client_folder))
    assert result["ok"] is True
    assert rig.opened


def test_contained_but_missing_path_is_404_not_mkdir(rig):
    ghost = rig.recordings / "never-made"
    with pytest.raises(HTTPException) as e:
        _call(kind="path", path=str(ghost))
    assert e.value.status_code == 404
    assert not ghost.exists()
    assert rig.opened == []


def test_kind_recordings_still_creates_and_opens(rig):
    """App-owned dir: mkdir-if-missing stays — this is the 'open my
    recordings folder' button working on a fresh install."""
    import shutil
    shutil.rmtree(rig.recordings)
    result = _call(kind="recordings")
    assert result["ok"] is True
    assert rig.recordings.exists()


def test_kind_client_still_works(rig):
    result = _call(kind="client", client="Acme")
    assert result["ok"] is True
    assert rig.opened
