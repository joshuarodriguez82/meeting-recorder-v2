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
from types import SimpleNamespace

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
    email_to: str = "user@example.com"
    recordings_dir: str = r"C:\Users\sampleuser\AppData\Local\MeetingRecorder"
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

    assert "sampleuser" not in blob
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
        "user@example.com",  # email
        "sampleuser",                      # username inside paths
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


# ── app_version: the field that was null in every real bundle ────────
#
# A real exported bundle came back with `"app_version": null` and every
# other field in versions.json populated. That file exists to say which
# build produced a report, so a null there costs a round trip on every
# single one.
#
# Root cause: only the third source below was ever implemented, and it
# only works in a dev checkout. A release build runs the backend out of
# the extracted runtime directory — which contains no `src-tauri/` and
# no `package.json` — and nothing set the env var. Both of the first two
# sources are new.


def test_app_version_prefers_the_value_the_shell_handed_down(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_APP_VERSION", "9.9.9")
    assert db.app_version() == "9.9.9"


def test_app_version_reads_the_stamp_shipped_in_the_runtime_bundle(
        tmp_path, monkeypatch):
    """zip-bundle.py writes app_version.txt next to server.py, so a
    packaged backend can name its own build with no help from the shell
    and no checkout on disk."""
    monkeypatch.delenv("MEETING_RECORDER_APP_VERSION", raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / db.APP_VERSION_FILE).write_text("2.37.0\n", encoding="utf-8")
    monkeypatch.setattr(db, "_backend_dir", lambda: runtime)
    assert db.app_version() == "2.37.0"


def test_app_version_falls_back_to_the_dev_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_RECORDER_APP_VERSION", raising=False)
    checkout = tmp_path / "checkout"
    (checkout / "src-tauri").mkdir(parents=True)
    (checkout / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"version": "2.37.0"}), encoding="utf-8")
    monkeypatch.setattr(db, "_backend_dir", lambda: checkout / "backend")
    assert db.app_version() == "2.37.0"


def test_app_version_is_none_rather_than_a_guess(tmp_path, monkeypatch):
    """A wrong version in a bug report is worse than a missing one —
    the lesson of the 2.7.5/2.7.6/2.7.7 tag incident in AGENTS.md."""
    monkeypatch.delenv("MEETING_RECORDER_APP_VERSION", raising=False)
    monkeypatch.setattr(db, "_backend_dir", lambda: tmp_path / "nowhere")
    assert db.app_version() is None


def test_gather_versions_populates_app_version(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_APP_VERSION", "2.37.0")
    assert db.gather_versions()["app_version"] == "2.37.0"


def test_exported_versions_json_carries_the_app_version(tmp_path, monkeypatch):
    """The end the user actually sees: the file inside the zip."""
    root = _make_log_dir(tmp_path)
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    monkeypatch.setenv("MEETING_RECORDER_APP_VERSION", "2.37.0")
    result = db.build_diagnostics_zip(
        settings=_Settings(), log_dir=root, out_dir=tmp_path / "out")
    with zipfile.ZipFile(result["path"]) as zf:
        versions = json.loads(zf.read("versions.json"))
    assert versions["app_version"] == "2.37.0"


def test_the_shell_passes_the_app_version_to_the_backend():
    """The env var above only helps if something sets it. Nothing did —
    that is the whole bug — so this pins the Rust side of the contract
    the way the log-rotation tests pin the shell's append-mode handle.
    """
    lib_rs = (Path(__file__).resolve().parents[2]
              / "src-tauri" / "src" / "lib.rs")
    if not lib_rs.exists():  # pragma: no cover - source-tree-only check
        pytest.skip("Tauri shell source not present in this tree")
    source = lib_rs.read_text(encoding="utf-8")
    assert '.env("MEETING_RECORDER_APP_VERSION"' in source, (
        "src-tauri/src/lib.rs must pass MEETING_RECORDER_APP_VERSION to the "
        "Python backend — without it a packaged build has no way to know "
        "its own version and versions.json goes back to null.")
    assert "app.package_info().version" in source


def test_zip_bundle_stamps_the_version_into_the_runtime_bundle():
    """The other half of the same contract: the stamp only exists if the
    packaging step writes it."""
    zip_bundle = Path(__file__).resolve().parents[2] / "zip-bundle.py"
    if not zip_bundle.exists():  # pragma: no cover - source-tree-only check
        pytest.skip("Packaging script not present in this tree")
    source = zip_bundle.read_text(encoding="utf-8")
    assert 'VERSION_FILE = "app_version.txt"' in source
    assert "zf.writestr(VERSION_FILE" in source


# ── the extension version the store actually recorded ────────────────
#
# `extension_last_seen_version` used to be the literal `None` in
# `gather_versions`, so every exported versions.json said null no matter
# what. A real field bundle exported at 21:04 carried an events.jsonl
# line for an import at 21:03:27 with `extension_version: "1.4.0"` — 37
# seconds earlier, in the SAME zip — while versions.json in that zip
# said null. Same defect as the pre-v2.38.0 `app_version: null`: the
# store knew, the export never asked. `extension_bundled_version`, one
# line above it in the same dict, was read from real data all along.


def _ext_settings(recordings_dir: Path) -> "_Settings":
    s = _Settings()
    s.recordings_dir = str(recordings_dir)
    return s


def _store_with(recordings_dir: Path, version, at="2026-08-19T21:03:27"):
    """Write the extension calendar store the way a real POST does —
    through the service itself rather than by hand-writing JSON, so this
    breaks if the recorded shape ever changes."""
    from datetime import datetime

    from services.extension_calendar_service import ExtensionCalendarService
    recordings_dir.mkdir(parents=True, exist_ok=True)
    svc = ExtensionCalendarService(recordings_dir)
    svc.record_extension_version(version, now=datetime.fromisoformat(at))
    return svc


def test_the_recorded_extension_version_reaches_versions_json(tmp_path):
    """The state that matters most: the extension posted and said which
    build it was. That string has to come out the other end."""
    rec = tmp_path / "recordings"
    _store_with(rec, "1.4.0")

    versions = db.gather_versions(_ext_settings(rec))

    assert versions["extension_last_seen_version"] == "1.4.0"
    assert versions["extension_last_seen_at"].startswith("2026-08-19T21:03:27")


def test_the_extension_version_is_read_not_hardcoded(tmp_path):
    """THE REGRESSION GUARD. A hardcoded field satisfies any
    single-value assertion, so this asserts against TWO different
    stores: no constant — null or otherwise — can satisfy both."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    _store_with(first, "1.4.0")
    _store_with(second, "1.2.0")

    got_first = db.gather_versions(_ext_settings(first))
    got_second = db.gather_versions(_ext_settings(second))

    assert got_first["extension_last_seen_version"] == "1.4.0"
    assert got_second["extension_last_seen_version"] == "1.2.0"


def test_never_posted_is_the_only_state_that_reads_as_null(tmp_path):
    """Nothing has ever POSTed — no store on disk at all. This is the
    ONE case null is the honest answer for, which is exactly why the
    other two must not use it."""
    versions = db.gather_versions(_ext_settings(tmp_path / "empty"))

    assert versions["extension_last_seen_version"] is None
    assert versions["extension_last_seen_at"] is None
    assert versions["extension_version_status"] in ("never_posted", "unknown")


def test_an_extension_too_old_to_report_its_version_says_so(tmp_path):
    """background.js only started sending
    `chrome.runtime.getManifest().version` in 1.2.0. An older build
    posts and reports nothing. Collapsing that into null makes it
    indistinguishable from "nothing has ever posted" — the difference
    between "your extension is ancient" and "your extension never ran",
    which are two completely different bug reports."""
    rec = tmp_path / "recordings"
    _store_with(rec, None)

    versions = db.gather_versions(_ext_settings(rec))

    assert versions["extension_last_seen_version"] is not None
    assert versions["extension_last_seen_version"] == (
        db.EXTENSION_VERSION_UNREPORTED)
    # The timestamp is what carries "it DID post" at the data level.
    assert versions["extension_last_seen_at"].startswith("2026-08-19T21:03:27")


def test_the_exported_zip_carries_the_recorded_extension_version(
        tmp_path, monkeypatch):
    """The end the user actually sends: the file inside the zip, built
    through the real `build_diagnostics_zip` rather than by calling
    `gather_versions` directly — the wiring between the two is the half
    that was missing."""
    root = _make_log_dir(tmp_path)
    monkeypatch.setenv("MEETING_RECORDER_LOG_DIR", str(root))
    rec = tmp_path / "recordings"
    _store_with(rec, "1.4.0")

    result = db.build_diagnostics_zip(
        settings=_ext_settings(rec), log_dir=root, out_dir=tmp_path / "out")

    with zipfile.ZipFile(result["path"]) as zf:
        versions = json.loads(zf.read("versions.json"))
    assert versions["extension_last_seen_version"] == "1.4.0"


def test_an_unreadable_store_degrades_to_null_rather_than_raising(
        tmp_path, monkeypatch):
    """A diagnostics export must survive anything — it is the thing the
    user runs when the app is already broken. A store that cannot be
    read reports the same shape as "never posted"."""
    from services.extension_calendar_service import ExtensionCalendarService

    def _explode(self, *a, **kw):
        raise RuntimeError("store is an un-downloaded cloud placeholder")

    monkeypatch.setattr(ExtensionCalendarService, "capture_status", _explode)

    versions = db.gather_versions(_ext_settings(tmp_path / "recordings"))
    assert versions["extension_last_seen_version"] is None


def test_the_export_never_leaks_the_recordings_path_it_read(tmp_path):
    """`recordings_dir` is presence-only in the redacted settings
    because it carries the user's account name. Using it to LOCATE the
    extension store must not put it into the output."""
    rec = tmp_path / "recordings"
    _store_with(rec, "1.4.0")

    versions = db.gather_versions(_ext_settings(rec))
    assert str(rec) not in json.dumps(versions)


# ── The capture recorder's counters reach the bundle (v2.44.0) ───────
#
# Extension 1.7 computed exactly the counters needed to tell its
# failure modes apart and kept them inside the extension, where the
# bundle could not see them. "Attendees still empty" therefore looked
# identical whether the recorder never installed, saw no responses at
# all, saw responses holding no meeting, or read meetings that matched
# no captured event — four causes, four different fixes, one symptom.


def test_capture_diag_reaches_the_bundle(tmp_path):
    from services.extension_calendar_service import ExtensionCalendarService
    from utils.diagnostics_bundle import extension_capture_diag

    ExtensionCalendarService(tmp_path).record_capture_diag({
        "recorderInstalled": True,
        "responsesSeen": 42,
        "responsesMatched": 0,
        "detailMatched": 0,
    })
    got = extension_capture_diag(
        SimpleNamespace(recordings_dir=str(tmp_path)))["extension_capture_diag"]

    # This exact shape — installed, saw traffic, matched nothing — is
    # the "URL/shape problem, not an install problem" case, and it must
    # be readable straight out of the zip.
    assert got["recorderInstalled"] is True
    assert got["responsesSeen"] == 42
    assert got["responsesMatched"] == 0
    assert got["at"]


def test_no_capture_reported_is_distinct_from_a_capture_that_found_nothing(tmp_path):
    from services.extension_calendar_service import ExtensionCalendarService
    from utils.diagnostics_bundle import extension_capture_diag

    settings = SimpleNamespace(recordings_dir=str(tmp_path))
    # Nothing reported: empty. NOT zeros — "no capture has run since
    # this existed" is a different fact from "a capture ran and saw
    # nothing", and conflating them is the defect this whole field
    # exists to end.
    assert extension_capture_diag(settings)["extension_capture_diag"] == {}

    ExtensionCalendarService(tmp_path).record_capture_diag({"responsesSeen": 0})
    after = extension_capture_diag(settings)["extension_capture_diag"]
    assert after["responsesSeen"] == 0
    assert after != {}


def test_capture_diag_stores_scalars_only(tmp_path):
    # The payload is opaque diagnostic data from the extension. Bound
    # what it can write into the store rather than trusting a future
    # extension build not to send something large or nested.
    from services.extension_calendar_service import ExtensionCalendarService

    svc = ExtensionCalendarService(tmp_path)
    svc.record_capture_diag({
        "responsesSeen": 3,
        "recorderInstalled": False,
        "sneakyUrl": "https://tenant.example/j/secret",
        "nested": {"subject": "Real Meeting Name"},
        "list": ["a.doe@globex.example"],
    })
    got = svc.last_capture_diag()
    assert got["responsesSeen"] == 3
    assert got["recorderInstalled"] is False
    # Strings and containers are dropped, so no URL, subject or address
    # can reach a bundle users paste into chat.
    assert "sneakyUrl" not in got
    assert "nested" not in got
    assert "list" not in got


# ── /health's version: the same lie, in a place users read ───────────
#
# `GET /health` hardcoded "2.0.0" from the day it was written. It is the
# ONE endpoint that needs no token, so it is what every external client
# probes first — and with v2.72.0 it acquired a reader: the MCP server's
# --doctor prints it. A user on 2.72.0 was told "backend reports version
# 2.0.0" the first time they connected an AI assistant.
#
# It shares app_version()'s sources, so it is right in a packaged build
# and in a dev checkout alike, and says "unknown" rather than inventing
# one when neither is readable.


def test_health_payload_reports_the_real_build(monkeypatch):
    monkeypatch.setenv("MEETING_RECORDER_APP_VERSION", "2.72.0")
    assert db.health_payload() == {"status": "ok", "version": "2.72.0"}


def test_health_payload_says_unknown_rather_than_inventing_a_version(
        tmp_path, monkeypatch):
    """Same rule as versions.json: a wrong version is worse than an
    absent one. 'unknown' is a statement; '2.0.0' was a fabrication."""
    monkeypatch.delenv("MEETING_RECORDER_APP_VERSION", raising=False)
    monkeypatch.setattr(db, "_backend_dir", lambda: tmp_path / "nowhere")
    assert db.health_payload() == {"status": "ok", "version": "unknown"}


def test_health_payload_keeps_the_status_key_the_watchdog_contract_needs():
    """The Rust watchdog only reads the HTTP status line, but every
    other caller reads `status`. Changing the version must not disturb
    the shape around it."""
    assert db.health_payload()["status"] == "ok"
