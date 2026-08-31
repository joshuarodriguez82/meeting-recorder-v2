"""
The auto-index loop actually does the work.

THE BUG THIS EXISTS FOR
-----------------------
v2.77.0 shipped automatic Knowledge Folder indexing, on by default, and
it never indexed anything. `_auto_index_one_client` called
`svc.client_cfg_svc.all` — a method that does not exist; every other
call site in server.py uses `get_all()`. The loop's `except Exception`
caught the AttributeError, logged "Auto-index pass failed", and carried
on forever.

Fifteen tests covered the SCHEDULING and every one of them passed,
because the scheduling was correct. Nothing exercised the loop BODY,
so a one-word typo in the part that does the work shipped as a feature
that silently did nothing — which is the same defect this codebase keeps
producing, wearing a different hat: a failure that renders as a success.

So the body gets tested too, against fakes rather than a real folder:
the point is that it calls the right things in the right order, not that
embedding works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from _app_import import import_app

# import_app() returns the FastAPI app; the module itself is what
# holds the loop and its rotation state, so import it after.
import_app()
import server  # noqa: E402


class _Cfg:
    def __init__(self, folder: str) -> None:
        self.knowledge_folder = folder
        self.export_folder = ""


class _CfgSvc:
    """Only get_all/get — deliberately NOT an `all` attribute, so the
    original bug reproduces as an AttributeError rather than passing
    against an over-permissive Mock."""

    def __init__(self, configs: Dict[str, _Cfg]) -> None:
        self._configs = configs

    def get_all(self) -> Dict[str, _Cfg]:
        return dict(self._configs)

    def get(self, name: str):
        return self._configs.get(name)


@pytest.fixture(autouse=True)
def _reset_rotation():
    server._auto_index_last.clear()
    server._auto_index_last_pass = None
    yield
    server._auto_index_last.clear()
    server._auto_index_last_pass = None


def _wire(monkeypatch, tmp_path: Path, configs: Dict[str, _Cfg],
          calls: List[Any]):
    monkeypatch.setattr(server.svc, "client_cfg_svc", _CfgSvc(configs))
    monkeypatch.setattr(server.svc, "session_svc",
                        SimpleNamespace(recordings_dir=str(tmp_path)))
    monkeypatch.setattr(server.svc, "search_svc", None)
    monkeypatch.setattr(server.svc, "load_settings", lambda: None)
    monkeypatch.setattr(server, "_knowledge_embed_fn", lambda: (lambda t: t))

    def _fake_index(folder, client, embed_fn, recordings_dir):
        calls.append((folder, client))
        return {"indexed": 1, "unchanged": 0, "skipped": [], "total_chunks": 3}

    monkeypatch.setattr(server.document_service, "index_folder", _fake_index)


def test_it_actually_indexes_a_client(tmp_path: Path, monkeypatch):
    """THE REGRESSION. Against the shipped v2.77.0 code this raises
    AttributeError on `client_cfg_svc.all` and indexes nothing."""
    folder = tmp_path / "acme"
    folder.mkdir()
    calls: List[Any] = []
    _wire(monkeypatch, tmp_path, {"Acme": _Cfg(str(folder))}, calls)

    asyncio.run(server._auto_index_one_client())

    assert calls == [(str(folder), "Acme")]


def test_it_rotates_rather_than_re_indexing_the_same_client(
        tmp_path: Path, monkeypatch):
    """Least-recently-indexed first. Two passes must touch two
    different clients, or one folder starves every other."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    calls: List[Any] = []
    _wire(monkeypatch, tmp_path,
          {"Acme": _Cfg(str(a)), "Globex": _Cfg(str(b))}, calls)

    asyncio.run(server._auto_index_one_client())
    asyncio.run(server._auto_index_one_client())

    assert {c[1] for c in calls} == {"Acme", "Globex"}


def test_clients_without_a_knowledge_folder_are_skipped(
        tmp_path: Path, monkeypatch):
    calls: List[Any] = []
    _wire(monkeypatch, tmp_path, {"NoFolder": _Cfg("")}, calls)
    asyncio.run(server._auto_index_one_client())
    assert calls == []


def test_an_unreachable_folder_does_not_block_the_next_client(
        tmp_path: Path, monkeypatch):
    """A Drive path that is not synced must be skipped AND must advance
    the rotation, or one offline client starves the rest forever."""
    good = tmp_path / "good"
    good.mkdir()
    calls: List[Any] = []
    _wire(monkeypatch, tmp_path,
          {"Offline": _Cfg(str(tmp_path / "missing")),
           "Good": _Cfg(str(good))}, calls)

    asyncio.run(server._auto_index_one_client())   # picks Offline, skips
    asyncio.run(server._auto_index_one_client())   # must reach Good

    assert calls == [(str(good), "Good")]


def test_no_clients_at_all_is_not_an_error(tmp_path: Path, monkeypatch):
    calls: List[Any] = []
    _wire(monkeypatch, tmp_path, {}, calls)
    asyncio.run(server._auto_index_one_client())
    assert calls == []
