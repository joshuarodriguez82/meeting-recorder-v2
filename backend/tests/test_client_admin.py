"""
Renaming, merging and deleting clients and projects.

WHY IT EXISTS
-------------
There was no way to remove or correct a client. `ClientConfigService`
carried `rename()` and `delete()` but nothing exposed them, and both
touch only the CONFIG entry — folders and portal binding — never the
meetings.

That gap produced a real, live state: an install with both "Northwind"
and "Nortwind" configured, the misspelling holding no folders, and
every meeting tagged to the typo orphaned from the real account's data.
Deleting the typo's config would not have fixed it; the meetings would
still carry the wrong tag and still be missing from the right client.

So the operation that actually repairs it is MERGE, and delete is the
lesser case.

THE RULE THAT OUTRANKS EVERYTHING HERE
--------------------------------------
**Deleting a client must never delete a recording.** A client is a tag
and a folder configuration. The meetings are the user's data and the
whole point of the product. Every path below is tested for that, because
it is the one mistake that cannot be undone with a button.

WHAT ELSE THE TESTS PIN
-----------------------
Merging is the dangerous direction: the target already has folders, a
portal binding and indexed documents, and a careless merge silently
replaces them with the empty ones from the entry being folded in.
Counts are returned rather than implied, because "renamed" with no
number is indistinguishable from "renamed nothing" — this codebase's
recurring defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import client_admin


def _session(sid: str, client: str, project: str = "") -> dict:
    return {"session_id": sid, "client": client, "project": project}


class TestPlanRename:
    def test_pure_rename_when_the_target_does_not_exist(self):
        plan = client_admin.plan_rename(
            "Nortwind", "Northwind",
            existing_clients=["Nortwind"],
            sessions=[_session("s1", "Nortwind")])
        assert plan.is_merge is False
        assert plan.session_ids == ["s1"]

    def test_merge_when_the_target_already_exists(self):
        """The live case. Both names configured, meetings on the typo."""
        plan = client_admin.plan_rename(
            "Nortwind", "Northwind",
            existing_clients=["Nortwind", "Northwind"],
            sessions=[_session("s1", "Nortwind"),
                      _session("s2", "Northwind")])
        assert plan.is_merge is True
        # Only the source's sessions are retagged; the target's are
        # already where they belong.
        assert plan.session_ids == ["s1"]

    def test_renaming_to_itself_is_a_no_op(self):
        plan = client_admin.plan_rename(
            "Acme", "Acme", existing_clients=["Acme"],
            sessions=[_session("s1", "Acme")])
        assert plan.is_noop is True
        assert plan.session_ids == []

    def test_case_only_change_is_a_rename_not_a_merge(self):
        """"acme" -> "Acme" is the same client wearing better
        capitalisation. Treating it as a merge would fold a client into
        itself, and the counts would claim work that never happened."""
        plan = client_admin.plan_rename(
            "acme", "Acme", existing_clients=["acme"],
            sessions=[_session("s1", "acme")])
        assert plan.is_merge is False
        assert plan.is_noop is False

    def test_matching_is_case_insensitive_like_the_config_store(self):
        plan = client_admin.plan_rename(
            "nortwind", "Northwind",
            existing_clients=["Nortwind"],
            sessions=[_session("s1", "NORTWIND")])
        assert plan.session_ids == ["s1"]

    def test_an_empty_new_name_is_rejected(self):
        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                client_admin.plan_rename(
                    "Acme", bad, existing_clients=["Acme"], sessions=[])

    def test_sessions_of_other_clients_are_untouched(self):
        plan = client_admin.plan_rename(
            "Acme", "Acme Corp", existing_clients=["Acme"],
            sessions=[_session("s1", "Acme"), _session("s2", "Globex")])
        assert plan.session_ids == ["s1"]


class TestMergeConfig:
    def test_target_keeps_its_own_folders(self):
        """THE DANGEROUS DIRECTION. The typo entry has no folders; the
        real one has both. A merge that lets the source win silently
        unconfigures a working client."""
        merged = client_admin.merge_config(
            source={"export_folder": "", "knowledge_folder": "",
                    "customer_id": ""},
            target={"export_folder": "/drive/Northwind/Exports",
                    "knowledge_folder": "/drive/Northwind/Knowledge",
                    "customer_id": "cus_real"})
        assert merged["export_folder"] == "/drive/Northwind/Exports"
        assert merged["knowledge_folder"] == "/drive/Northwind/Knowledge"
        assert merged["customer_id"] == "cus_real"

    def test_source_fills_only_the_gaps(self):
        """If the target is missing something the source has, take it —
        that is the useful half of a merge."""
        merged = client_admin.merge_config(
            source={"export_folder": "/drive/old/Exports",
                    "knowledge_folder": "", "customer_id": "cus_old"},
            target={"export_folder": "", "knowledge_folder": "/drive/new/K",
                    "customer_id": ""})
        assert merged["export_folder"] == "/drive/old/Exports"
        assert merged["knowledge_folder"] == "/drive/new/K"
        assert merged["customer_id"] == "cus_old"

    def test_no_target_yet_takes_the_source_wholesale(self):
        src = {"export_folder": "/a", "knowledge_folder": "/b",
               "customer_id": "c"}
        assert client_admin.merge_config(source=src, target=None) == src


class TestPlanDelete:
    def test_reports_what_will_happen_to_the_meetings(self):
        """A delete that does not say how many meetings it affects is
        one the user cannot consent to."""
        plan = client_admin.plan_delete(
            "Acme", sessions=[_session("s1", "Acme"), _session("s2", "Acme"),
                              _session("s3", "Globex")], untag_sessions=True)
        assert plan.session_ids == ["s1", "s2"]

    def test_config_only_delete_leaves_the_tags_alone(self):
        plan = client_admin.plan_delete(
            "Acme", sessions=[_session("s1", "Acme")], untag_sessions=False)
        assert plan.session_ids == []

    def test_a_delete_plan_never_names_a_session_for_deletion(self):
        """The rule that outranks everything: a client is a tag and some
        folder settings. Deleting one must never remove a recording, in
        either mode, ever."""
        for untag in (True, False):
            plan = client_admin.plan_delete(
                "Acme", sessions=[_session("s1", "Acme")],
                untag_sessions=untag)
            assert not hasattr(plan, "delete_session_ids")
            assert getattr(plan, "deletes_recordings", False) is False


class TestProjects:
    def test_rename_only_touches_the_named_client(self):
        ids = client_admin.plan_project_rename(
            "Acme", "Phase 1", "Phase One",
            sessions=[_session("s1", "Acme", "Phase 1"),
                      _session("s2", "Globex", "Phase 1"),
                      _session("s3", "Acme", "Phase 2")])
        assert ids == ["s1"]

    def test_delete_clears_the_project_on_that_client_only(self):
        ids = client_admin.plan_project_delete(
            "Acme", "Phase 1",
            sessions=[_session("s1", "Acme", "Phase 1"),
                      _session("s2", "Globex", "Phase 1")])
        assert ids == ["s1"]

    def test_project_matching_is_case_insensitive(self):
        ids = client_admin.plan_project_delete(
            "Acme", "phase 1",
            sessions=[_session("s1", "Acme", "Phase 1")])
        assert ids == ["s1"]

    def test_an_empty_project_rename_target_is_rejected(self):
        with pytest.raises(ValueError):
            client_admin.plan_project_rename("Acme", "P1", "  ", sessions=[])


class TestDocumentRekey:
    def test_rewrites_only_the_matching_client(self, tmp_path: Path):
        """Indexed documents carry the client name too. A rename that
        left them behind would make the merged client's own documents
        unfindable — the "0 documents" failure again, self-inflicted."""
        doc_dir = tmp_path / "doc_index"
        doc_dir.mkdir()
        (doc_dir / "doc_a.json").write_text(
            json.dumps({"client": "Nortwind", "doc_name": "sow.pdf"}),
            encoding="utf-8")
        (doc_dir / "doc_b.json").write_text(
            json.dumps({"client": "Globex", "doc_name": "other.pdf"}),
            encoding="utf-8")

        n = client_admin.rekey_documents(tmp_path, "Nortwind", "Northwind")

        assert n == 1
        assert json.loads((doc_dir / "doc_a.json").read_text())["client"] \
            == "Northwind"
        assert json.loads((doc_dir / "doc_b.json").read_text())["client"] \
            == "Globex"

    def test_a_missing_index_is_not_an_error(self, tmp_path: Path):
        assert client_admin.rekey_documents(tmp_path, "A", "B") == 0

    def test_an_unreadable_sidecar_is_skipped_not_fatal(self, tmp_path: Path):
        doc_dir = tmp_path / "doc_index"
        doc_dir.mkdir()
        (doc_dir / "doc_bad.json").write_text("{ not json", encoding="utf-8")
        (doc_dir / "doc_ok.json").write_text(
            json.dumps({"client": "A"}), encoding="utf-8")
        assert client_admin.rekey_documents(tmp_path, "A", "B") == 1
