"""
Knowledge Folder format coverage — the gaps a real client folder hit.

FIELD DATA (2026-08-19)
-----------------------
One client Knowledge Folder skipped 19 files. Ten of those skips were
CORRECT and are pinned here so they stay that way: 6 images, 3 archives,
1 .drawio. The other nine were coverage gaps:

  * .csv  x 1 — indexed .xlsx but reported "unsupported file type: .csv"
  * .yaml x 2 — CloudFormation templates; plain text
  * .doc  x 5 — legacy binary Word; the valuable ones (TCO/ROI models,
                a competitive battle card, an architecture document)

The `.doc` tests are the interesting ones: that extension covers four
unrelated formats, so the extractor sniffs magic bytes before it does
anything else. See ``_sniff_document_kind``.
"""

from pathlib import Path

import numpy as np
import pytest

from services.document_service import (
    NON_TEXT_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    chunk_text,
    extract_text,
    index_folder,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fake_embed(chunks):
    return np.ones((len(chunks), 8), dtype=np.float32)


# ── plain-text formats ───────────────────────────────────────────────

@pytest.mark.parametrize("ext", [
    ".yaml", ".yml", ".json", ".xml", ".log",
    ".ini", ".cfg", ".toml", ".tf", ".sql", ".rst",
])
def test_plain_text_formats_are_extracted_and_chunked(tmp_path, ext):
    body = (
        "AWSTemplateFormatVersion 2010-09-09\n"
        "Description: Amazon Connect instance with Contact Lens enabled\n"
        + "Resources: ConnectInstance Type AWS::Connect::Instance\n" * 200
    )
    p = tmp_path / f"template{ext}"
    p.write_text(body, encoding="utf-8")

    text, reason = extract_text(p)
    assert reason is None, reason
    assert "Contact Lens" in text
    chunks = chunk_text(text)
    assert len(chunks) > 1, "long document should chunk into several pieces"
    assert ext in SUPPORTED_EXTENSIONS


def test_yaml_cloudformation_is_no_longer_an_unsupported_skip(tmp_path):
    """The exact field case: a CloudFormation template next to .txt
    files it is no less readable than."""
    p = tmp_path / "connect-instance.yaml"
    p.write_text(
        "Resources:\n  ConnectInstance:\n    Type: AWS::Connect::Instance\n",
        encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    assert "AWS::Connect::Instance" in text


@pytest.mark.parametrize("ext", [".csv", ".tsv"])
def test_csv_rows_stay_coherent(tmp_path, ext):
    """A CSV must chunk as records, not collapse into one unreadable
    line — consistent with how _extract_xlsx renders sheet rows."""
    delim = "," if ext == ".csv" else "\t"
    rows = [
        ["Role", "Rate", "Hours", "Notes"],
        ["Solution Architect", "225", "120", "Discovery and design"],
        ["Engineer", "185", "400", "Build, test, cutover"],
    ]
    p = tmp_path / f"estimate{ext}"
    p.write_text("\n".join(delim.join(r) for r in rows), encoding="utf-8")

    text, reason = extract_text(p)
    assert reason is None, reason
    lines = text.splitlines()
    assert len(lines) == 3, lines
    assert lines[0] == "Role | Rate | Hours | Notes"
    assert lines[1] == "Solution Architect | 225 | 120 | Discovery and design"


def test_csv_quoted_field_containing_the_delimiter_is_not_split(tmp_path):
    """Split-on-comma would shatter this row; the csv module does not."""
    p = tmp_path / "matrix.csv"
    p.write_text(
        'Requirement,Response\n'
        '"Routing, queues and overflow",Supported in phase one\n',
        encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    assert "Routing, queues and overflow | Supported in phase one" in text


def test_csv_embedded_newline_keeps_one_record_on_one_line(tmp_path):
    p = tmp_path / "notes.csv"
    p.write_text('Item,Detail\nSLA,"99.9% uptime\nmeasured monthly"\n',
                 encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    # The embedded newline must not break the record into two rows —
    # one record, one line, so chunking never splits mid-record.
    assert text.splitlines() == [
        "Item | Detail",
        "SLA | 99.9% uptime measured monthly",
    ]


def test_plain_text_read_is_bounded(tmp_path):
    from services.document_service import MAX_EXTRACTED_CHARS
    p = tmp_path / "huge.log"
    p.write_text("y" * (MAX_EXTRACTED_CHARS + 50_000), encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    assert len(text) == MAX_EXTRACTED_CHARS


def test_csv_extraction_is_bounded(tmp_path):
    from services.document_service import MAX_EXTRACTED_CHARS
    p = tmp_path / "huge.csv"
    row = ",".join(["cell"] * 20) + "\n"
    p.write_text(row * 40_000, encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None
    assert len(text) <= MAX_EXTRACTED_CHARS


# ── legacy .doc ──────────────────────────────────────────────────────
#
# The extension covers four unrelated formats. Each gets a test.

def test_doc_that_is_really_rtf_is_extracted(tmp_path):
    """The most common case in the wild: "save as .doc" from anything
    that is not Word writes RTF."""
    p = tmp_path / "battle-card.doc"
    p.write_bytes(
        rb"{\rtf1\ansi\deff0"
        rb"{\fonttbl{\f0\fnil Calibri;}}"
        rb"{\colortbl ;\red0\green0\blue0;}"
        rb"\f0\fs22 Amazon Connect vs Genesys Cloud\par "
        rb"Connect bills per minute with no seat licences.\par "
        rb"Contact Lens is native rather than a separate SKU.\par}"
    )
    text, reason = extract_text(p)
    assert reason is None, reason
    assert "Amazon Connect vs Genesys Cloud" in text
    assert "no seat licences" in text
    # Markup and the header tables must not survive into the index.
    assert "rtf1" not in text
    assert "Calibri" not in text
    assert "colortbl" not in text
    assert "\\par" not in text


def test_rtf_hex_escapes_are_decoded(tmp_path):
    p = tmp_path / "notes.rtf"
    p.write_bytes(rb"{\rtf1\ansi Caf\'e9 migration plan\par}")
    text, reason = extract_text(p)
    assert reason is None
    assert "Café migration plan" in text


def test_doc_that_is_really_html_is_extracted(tmp_path):
    """Word's own "Save as Web Page", and most server-side "export to
    Word" features, produce HTML with a .doc extension."""
    p = tmp_path / "architecture.doc"
    p.write_bytes(
        b"<html><head><style>p{color:red}</style></head><body>"
        b"<h1>Target Architecture</h1>"
        b"<p>Amazon Connect with Lex bots fronting the IVR.</p>"
        b"</body></html>"
    )
    text, reason = extract_text(p)
    assert reason is None, reason
    assert "Target Architecture" in text
    assert "Lex bots fronting the IVR" in text
    assert "color:red" not in text  # <style> contents dropped


def test_doc_that_is_really_plain_text_is_extracted(tmp_path):
    p = tmp_path / "assumptions.doc"
    p.write_text("450 agents across three sites.\nBlended rate 185.\n",
                 encoding="utf-8")
    text, reason = extract_text(p)
    assert reason is None, reason
    assert "450 agents across three sites" in text


def test_genuine_word97_doc_is_extracted():
    """A REAL Word 97-2003 binary document — see fixtures/README.md for
    why the fixture is not one this repo generated."""
    pytest.importorskip(
        "olefile",
        reason="olefile is an optional extractor dependency; the "
               "missing-dependency path is covered separately below.")
    fixture = FIXTURES / "word97-sample.doc"
    assert fixture.exists()
    # It really is an OLE2 compound file, not a zip or RTF.
    assert fixture.read_bytes()[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    text, reason = extract_text(fixture)
    assert reason is None, reason
    assert "Test OLE file, saved as Word 97-2003 Document." in text
    # The piece-table walk must yield prose, not stream garbage: no NULs
    # and no field-code control characters.
    assert "\x00" not in text
    for control in ("\x13", "\x14", "\x15"):
        assert control not in text


def test_genuine_word97_doc_chunks():
    pytest.importorskip("olefile")
    text, reason = extract_text(FIXTURES / "word97-sample.doc")
    assert reason is None
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert "Word 97-2003" in chunks[0]


def test_doc_ole2_missing_dependency_names_olefile(tmp_path, monkeypatch):
    """olefile is lazily imported like every other extractor library, so
    a build without it degrades to a counted skip naming the component —
    never an ImportError at module load."""
    import builtins
    real_import = builtins.__import__

    def no_olefile(name, *args, **kwargs):
        if name == "olefile":
            raise ImportError("no olefile")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_olefile)

    p = tmp_path / "model.doc"
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    text, reason = extract_text(p)
    assert text == ""
    assert "olefile" in reason.lower()
    # Same phrasing rule as every other optional dependency: "pip
    # install" is unactionable inside a packaged app.
    assert "pip install" not in reason.lower()
    assert "reinstall" in reason.lower()


def test_ole2_file_that_is_not_a_word_document_says_so(tmp_path):
    """An old .xls or .ppt renamed to .doc is an OLE2 file with no
    WordDocument stream. That is a distinct, accurate reason — not
    "corrupt"."""
    pytest.importorskip("olefile")
    import olefile
    p = tmp_path / "renamed.doc"
    # Truncated OLE2 header: olefile will either reject it or find no
    # WordDocument stream. Either way the reason must be specific.
    p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 1024)
    text, reason = extract_text(p)
    assert text == ""
    assert reason
    assert "unsupported file type" not in reason.lower()
    assert olefile  # referenced so the import is not flagged unused


def test_unrecognised_doc_content_is_reported_precisely(tmp_path):
    """Binary that is none of the four known .doc shapes must say what
    was actually checked, not "unsupported file type: .doc" — the user
    can act on the former."""
    p = tmp_path / "mystery.doc"
    p.write_bytes(bytes(range(1, 32)) * 200)
    text, reason = extract_text(p)
    assert text == ""
    assert "unrecognised .doc format" in reason.lower()
    assert "unsupported file type" not in reason.lower()


# ── the ten correct skips must stay correct ──────────────────────────

@pytest.mark.parametrize("filename", [
    "logo.png", "screenshot.jpg", "icon.jpeg", "banner.gif",
    "photo.bmp", "diagram.svg",
])
def test_images_are_still_skipped_as_non_text(tmp_path, filename):
    """6 of the 19 field skips were images. Correct then, correct now."""
    p = tmp_path / filename
    p.write_bytes(b"\x89PNG\r\n\x1a\n binary junk")
    text, reason = extract_text(p)
    assert text == ""
    assert "not a text document" in reason.lower()
    assert "images aren't indexed" in reason.lower()
    assert "unsupported" not in reason.lower()
    assert p.suffix.lower() not in SUPPORTED_EXTENSIONS


@pytest.mark.parametrize("filename", ["corpus.zip", "backup.rar", "old.7z"])
def test_archives_are_still_skipped_as_non_text(tmp_path, filename):
    """3 of the 19 field skips were archives. Still not indexed."""
    p = tmp_path / filename
    p.write_bytes(b"PK\x03\x04 binary junk")
    text, reason = extract_text(p)
    assert text == ""
    assert "not a text document" in reason.lower()
    assert "archives aren't indexed" in reason.lower()
    assert p.suffix.lower() not in SUPPORTED_EXTENSIONS


def test_drawio_is_still_classified_as_a_diagram(tmp_path):
    """1 of the 19 field skips was .drawio, and it stays a skip.

    Modern draw.io stores each <diagram> as base64 + deflate-compressed,
    URL-encoded XML, while older exports are plain XML with mxCell
    value="..." labels. Telling those apart reliably needs a real
    fixture from an actual draw.io export, which this repo does not
    have — so it did not "fall out easily" and is not being guessed at.
    See the NON_TEXT_EXTENSIONS comment.
    """
    p = tmp_path / "architecture.drawio"
    p.write_bytes(b"<mxfile><diagram>compressed-payload</diagram></mxfile>")
    text, reason = extract_text(p)
    assert text == ""
    assert "not a text document" in reason.lower()
    assert "diagrams aren't indexed" in reason.lower()
    assert ".drawio" in NON_TEXT_EXTENSIONS


def test_image_only_document_reads_as_no_extractable_text(tmp_path):
    """The distinction the field output already got right: a SUPPORTED
    format that simply holds no text must say "no extractable text",
    never "unsupported type"."""
    p = tmp_path / "scan.html"
    p.write_bytes(b"<html><body><img src='scan.png'></body></html>")
    text, reason = extract_text(p)
    assert text == ""
    assert "no extractable text" in reason.lower()
    assert "unsupported" not in reason.lower()
    assert "not a text document" not in reason.lower()


# ── the derived-supported-list property must still hold ──────────────

def test_supported_extensions_stays_derived_from_the_extractors():
    """v2.28.0 fixed a bug where the declared list drifted from actual
    capability. Everything added here must flow through the derivation,
    not be declared alongside it."""
    from services.document_service import _EXTRACTORS, _PLAIN_TEXT_EXTENSIONS
    assert SUPPORTED_EXTENSIONS == _PLAIN_TEXT_EXTENSIONS | set(_EXTRACTORS)
    # The newly covered formats are genuinely reachable.
    for ext in (".csv", ".tsv", ".yaml", ".yml", ".json", ".xml", ".log",
                ".doc", ".rtf"):
        assert ext in SUPPORTED_EXTENSIONS


def test_supported_and_non_text_sets_never_overlap():
    """A format cannot be both readable and "never going to be text" —
    that overlap is how a skip reason ends up contradicting itself."""
    assert not (SUPPORTED_EXTENSIONS & set(NON_TEXT_EXTENSIONS))


# ── end to end through index_folder ──────────────────────────────────

def test_index_folder_indexes_the_new_formats_and_still_skips_the_rest(
        tmp_path):
    folder = tmp_path / "knowledge"
    folder.mkdir()
    (folder / "notes.txt").write_text("baseline", encoding="utf-8")
    (folder / "stack.yaml").write_text(
        "Resources:\n  Instance:\n    Type: AWS::Connect::Instance\n",
        encoding="utf-8")
    (folder / "estimate.csv").write_text(
        "Role,Rate\nArchitect,225\n", encoding="utf-8")
    (folder / "card.doc").write_bytes(
        rb"{\rtf1\ansi Competitive positioning notes\par}")
    # …and the correct skips.
    (folder / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "corpus.zip").write_bytes(b"PK\x03\x04")
    (folder / "flow.drawio").write_bytes(b"<mxfile/>")

    recordings = tmp_path / "recordings"
    recordings.mkdir()
    report = index_folder(folder, "Acme", fake_embed, recordings)

    assert report["indexed"] == 4, report
    skipped = {Path(s["file"]).name: s for s in report["skipped"]}
    assert set(skipped) == {"logo.png", "corpus.zip", "flow.drawio"}
    for s in skipped.values():
        assert s["expected"] is True
        assert "not a text document" in s["reason"].lower()
