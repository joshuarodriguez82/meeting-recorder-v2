"""
Importing a Teams / Zoom transcript with, or instead of, the recording.

PROVENANCE OF THE FIXTURES
--------------------------
The VTT shapes follow what each platform emits: Teams puts the speaker
in a WebVTT voice span and prefixes each cue with a "<guid>/<n>-<m>"
identifier (the Graph API's callTranscript content uses the short
``0:0:0.0`` timestamp form); Zoom numbers its cues and writes
"Name: text". Names are the repo's placeholders, and the Teams roster
form "Doe, Jane [NA]" is kept because commas and brackets in a name are
exactly what a parser trips on.

The Teams .docx layout ("Name   0:03", then the words) is reproduced
from the platform's download, not captured from one. When a real export
is available, replace these with a scrubbed copy of it (AGENTS.md:
fixtures are copied from real payloads).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core import transcript_import as ti

TEAMS_VTT = """WEBVTT

3f1a2b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b/12-0
00:00:03.258 --> 00:00:05.418
<v Doe, Jane [NA]>Good morning everyone. Let's</v>

3f1a2b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b/12-1
00:00:05.418 --> 00:00:08.100
<v Doe, Jane [NA]>review the migration timeline.</v>

3f1a2b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b/18-0
00:00:09.002 --> 00:00:13.540
<v Roe, Richard>The pilot queue goes live on Monday.</v>

3f1a2b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b/23-0
00:00:14.100 --> 00:00:16.000
<v Doe, Jane [NA]>Who owns the holiday routing rules?</v>
"""

GRAPH_VTT = """WEBVTT

0:0:0.0 --> 0:0:5.320
<v Jane Doe>This is a transcript test.</v>

0:0:6.1 --> 0:0:8.0
<v Rob Poe>Thanks Jane.</v>
"""

ZOOM_VTT = """WEBVTT

1
00:00:01.170 --> 00:00:04.560
Jane Doe: Good morning everyone.

2
00:00:05.020 --> 00:00:09.880
Richard Roe: Thanks. Note: the pilot goes live Monday.

3
00:00:10.300 --> 00:00:12.000
Jane Doe: Great.
"""

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello and welcome.

2
00:00:04,000 --> 00:00:06,250
Let's get started.
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _docx(tmp_path: Path, name: str, paragraphs) -> Path:
    """A real (minimal) Word document: one <w:p> per paragraph."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def para(text: str) -> str:
        runs = []
        for i, part in enumerate(text.split("\t")):
            if i:
                runs.append("<w:r><w:tab/></w:r>")
            if part:
                esc = (part.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;"))
                runs.append(f'<w:r><w:t xml:space="preserve">{esc}</w:t></w:r>')
        return f"<w:p>{''.join(runs)}</w:p>"

    body = "".join(para(t) for t in paragraphs)
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", xml)
    return p


# ── reading ──────────────────────────────────────────────────────────


def test_teams_vtt_names_and_merges_split_sentences(tmp_path):
    cues = ti.read_transcript(_write(tmp_path, "t.vtt", TEAMS_VTT))
    assert [(c.speaker, c.text) for c in cues] == [
        ("Doe, Jane [NA]",
         "Good morning everyone. Let's review the migration timeline."),
        ("Roe, Richard", "The pilot queue goes live on Monday."),
        ("Doe, Jane [NA]", "Who owns the holiday routing rules?"),
    ]
    assert cues[0].start == pytest.approx(3.258)
    assert cues[0].end == pytest.approx(8.1)
    assert ti.speaker_names(cues) == ["Doe, Jane [NA]", "Roe, Richard"]


def test_graph_api_short_timestamps(tmp_path):
    cues = ti.read_transcript(_write(tmp_path, "g.vtt", GRAPH_VTT))
    assert [(c.start, c.end, c.speaker) for c in cues] == [
        (0.0, 5.32, "Jane Doe"), (6.1, 8.0, "Rob Poe")]


def test_zoom_prefixes_are_names_but_a_colon_in_the_text_is_not(tmp_path):
    cues = ti.read_transcript(_write(tmp_path, "z.vtt", ZOOM_VTT))
    assert [(c.speaker, c.text) for c in cues] == [
        ("Jane Doe", "Good morning everyone."),
        ("Richard Roe", "Thanks. Note: the pilot goes live Monday."),
        ("Jane Doe", "Great."),
    ]


def test_srt_has_no_names(tmp_path):
    cues = ti.read_transcript(_write(tmp_path, "s.srt", SRT))
    assert [c.text for c in cues] == ["Hello and welcome.",
                                      "Let's get started."]
    assert cues[1].start == pytest.approx(4.0)
    assert ti.speaker_names(cues) == []


def test_teams_docx_speaker_lines(tmp_path):
    doc = _docx(tmp_path, "Weekly sync.docx", [
        "Weekly sync",
        "September 30, 2026, 2:00PM",
        "45m 12s",
        "Jane Doe started transcription",
        "Jane Doe   0:03",
        "Good morning everyone.",
        "Doe, Rob [EMEA]\t0:12",
        "Thanks Jane. The cutover is on track.",
        "Jane Doe   1:05",
        "We start at 9:30",
        "Jane Doe   1:09:30",
        "And the next review is next week.",
    ])
    cues = ti.read_transcript(doc)
    assert [(c.start, c.speaker, c.text) for c in cues] == [
        (3.0, "Jane Doe", "Good morning everyone."),
        (12.0, "Doe, Rob [EMEA]", "Thanks Jane. The cutover is on track."),
        (65.0, "Jane Doe", "We start at 9:30"),
        (4170.0, "Jane Doe", "And the next review is next week."),
    ]
    assert cues[0].end == 12.0              # runs until the next speaker


def test_a_one_line_remark_with_a_time_is_not_a_speaker(tmp_path):
    """'We start at 9:30' looks like 'Name 9:30' — but it comes right
    after a speaker line, and the line after a speaker line is always
    what they said."""
    doc = _docx(tmp_path, "t.docx", [
        "Jane Doe   0:03",
        "We start at 9:30",
        "Rob Poe   0:10",
        "Sounds good.",
    ])
    cues = ti.read_transcript(doc)
    assert [(c.speaker, c.text) for c in cues] == [
        ("Jane Doe", "We start at 9:30"), ("Rob Poe", "Sounds good.")]


def test_older_teams_docx_cue_layout(tmp_path):
    doc = _docx(tmp_path, "old.docx", [
        "0:0:0.0 --> 0:0:4.120",
        "Jane Doe",
        "Can everyone hear me?",
        "",
        "0:0:5.0 --> 0:0:6.500",
        "Rob Poe",
        "Yes.",
    ])
    cues = ti.read_transcript(doc)
    assert [(c.start, c.speaker, c.text) for c in cues] == [
        (0.0, "Jane Doe", "Can everyone hear me?"),
        (5.0, "Rob Poe", "Yes.")]


@pytest.mark.parametrize("name,body", [
    ("empty.vtt", "WEBVTT\n\n"),
    ("notes.txt", "just some notes\nwith no times\n"),
])
def test_files_without_timed_lines_are_refused(tmp_path, name, body):
    with pytest.raises(ti.TranscriptError, match="No timed lines"):
        ti.read_transcript(_write(tmp_path, name, body))


def test_a_broken_docx_is_refused(tmp_path):
    p = tmp_path / "x.docx"
    p.write_bytes(b"not a zip")
    with pytest.raises(ti.TranscriptError, match="readable Word file"):
        ti.read_transcript(p)


def test_a_docx_with_an_entity_bomb_is_refused_not_parsed(tmp_path):
    """The file is untrusted; Word never writes a DOCTYPE."""
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            + "".join(f'<!ENTITY lol{i} "{"&lol%s;" % ("" if i == 1 else i - 1) * 10}">'
                      for i in range(1, 10))
            + ']><w:document xmlns:w="http://schemas.openxmlformats.org/'
              'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&lol9;'
              '</w:t></w:r></w:p></w:body></w:document>')
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", bomb)
    with pytest.raises(ti.TranscriptError, match="DOCTYPE"):
        ti.read_transcript(p)


def test_word_tab_stops_and_formatting_are_not_text(tmp_path):
    """Real Word XML carries paragraph and run properties (w:pPr, w:rPr)
    and tab-stop definitions (w:tabs/w:tab outside any run); none of
    that is what was said."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = (f'<w:document xmlns:w="{ns}"><w:body>'
           '<w:p w14:paraId="1A2B"><w:pPr><w:tabs><w:tab w:val="left" '
           'w:pos="720"/></w:tabs></w:pPr><w:r><w:rPr><w:b/></w:rPr>'
           '<w:t>Jane Doe</w:t></w:r><w:r><w:tab/></w:r><w:r>'
           '<w:t>0:03</w:t></w:r></w:p>'
           '<w:p><w:r><w:t xml:space="preserve">Fish &amp; chips </w:t>'
           '</w:r><w:r><w:t>at noon.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    p = tmp_path / "real.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", xml)
    cues = ti.read_transcript(p)
    assert [(c.start, c.speaker, c.text) for c in cues] == [
        (3.0, "Jane Doe", "Fish & chips at noon.")]


def test_an_oversized_docx_is_refused_before_inflating(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "MAX_DOCX_XML_BYTES", 1000)
    p = _docx(tmp_path, "big.docx", ["Jane Doe   0:01", "x" * 5000])
    with pytest.raises(ti.TranscriptError, match="too large"):
        ti.read_transcript(p)


@pytest.mark.parametrize("text,seconds", [
    ("00:01:02.500", 62.5), ("01:02.500", 62.5), ("0:1:2.5", 62.5),
    ("00:01:02,500", 62.5), ("1:00:00", 3600.0),
])
def test_timestamps(text, seconds):
    assert ti.parse_timestamp(text) == pytest.approx(seconds)


# ── importing ────────────────────────────────────────────────────────


def _svc(tmp_path):
    from services.session_service import SessionService
    return SessionService(str(tmp_path / "recordings"), index_enabled=False)


def test_a_transcript_alone_becomes_a_session_with_named_speakers(tmp_path):
    vtt = _write(tmp_path, "Acme sync.vtt", TEAMS_VTT)
    svc = _svc(tmp_path)
    session = svc.import_from_file(transcript_path=str(vtt), client="Acme")

    assert session.audio_path in ("", None)
    assert session.display_name == "Acme sync"
    names = sorted(sp.display_name for sp in session.speakers.values())
    assert names == ["Doe, Jane [NA]", "Roe, Richard"]
    assert len(session.segments) == 3
    length = (session.ended_at - session.started_at).total_seconds()
    assert length == pytest.approx(16.0)
    loaded = svc.load_full(session.session_id)
    transcript = loaded.full_transcript()
    assert "Doe, Jane [NA]: Good morning everyone." in transcript
    assert "Roe, Richard: The pilot queue" in transcript
    # The Sessions list sees a processed transcript, not an unprocessed
    # recording waiting for Whisper.
    row = next(r for r in svc.list_sessions()
               if r["session_id"] == session.session_id)
    assert row["has_transcript"] and not row["audio_exists"]


def test_lines_without_a_name_are_not_given_to_the_first_speaker(tmp_path):
    vtt = _write(tmp_path, "mixed.vtt", GRAPH_VTT + """
0:0:9.0 --> 0:0:10.0
Someone joined late.
""")
    session = _svc(tmp_path).import_from_file(transcript_path=str(vtt))
    by_id = {sid: sp.display_name for sid, sp in session.speakers.items()}
    last = session.segments[-1]
    assert by_id[last.speaker_id] == "Unknown speaker"
    assert by_id[session.segments[0].speaker_id] == "Jane Doe"


def test_an_unnamed_transcript_alone_is_one_speaker(tmp_path):
    srt = _write(tmp_path, "call.srt", SRT)
    session = _svc(tmp_path).import_from_file(transcript_path=str(srt))
    assert [sp.display_name for sp in session.speakers.values()] == ["Speaker 1"]
    assert session.import_notes


def test_a_bad_transcript_fails_before_anything_is_written(tmp_path):
    bad = _write(tmp_path, "bad.vtt", "WEBVTT\n\n")
    svc = _svc(tmp_path)
    with pytest.raises(ti.TranscriptError):
        svc.import_from_file(transcript_path=str(bad))
    assert not list((tmp_path / "recordings").glob("session_*"))


def test_nothing_chosen_is_refused(tmp_path):
    with pytest.raises(ValueError, match="recording, a transcript"):
        _svc(tmp_path).import_from_file()


def test_processing_skips_the_speech_models_for_a_transcript(
        tmp_path, monkeypatch):
    """process_full must not wait on — or fail with — a model load a
    transcript never uses."""
    import asyncio
    import sys
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    sys.modules.setdefault("dotenv", MagicMock())
    from _app_import import import_app
    import_app()
    import server

    svc = _svc(tmp_path)
    session = svc.import_from_file(
        transcript_path=str(_write(tmp_path, "t.vtt", TEAMS_VTT)))
    monkeypatch.setattr(server.svc, "settings",
                        SimpleNamespace(is_configured=True))
    monkeypatch.setattr(server.svc, "_services_ready", True)
    monkeypatch.setattr(server.svc, "session_svc", svc)

    loads = []
    monkeypatch.setattr(server.svc, "ensure_models_loaded",
                        lambda: loads.append(1))

    class _Stop(Exception):
        pass

    def _past_the_model_gate(*a, **k):
        raise _Stop()
    # The first thing after the gate is the summary; stop there.
    monkeypatch.setattr(server, "_llm_notes", _past_the_model_gate)
    with pytest.raises(_Stop):
        asyncio.run(server.process_full(
            session.session_id, server.ProcessFullRequest()))
    assert loads == []


def _wav(tmp_path: Path, seconds: float = 20.0) -> Path:
    import numpy as np
    sf = pytest.importorskip("soundfile")
    p = tmp_path / "Acme sync.wav"
    sf.write(str(p), np.zeros(int(16000 * seconds), dtype=np.float32), 16000)
    return p


def test_recording_and_transcript_keep_the_audio_and_the_names(tmp_path):
    svc = _svc(tmp_path)
    session = svc.import_from_file(
        str(_wav(tmp_path)),
        transcript_path=str(_write(tmp_path, "t.vtt", TEAMS_VTT)))
    assert Path(session.audio_path).is_file()
    assert sorted(sp.display_name for sp in session.speakers.values()) == [
        "Doe, Jane [NA]", "Roe, Richard"]
    # The recording's length, not the transcript's.
    length = (session.ended_at - session.started_at).total_seconds()
    assert length == pytest.approx(20.0)
    assert session.import_notes == []


def test_an_unnamed_transcript_with_a_recording_is_set_aside(tmp_path):
    svc = _svc(tmp_path)
    session = svc.import_from_file(
        str(_wav(tmp_path)),
        transcript_path=str(_write(tmp_path, "c.srt", SRT)))
    # Left for Whisper + speaker separation, which tell voices apart.
    assert session.segments == []
    assert any("transcribed instead" in n for n in session.import_notes)
