"""
A half-installed speech library is found and repaired at startup.

Field log 2026-09-15: fingerprinting off for every meeting, logged only
as "cannot import name 'DependencyGraph' from 'speechbrain.utils.depgraph'"
— files from two speechbrain versions side by side. The startup check
asked only whether the package existed, which it did.
"""

from __future__ import annotations

from types import SimpleNamespace

from core import dependency_health as dh
from core.fingerprint_status import FingerprintInputs, describe_missing_fingerprint

# The field error, user path removed.
FIELD_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "<string>", line 1, in <module>\n'
    "ImportError: cannot import name 'DependencyGraph' from "
    "'speechbrain.utils.depgraph' "
    "(C:\\Users\\<you>\\AppData\\Local\\MeetingRecorder\\.venv\\Lib\\"
    "site-packages\\speechbrain\\utils\\depgraph.py)")


class _Pip:
    """Stands in for subprocess.run: scripted import results, records
    every command."""

    def __init__(self, imports, pip_rc=0):
        self.imports = list(imports)
        self.pip_rc = pip_rc
        self.cmds = []

    def __call__(self, cmd, timeout):
        self.cmds.append(cmd)
        if cmd[1:3] == ["-m", "pip"]:
            return SimpleNamespace(returncode=self.pip_rc, stdout="",
                                   stderr="" if self.pip_rc == 0 else "boom")
        ok = self.imports.pop(0)
        return SimpleNamespace(returncode=0 if ok else 1, stdout="",
                               stderr="" if ok else FIELD_ERROR)


def test_the_field_error_is_a_half_installed_package():
    assert dh.classify_import_error(FIELD_ERROR) == "half_installed"
    assert dh.classify_import_error(
        "ModuleNotFoundError: No module named 'speechbrain'") == "missing"
    assert dh.classify_import_error("OSError: libsndfile") == "other"


def test_the_pin_is_read_from_the_constraints_file():
    assert dh.pinned_version("numpy==2.1.3\nspeechbrain==1.0.3\n") == "1.0.3"
    assert dh.pinned_version("SpeechBrain == 1.0.3 ; python_version>'3'") == "1.0.3"
    assert dh.pinned_version("numpy==2.1.3\n") is None


def test_a_healthy_install_is_left_alone():
    pip = _Pip([True])
    assert dh.check_and_repair(run=pip).state == "ok"
    assert len(pip.cmds) == 1


def test_a_half_installed_package_is_reinstalled_at_its_pin(tmp_path):
    c = tmp_path / "constraints-cpu.txt"
    c.write_text("speechbrain==1.0.3\n", encoding="utf-8")
    pip = _Pip([False, True])
    health = dh.check_and_repair(constraints=c, run=pip)
    assert health.state == "repaired"
    install = pip.cmds[1]
    assert "--force-reinstall" in install and "--no-deps" in install
    assert install[-1] == "speechbrain==1.0.3"


def test_a_failed_repair_is_reported_in_the_users_terms(tmp_path):
    pip = _Pip([False], pip_rc=1)
    health = dh.check_and_repair(run=pip)
    assert health.state == "broken"
    assert "reinstalling Meeting Recorder" in health.user_reason()


def test_a_repair_that_still_does_not_import_is_broken():
    pip = _Pip([False, False])
    assert dh.check_and_repair(run=pip).state == "broken"


def test_other_import_failures_are_not_reinstalled():
    """Only the half-installed shape triggers a reinstall; anything else
    is reported, not 'fixed' by guesswork."""
    class Other(_Pip):
        def __call__(self, cmd, timeout):
            self.cmds.append(cmd)
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="OSError: [WinError 126] torch")
    pip = Other([])
    assert dh.check_and_repair(run=pip).state == "error"
    assert len(pip.cmds) == 1


def test_the_speaker_tab_names_the_cause():
    msg = describe_missing_fingerprint(FingerprintInputs(
        encoder_available=False,
        encoder_problem=dh.EncoderHealth("broken").user_reason()))
    assert "damaged" in msg and "reinstalling Meeting Recorder" in msg
    plain = describe_missing_fingerprint(FingerprintInputs(
        encoder_available=False))
    assert plain.endswith("known-speakers list")
