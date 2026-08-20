"""
One-click diagnostics export — the zip a user attaches to a bug report.

WHY
---
Five field bugs in one day were diagnosed by hand-writing five one-off
``.bat`` scripts and talking the user through running each of them. This
module is that workflow collapsed into a button: everything those
scripts went looking for, gathered once, into one file the user can send.

WHAT GOES IN
------------
See :data:`MEMBER_DESCRIPTIONS` — it is also what the UI shows the user,
so the list can never drift from the contents.

WHAT NEVER GOES IN
------------------
No transcripts. No audio. No meeting titles. No attendee names. No email
addresses. No file paths. No secrets.

The settings snapshot is the dangerous part, and it is **allow-list**
based: :data:`SAFE_SETTINGS_KEYS` is the complete list of setting names
permitted into the export, and :func:`redact_settings` iterates over
*that*, never over the ``Settings`` object's own fields. The consequence
is the one that matters — **a setting added tomorrow is excluded by
default**. A new API key, a new webhook URL, a new folder path leaks
nothing until someone deliberately adds its name here, at which point
they are looking straight at this docstring.

A deny-list would have the opposite failure mode: it protects only what
it already knows about, so the very act of adding a new secret leaks it.
That is the mistake this module exists to make structurally impossible,
and ``test_diagnostics_export.py`` pins it with a fictional new secret
field that must not appear in the output.

Three further passes run on top of the allow-list, each one belt-and-
braces rather than the primary defence:

1. any allow-listed name that *looks* credential-shaped
   (key/token/secret/password/credential) is dropped anyway;
2. any value that string-matches a live secret — read from the
   ``Settings`` object and from the OS keychain — is dropped, which
   catches a credential that leaked sideways into an innocuous field;
3. values must be scalars, and strings must be short and free of the
   characters that show up in prose, paths and addresses.

Folder settings (recordings dir, cloud mirror, session archive) are
genuinely useful to a diagnosis and are genuinely full of the user's
account name, so they are reported as ``*_configured: true/false``
presence booleans instead of values — see :data:`PRESENCE_ONLY_KEYS`.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import zipfile
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

EXPORT_SUBDIR = "diagnostics"

#: backend.log can be 200 MB+. The tail is what carries the failure.
BACKEND_LOG_TAIL_BYTES = 2 * 1024 * 1024
CRASH_LOG_TAIL_BYTES = 256 * 1024


# ── settings redaction ───────────────────────────────────────────────

#: The COMPLETE set of setting names allowed into the export. Adding a
#: name here is a deliberate act; forgetting to add one costs a slightly
#: thinner bug report, which is the correct direction to fail in.
SAFE_SETTINGS_KEYS = frozenset({
    # models / providers — which engine, never which credential
    "whisper_model",
    "claude_model",
    "ai_provider",
    "live_ai_provider",
    "live_claude_model",
    "diarization_device",
    "max_speakers",
    # behaviour toggles
    "auto_process_after_stop",
    "launch_on_startup",
    "auto_follow_up_email",
    "live_transcription_enabled",
    "live_copilot_enabled",
    "live_vad_enabled",
    "live_speaker_split_enabled",
    "auto_record_enabled",
    "today_view_enabled",
    "auto_prep_brief_enabled",
    "retention_enabled",
    "audio_mix_format_lookup_enabled",
    "echo_cancellation_enabled",
    "session_index_enabled",
    "channel_attribution_enabled",
    "calendar_source",
    # numeric tuning
    "notify_minutes_before",
    "retention_processed_days",
    "retention_unprocessed_days",
    "auto_screenshot_interval_minutes",
    "silence_warn_min",
    "silence_stop_min",
    "overrun_warn_min",
    "overrun_stop_min",
    "hard_cap_hours",
    "auto_prep_brief_lead_min",
    "live_copilot_wide_interval_sec",
    "live_copilot_hot_interval_sec",
    # co-pilot persona names — these are the app's own preset labels
    # ("SA", "General"), not user prose. copilot_custom_context, which
    # IS user prose and routinely names a client, is not here.
    "live_copilot_mode",
    "live_copilot_meeting_type",
})

#: Reported as ``"<name>_configured": bool`` — the value itself is a
#: filesystem path containing the user's account name, or an address.
PRESENCE_ONLY_KEYS = (
    "recordings_dir",
    "cloud_mirror_dir",
    "session_archive_dir",
    "email_to",
    "copilot_custom_context",
    "openai_base_url",
    "live_openai_base_url",
)

_CREDENTIAL_NAME_RE = re.compile(
    r"key|token|secret|password|passwd|credential", re.IGNORECASE)

# Model ids legitimately contain "/" and ":" ("meta-llama/llama-3.3-70b-
# instruct:free"). Path separators are therefore not disqualifying on
# their own here; the length cap, the name check and the live-secret
# comparison are what carry the weight.
_SAFE_STR_RE = re.compile(r"^[A-Za-z0-9_.:/@+\- ]{0,128}$")

REDACTED = "<redacted>"


def _live_secret_values(settings: Any) -> set:
    """Every credential we can currently see, for the value-match pass.

    Reads the ``Settings`` object's own secret fields and the OS
    keychain. Best-effort: a keychain that will not open just means this
    particular backstop contributes nothing, and the allow-list is still
    the actual guarantee.
    """
    found = set()
    try:
        from config import secrets as _secrets
        for name in _secrets.SECRET_KEYS:
            try:
                v = _secrets.get_secret(name)
            except Exception:
                v = None
            if v and len(str(v)) >= 8:
                found.add(str(v))
    except Exception:
        pass
    if settings is not None:
        for fname in dir(settings):
            if fname.startswith("_"):
                continue
            if not _CREDENTIAL_NAME_RE.search(fname):
                continue
            try:
                v = getattr(settings, fname)
            except Exception:
                continue
            if isinstance(v, str) and len(v) >= 8:
                found.add(v)
    return found


def redact_settings(settings: Any) -> Dict[str, Any]:
    """An allow-listed, credential-free view of ``settings``.

    Iterates :data:`SAFE_SETTINGS_KEYS`, NOT the object's fields — that
    direction is the whole design. Returns a dict with three sections so
    a reader can see what was withheld as well as what was included.
    """
    safe: Dict[str, Any] = {}
    withheld: List[str] = []
    secret_values = _live_secret_values(settings)

    for key in sorted(SAFE_SETTINGS_KEYS):
        if not hasattr(settings, key):
            # Allow-list entry for a setting that no longer exists.
            continue
        if _CREDENTIAL_NAME_RE.search(key):
            withheld.append(key)
            continue
        try:
            value = getattr(settings, key)
        except Exception:
            continue
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, int):
            safe[key] = value
        elif isinstance(value, float):
            safe[key] = round(value, 4)
        elif isinstance(value, str):
            if value in secret_values or not _SAFE_STR_RE.match(value):
                safe[key] = REDACTED
                withheld.append(key)
            else:
                safe[key] = value
        else:
            # Anything structured is not a setting we understand well
            # enough to vouch for.
            withheld.append(key)

    presence: Dict[str, bool] = {}
    for key in PRESENCE_ONLY_KEYS:
        if hasattr(settings, key):
            try:
                presence[f"{key}_configured"] = bool(
                    str(getattr(settings, key) or "").strip())
            except Exception:
                presence[f"{key}_configured"] = False

    # Every field the Settings dataclass has that the allow-list does
    # NOT cover, by name only. This is what makes an omission visible
    # instead of invisible: the reader can see that a field exists and
    # was withheld, without seeing a byte of its value.
    excluded: List[str] = []
    try:
        names = [f.name for f in dataclass_fields(settings)]
    except Exception:
        names = [n for n in dir(settings)
                 if not n.startswith("_") and not callable(getattr(settings, n, None))]
    for name in sorted(names):
        if name in SAFE_SETTINGS_KEYS:
            continue
        excluded.append(name)

    return {
        "note": (
            "Allow-list redaction: only the keys in 'settings' were "
            "permitted into this export. Everything under "
            "'excluded_field_names' was withheld — names only, no "
            "values. Any setting added to the app after this export was "
            "built is excluded by default."
        ),
        "settings": safe,
        "presence": presence,
        "withheld_keys": sorted(set(withheld)),
        "excluded_field_names": excluded,
    }


# ── version / system / device gathering ──────────────────────────────

def _backend_dir() -> Path:
    return Path(__file__).resolve().parent.parent


# Stamped into the runtime bundle by zip-bundle.py, landing next to
# server.py once the shell extracts it. See the comment there.
APP_VERSION_FILE = "app_version.txt"


def app_version() -> Optional[str]:
    """The shipped app version.

    Sources, in order:

    1. ``MEETING_RECORDER_APP_VERSION`` — set by the Tauri shell when it
       spawns the backend, from the compiled ``package_info().version``
       (i.e. ``src-tauri/tauri.conf.json``). Authoritative.
    2. ``app_version.txt`` beside ``server.py`` — stamped into
       ``backend-bundle.zip`` at build time from the same
       ``tauri.conf.json``.
    3. The dev checkout's ``src-tauri/tauri.conf.json`` / ``package.json``
       one level above ``backend/``.

    None rather than a guess when none of them is readable — a wrong
    version in a bug report is worse than a missing one, which is the
    exact lesson of the 2.7.5/2.7.6/2.7.7 tag incident in AGENTS.md.

    Why (1) and (2) both exist: only (3) was ever implemented, and it
    only ever works in a dev checkout. A release build runs the backend
    out of the extracted runtime directory, which contains neither
    ``src-tauri/`` nor ``package.json``, and nothing set the env var —
    so every field-exported ``versions.json`` said ``"app_version":
    null`` while every other field was populated, costing a round trip
    per bug report just to establish which build it came from.
    """
    env = (os.environ.get("MEETING_RECORDER_APP_VERSION") or "").strip()
    if env:
        return env
    try:
        stamped = (_backend_dir() / APP_VERSION_FILE).read_text(
            encoding="utf-8").strip()
    except Exception:
        stamped = ""
    if stamped:
        return stamped
    base = _backend_dir().parent
    for rel in ("src-tauri/tauri.conf.json", "package.json"):
        try:
            data = json.loads((base / rel).read_text(encoding="utf-8"))
            v = data.get("version")
            if v:
                return str(v)
        except Exception:
            continue
    logger.warning(
        "Diagnostics export could not determine the app version — the shell "
        "did not pass MEETING_RECORDER_APP_VERSION, there is no "
        f"{APP_VERSION_FILE} in the runtime directory, and no checkout "
        "manifest is readable. versions.json will say null.")
    return None


#: What ``extension_last_seen_version`` says when the extension HAS
#: posted but the build that posted predates version reporting
#: (background.js only started sending
#: ``chrome.runtime.getManifest().version`` in 1.2.0). Deliberately not
#: None: null is reserved for "nothing has ever posted", and collapsing
#: the two is the exact ambiguity this field exists to remove.
EXTENSION_VERSION_UNREPORTED = "unknown (extension predates version reporting)"


def extension_last_seen(settings: Any = None) -> Dict[str, Any]:
    """What the extension store actually recorded about the last POST.

    Returns ``{"extension_last_seen_version", "extension_last_seen_at",
    "extension_version_status"}``.

    WHY THIS ISN'T A CONSTANT ANY MORE
    ----------------------------------
    ``gather_versions`` used to emit ``"extension_last_seen_version":
    None`` as a literal, so every exported ``versions.json`` said null
    no matter what. A real bundle exported at 21:04 had an
    ``events.jsonl`` line for an import at 21:03:27 carrying
    ``extension_version: "1.4.0"`` — 37 seconds earlier, in the SAME
    zip — while ``versions.json`` in that zip reported null. The store
    knew; the export never asked. That is the same defect as the
    pre-v2.38.0 ``app_version: null``, and it costs a round trip on
    every bug report just to establish which extension build produced
    the behaviour.

    ``ExtensionCalendarService.capture_status`` is the same source the
    Settings → Chrome Extension card reads through ``/extension/info``,
    and ``extension_bundle_service.extension_version_status`` is the
    same classifier, so the zip cannot disagree with what the user is
    looking at in the app.

    THREE STATES, KEPT DISTINCT
    ---------------------------
      * posted AND reported a version → that version string.
      * posted but the build predates version reporting →
        :data:`EXTENSION_VERSION_UNREPORTED`, never null.
      * nothing has ever posted → ``None``. The only null case.

    ``extension_last_seen_at`` is what separates the second state from
    the third at the data level (see ``capture_status``'s docstring),
    and ``extension_version_status`` carries the classifier's own
    verdict — "never_posted" / "unknown_version" / "up_to_date" /
    "update_available" / "unknown".

    Never raises. The store lives in ``recordings_dir``, which a
    diagnostics export must be able to run without: an absent, corrupt
    or un-downloaded store degrades to the "nothing has ever posted"
    shape, exactly like the rest of this module degrades to null rather
    than guessing.
    """
    out: Dict[str, Any] = {
        "extension_last_seen_version": None,
        "extension_last_seen_at": None,
        "extension_version_status": "unknown",
    }
    data_dir = ""
    try:
        data_dir = str(getattr(settings, "recordings_dir", "") or "").strip()
    except Exception:  # noqa: BLE001
        data_dir = ""
    if not data_dir:
        # No settings handed in (a bare gather_versions() call). Fall
        # back to the same default recordings location the app uses so
        # the field still reports rather than reporting null-by-omission
        # — which is the bug.
        try:
            from config.settings import USER_DATA_DIR
            data_dir = str(Path(USER_DATA_DIR) / "recordings")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Diagnostics export could not locate the extension calendar "
                f"store ({e}); extension_last_seen_version will say null.")
            return out
    try:
        from services.extension_calendar_service import ExtensionCalendarService
        status = ExtensionCalendarService(Path(data_dir)).capture_status()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"Diagnostics export could not read the extension calendar store "
            f"({e}); extension_last_seen_version will say null.")
        return out

    seen_version = status.get("last_seen_version") or None
    seen_at = status.get("last_seen_version_at") or None
    out["extension_last_seen_at"] = seen_at
    if seen_version:
        out["extension_last_seen_version"] = seen_version
    elif seen_at:
        out["extension_last_seen_version"] = EXTENSION_VERSION_UNREPORTED
    try:
        from services.extension_bundle_service import (
            bundled_extension_version, extension_version_status,
        )
        out["extension_version_status"] = extension_version_status(
            bundled_extension_version(), seen_version, seen_at)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Extension version status unavailable ({e})")
    return out


def gather_versions(settings: Any = None) -> Dict[str, Any]:
    """Versions of everything that could plausibly explain a bug.

    ``settings`` is optional and is used only to locate the extension
    calendar store (``recordings_dir``) — see ``extension_last_seen``.
    Passing it is how the real export reaches the recorded extension
    version; omitting it falls back to the default location rather than
    to null.
    """
    import sys
    out: Dict[str, Any] = {
        "app_version": app_version(),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "event_log_schema": None,
        "extension_bundled_version": None,
        # Placeholders only, so the three extension fields stay next to
        # `extension_bundled_version` in the exported JSON. They are
        # REPLACED below from the extension calendar store — see
        # `extension_last_seen`, and see
        # test_diagnostics_export.py::test_the_extension_version_is_
        # read_not_hardcoded, which asserts against two different stores
        # so no constant left here can survive.
        "extension_last_seen_version": None,
        "extension_last_seen_at": None,
        "extension_version_status": "unknown",
    }
    try:
        from utils import events
        out["event_log_schema"] = events.SCHEMA_VERSION
    except Exception:
        pass
    try:
        from services.extension_bundle_service import bundled_extension_version
        out["extension_bundled_version"] = bundled_extension_version()
    except Exception:
        pass
    for mod in ("numpy", "torch", "soundfile", "sounddevice", "fastapi"):
        try:
            m = __import__(mod)
            out[f"{mod}_version"] = str(getattr(m, "__version__", "unknown"))
        except Exception:
            out[f"{mod}_version"] = "not installed"
    # What the extension last told us, READ from the store rather than
    # hardcoded. `extension_bundled_version` above is the model: both
    # halves of "which extension build is involved" now come from real
    # data, so the two can finally be compared in a bug report. Updates
    # the placeholders seeded above, so their position in the exported
    # JSON is unchanged.
    out.update(extension_last_seen(settings))
    return out


def gather_system() -> Dict[str, Any]:
    """OS + hardware summary. Deliberately no hostname and no username —
    both are personal data and neither has ever been needed to diagnose
    anything in this repo."""
    out: Dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
    }
    try:
        import shutil as _sh
        from utils.logger import default_log_dir
        usage = _sh.disk_usage(str(default_log_dir()))
        out["log_disk_total_gb"] = round(usage.total / 1e9, 1)
        out["log_disk_free_gb"] = round(usage.free / 1e9, 1)
    except Exception:
        pass
    try:
        import torch  # noqa: PLC0415
        out["torch_cuda_available"] = bool(torch.cuda.is_available())
        if torch.cuda.is_available():
            out["torch_cuda_device"] = torch.cuda.get_device_name(0)
    except Exception:
        out["torch_cuda_available"] = None
    return out


def gather_audio_devices() -> Dict[str, Any]:
    """Input + output device list.

    Device NAMES are included — they are what identifies a broken
    driver, and the task this module was written for asks for them
    explicitly. They are also the one thing in this zip that can carry
    a personal name ("Sam's AirPods"), so the manifest says so out
    loud rather than burying it.
    """
    out: Dict[str, Any] = {"inputs": [], "outputs": [], "error": None}
    try:
        from core.audio_capture import list_input_devices, list_output_devices
        out["inputs"] = list_input_devices()
        out["outputs"] = list_output_devices()
    except Exception as e:
        out["error"] = type(e).__name__
    return out


def _tail_bytes(path: Path, limit: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            chunk = f.read()
        text = chunk.decode("utf-8", errors="replace")
        if size > limit:
            text = (f"[... {size - limit} earlier bytes omitted; this is "
                    f"the last {limit} bytes of a {size}-byte file ...]\n"
                    + text)
        return text
    except OSError as e:
        return f"(could not read: {type(e).__name__})"


# ── the bundle ───────────────────────────────────────────────────────

MEMBER_DESCRIPTIONS = {
    "manifest.json":
        "What this zip contains, when it was made, and what was left out.",
    "events.jsonl":
        "Structured outcome log — one JSON line per recording stop, "
        "finalize, calendar import, indexing run, start/stop and crash. "
        "Counts, durations and reason codes only.",
    "events.jsonl.1":
        "The previous rotation of the structured outcome log.",
    "backend.log.tail.txt":
        "The tail of the human-readable backend log.",
    "crash.log.tail.txt":
        "The tail of the native-crash log (empty on a healthy machine).",
    "versions.json":
        "App, backend, Python, Chrome-extension and key library versions.",
    "system.json":
        "OS, CPU, GPU and free-disk summary. No hostname, no username.",
    "audio-devices.json":
        "Input and output audio devices as the OS reports them. NOTE: "
        "device names can include a personalised name such as "
        "\"<your name>'s AirPods\".",
    "settings.redacted.json":
        "Your settings, allow-list redacted. API keys, folder paths, "
        "email address and custom co-pilot context are excluded; "
        "excluded field NAMES are listed so nothing is hidden from you.",
}

#: Rotation artefacts — present in an export only if that rotation
#: happened. Listing them in the up-front preview would show the user a
#: file that will not be in their zip, so the preview omits them; the
#: post-export listing is read from the real archive and includes them
#: when they are genuinely there.
CONDITIONAL_MEMBERS = frozenset({"events.jsonl.1"})


def preview_members() -> List[str]:
    """What an export will contain, for the pre-click listing."""
    return sorted(set(MEMBER_DESCRIPTIONS) - CONDITIONAL_MEMBERS)


EXCLUDED_STATEMENT = [
    "No transcripts or transcript text",
    "No audio or recordings",
    "No meeting titles, agendas or calendar event content",
    "No attendee names or email addresses",
    "No API keys or other credentials",
    "No file paths from your machine",
]


def build_diagnostics_zip(
    *,
    settings: Any = None,
    log_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    include_audio_devices: bool = True,
) -> Dict[str, Any]:
    """Write the diagnostics zip and describe it.

    Returns ``{"path", "filename", "bytes", "members", "descriptions",
    "excluded"}``. ``members`` is exactly what the archive holds, read
    back from the archive itself rather than from the plan — so the
    listing the user is shown cannot drift from the file they send.
    """
    from utils.logger import default_log_dir, backend_log_path

    root = Path(log_dir) if log_dir is not None else default_log_dir()
    dest_dir = Path(out_dir) if out_dir is not None else (root / EXPORT_SUBDIR)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"meeting-recorder-diagnostics-{stamp}.zip"
    zip_path = dest_dir / filename

    planned: List[str] = []
    payload: Dict[str, str] = {}

    # 1. structured event log (current + one rotation)
    try:
        from utils import events
        ev_path = events.event_log_path()
    except Exception:
        ev_path = root / "events.jsonl"
    if ev_path.exists():
        payload["events.jsonl"] = _tail_bytes(ev_path, 4 * 1024 * 1024)
    prev = Path(f"{ev_path}.1")
    if prev.exists():
        payload["events.jsonl.1"] = _tail_bytes(prev, 4 * 1024 * 1024)

    # 2. log tails
    payload["backend.log.tail.txt"] = _tail_bytes(
        backend_log_path(), BACKEND_LOG_TAIL_BYTES)
    try:
        from utils.crash_log import crash_log_path
        payload["crash.log.tail.txt"] = _tail_bytes(
            crash_log_path(root), CRASH_LOG_TAIL_BYTES)
    except Exception:
        payload["crash.log.tail.txt"] = "(crash log unavailable)"

    # 3. versions / system / devices
    # `settings` is threaded in so `extension_last_seen` can find the
    # extension calendar store under the user's real recordings_dir.
    payload["versions.json"] = json.dumps(
        gather_versions(settings), indent=2)
    payload["system.json"] = json.dumps(gather_system(), indent=2)
    if include_audio_devices:
        payload["audio-devices.json"] = json.dumps(
            gather_audio_devices(), indent=2, default=str)

    # 4. redacted settings
    if settings is not None:
        payload["settings.redacted.json"] = json.dumps(
            redact_settings(settings), indent=2)

    # The manifest lists itself. Anything else and the file that tells
    # the user what they are sending disagrees with what they are
    # sending, which is the exact confusion this manifest exists to
    # prevent.
    planned = sorted(list(payload) + ["manifest.json"])
    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(
            timespec="seconds"),
        "app_version": app_version(),
        "members": planned,
        "contents": {k: MEMBER_DESCRIPTIONS.get(k, "") for k in planned},
        "deliberately_excluded": EXCLUDED_STATEMENT,
        "redaction": (
            "Settings are filtered by an allow-list: only explicitly "
            "named keys are included, so any setting added to the app "
            "later is excluded by default rather than leaked."
        ),
    }
    payload["manifest.json"] = json.dumps(manifest, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(payload):
            zf.writestr(name, payload[name])
    data = buf.getvalue()
    zip_path.write_bytes(data)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        actual_members = sorted(zf.namelist())

    return {
        "path": str(zip_path),
        "filename": filename,
        "bytes": len(data),
        "members": actual_members,
        "descriptions": {
            k: MEMBER_DESCRIPTIONS.get(k, "") for k in actual_members},
        "excluded": EXCLUDED_STATEMENT,
    }
