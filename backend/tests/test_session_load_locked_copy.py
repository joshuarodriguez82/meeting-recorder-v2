"""
A copy that will not open is not a session that does not exist.

THE FIELD REPRO (2026-09-10, Google Drive ``G:\\`` stream mount)
----------------------------------------------------------------
Merging two speakers on a session produced, in this order::

    POST /sessions/<id>/speakers/merge              200 OK
    GET  /sessions/<id>                             500 Internal Server Error
    POST /sessions/<id>/speakers/SPEAKER_03/confirm 404 Not Found

The merge WORKED — 153 segments were rewritten and the session saved.
Then the export it queued ran its archive step, which rewrote the copy
under the Drive mount and so made THAT copy the newest. The UI's
refresh 91ms later resolved to the newest copy and got::

    PermissionError: [Errno 13] Permission denied:
      'G:\\My Drive\\<archive>\\session_<id>.json'

because the sync client still had the file open. On Windows a sharing
violation is reported as EACCES, indistinguishable by errno from a real
permissions problem — but a file the app itself wrote 91ms earlier is
not one the user has lost access to.

``load()`` was a plain ``open()`` on the single newest copy, so the
request 500'd. The Speakers list therefore never refreshed, the user
clicked "Yes, this is …" on a speaker the merge had already removed,
and got "Speaker not on this session" — a message pointing at nothing
real, three steps downstream of the actual fault.

The local copy was readable the entire time.
"""

from __future__ import annotations

import builtins
import errno
import json
import time

import pytest

from services.session_service import SessionService


def _write(path, session_id, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": session_id,
        "display_name": marker,
        "speakers": {},
        "segments": [],
    }), encoding="utf-8")


@pytest.fixture
def two_roots(tmp_path):
    """A primary recordings dir and an archive root, as the roaming
    archive setup produces."""
    primary = tmp_path / "recordings"
    archive = tmp_path / "drive" / "archive"
    _write(primary / "session_S1.json", "S1", "local")
    _write(archive / "session_S1.json", "S1", "archive")
    # The archive copy is newest — which is exactly what the export's
    # archive step does right before the UI refreshes.
    time.sleep(0.01)
    (archive / "session_S1.json").touch()
    return SessionService(str(primary), extra_dirs=[str(archive)]), \
        primary, archive


def _lock(monkeypatch, locked_path, err=errno.EACCES):
    """Make exactly one path raise as a Windows sharing violation does."""
    real = builtins.open

    def _fake(file, *a, **kw):
        if str(file) == str(locked_path):
            raise PermissionError(err, "Permission denied", str(file))
        return real(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", _fake)
    # read_text_hydrated goes through Path.read_bytes, not open().
    from pathlib import Path as _P
    real_rb = _P.read_bytes

    def _fake_rb(self):
        if str(self) == str(locked_path):
            raise PermissionError(err, "Permission denied", str(self))
        return real_rb(self)

    monkeypatch.setattr(_P, "read_bytes", _fake_rb)


# ── The reported failure ────────────────────────────────────────────

def test_a_locked_newest_copy_falls_back_to_the_readable_one(
        two_roots, monkeypatch):
    """The whole incident in one assertion. The archive copy is newest
    and locked by the sync client; the local copy is fine. Before this,
    the request 500'd."""
    svc, primary, archive = two_roots
    _lock(monkeypatch, archive / "session_S1.json")

    data = svc.load("S1")

    assert data is not None, "a locked copy rendered as a missing session"
    assert data["display_name"] == "local"


def test_the_load_does_not_raise(two_roots, monkeypatch):
    """It 500'd because PermissionError escaped. Nothing above load()
    was catching it, and a read of a session the machine can read must
    not fail the request."""
    svc, primary, archive = two_roots
    _lock(monkeypatch, archive / "session_S1.json")
    svc.load("S1")  # must not raise


def test_load_full_survives_it_too(two_roots, monkeypatch):
    """load_full is what the merge, rename and confirm endpoints call —
    the actual path the user was on."""
    svc, primary, archive = two_roots
    _lock(monkeypatch, archive / "session_S1.json")
    session = svc.load_full("S1")
    assert session is not None and session.session_id == "S1"


@pytest.mark.parametrize("err", [errno.EACCES, errno.EBUSY])
def test_both_lock_errnos_are_survivable(two_roots, monkeypatch, err):
    """EACCES is what Windows reports for a sharing violation; EBUSY is
    the other shape a mount uses for the same condition."""
    svc, primary, archive = two_roots
    _lock(monkeypatch, archive / "session_S1.json", err=err)
    assert svc.load("S1") is not None


# ── Without a fallback available ────────────────────────────────────

def test_when_no_copy_opens_it_says_so_and_names_them(tmp_path, monkeypatch):
    """One unreadable copy and nothing to fall back to. This must not
    return None — "couldn't read it" rendering as "it isn't there" is
    the defect this module exists to end — and the error has to name
    what was tried, or the next report is another screenshot."""
    primary = tmp_path / "recordings"
    _write(primary / "session_S1.json", "S1", "local")
    svc = SessionService(str(primary))
    _lock(monkeypatch, primary / "session_S1.json")

    with pytest.raises(ValueError) as e:
        svc.load("S1")
    assert "S1" in str(e.value)
    assert "none could be read" in str(e.value)


def test_a_genuinely_absent_session_is_still_none(tmp_path):
    """The fallback must not turn "no such session" into an exception —
    callers 404 on None and that behaviour is load-bearing."""
    svc = SessionService(str(tmp_path / "recordings"))
    assert svc.load("nope") is None


# ── Corruption is not contagious ────────────────────────────────────

def test_a_corrupt_newest_copy_falls_back_to_an_intact_one(two_roots):
    """A half-written copy on the synced side must not cost the user a
    session that is intact locally."""
    svc, primary, archive = two_roots
    (archive / "session_S1.json").write_text("{ this is not json",
                                             encoding="utf-8")
    data = svc.load("S1")
    assert data is not None and data["display_name"] == "local"


def test_the_newest_readable_copy_still_wins(two_roots):
    """The fallback is for failure only. With both copies readable the
    canonical choice is unchanged — otherwise the list and the detail
    view would show different things."""
    svc, primary, archive = two_roots
    assert svc.load("S1")["display_name"] == "archive"


def test_candidates_are_ordered_newest_first_and_stably(two_roots):
    """Stable ordering matters: an unstable one would have the same
    session resolve to a different copy between two requests."""
    svc, primary, archive = two_roots
    first = svc._resolve_json_candidates("S1")
    assert [str(p) for p in first] == [
        str(p) for p in svc._resolve_json_candidates("S1")]
    assert first[0] == (archive / "session_S1.json")
