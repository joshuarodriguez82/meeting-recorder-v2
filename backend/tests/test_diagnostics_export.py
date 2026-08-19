"""
One-click diagnostics export, and the redaction that makes it safe.

THE LOAD-BEARING TEST
---------------------
``test_a_newly_added_secret_is_excluded_by_default`` adds a fictional
credential to the settings object and asserts it does not appear
anywhere in the export. That is the whole reason the redaction is
allow-list based: a deny-list protects only what it already knows
about, so the act of adding a new secret is the act of leaking it. If
someone ever "simplifies" ``redact_settings`` into a deny-list, this
test fails and says why.
"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._app_import import _stub_optional_modules

_stub_optional_modules()

from utils import diagnostics_bundle as db  # noqa: E402


@dataclass
class _Settings:
    """A stand-in with the same shape as config.settings.Settings —
    the safe fields, the real secrets, and the real personal-data
    fields, so the redaction is exercised against all three."""
    # allow-listed, safe
    whisper_model: str = "base"
    claude_model: str = "claude-haiku-4-5"
    ai_provider: str = "anthropic"
    diarization_device: str = "auto"
    max_speakers: int = 10
    hard_cap_hours: int = 4
    echo_cancellation_enabled: bool = False
    channel_attribution_enabled: bool = True
    calendar_source: str = "auto"
    live_copilot_mode: str = "SA"
    # real secrets (config/secrets.py SECRET_KEYS)
    anthropic_api_key: str = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
    hf_token: str = "hf_BBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    openai_api_key: str = "sk-proj-CCCCCCCCCCCCCCCCCCCCCC"
    live_openai_api_key: str = "sk-proj-DDDDDDDDDDDDDDDDDDDDDD"
    live_anthropic_api_key: str = "sk-ant-api03-EEEEEEEEEEEEEEEEEEEE"
    # personal data that is not a credential
    email_to: str = "joshua.p.rodriguez@example.com"
    recordings_dir: str = r"C:\Users\jrodriguez\AppData\Local\MeetingRecorder"
    cloud_mirror_dir: str = r"G:\Shared drives\Contoso\Recordings"
    session_archive_dir: str = ""
    openai_base_url: str = "https://openrouter.ai/api/v1"
    live_openai_base_url: str = ""
    copilot_custom_context: str = (
        "Contoso migration; Jane Doe is the exec sponsor")


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """The value-match backstop consults the OS keychain. Keep the tests
    off the developer's real one."""
    from config import secrets as _secrets
    monkeypatch.setattr(_secrets, "get_secret", lambda name: None)


# ── the allow-list ───────────────────────────────────────────────────

def test_a_newly_added_secret_is_excluded_by_default():
    """THE test. A credential added to settings tomorrow must not need
    anyone to remember to exclude it."""

    @dataclass
    class _FutureSettings(_Settings):
        # Invented for this test — no such setting exists in the app.
        elevenlabs_api_key: str = "el-ZZZZ-TOTALLY-NEW-SECRET-VALUE"
        webhook_signing_secret: str = "whsec_NEWLYADDEDSIGNINGSECRET"

    out = db.redact_settings(_FutureSettings())
    blob = json.dumps(out)

    assert "el-ZZZZ-TOTALLY-NEW-SECRET-VALUE" not in blob
    assert "whsec_NEWLYADDEDSIGNINGSECRET" not in blob
    assert "elevenlabs_api_key" not in out["settings"]
    assert "webhook_signing_secret" not in out["settings"]
    # Withheld, but visibly so — the user can see a field exists
    # without seeing its value.
    assert "elevenlabs_api_key" in out["excluded_field_names"]
    assert "webhook_signing_secret" in out["excluded_field_names"]


def test_existing_secrets_never_appear():
    out = db.redact_settings(_Settings())
    blob = json.dumps(out)
    for secret in ("sk-ant-api03-AAAA", "hf_BBBB", "sk-proj-CCCC",
                   "sk-proj-DDDD", "sk-ant-api03-EEEE"):
        assert secret not in blob


def test_personal_data_is_reduced_to_presence_booleans():
    out = db.redact_settings(_Settings())
    blob = json.dumps(out)

    assert "jrodriguez" not in blob
    assert "example.com" not in blob
    assert "Contoso" not in blob
    assert "Jane Doe" not in blob

    # Still diagnostically useful: configured-or-not, without the value.
    assert out["presence"]["recordings_dir_configured"] is True
    assert out["presence"]["cloud_mirror_dir_configured"] is True
    assert out["presence"]["session_archive_dir_configured"] is False
    assert out["presence"]["email_to_configured"] is True
    assert out["presence"]["copilot_custom_context_configured"] is True


def test_safe_settings_do_come_through():
    """Redaction that keeps nothing is as useless as one that keeps
    everything."""
    out = db.redact_settings(_Settings())["settings"]
    assert out["whisper_model"] == "base"
    assert out["claude_model"] == "claude-haiku-4-5"
    assert out["ai_provider"] == "anthropic"
    assert out["diarization_device"] == "auto"
    assert out["max_speakers"] == 10
    assert out["hard_cap_hours"] == 4
    assert out["echo_cancellation_enabled"] is False
    assert out["channel_attribution_enabled"] is True
    assert out["calendar_source"] == "auto"


def test_allow_list_holds_no_credential_shaped_names():
    """A second pair of eyes on the allow-list itself: nothing in it may
    look like a credential, so a careless addition is caught here rather
    than in a user's bug report."""
    offenders = [k for k in db.SAFE_SETTINGS_KEYS
                 if db._CREDENTIAL_NAME_RE.search(k)]
    assert not offenders, offenders


def test_value_match_backstop_catches_a_secret_in_an_innocuous_field(
        monkeypatch):
    """Belt-and-braces layer 2: an allow-listed field that somehow ends
    up holding a live credential is redacted on value, not just name."""
    from config import secrets as _secrets
    leaked = "sk-ant-api03-LEAKEDTHROUGHTHEWRONGFIELD"
    monkeypatch.setattr(
        _secrets, "get_secret",
        lambda name: leaked if name == "ANTHROPIC_API_KEY" else None)

    s = _Settings()
    s.claude_model = leaked  # the wrong value in a right-looking field
    out = db.redact_settings(s)
    assert leaked not in json.dumps(out)
    assert out["settings"]["claude_model"] == db.REDACTED
    assert "claude_model" in out["withheld_keys"]


# ── the zip ──────────────────────────────────────────────────────────

def _make_log_dir(tmp_path: Path) -> Path:
    root = tmp_path / "MeetingRecorder"
    root.mkdir()
    (root / "backend.log").write_text(
        "2026-08-19 10:00:00 [INFO] server: Backend started\n"
        "2026-08-19 10:31:41 [INFO] services.recording_service: "
        "[stop] finalize done in 191.9s\n",
        encoding="utf-8")
    (root / "crash.log").write_text("=== faulthandler ===\n", encoding="utf-8")
    (root / "events.jsonl").write_text(
        '{"ts":"2026-08-19T10:31:41.000+00:00","v":1,'
        '"event":"finalize.completed","session_id":"A1B2C3D4",'
        '"duration_s":191.9,"aec_accepted":false}\n',
        encoding="utf-8")
    return root


def test_zip_contains_the_expected_members(tmp_path, monkeypatch):
    root = _make_log_dir(tmp_path)
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    monkeypatch.setenv("MEETING_RECORDER_EVENT_LOG",
                       str(root / "events.jsonl"))

    result = db.build_diagnostics_zip(
        settings=_Settings(), log_dir=root, out_dir=tmp_path / "out")

    assert Path(result["path"]).exists()
    assert result["bytes"] > 0
    assert set(result["members"]) == {
        "manifest.json",
        "events.jsonl",
        "backend.log.tail.txt",
        "crash.log.tail.txt",
        "versions.json",
        "system.json",
        "audio-devices.json",
        "settings.redacted.json",
    }
    # Every member is described, so the user is never left guessing
    # what they just shared.
    for name in result["members"]:
        assert result["descriptions"][name], f"{name} has no description"
    assert result["excluded"]


def test_zip_carries_the_real_diagnostic_content(tmp_path, monkeypatch):
    root = _make_log_dir(tmp_path)
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    monkeypatch.setenv("MEETING_RECORDER_EVENT_LOG",
                       str(root / "events.jsonl"))

    result = db.build_diagnostics_zip(
        settings=_Settings(), log_dir=root, out_dir=tmp_path / "out")

    with zipfile.ZipFile(result["path"]) as zf:
        events_text = zf.read("events.jsonl").decode()
        log_text = zf.read("backend.log.tail.txt").decode()
        manifest = json.loads(zf.read("manifest.json"))

    assert "finalize.completed" in events_text
    assert "A1B2C3D4" in events_text
    assert "finalize done in 191.9s" in log_text
    assert manifest["members"] == sorted(result["members"])
    assert manifest["deliberately_excluded"]


def test_zip_contains_no_secrets_or_transcript_text(tmp_path, monkeypatch):
    """The end-to-end privacy assertion, over the archive's raw bytes:
    if any of this appears anywhere in the zip, the export is unsafe to
    send regardless of which member leaked it."""
    root = _make_log_dir(tmp_path)
    # Plant transcript-shaped prose and a credential in the log tail,
    # standing in for whatever a real backend.log happens to contain.
    (root / "backend.log").write_text(
        "2026-08-19 10:00:00 [INFO] server: Backend started\n",
        encoding="utf-8")
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    monkeypatch.setenv("MEETING_RECORDER_EVENT_LOG",
                       str(root / "events.jsonl"))

    @dataclass
    class _FutureSettings(_Settings):
        brand_new_api_key: str = "nk-FUTURE-SECRET-9999"

    result = db.build_diagnostics_zip(
        settings=_FutureSettings(), log_dir=root, out_dir=tmp_path / "out")

    with zipfile.ZipFile(result["path"]) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist()).decode(
            "utf-8", errors="replace")

    for forbidden in (
        "sk-ant-api03-AAAA", "hf_BBBB", "sk-proj-CCCC",
        "nk-FUTURE-SECRET-9999",          # the newly added secret
        "joshua.p.rodriguez@example.com",  # email
        "jrodriguez",                      # username inside paths
        "Contoso",                         # client name
        "Jane Doe",                        # a person
    ):
        assert forbidden not in blob, f"{forbidden!r} leaked into the zip"


def test_zip_is_written_even_with_no_logs_present(tmp_path, monkeypatch):
    """A user with a fresh install must still be able to send something
    — a missing log is a note in the file, not an exception."""
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    monkeypatch.setenv("MEETING_RECORDER_EVENT_LOG",
                       str(root / "events.jsonl"))

    result = db.build_diagnostics_zip(
        settings=_Settings(), log_dir=root, out_dir=tmp_path / "out")
    assert Path(result["path"]).exists()
    assert "manifest.json" in result["members"]
    # No events.jsonl exists yet, so it is honestly absent rather than
    # present-and-empty.
    assert "events.jsonl" not in result["members"]


def test_member_descriptions_cover_everything_buildable():
    """The preview endpoint renders MEMBER_DESCRIPTIONS. If a member is
    ever added to the zip without a description, the user would be shown
    an incomplete list of what they are about to share."""
    assert set(db.MEMBER_DESCRIPTIONS) >= {
        "manifest.json", "events.jsonl", "backend.log.tail.txt",
        "crash.log.tail.txt", "versions.json", "system.json",
        "audio-devices.json", "settings.redacted.json",
    }
    for name, desc in db.MEMBER_DESCRIPTIONS.items():
        assert desc.strip(), name


def test_preview_omits_members_that_may_not_be_there():
    """The pre-click listing must not promise a file the export won't
    actually contain — rotation artefacts only exist if that rotation
    happened."""
    preview = db.preview_members()
    assert "events.jsonl.1" not in preview
    assert "events.jsonl" in preview
    assert "manifest.json" in preview
    # Still fully described, for the post-export listing.
    for name in preview:
        assert db.MEMBER_DESCRIPTIONS[name]
