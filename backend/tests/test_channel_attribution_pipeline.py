"""
Channel-aware diarization: settings plumbing and pipeline wiring.

test_channel_attribution.py covers the DSP and the decision rules. This
file covers the boring-but-load-bearing plumbing around them, which is
where features in this repo have historically died silently:

  * the `channel_attribution_enabled` kill switch reaching every layer
    (dataclass → save_to_env → config.env → from_env), the exact bug
    class test_live_vad_settings.py's docstring calls out;
  * the flag actually reaching the finalize subprocess's argv, and NOT
    reaching it when the switch is off;
  * `finalize_audio.py` accepting the flags end-to-end and producing
    the sidecar;
  * `DiarizationEngine.apply_channel_attribution` constraining turns
    without pyannote in the room, and no-op'ing on every fallback path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import soundfile as sf

sys.modules.setdefault("dotenv", MagicMock())

from tests._app_import import _stub_optional_modules  # noqa: E402

# recording_service pulls in sounddevice / faster_whisper transitively
# at module load; same headless-stub trick test_finalize_timing.py uses.
_stub_optional_modules()

from config import settings as settings_mod  # noqa: E402
from core.channel_attribution import (  # noqa: E402
    OWNER_SPEAKER_LABEL,
    load_sidecar_for_audio,
)
from core.diarization import DiarizationEngine  # noqa: E402
from tests.test_channel_attribution import two_stream_fixture  # noqa: E402

SR = 16000
FINALIZE_SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "finalize_audio.py"
)


# ── Settings kill switch ─────────────────────────────────────────────


def _real_dotenv_values(path) -> dict:
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v
    except OSError:
        pass
    return out


def _patch_dotenv(monkeypatch) -> None:
    monkeypatch.setattr(settings_mod, "dotenv_values", _real_dotenv_values)
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **kw: True)


def _isolate_env_path(monkeypatch, tmp_path: Path) -> Path:
    env_path = tmp_path / "config.env"
    fallback_path = tmp_path / "dev-fallback.env"
    monkeypatch.setattr(settings_mod, "ENV_PATH", env_path)
    monkeypatch.setattr(settings_mod, "_resolve_env_path", lambda: env_path)

    real_write = settings_mod.Settings._write_env_file

    def _redirect_write(target: Path, content: str) -> bool:
        if target != env_path:
            target = fallback_path
        return real_write(target, content)

    monkeypatch.setattr(
        settings_mod.Settings, "_write_env_file", staticmethod(_redirect_write))
    return env_path


def test_channel_attribution_enabled_is_a_dataclass_field():
    assert ("channel_attribution_enabled"
            in settings_mod.Settings.__dataclass_fields__)


def test_channel_attribution_defaults_on(tmp_path, monkeypatch):
    """Default ON. A user who never opens Settings gets channel-aware
    diarization; the switch exists to turn it OFF if it ever makes
    things worse on real recordings."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    assert settings_mod.Settings.from_env().channel_attribution_enabled is True


def test_channel_attribution_off_round_trips_through_config_env(
    tmp_path, monkeypatch,
):
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    settings_mod.Settings.save_to_env(
        anthropic_api_key="", hf_token="", whisper_model="base",
        max_speakers=10, recordings_dir=str(tmp_path / "recordings"),
        channel_attribution_enabled=False,
    )
    assert settings_mod.Settings.from_env().channel_attribution_enabled is False


def test_channel_attribution_survives_alongside_other_settings(
    tmp_path, monkeypatch,
):
    """The failure this repo keeps hitting: a field in the dataclass
    that some save_to_env call site forgot, silently reset on the next
    settings save."""
    _patch_dotenv(monkeypatch)
    _isolate_env_path(monkeypatch, tmp_path)
    settings_mod.Settings.save_to_env(
        anthropic_api_key="sk-test", hf_token="hf-test", whisper_model="small",
        max_speakers=6, recordings_dir=str(tmp_path / "recordings"),
        echo_cancellation_enabled=True,
        session_index_enabled=False,
        channel_attribution_enabled=False,
    )
    loaded = settings_mod.Settings.from_env()
    assert loaded.channel_attribution_enabled is False
    assert loaded.session_index_enabled is False
    assert loaded.echo_cancellation_enabled is True


# ── The flag reaching the finalize subprocess ────────────────────────


class _FakeCompleted:
    returncode = 0
    stdout = "RESULT duration_s=1.000000 loopback_mixed=false\n"
    stderr = ""


def _capture_argv(monkeypatch) -> list:
    import services.recording_service as rs
    seen: list = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    return seen


def test_finalize_argv_carries_the_flag_when_enabled(monkeypatch):
    import services.recording_service as rs
    seen = _capture_argv(monkeypatch)
    rs.RecordingService._run_finalize_subprocess(
        mic_wav_path="mic.wav", loopback_wav_path="lb.wav",
        output_wav_path="out.wav", target_sr=16000,
        loopback_start_offset_s=0.25,
        channel_attribution_enabled=True,
    )
    assert "--channel-attribution" in seen[0]
    assert "--conference-room" not in seen[0]


def test_finalize_argv_carries_conference_room_mode(monkeypatch):
    """Conference-room mode has to travel WITH the request, because the
    child has no other way to know the mic was pointed at a room."""
    import services.recording_service as rs
    seen = _capture_argv(monkeypatch)
    rs.RecordingService._run_finalize_subprocess(
        mic_wav_path="mic.wav", loopback_wav_path="", output_wav_path="out.wav",
        target_sr=16000, loopback_start_offset_s=None,
        channel_attribution_enabled=True, conference_room_mode=True,
    )
    assert "--channel-attribution" in seen[0]
    assert "--conference-room" in seen[0]


def test_finalize_argv_omits_the_flag_when_switched_off(monkeypatch):
    """Kill switch off means the child never even computes it — no
    sidecar, no extra read pass, nothing."""
    import services.recording_service as rs
    seen = _capture_argv(monkeypatch)
    rs.RecordingService._run_finalize_subprocess(
        mic_wav_path="mic.wav", loopback_wav_path="lb.wav",
        output_wav_path="out.wav", target_sr=16000,
        loopback_start_offset_s=0.25,
        channel_attribution_enabled=False,
    )
    assert "--channel-attribution" not in seen[0]


def test_finalize_subprocess_writes_the_sidecar(recordings_dir: Path):
    """End-to-end through the real child process — the same contract
    test_finalize_subprocess.py pins for --echo-cancellation."""
    mic, lb = two_stream_fixture()
    mic_path = recordings_dir / "_recording_SUBCH.wav"
    lb_path = recordings_dir / "_loopback_SUBCH.wav"
    sf.write(str(mic_path), mic, SR, subtype="FLOAT")
    sf.write(str(lb_path), lb, SR, subtype="FLOAT")
    out = recordings_dir / "session_SUBCH.wav"

    proc = subprocess.run(
        [sys.executable, str(FINALIZE_SCRIPT),
         "--mic", str(mic_path), "--loopback", str(lb_path),
         "--output", str(out), "--target-sr", "16000", "--offset", "0.0",
         "--channel-attribution"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists()
    doc = load_sidecar_for_audio(str(out))
    assert doc is not None, proc.stdout
    assert doc["summary"]["usable"] is True, doc["summary"]
    # The summary is echoed to the child's stdout, which the parent
    # mirrors into backend.log — the only place a field pull can see it.
    assert "Channel attribution:" in proc.stdout


# ── DiarizationEngine.apply_channel_attribution ──────────────────────


def _clean_doc():
    mic, lb = two_stream_fixture()
    from core.channel_attribution import compute_attribution
    return compute_attribution(mic, lb, SR, loopback_offset_s=0.0)


def test_engine_applies_attribution_to_turns():
    turns = [
        {"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 6.0, "end": 10.0, "speaker": "SPEAKER_00"},
    ]
    out = DiarizationEngine.apply_channel_attribution(turns, _clean_doc())
    labels = {t["speaker"] for t in out}
    assert OWNER_SPEAKER_LABEL in labels
    far = [t for t in out if t["speaker"] != OWNER_SPEAKER_LABEL]
    assert all(t["start"] >= 5.0 for t in far), out


def test_engine_is_a_noop_without_a_sidecar():
    turns = [{"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    assert DiarizationEngine.apply_channel_attribution(turns, None) is turns
    assert DiarizationEngine.apply_channel_attribution(turns, {}) is turns


def test_engine_survives_a_garbage_sidecar():
    """Anything unreadable must cost the feature and nothing else — the
    voice-only turns come back untouched rather than an exception
    escaping into the processing pipeline."""
    turns = [{"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"}]
    for junk in ({"version": "not-a-number"}, {"spans": "nonsense"},
                 {"version": 1, "loopback_present": True,
                  "alignment": "wallclock", "summary": None,
                  "spans": [{"start": "x"}]}):
        assert DiarizationEngine.apply_channel_attribution(turns, junk) == turns


def test_owner_speaker_gets_a_readable_name_but_never_overwrites_one():
    """The user's speaker starts as the machine identity string and is
    shown as the live preview's "You" badge, then yields to the real
    name the known-speakers store supplies."""
    import services.recording_service as rs
    from models.speaker import Speaker
    from core.channel_attribution import OWNER_SPEAKER_DISPLAY_NAME

    fresh = Speaker(speaker_id=OWNER_SPEAKER_LABEL)
    rs.RecordingService._name_owner_speaker(fresh)
    assert fresh.display_name == OWNER_SPEAKER_DISPLAY_NAME

    named = Speaker(speaker_id=OWNER_SPEAKER_LABEL,
                    display_name="Joshua Rodriguez")
    rs.RecordingService._name_owner_speaker(named)
    assert named.display_name == "Joshua Rodriguez"

    other = Speaker(speaker_id="SPEAKER_01")
    rs.RecordingService._name_owner_speaker(other)
    assert other.display_name == "SPEAKER_01"


def test_missing_sidecar_lookup_is_silent(tmp_path):
    """Sessions recorded before this shipped: the lookup returns None,
    and process_session diarizes by voice alone exactly as it did."""
    import services.recording_service as rs
    session = rs.Session(session_id="OLD12345")
    session.audio_path = str(tmp_path / "session_OLD12345.wav")
    assert rs.RecordingService._load_channel_attribution(session) is None
    session.audio_path = None
    assert rs.RecordingService._load_channel_attribution(session) is None


def test_analysis_pass_is_finalize_only(monkeypatch):
    """The live path must pay nothing. LiveTranscriber and the capture
    callbacks have no reference to this module at all — the only
    entry points are finalize (write) and diarization (read)."""
    import core.live_transcriber as lt
    import core.audio_capture as ac
    for mod in (lt, ac):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "channel_attribution" not in src, mod.__name__


def test_streaming_analysis_reads_a_bounded_amount(recordings_dir: Path):
    """Bounded memory is a requirement, not a nicety: a 3-hour meeting
    goes through this. Frame reduction happens per block, so the
    retained state is the frame array (one float64 per 32 ms), never
    the resampled audio."""
    from core.channel_attribution import _FrameEnergyAccumulator

    acc = _FrameEnergyAccumulator(512)
    rng = np.random.default_rng(1)
    total = 0
    for _ in range(20):
        block = rng.standard_normal(3333).astype(np.float32)
        acc.push(block)
        total += len(block)
    frames = acc.finish()
    assert len(frames) == int(np.ceil(total / 512))
    assert frames.dtype == np.float64
    # Same answer as the whole-array reference implementation.
    from core.channel_attribution import _frame_mean_square
    rng2 = np.random.default_rng(1)
    whole = np.concatenate(
        [rng2.standard_normal(3333).astype(np.float32) for _ in range(20)])
    assert np.allclose(frames, _frame_mean_square(whole, 512))
