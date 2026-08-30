"""
"0 documents" when the folder has forty-seven of them.

THE FIELD REPORT
----------------
An install with 20 clients and 117 meetings reported **0 indexed
documents and 0 chunks on every single client**. Semantic search was
running on transcripts alone; every SOW, SDD and questionnaire in those
folders was invisible, and had been for months.

Nothing was broken in the indexer. Indexing runs when the knowledge
folder is *set*, and never again — no watcher, no startup scan, no
periodic sweep. Point a folder at Drive, add documents to it over the
following six months, and none of them are indexed. That is a design
choice, and a defensible one: reading and embedding a folder is
expensive and touches a network drive.

What is NOT defensible is that nothing said so. `/clients/{c}/knowledge`
reported `indexed_documents` and `total_chunks` and never how many
files were actually sitting in the folder — so "nothing is indexed" and
"the folder is empty" were the same answer, in the API, in the UI, and
in the `list_clients` tool an assistant reads. The house defect, in the
place it hid longest: **a result you couldn't read must never render as
a result that isn't there.**

WHAT THESE TESTS PIN
--------------------
`scan_folder` counting what is really on disk, so the gap between
present and indexed can be stated. Its edges matter more than its happy
path — a scan that walks a Drive folder on every status call has to be
bounded, and one that miscounts Office lock files reports work to do
that does not exist.
"""

from __future__ import annotations

from pathlib import Path

from services import document_service as ds


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


class TestScanCounts:
    def test_counts_supported_documents(self, tmp_path: Path):
        for name in ("sow.pdf", "notes.docx", "pricing.xlsx", "readme.md"):
            _touch(tmp_path, name)
        r = ds.scan_folder(tmp_path)
        assert r["indexable"] == 4
        assert r["unsupported"] == 0

    def test_counts_unsupported_separately_rather_than_ignoring_them(
            self, tmp_path: Path):
        """A folder of forty .drawio files is not an empty folder, and
        telling the user "0 documents" about it is the same lie in a
        different shape."""
        _touch(tmp_path, "sow.pdf")
        _touch(tmp_path, "diagram.drawio")
        _touch(tmp_path, "recording.mp4")
        r = ds.scan_folder(tmp_path)
        assert r["indexable"] == 1
        assert r["unsupported"] == 2

    def test_recurses_into_subfolders(self, tmp_path: Path):
        _touch(tmp_path, "top.pdf")
        _touch(tmp_path, "2026/q3/deep.docx")
        assert ds.scan_folder(tmp_path)["indexable"] == 2

    def test_skips_hidden_and_sync_client_noise(self, tmp_path: Path):
        """Same rule the indexer itself uses — .git, .DS_Store and
        sync sentinel directories are tooling noise, never a document
        anyone meant to index."""
        _touch(tmp_path, "real.pdf")
        _touch(tmp_path, ".git/config")
        _touch(tmp_path, ".DS_Store")
        _touch(tmp_path, ".dropbox.cache/tmp.docx")
        r = ds.scan_folder(tmp_path)
        assert r["indexable"] == 1
        assert r["unsupported"] == 0

    def test_skips_office_lock_files(self, tmp_path: Path):
        """Word and Excel leave `~$name.docx` beside an open document.
        Counting them reports work to do that does not exist, and on a
        shared Drive folder there can be a lot of them."""
        _touch(tmp_path, "proposal.docx")
        _touch(tmp_path, "~$proposal.docx")
        _touch(tmp_path, "~$budget.xlsx")
        r = ds.scan_folder(tmp_path)
        assert r["indexable"] == 1

    def test_empty_folder_is_genuinely_zero(self, tmp_path: Path):
        r = ds.scan_folder(tmp_path)
        assert r["indexable"] == 0 and r["unsupported"] == 0
        assert r["capped"] is False


class TestBounded:
    def test_stops_at_the_cap_and_says_so(self, tmp_path: Path):
        """This runs on a status call, against a folder that is often a
        network drive. An unbounded walk turns opening the Clients tab
        into a stall."""
        for i in range(12):
            _touch(tmp_path, f"doc{i}.pdf")
        r = ds.scan_folder(tmp_path, max_files=5)
        assert r["capped"] is True
        assert r["indexable"] + r["unsupported"] == 5

    def test_not_capped_when_under_the_limit(self, tmp_path: Path):
        _touch(tmp_path, "a.pdf")
        assert ds.scan_folder(tmp_path, max_files=5)["capped"] is False


class TestNeverRaises:
    def test_missing_folder(self, tmp_path: Path):
        """A Drive folder that isn't mounted yet is an ordinary state,
        not an error — the caller already reports reachability."""
        r = ds.scan_folder(tmp_path / "nope")
        assert r["indexable"] == 0
        assert r["unreadable"] is True

    def test_a_file_where_a_folder_was_expected(self, tmp_path: Path):
        f = _touch(tmp_path, "not-a-folder.txt")
        r = ds.scan_folder(f)
        assert r["unreadable"] is True

    def test_empty_path(self, tmp_path: Path):
        assert ds.scan_folder("")["unreadable"] is True
