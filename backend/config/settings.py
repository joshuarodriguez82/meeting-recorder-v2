"""
Application configuration loaded from environment variables.
All secrets are sourced from .env — never hardcoded.

Per-user config + recordings live under a writable per-platform location
so the bundled backend (inside the installed app directory, which is
read-only for non-admin users) never needs to write anywhere outside the
user's profile:

  Windows: %LOCALAPPDATA%\\MeetingRecorder
  macOS:   ~/Library/Application Support/MeetingRecorder
  Linux:   $XDG_CONFIG_HOME/MeetingRecorder (or ~/.config/MeetingRecorder)
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass
from dotenv import dotenv_values, load_dotenv

from . import secrets as _secrets


def _user_data_dir() -> Path:
    """The canonical writable directory for this user's data.

    Notes:
      - Windows: uses LOCALAPPDATA (non-roaming), NOT APPDATA. Corporate
        environments enable OneDrive Known Folder Move which redirects
        %APPDATA% into OneDrive; sync causes stale reads, file locks
        mid-write, and intermittent 'failed to fetch' errors. LOCALAPPDATA
        is per-machine and never redirected.
      - macOS: Library/Application Support is the canonical Apple-blessed
        spot for app data that doesn't need to be user-visible. It's
        included in Time Machine backups (good for transcripts/sessions)
        but not in iCloud Drive sync.
    """
    if os.name == "nt":
        base = (os.getenv("LOCALAPPDATA")
                or os.getenv("APPDATA")
                or os.getenv("USERPROFILE")
                or str(Path.home()))
        d = Path(base) / "MeetingRecorder"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "MeetingRecorder"
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        d = Path(base) / "MeetingRecorder"
    d.mkdir(parents=True, exist_ok=True)
    return d


USER_DATA_DIR = _user_data_dir()
ENV_PATH = USER_DATA_DIR / "config.env"


def _resolve_env_path() -> Path:
    """
    Resolve config.env path at call time, in priority order. Prefers whichever
    file actually exists on disk — this means a spawn env with LOCALAPPDATA
    stripped still finds the user's saved settings under APPDATA (Roaming) or
    the dev fallback, rather than defaulting to a fresh install.
    """
    candidates = []
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            v = os.getenv(var)
            if v:
                candidates.append(Path(v) / "MeetingRecorder" / "config.env")
        candidates.append(Path.home() / "MeetingRecorder" / "config.env")
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support"
            / "MeetingRecorder" / "config.env")
        # Some users prefer XDG-style on Mac; respect it as a fallback.
        v = os.getenv("XDG_CONFIG_HOME")
        if v:
            candidates.append(Path(v) / "MeetingRecorder" / "config.env")
    else:
        v = os.getenv("XDG_CONFIG_HOME")
        if v:
            candidates.append(Path(v) / "MeetingRecorder" / "config.env")
        candidates.append(Path.home() / ".config" / "MeetingRecorder" / "config.env")
    # Dev fallback — sibling .env next to the backend source tree
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    for c in candidates:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    # None exists yet — return the primary so save_to_env creates it there.
    return ENV_PATH

# Migration: pre-v2.1.2 stored config.env under %APPDATA% (Roaming),
# which OneDrive Known Folder Move redirects on corporate laptops and
# causes sync conflicts. If the old config is still there and the new
# one isn't, seed the new location so users keep their API keys.
_OLD_ROAMING_ENV = None
if os.name == "nt":
    _roaming = os.getenv("APPDATA")
    if _roaming:
        _OLD_ROAMING_ENV = Path(_roaming) / "MeetingRecorder" / "config.env"
if (_OLD_ROAMING_ENV and _OLD_ROAMING_ENV.exists()
        and _OLD_ROAMING_ENV.resolve() != ENV_PATH.resolve()
        and not ENV_PATH.exists()):
    try:
        ENV_PATH.write_text(
            _OLD_ROAMING_ENV.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass

# Dev fallback: if config.env still doesn't exist but backend/.env does,
# seed from it so existing developers don't have to reconfigure.
_LEGACY_ENV = Path(__file__).resolve().parent.parent / ".env"
if not ENV_PATH.exists() and _LEGACY_ENV.exists():
    try:
        ENV_PATH.write_text(_LEGACY_ENV.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass

load_dotenv(dotenv_path=ENV_PATH, override=True)


# Model ids the Anthropic API rejects with 404 not_found, mapped to the
# canonical id that resolves. claude-3-5-haiku-latest shipped in the
# settings dropdown and got persisted into users' config.env; the
# "-latest" alias is not resolvable for 3.5 Haiku. Heal it on read so
# every consumer (summarizer + the direct client-suggestion call) uses
# the working id without each user having to re-pick the model.
_DEAD_MODEL_ALIASES = {
    "claude-3-5-haiku-latest": "claude-3-5-haiku-20241022",
}


def _normalize_model(model: str) -> str:
    return _DEAD_MODEL_ALIASES.get((model or "").strip(), model)


# Field report 2026-08-11 (0xC0000005 after recording stop): the backend
# process was observed dying with STATUS_ACCESS_VIOLATION 3-4s after a
# recording stopped, with no Python traceback (native crash). Working
# hypothesis: during a recording, LiveTranscriber holds a faster-whisper
# (CTranslate2, its own bundled cuDNN) model resident on CUDA; on stop,
# auto-process loads pyannote via PyTorch (a *different* bundled cuDNN)
# and moves it onto CUDA too — two CUDA/cuDNN runtimes alive in one
# process at once. `diarization_device` makes the pyannote device
# user-selectable so that hypothesis can be tested/worked around instead
# of guessed at. Any value outside this set (typo'd config.env, an old
# build's now-removed option, hand-edited garbage) must fall back to
# "auto" rather than raise — a corrupt single field must never brick the
# whole settings load.
_VALID_DIARIZATION_DEVICES = {"auto", "cpu", "cuda"}


def _normalize_diarization_device(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in _VALID_DIARIZATION_DEVICES else "auto"


# Field report 2026-08-14: a user on a locked-down corporate tenant
# hit a Microsoft sign-in prompt every time they opened
# the Record tab, because /calendar/upcoming's Outlook COM fetch
# (Dispatch("Outlook.Application") launches Outlook if it isn't already
# running, which is enough to trigger the tenant's conditional-access
# challenge) ran unconditionally. They'd already given up on Outlook and
# switched to the Chrome extension (scrapes Outlook Web from their real,
# already-authenticated browser — see services/extension_calendar_service.py)
# but had no way to tell the backend to stop touching Outlook entirely, so
# the prompt kept firing on every Record-tab open regardless.
#
#   "auto"      — current behavior: local calendar (Outlook COM / macOS
#                 EventKit) plus extension events, merged.
#   "outlook"   — local calendar only, no extension merge.
#   "extension" — Chrome-extension-scraped events only. NO Outlook COM /
#                 EventKit call is made anywhere, ever — this is the
#                 whole point, since a single stray call re-triggers the
#                 sign-in prompt. See services/calendar_service.py, which
#                 is the single choke point every caller (server.py
#                 endpoints, calendar_monitor.py, auto_record_service.py)
#                 goes through.
#   "off"       — no calendar data of any kind; /calendar/* endpoints
#                 return empty results without error.
#
# Self-healing on read (see _normalize_diarization_device above for the
# precedent): a corrupt/unrecognized value falls back to "auto" rather
# than raising.
_VALID_CALENDAR_SOURCES = {"auto", "outlook", "extension", "off"}


def _normalize_calendar_source(value: str) -> str:
    v = (value or "").strip().lower()
    return v if v in _VALID_CALENDAR_SOURCES else "auto"


@dataclass(frozen=True)
class Settings:
    """Immutable application settings resolved at startup."""

    anthropic_api_key: str
    hf_token: str
    whisper_model: str
    max_speakers: int
    recordings_dir: str
    email_to: str
    claude_model: str
    notify_minutes_before: int
    auto_process_after_stop: bool
    launch_on_startup: bool
    auto_follow_up_email: bool
    retention_enabled: bool
    retention_processed_days: int
    retention_unprocessed_days: int
    # Whether to run the streaming live-transcription pipeline during
    # recording. When False the recording itself is unaffected — we just
    # don't spin up the LiveTranscriber thread or its 16 kHz resample,
    # which saves CPU on long calls. The canonical post-stop transcript
    # always runs regardless.
    live_transcription_enabled: bool
    # Auto-screenshot cadence during active recording. 0 = off (manual
    # button only — pre-v2.12.1 behavior). When > 0 the frontend fires a
    # screenshot capture every N minutes while a recording is active.
    # Field repro 2026-06-26: user expected auto-capture and got 1
    # screenshot per 28-min meeting (manual click count). Default stays
    # 0 to avoid surprising existing users; recommended value is 3 min.
    auto_screenshot_interval_minutes: int
    # ── Auto-stop watchdog ────────────────────────────────────────
    # Warn (and optionally auto-stop) when a recording has been silent
    # for too long, run past its scheduled end time, or exceeded the
    # hard cap. All values are minutes except hard_cap_hours. 0
    # disables a given trigger; the hard cap is always-on.
    silence_warn_min: int           # warn at N min of silence; 0 = off
    silence_stop_min: int           # auto-stop at N min of silence; 0 = off
    overrun_warn_min: int           # warn N min after scheduled end; 0 = off
    overrun_stop_min: int           # auto-stop N min after scheduled end; 0 = off
    hard_cap_hours: int             # never let a recording exceed this; 0 = off
    # Which LLM family powers summaries/extractions.
    #   "anthropic"         → native Anthropic SDK, uses anthropic_api_key +
    #                         claude_model (e.g. claude-haiku-4-5)
    #   "openai"            → OpenAI-compatible endpoint (OpenRouter, Ollama,
    #                         LM Studio, vLLM) via openai_base_url +
    #                         openai_api_key + claude_model (reused as the
    #                         model id — e.g. "meta-llama/llama-3.3-70b-
    #                         instruct:free" on OpenRouter, "llama3" on Ollama)
    ai_provider: str
    openai_api_key: str
    openai_base_url: str
    # When True, a background loop watches the connected calendar and
    # auto-starts a recording at each qualifying event's scheduled start
    # time (filtered to non-all-day events with a conference link).
    # Manual recordings always win — auto-start is a no-op while
    # `recording_svc.is_recording` is True.
    auto_record_enabled: bool
    # When True, the Record view shows a "Live Co-Pilot" panel that calls
    # the LLM every ~45s with the recent transcript and surfaces three
    # short lists (clarifying questions / risks / suggested follow-ups).
    # Opt-in by default — it costs LLM calls during every recording.
    live_copilot_enabled: bool
    # Optional separate LLM for the live co-pilot ticks. When
    # `live_ai_provider` is empty (default) the co-pilot reuses the main
    # provider — same client, same key, same model as post-meeting
    # summaries. When it's set, we build a second Summarizer with these
    # fields so the live side can run on something cheap or free (local
    # Ollama, a free OpenRouter model) while the post-meeting summary
    # stays on the main provider. `live_anthropic_api_key` is only used
    # when the override provider is "anthropic"; otherwise the main
    # `anthropic_api_key` is reused.
    live_ai_provider: str
    live_claude_model: str
    live_openai_api_key: str
    live_openai_base_url: str
    live_anthropic_api_key: str
    # Active co-pilot persona + meeting-type modifier names. Both resolve
    # through the CoPilotMode / CoPilotMeetingType services, which seed
    # editable defaults. Defaults: SA persona, General type.
    live_copilot_mode: str
    live_copilot_meeting_type: str
    # Co-Pilot polling intervals (seconds). The wide tick uses the full
    # ~10 min window; the hot tick uses only the last ~90s and biases
    # toward emptiness for just-in-time coaching. Set hot to 0 to
    # disable the hot tier entirely (just the wide pass runs).
    live_copilot_wide_interval_sec: int
    live_copilot_hot_interval_sec: int
    # Free-text the SA pins per-engagement as authoritative role / topic
    # framing for the co-pilot. Appended to every coach_tick prompt.
    # Empty by default — the baked-in SA-flavored prompt runs as-is.
    copilot_custom_context: str
    # When True, the "Today" daily-briefing tab is shown and becomes the
    # default landing view. OFF by default — Today depends on the user
    # running a Microsoft 365 Copilot scheduled prompt and pasting its
    # output in; that's a power-user setup not everyone has. Opt-in from
    # Settings; persists across restarts like every other toggle.
    today_view_enabled: bool
    # When True, a backend loop auto-generates a pre-meeting brief shortly
    # before each calendar meeting and fires a notification when it's
    # ready. OFF by default — it costs an LLM call per meeting. Lead time
    # is how many minutes before the meeting to generate it.
    auto_prep_brief_enabled: bool
    auto_prep_brief_lead_min: int
    # Root network folder for background per-client session exports
    # ("Cloud Mirror"). When set, every session whose client has no
    # explicit Designated Folder is exported to
    # <cloud_mirror_dir>/<client>/ (or /Unfiled/ without a client tag)
    # by the background export worker — asynchronously, so a slow
    # cloud-stream mount can never stall recording/processing. Empty =
    # off. This is the supported way to get sessions onto Google Drive
    # / a NAS; recordings_dir itself must stay on LOCAL disk (see the
    # 2026-07-09 Drive-stall incident notes in services/export_worker.py).
    cloud_mirror_dir: str
    # Roaming folder for session JSONs (the "Session Archive") so a
    # library shows up on every machine pointed at the same synced
    # folder. Read from SESSION_ARCHIVE_DIR in config.env. Empty = off.
    #
    # Field report 2026-08-07: this setting existed ONLY as an
    # os.getenv() read in server.py — there was no way to set it short
    # of hand-editing config.env or the process environment, so a user
    # who wanted cross-device sync had no discoverable path to it. It's
    # a first-class Settings field now (with its own Settings-view card)
    # for exactly that reason; _session_archive_dir() in server.py still
    # falls back to the raw env var so anyone who was already setting it
    # by hand keeps working unchanged. See the three-location-rule
    # docstring on _session_archive_dir() for how this differs from
    # recordings_dir and cloud_mirror_dir.
    session_archive_dir: str
    # Speech-boundary chunking for the live transcript, instead of the
    # fixed 15s windows. Default TRUE — VAD chunking is what gets the
    # live preview down to Zoom-notetaker-ish latency (~1-3s instead of
    # ~15s); the fixed-window path only exists as an explicit-opt-out /
    # runtime-failure fallback now. See core/live_transcriber.py and
    # core/vad.py (field report 2026-08-10, Zoom notetaker parity).
    live_vad_enabled: bool
    # Live per-speaker labelling of the far-end ("them") stream — the
    # "Speaker 1 / Speaker 2" badges in the live transcript preview.
    # Default TRUE. When False, LiveTranscriber gets no speaker tracker
    # at all, so loopback segments keep the plain "them" label and zero
    # embedding work happens during the call.
    #
    # Field report 2026-08-11: on a real 2-person call the live splitter
    # gave ONE continuous speaker eight different identities (SPEAKER
    # 1,2,3,4,5,6,7,9) and attached a saved colleague's real name to the
    # wrong person. The thresholds in core/live_speakers.py were retuned
    # hard toward merging, but a user whose calls still label badly needs
    # a way to turn the whole thing off and get the old, plain, never-
    # wrong "them" back — that's this flag.
    live_speaker_split_enabled: bool
    # Which device the pyannote speaker-diarization pipeline loads on:
    # "auto" (default, prefer CUDA then MPS then CPU — identical to the
    # pre-2026-08-11 hardcoded behavior), "cpu" (force CPU, never probe
    # CUDA/MPS — the workaround for the field report above), or "cuda"
    # (force CUDA, falling back to CPU with a warning on a machine with
    # no GPU rather than crashing). See core/diarization.py.
    diarization_device: str
    # Kill switch for the WASAPI mix-format lookup behind /audio/sync-
    # risk (core/audio_format_inspector.get_device_mix_format). That
    # lookup uses pycaw/comtypes, the confirmed sole source (10/10
    # captured crash dumps) of the STATUS_ACCESS_VIOLATION crashes
    # tracked across v2.23.2 / v2.25.0 — see utils/com_worker.py and
    # core/audio_format_inspector.py for the full diagnosis. As of
    # v2.25.1 the lookup always runs in an isolated child process, so
    # pycaw never runs in the backend regardless of this setting; True
    # (default) means "run the subprocess-isolated lookup", False
    # means "skip it entirely and report sync-risk as unknown". There
    # is deliberately no third option that runs pycaw in-process — see
    # the module docstring for why that must never come back.
    audio_mix_format_lookup_enabled: bool
    # Offline acoustic echo cancellation (AEC) for the mic channel,
    # applied during finalize (before the mic+loopback mix) — see
    # utils/aec.py and utils/audio_utils.py's finalize_recording_
    # streaming. Helps a specific setup: recording with an external
    # mic + SPEAKERS (not a headset), where unmuting lets the far-end
    # caller's voice come back out of the speakers and get picked up a
    # second time on the mic, producing a duplicate transcript entry
    # mis-attributed to the user and degrading speaker diarization.
    # Default False — this is a new, offline-only, opt-in feature
    # while it's validated against real recordings; a rejected/failed
    # AEC attempt always falls back to the original mic untouched, but
    # the toggle stays off by default until there's field evidence it
    # helps more than it costs (extra finalize time on long meetings).
    echo_cancellation_enabled: bool
    # Kill switch for the SQLite session index (services/session_index.py).
    # Default ON: list_sessions() (called by nearly every endpoint — see
    # services/session_service.py's crash-dump docstring) is served from
    # a disposable SQLite cache instead of re-parsing every session_*.json
    # on every call. The cache is strictly derived from the JSON files —
    # deleting it just costs one slow rebuild scan, nothing is lost. Set
    # False to bypass it entirely and go back to the old always-correct
    # direct-scan path, e.g. while ruling out the index as a cause of a
    # sessions-list bug.
    session_index_enabled: bool
    # Kill switch for channel-aware diarization (core/channel_
    # attribution.py). Default ON.
    #
    # WHAT IT DOES WHEN ON: during finalize — the only moment both raw
    # capture streams still exist on disk — the mic and loopback tracks
    # are compared frame by frame and a timeline of who-owned-the-audio
    # spans is written to session_<ID>.channel_attribution.json. At
    # Process time, diarization uses that timeline to assign the user's
    # spans to the user OUTRIGHT, leaving pyannote only the job of
    # telling far-end speakers apart from each other.
    #
    # WHY IT EXISTS: the recorder captures two physically separate
    # streams — the mic (definitionally the user) and system-audio
    # loopback (definitionally everyone else) — and then merges them
    # into one mono WAV before diarization ever sees them. That merge
    # throws away perfect, free ground truth about who-is-who and asks
    # a clustering model to re-derive it from voice alone. When it gets
    # that wrong, the far end's words appear in the user's own turns —
    # the "user repeating what they're saying" symptom, long
    # misattributed to acoustic echo until a real session measured
    # `Echo cancellation: not applied (erle_non_positive)` on a headset
    # recording, i.e. no echo path existed at all.
    #
    # WHAT TURNING IT OFF COSTS: speaker attribution goes back to being
    # decided purely by voice similarity, so the user's transcript
    # turns can again contain the far end's words, and the user's own
    # speaker centroid (the one the known-speakers store matches their
    # real name against) is again computed from whatever pyannote put
    # in that cluster rather than from mic-confirmed audio only.
    # NOTHING ELSE changes: no audio is altered either way — the merged
    # WAV is byte-identical with this on or off — and the extra
    # finalize work is one additional streaming read of each raw stream
    # in the below-normal-priority finalize subprocess, with zero
    # per-frame cost on the live transcription path.
    #
    # Turning it off is the right move if attribution is ever seen
    # making things WORSE on real recordings. Note that it already
    # stands down on its own — falling back to exactly the old
    # behaviour, with no override — for mic-only sessions, conference-
    # room mode, non-headset (speaker-bleed) recordings, low-confidence
    # timelines, and every session recorded before this shipped; see
    # core/channel_attribution.evaluate_trust for the full list of
    # stand-down reasons, each of which is recorded in the sidecar.
    channel_attribution_enabled: bool
    # Which calendar source(s) the backend is allowed to consult:
    # "auto" (default, local calendar + extension merged), "outlook"
    # (local calendar only), "extension" (Chrome-extension-scraped
    # events only — NEVER touches Outlook COM / EventKit, anywhere; see
    # services/calendar_service.py), or "off" (no calendar data at
    # all). See the field report above _normalize_calendar_source.
    calendar_source: str

    @classmethod
    def from_env(cls) -> "Settings":
        """
        Load settings directly from the on-disk config.env every call.

        Why not just trust os.environ / load_dotenv at import time: some
        Tauri spawn contexts deliver Python a partial/stale environment
        where ANTHROPIC_API_KEY leaks in from the parent but none of the
        other KEY=VALUE pairs do. When load_dotenv then sees those vars
        already present some combinations refuse to override, and the
        backend boots with defaults for everything except the one key —
        exactly the "app opens with 0 sessions" symptom.

        Reading the file directly (not through os.environ at all) removes
        the whole class of inherited-env bugs. load_dotenv still runs as
        a side effect so child processes (subprocess.run) see the same
        values.
        """
        env_path = _resolve_env_path()
        file_values: dict = {}
        if env_path.exists():
            try:
                file_values = dotenv_values(str(env_path)) or {}
            except Exception:
                file_values = {}
            # Also populate os.environ so subprocesses inherit.
            load_dotenv(dotenv_path=env_path, override=True)

        # One-shot migration: copy any secrets still living in config.env
        # into the OS keychain. After this point get_secret() returns the
        # value; on the next save we blank the env line so the file no
        # longer holds plaintext credentials.
        _secrets.migrate_from_env(file_values)

        def _get(key: str, default: str = "") -> str:
            # config.env is authoritative (save_to_env always writes it).
            # A non-empty file value wins so a stale/unreadable keychain
            # entry can never shadow the real key — that shadowing is the
            # "401 invalid x-api-key" bug. Keychain is only consulted when
            # the file has nothing (pre-migration / externally cleared).
            v = file_values.get(key)
            if v is not None and v != "":
                return v
            if key in _secrets.SECRET_KEYS:
                kc = _secrets.get_secret(key)
                if kc:
                    return kc
            return os.getenv(key, default)

        def _get_int(key: str, default: int) -> int:
            try:
                return int(_get(key, str(default)))
            except ValueError:
                return default

        def _get_bool(key: str, default: bool) -> bool:
            v = _get(key, "true" if default else "false").lower()
            return v == "true"

        return cls._build_from_source(_get, _get_int, _get_bool)

    @classmethod
    def _build_from_source(cls, _get, _get_int, _get_bool) -> "Settings":
        return cls(
            anthropic_api_key=_get("ANTHROPIC_API_KEY", ""),
            hf_token=_get("HF_TOKEN", ""),
            whisper_model=_get("WHISPER_MODEL", "base"),
            max_speakers=_get_int("MAX_SPEAKERS", 10),
            # Default recordings dir is %LOCALAPPDATA%\MeetingRecorder\recordings.
            # Users can override via RECORDINGS_DIR in config.env but shouldn't
            # need to — the default just works on a fresh install.
            recordings_dir=_get(
                "RECORDINGS_DIR", str(USER_DATA_DIR / "recordings")),
            email_to=_get("EMAIL_TO", ""),
            claude_model=_normalize_model(
                _get("CLAUDE_MODEL", "claude-haiku-4-5")),
            notify_minutes_before=_get_int("NOTIFY_MINUTES_BEFORE", 2),
            auto_process_after_stop=_get_bool("AUTO_PROCESS_AFTER_STOP", True),
            launch_on_startup=_get_bool("LAUNCH_ON_STARTUP", False),
            auto_follow_up_email=_get_bool("AUTO_FOLLOW_UP_EMAIL", False),
            retention_enabled=_get_bool("RETENTION_ENABLED", False),
            retention_processed_days=_get_int("RETENTION_PROCESSED_DAYS", 7),
            retention_unprocessed_days=_get_int("RETENTION_UNPROCESSED_DAYS", 30),
            ai_provider=_get("AI_PROVIDER", "anthropic"),
            openai_api_key=_get("OPENAI_API_KEY", ""),
            openai_base_url=_get("OPENAI_BASE_URL", ""),
            # Default ON — feature is opt-OUT, since most users will want
            # the live preview. Explicit "false" in config.env disables.
            live_transcription_enabled=_get_bool(
                "LIVE_TRANSCRIPTION_ENABLED", True),
            auto_screenshot_interval_minutes=_get_int(
                "AUTO_SCREENSHOT_INTERVAL_MINUTES", 0),
            # Auto-stop defaults: warnings on, auto-stops opt-in, 4h hard cap.
            # The user reported real "I forgot the recording was still going
            # for hours" pain — these defaults catch the common case while
            # leaving the more aggressive auto-stop behaviour off until the
            # user explicitly opts in.
            silence_warn_min=_get_int("SILENCE_WARN_MIN", 5),
            silence_stop_min=_get_int("SILENCE_STOP_MIN", 0),
            overrun_warn_min=_get_int("OVERRUN_WARN_MIN", 5),
            overrun_stop_min=_get_int("OVERRUN_STOP_MIN", 0),
            hard_cap_hours=_get_int("HARD_CAP_HOURS", 4),
            auto_record_enabled=_get_bool("AUTO_RECORD_ENABLED", False),
            live_copilot_enabled=_get_bool("LIVE_COPILOT_ENABLED", False),
            live_ai_provider=_get("LIVE_AI_PROVIDER", ""),
            live_claude_model=_get("LIVE_CLAUDE_MODEL", ""),
            live_openai_api_key=_get("LIVE_OPENAI_API_KEY", ""),
            live_openai_base_url=_get("LIVE_OPENAI_BASE_URL", ""),
            live_anthropic_api_key=_get("LIVE_ANTHROPIC_API_KEY", ""),
            live_copilot_mode=_get("LIVE_COPILOT_MODE", "SA"),
            live_copilot_meeting_type=_get(
                "LIVE_COPILOT_MEETING_TYPE", "General"),
            live_copilot_wide_interval_sec=_get_int(
                "LIVE_COPILOT_WIDE_INTERVAL_SEC", 45),
            live_copilot_hot_interval_sec=_get_int(
                "LIVE_COPILOT_HOT_INTERVAL_SEC", 0),
            # Mirrors the escape in save() — `\n` literal on disk →
            # real newlines in the runtime value.
            copilot_custom_context=_get(
                "COPILOT_CUSTOM_CONTEXT", "").replace("\\n", "\n"),
            today_view_enabled=_get_bool("TODAY_VIEW_ENABLED", False),
            auto_prep_brief_enabled=_get_bool("AUTO_PREP_BRIEF_ENABLED", False),
            auto_prep_brief_lead_min=_get_int("AUTO_PREP_BRIEF_LEAD_MIN", 10),
            cloud_mirror_dir=_get("CLOUD_MIRROR_DIR", ""),
            session_archive_dir=_get("SESSION_ARCHIVE_DIR", ""),
            live_vad_enabled=_get_bool("LIVE_VAD_ENABLED", True),
            live_speaker_split_enabled=_get_bool(
                "LIVE_SPEAKER_SPLIT_ENABLED", True),
            diarization_device=_normalize_diarization_device(
                _get("DIARIZATION_DEVICE", "auto")),
            audio_mix_format_lookup_enabled=_get_bool(
                "AUDIO_MIX_FORMAT_LOOKUP_ENABLED", True),
            echo_cancellation_enabled=_get_bool(
                "ECHO_CANCELLATION_ENABLED", False),
            session_index_enabled=_get_bool("SESSION_INDEX_ENABLED", True),
            channel_attribution_enabled=_get_bool(
                "CHANNEL_ATTRIBUTION_ENABLED", True),
            calendar_source=_normalize_calendar_source(
                _get("CALENDAR_SOURCE", "auto")),
        )

    @property
    def is_configured(self) -> bool:
        """
        True if required keys are set for the active provider.

        HF token is always required (pyannote speaker diarization). The LLM
        credential depends on provider: Anthropic needs anthropic_api_key;
        OpenAI-compatible needs openai_api_key unless the user has set up
        a local Ollama endpoint, in which case no real key is needed (the
        SDK just needs something non-empty, and we supply a placeholder).
        """
        if not self.hf_token:
            return False
        if self.ai_provider == "openai":
            # Ollama / LocalAI endpoints don't need a real key — detect by
            # URL so we don't block the user on a field they don't need.
            if self.openai_api_key:
                return True
            base = (self.openai_base_url or "").lower()
            return "localhost" in base or "127.0.0.1" in base
        return bool(self.anthropic_api_key)

    @staticmethod
    def _write_env_file(target: Path, content: str) -> bool:
        """Atomic-ish write of config.env to `target`. Returns True on success."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
            return True
        except Exception:
            return False

    @staticmethod
    def save_to_env(
        anthropic_api_key: str,
        hf_token: str,
        whisper_model: str,
        max_speakers: int,
        recordings_dir: str,
        email_to: str = "",
        claude_model: str = "claude-haiku-4-5",
        notify_minutes_before: int = 2,
        auto_process_after_stop: bool = True,
        launch_on_startup: bool = False,
        auto_follow_up_email: bool = False,
        retention_enabled: bool = False,
        retention_processed_days: int = 7,
        retention_unprocessed_days: int = 30,
        ai_provider: str = "anthropic",
        openai_api_key: str = "",
        openai_base_url: str = "",
        live_transcription_enabled: bool = True,
        auto_screenshot_interval_minutes: int = 0,
        silence_warn_min: int = 5,
        silence_stop_min: int = 0,
        overrun_warn_min: int = 5,
        overrun_stop_min: int = 0,
        hard_cap_hours: int = 4,
        auto_record_enabled: bool = False,
        live_copilot_enabled: bool = False,
        live_ai_provider: str = "",
        live_claude_model: str = "",
        live_openai_api_key: str = "",
        live_openai_base_url: str = "",
        live_anthropic_api_key: str = "",
        live_copilot_mode: str = "SA",
        live_copilot_meeting_type: str = "General",
        live_copilot_wide_interval_sec: int = 45,
        live_copilot_hot_interval_sec: int = 0,
        copilot_custom_context: str = "",
        today_view_enabled: bool = False,
        auto_prep_brief_enabled: bool = False,
        auto_prep_brief_lead_min: int = 10,
        cloud_mirror_dir: str = "",
        session_archive_dir: str = "",
        live_vad_enabled: bool = True,
        live_speaker_split_enabled: bool = True,
        diarization_device: str = "auto",
        audio_mix_format_lookup_enabled: bool = True,
        echo_cancellation_enabled: bool = False,
        session_index_enabled: bool = True,
        channel_attribution_enabled: bool = True,
        calendar_source: str = "auto",
    ) -> None:
        """Write settings back to the .env file.

        Secrets (Anthropic / HF / OpenAI / LIVE_* keys) are mirrored into
        the OS keychain when available, but ALSO always kept in config.env
        as the durable source of truth. Earlier builds blanked the env
        line on a successful keychain write; that lost the key whenever
        the keychain entry later became unreadable — e.g. an unsigned
        macOS app rebuilt with a new ad-hoc signature, or a Windows
        Credential Manager entry written under a different context —
        producing a hard "401 invalid x-api-key" with no fallback. The
        plaintext fallback is the lesser evil versus silently losing the
        key on every upgrade.

        REVISITED 2026-08-13 (security review item 1): a later attempt
        replaced this with keychain-as-sole-copy, blanking config.env
        only after an immediate `set_secret()` + `get_secret()` read-back
        confirmed the write actually persisted. That guards against a
        keychain call that lies about success, but it CANNOT guard
        against the documented failure mode above — the entry becoming
        unreadable LATER, on a subsequent upgrade/re-sign, which is
        exactly what a read-back taken at save time can never observe.
        For this app specifically that risk is not theoretical: the
        macOS build is unsigned (see AGENTS.md), the user updates
        frequently (four releases shipped in a single day is not
        unusual), and runs both Windows and macOS. config.env lives
        under %LOCALAPPDATA% / ~/Library/Application Support — it is
        NOT cloud-synced — so the plaintext copy is local-only and does
        not enlarge the exposure beyond "readable by anything already
        running as this OS user," which is the keychain's own threat
        model too. Given that, silently losing the user's API keys on
        an ordinary upgrade is worse than keeping a local-only plaintext
        copy, and the mirror-not-replace behavior below stands.
        A keychain-only design could be revisited again, but only once
        two things exist: (a) load-time key-recovery UX — detect an
        unreadable/missing keychain entry and prompt the user to
        re-enter the key, instead of a bare 401 — and (b) a signed and
        notarized macOS build, so entries stop being tied to a
        per-build ad-hoc signing identity in the first place.
        """
        # Mirror secrets into the keychain best-effort. We no longer
        # blank the file on success — config.env stays authoritative so
        # the key survives the keychain entry becoming unreadable across
        # an upgrade / re-sign / different-user context.
        _secrets.set_secret("ANTHROPIC_API_KEY", anthropic_api_key)
        _secrets.set_secret("HF_TOKEN", hf_token)
        _secrets.set_secret("OPENAI_API_KEY", openai_api_key)
        _secrets.set_secret("LIVE_OPENAI_API_KEY", live_openai_api_key)
        _secrets.set_secret("LIVE_ANTHROPIC_API_KEY", live_anthropic_api_key)
        env_anthropic = anthropic_api_key
        env_hf        = hf_token
        env_openai    = openai_api_key
        env_live_openai_api_key    = live_openai_api_key
        env_live_anthropic_api_key = live_anthropic_api_key

        content = (
            f"ANTHROPIC_API_KEY={env_anthropic}\n"
            f"HF_TOKEN={env_hf}\n"
            f"WHISPER_MODEL={whisper_model}\n"
            f"MAX_SPEAKERS={max_speakers}\n"
            f"RECORDINGS_DIR={recordings_dir}\n"
            f"EMAIL_TO={email_to}\n"
            f"CLAUDE_MODEL={claude_model}\n"
            f"NOTIFY_MINUTES_BEFORE={notify_minutes_before}\n"
            f"AUTO_PROCESS_AFTER_STOP={'true' if auto_process_after_stop else 'false'}\n"
            f"LAUNCH_ON_STARTUP={'true' if launch_on_startup else 'false'}\n"
            f"AUTO_FOLLOW_UP_EMAIL={'true' if auto_follow_up_email else 'false'}\n"
            f"RETENTION_ENABLED={'true' if retention_enabled else 'false'}\n"
            f"RETENTION_PROCESSED_DAYS={retention_processed_days}\n"
            f"RETENTION_UNPROCESSED_DAYS={retention_unprocessed_days}\n"
            f"AI_PROVIDER={ai_provider}\n"
            f"OPENAI_API_KEY={env_openai}\n"
            f"OPENAI_BASE_URL={openai_base_url}\n"
            f"LIVE_TRANSCRIPTION_ENABLED={'true' if live_transcription_enabled else 'false'}\n"
            f"AUTO_SCREENSHOT_INTERVAL_MINUTES={int(auto_screenshot_interval_minutes)}\n"
            f"SILENCE_WARN_MIN={silence_warn_min}\n"
            f"SILENCE_STOP_MIN={silence_stop_min}\n"
            f"OVERRUN_WARN_MIN={overrun_warn_min}\n"
            f"OVERRUN_STOP_MIN={overrun_stop_min}\n"
            f"HARD_CAP_HOURS={hard_cap_hours}\n"
            f"AUTO_RECORD_ENABLED={'true' if auto_record_enabled else 'false'}\n"
            f"LIVE_COPILOT_ENABLED={'true' if live_copilot_enabled else 'false'}\n"
            f"LIVE_AI_PROVIDER={live_ai_provider}\n"
            f"LIVE_CLAUDE_MODEL={live_claude_model}\n"
            f"LIVE_OPENAI_API_KEY={env_live_openai_api_key}\n"
            f"LIVE_OPENAI_BASE_URL={live_openai_base_url}\n"
            f"LIVE_ANTHROPIC_API_KEY={env_live_anthropic_api_key}\n"
            f"LIVE_COPILOT_MODE={live_copilot_mode}\n"
            f"LIVE_COPILOT_MEETING_TYPE={live_copilot_meeting_type}\n"
            f"LIVE_COPILOT_WIDE_INTERVAL_SEC={live_copilot_wide_interval_sec}\n"
            f"LIVE_COPILOT_HOT_INTERVAL_SEC={live_copilot_hot_interval_sec}\n"
            # Newlines in copilot_custom_context would break the .env line
            # format — escape them to literal \n so the value round-trips
            # cleanly through dotenv. from_env unescapes on read.
            f"COPILOT_CUSTOM_CONTEXT={(copilot_custom_context or '').replace(chr(10), '\\n').replace(chr(13), '')}\n"
            f"TODAY_VIEW_ENABLED={'true' if today_view_enabled else 'false'}\n"
            f"AUTO_PREP_BRIEF_ENABLED={'true' if auto_prep_brief_enabled else 'false'}\n"
            f"AUTO_PREP_BRIEF_LEAD_MIN={auto_prep_brief_lead_min}\n"
            f"CLOUD_MIRROR_DIR={cloud_mirror_dir}\n"
            f"SESSION_ARCHIVE_DIR={session_archive_dir}\n"
            f"LIVE_VAD_ENABLED={'true' if live_vad_enabled else 'false'}\n"
            f"LIVE_SPEAKER_SPLIT_ENABLED={'true' if live_speaker_split_enabled else 'false'}\n"
            # Not validated on write — from_env normalizes any garbage
            # value back to "auto" on read (see _normalize_diarization_device
            # above), so a bad value here is self-healing rather than a
            # crash risk.
            f"DIARIZATION_DEVICE={diarization_device}\n"
            f"AUDIO_MIX_FORMAT_LOOKUP_ENABLED="
            f"{'true' if audio_mix_format_lookup_enabled else 'false'}\n"
            f"ECHO_CANCELLATION_ENABLED="
            f"{'true' if echo_cancellation_enabled else 'false'}\n"
            f"SESSION_INDEX_ENABLED={'true' if session_index_enabled else 'false'}\n"
            f"CHANNEL_ATTRIBUTION_ENABLED="
            f"{'true' if channel_attribution_enabled else 'false'}\n"
            # Not validated on write — from_env normalizes any garbage
            # value back to "auto" on read (see _normalize_calendar_source
            # above), matching diarization_device's self-healing pattern.
            f"CALENDAR_SOURCE={calendar_source}\n"
        )
        # Write to the canonical LOCALAPPDATA location first. In rare cases
        # a Tauri-spawned Python child cannot open files under LOCALAPPDATA
        # (OneDrive KFM, filter drivers, some AV products — CreateFileW
        # returns ERROR_FILE_NOT_FOUND on files that exist from the user's
        # shell view). Mirror the write to the backend/ .env dev fallback
        # so the next backend spawn still finds the settings via
        # _resolve_env_path() even when the canonical path is unreachable
        # from the child process.
        Settings._write_env_file(ENV_PATH, content)
        Settings._write_env_file(
            Path(__file__).resolve().parent.parent / ".env",
            content,
        )