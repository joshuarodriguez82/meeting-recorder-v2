"""
Integration tests: CommitmentsService.list_all()'s owner filter goes
through services/owner_service.py's split/normalise/alias resolution
instead of a raw string compare — so "Sam" finds a commitment owned
by "Mark/Sam" or (once aliased) "Samantha".
"""

from __future__ import annotations

import json
from pathlib import Path

from services.commitments_service import Commitment, CommitmentsService
from services.owner_service import OwnerAliasStore, load_alias_index
from services.session_service import SessionService


def _write_session(recordings_dir: Path, session_id: str, **extra) -> None:
    data = {"session_id": session_id, "client": "Acme", **extra}
    (recordings_dir / f"session_{session_id}.json").write_text(
        json.dumps(data), encoding="utf-8")


def _commitment(session_id: str, owner: str, commitment_id: str) -> Commitment:
    return Commitment(
        commitment_id=commitment_id,
        session_id=session_id,
        owner=owner,
        side="unknown",
        description="do the thing",
        quote="",
        timestamp_seconds=0.0,
        due_date_iso="",
        created_at="2026-08-01T00:00:00",
        status="awaiting",
    )


def _make_service(tmp_path: Path) -> CommitmentsService:
    session_svc = SessionService(str(tmp_path), index_enabled=False)
    return CommitmentsService(session_svc)


class TestOwnerFilterSplitsMultiOwnerStrings:
    def test_multi_owner_string_matches_either_person(self, tmp_path: Path):
        svc = _make_service(tmp_path)
        _write_session(tmp_path, "s1")
        svc.replace_session_commitments(
            "s1", [_commitment("s1", "Mark/Sam", "c1")])

        by_sam = svc.list_all(owner="Sam")
        by_mark = svc.list_all(owner="Mark")
        by_other = svc.list_all(owner="Craig")

        assert [c["commitment_id"] for c in by_sam] == ["c1"]
        assert [c["commitment_id"] for c in by_mark] == ["c1"]
        assert by_other == []

    def test_org_suffix_matches_bare_name(self, tmp_path: Path):
        svc = _make_service(tmp_path)
        _write_session(tmp_path, "s1")
        svc.replace_session_commitments(
            "s1", [_commitment("s1", "Sam (AWS)", "c1")])

        assert [c["commitment_id"] for c in svc.list_all(owner="Sam")] == ["c1"]

    def test_comma_containing_owner_is_not_split(self, tmp_path: Path):
        svc = _make_service(tmp_path)
        _write_session(tmp_path, "s1")
        svc.replace_session_commitments(
            "s1", [_commitment("s1", "Roe, Pat Jr.", "c1")])

        # Filtering by the full (comma-containing) name still matches.
        assert [c["commitment_id"]
                for c in svc.list_all(owner="Roe, Pat Jr")] == ["c1"]
        # It must NOT be findable under just "Pat" or "Roe" —
        # those would only work if the comma had been split.
        assert svc.list_all(owner="Pat") == []
        assert svc.list_all(owner="Roe") == []


class TestOwnerFilterWithAliases:
    def test_alias_groups_items_across_a_filter(self, tmp_path: Path):
        svc = _make_service(tmp_path)
        _write_session(tmp_path, "s1")
        _write_session(tmp_path, "s2")
        svc.replace_session_commitments("s1", [_commitment("s1", "Sam", "c1")])
        svc.replace_session_commitments("s2", [_commitment("s2", "Samantha", "c2")])

        # Without an alias, "Samantha" isn't found under "Sam".
        assert {c["commitment_id"] for c in svc.list_all(owner="Sam")} == {"c1"}

        store = OwnerAliasStore(tmp_path)
        store.create("Sam", ["Sam", "Samantha"])
        idx = load_alias_index(store)

        assert {c["commitment_id"]
                for c in svc.list_all(owner="Sam", alias_index=idx)} == {"c1", "c2"}

    def test_removing_alias_ungroups_the_filter(self, tmp_path: Path):
        svc = _make_service(tmp_path)
        _write_session(tmp_path, "s1")
        svc.replace_session_commitments("s1", [_commitment("s1", "Samantha", "c1")])

        store = OwnerAliasStore(tmp_path)
        alias = store.create("Sam", ["Sam", "Samantha"])
        idx = load_alias_index(store)
        assert {c["commitment_id"]
                for c in svc.list_all(owner="Sam", alias_index=idx)} == {"c1"}

        store.delete(alias.id)
        idx_after = load_alias_index(store)
        assert svc.list_all(owner="Sam", alias_index=idx_after) == []
        assert {c["commitment_id"]
                for c in svc.list_all(owner="Samantha", alias_index=idx_after)} == {"c1"}
