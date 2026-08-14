"""
Tests for services/owner_service.py — the shared owner
splitting/normalising/aliasing logic behind the Follow Ups and
Commitments owner filters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.owner_service import (
    AliasIndex,
    OwnerAliasStore,
    aggregate_raw_owners,
    load_alias_index,
    normalize_owner,
    resolve_owners,
    split_owners,
    suggest_groups,
)


# ── Tier 1: splitting ───────────────────────────────────────────────────


class TestSplitOwners:
    def test_splits_on_slash(self):
        assert split_owners("Mark/Josh") == ["Mark", "Josh"]
        assert split_owners("Osmo/Craig/Josh") == ["Osmo", "Craig", "Josh"]

    def test_splits_on_ampersand(self):
        assert split_owners("Melissa & Kendra") == ["Melissa", "Kendra"]

    def test_splits_on_the_word_and(self):
        assert split_owners("Melissa and Kendra") == ["Melissa", "Kendra"]
        assert split_owners("melissa AND kendra") == ["melissa", "kendra"]

    def test_does_not_split_and_inside_a_word(self):
        # "Andrew" and "Sanders" both contain the letters "and" but not
        # as a standalone word — must not be shredded.
        assert split_owners("Andrew and Sanders") == ["Andrew", "Sanders"]
        assert split_owners("Andrew") == ["Andrew"]
        assert split_owners("Sanders") == ["Sanders"]

    def test_comma_is_not_split(self):
        """THE important negative test. Real names in this data contain
        commas — calendar organisers show up as
        "[scrubbed], Bob Jr. [US-US]" — so splitting on comma would
        shred a single name into two fake people. Do not add comma to
        the split pattern."""
        assert split_owners("[scrubbed], Bob Jr. [US-US]") == [
            "[scrubbed], Bob Jr. [US-US]"
        ]
        assert split_owners("Smith, John") == ["Smith, John"]

    def test_empty_and_blank(self):
        assert split_owners("") == []
        assert split_owners("   ") == []
        assert split_owners(None) == []  # type: ignore[arg-type]

    def test_dedupes_case_insensitively_within_one_string(self):
        assert split_owners("Josh / josh") == ["Josh"]

    def test_single_owner_passthrough(self):
        assert split_owners("Josh") == ["Josh"]


# ── Tier 2: safe normalisation ──────────────────────────────────────────


class TestNormalizeOwner:
    def test_case_and_whitespace(self):
        assert normalize_owner("  Josh  ") == ("josh", "Josh")
        assert normalize_owner("JOSH") == ("josh", "JOSH")

    def test_trailing_punctuation(self):
        assert normalize_owner("Josh.") == ("josh", "Josh")
        assert normalize_owner("Josh,") == ("josh", "Josh")

    def test_org_suffix_stripped(self):
        assert normalize_owner("Josh (AWS)") == ("josh", "Josh")
        assert normalize_owner("Josh (Guardian)") == ("josh", "Josh")

    def test_org_suffix_same_key_as_bare_name(self):
        # "Josh (AWS)" and "Josh" must land on the identical key so
        # they group automatically.
        key_a, _ = normalize_owner("Josh (AWS)")
        key_b, _ = normalize_owner("Josh")
        assert key_a == key_b == "josh"

    def test_different_base_names_do_not_share_a_key(self):
        # "Josh (AWS)" and "Jake (AWS)" must NOT collapse just because
        # they share an org suffix.
        key_josh, _ = normalize_owner("Josh (AWS)")
        key_jake, _ = normalize_owner("Jake (AWS)")
        assert key_josh != key_jake

    def test_internal_whitespace_collapsed(self):
        assert normalize_owner("[scrubbed]") == (
            "[scrubbed]", "[scrubbed]")

    def test_comma_preserved_within_a_single_piece(self):
        # normalize_owner operates on an already-split piece; a comma
        # inside it (e.g. from an un-split calendar name) must survive.
        key, display = normalize_owner("[scrubbed], Bob Jr.")
        assert display == "[scrubbed], Bob Jr"  # trailing period stripped
        assert "," in key


# ── Tier 3: suggestions (advisory only) ─────────────────────────────────


class TestSuggestGroups:
    def test_suggests_josh_joshua_and_[scrubbed](self):
        counts, display = aggregate_raw_owners(
            ["Josh", "Josh (AWS)", "[scrubbed]", "Joshua"])
        groups = suggest_groups(counts, display)
        assert len(groups) == 1
        member_displays = {m.display for m in groups[0].members}
        assert member_displays == {"Josh", "[scrubbed]", "Joshua"}

    def test_suggestions_are_not_applied(self):
        """Suggesting a merge must never itself change what
        resolve_owners() returns — only an accepted alias can do that."""
        raw_items = ["Josh", "Joshua", "[scrubbed]"]
        counts, display = aggregate_raw_owners(raw_items)
        groups = suggest_groups(counts, display)
        assert groups  # sanity: a suggestion exists

        # Without any alias, each raw owner still resolves to itself —
        # the suggestion changed nothing.
        assert resolve_owners("Josh") == ["Josh"]
        assert resolve_owners("Joshua") == ["Joshua"]
        assert resolve_owners("[scrubbed]") == ["[scrubbed]"]

    def test_josh_aws_and_jake_aws_not_suggested_together(self):
        counts, display = aggregate_raw_owners(["Josh (AWS)", "Jake (AWS)"])
        groups = suggest_groups(counts, display)
        assert groups == []

    def test_excludes_already_aliased_keys(self):
        counts, display = aggregate_raw_owners(["Josh", "Joshua"])
        groups = suggest_groups(counts, display, already_grouped_keys={"josh", "joshua"})
        assert groups == []

    def test_excludes_rejected_pairs(self):
        counts, display = aggregate_raw_owners(["Josh", "Joshua"])
        groups = suggest_groups(counts, display, rejected_pairs=[("josh", "joshua")])
        assert groups == []


# ── Aggregation / counting ───────────────────────────────────────────────


class TestAggregateRawOwners:
    def test_one_item_several_owners_counts_once_per_person(self):
        # "Mark/Josh" is ONE item but names two people; each should get
        # +1, not the item being double counted overall.
        counts, _ = aggregate_raw_owners(["Mark/Josh"])
        assert counts["mark"] == 1
        assert counts["josh"] == 1

    def test_totals_do_not_imply_duplicated_items(self):
        raw_items = ["Josh", "Mark/Josh", "Osmo/Craig/Josh"]
        counts, _ = aggregate_raw_owners(raw_items)
        # 3 raw items total, but "josh" appears in all 3 -> count 3,
        # which legitimately can equal (or, with more people, exceed)
        # len(raw_items) — callers must report the item total (3)
        # separately rather than summing per-person counts and calling
        # it "items".
        assert counts["josh"] == 3
        assert len(raw_items) == 3
        assert sum(counts.values()) > len(raw_items)  # mark + craig + osmo + josh*3

    def test_duplicate_person_within_one_item_counts_once(self):
        counts, _ = aggregate_raw_owners(["Josh / josh"])
        assert counts["josh"] == 1

    def test_real_owner_list_before_after(self):
        """The real client's Follow Ups owner list from the bug
        report: before/after counts should show the fix helps."""
        raw_list = [
            "All Sales Team Members", "Dale/Dan", "Dan [scrubbed]", "Emily",
            "Jake (AWS)", "Jeremy", "Josh", "Josh (AWS)", "[scrubbed]",
            "Joshua", "Kamal (Guardian)", "Karthik", "Ken (AWS)", "Lisa",
            "Madonna [scrubbed]", "Mark", "Mark/Josh", "Melissa",
            "Melissa & Kendra", "Osmo/Craig", "Osmo/Craig/Josh", "Paul",
            "Paul/Craig/Josh", "Quincy",
        ]
        before = len(raw_list)  # 24 opaque filter entries today
        counts, display = aggregate_raw_owners(raw_list)
        after_safe = len(counts)  # after tier-1 split + tier-2 normalize
        assert before == 24
        assert after_safe == 22  # safe (tier-1 + tier-2) auto-grouping only
        assert after_safe < before
        # Josh is directly findable now, including via the multi-owner
        # strings that used to hide him entirely.
        assert counts["josh"] == 5  # Josh, Josh (AWS), Mark/Josh,
        # Osmo/Craig/Josh, Paul/Craig/Josh
        groups = suggest_groups(counts, display)
        josh_group = next(
            g for g in groups if "Josh" in {m.display for m in g.members})
        assert {m.display for m in josh_group.members} == {
            "Josh", "[scrubbed]", "Joshua"}


# ── resolve_owners() — the function both call sites share ──────────────


class TestResolveOwners:
    def test_multi_owner_resolves_to_both_people(self):
        assert resolve_owners("Mark/Josh") == ["Mark", "Josh"]

    def test_org_suffix_resolves_to_bare_name(self):
        assert resolve_owners("Josh (AWS)") == ["Josh"]

    def test_no_alias_index_keeps_variants_separate(self):
        assert resolve_owners("Joshua") == ["Joshua"]
        assert resolve_owners("Josh") == ["Josh"]

    def test_alias_groups_items(self):
        store_aliases = [
            _fake_alias("a1", "Josh", ["josh", "joshua", "[scrubbed]"]),
        ]
        idx = AliasIndex(store_aliases)
        assert resolve_owners("Joshua", idx) == ["Josh"]
        assert resolve_owners("[scrubbed]", idx) == ["Josh"]
        assert resolve_owners("Josh", idx) == ["Josh"]

    def test_multi_owner_with_alias(self):
        idx = AliasIndex([_fake_alias("a1", "Josh", ["josh", "joshua"])])
        assert resolve_owners("Mark/Joshua", idx) == ["Mark", "Josh"]


def _fake_alias(id_: str, canonical: str, members: list[str]):
    from services.owner_service import OwnerAlias
    return OwnerAlias(id=id_, canonical=canonical, members=members, created_at="")


# ── OwnerAliasStore persistence ─────────────────────────────────────────


class TestOwnerAliasStore:
    def test_create_and_list_roundtrip(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        alias = store.create("Josh", ["Josh", "Joshua", "[scrubbed]"])
        assert alias.canonical == "Josh"
        assert set(alias.members) == {"josh", "joshua", "[scrubbed]"}

        all_aliases = store.list_all()
        assert len(all_aliases) == 1
        assert all_aliases[0].canonical == "Josh"

    def test_persists_across_new_store_instances(self, tmp_path: Path):
        OwnerAliasStore(tmp_path).create("Josh", ["Josh", "Joshua"])
        # A brand-new instance pointed at the same directory must see
        # what the first instance wrote — this is the "roams with the
        # recordings dir" contract.
        reloaded = OwnerAliasStore(tmp_path)
        aliases = reloaded.list_all()
        assert len(aliases) == 1
        assert set(aliases[0].members) == {"josh", "joshua"}
        assert (tmp_path / "owner_aliases.json").exists()

    def test_accepted_alias_groups_items(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        store.create("Josh", ["Josh", "Joshua", "[scrubbed]"])
        idx = load_alias_index(store)
        assert resolve_owners("Joshua", idx) == ["Josh"]
        assert resolve_owners("[scrubbed]", idx) == ["Josh"]

    def test_removing_alias_ungroups_items(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        alias = store.create("Josh", ["Josh", "Joshua"])
        idx = load_alias_index(store)
        assert resolve_owners("Joshua", idx) == ["Josh"]

        deleted = store.delete(alias.id)
        assert deleted is True
        idx_after = load_alias_index(store)
        # Reverts to the original (tier-2) unaliased entries — grouping
        # is fully reversible.
        assert resolve_owners("Joshua", idx_after) == ["Joshua"]
        assert resolve_owners("Josh", idx_after) == ["Josh"]

    def test_delete_missing_alias_returns_false(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        assert store.delete("does-not-exist") is False

    def test_update_removes_a_member_splitting_it_back_out(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        alias = store.create("Josh", ["Josh", "Joshua", "Mark"])
        store.update(alias.id, remove_members=["mark"])
        idx = load_alias_index(store)
        assert resolve_owners("Mark", idx) == ["Mark"]  # split back out
        assert resolve_owners("Joshua", idx) == ["Josh"]  # still grouped

    def test_update_removing_last_member_deletes_the_group(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        alias = store.create("Josh", ["Josh", "Joshua"])
        store.update(alias.id, remove_members=["josh", "joshua"])
        assert store.list_all() == []

    def test_update_rename_canonical(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        alias = store.create("Josh", ["Josh", "Joshua"])
        updated = store.update(alias.id, canonical="Josh R.")
        assert updated is not None
        assert updated.canonical == "Josh R."

    def test_a_member_cannot_belong_to_two_aliases(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        store.create("Mark", ["Mark", "Marky"])
        # Creating a second group that claims "marky" pulls it out of
        # the first group rather than duplicating it.
        store.create("Marky M.", ["Marky", "Mark Thompson"])
        aliases = {a.canonical: set(a.members) for a in store.list_all()}
        assert aliases["Mark"] == {"mark"}
        assert aliases["Marky M."] == {"marky", "mark thompson"}

    def test_reject_records_a_pair(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        store.reject("Josh", "Mark")
        pairs = store.rejected_pairs()
        assert ("josh", "mark") in pairs or ("mark", "josh") in pairs

    def test_missing_file_degrades_to_no_grouping(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path / "does-not-exist-yet")
        assert store.list_all() == []
        assert store.rejected_pairs() == []
        idx = load_alias_index(store)
        assert resolve_owners("Josh", idx) == ["Josh"]

    def test_corrupt_file_degrades_to_no_grouping(self, tmp_path: Path):
        path = tmp_path / "owner_aliases.json"
        path.write_text("{ this is not valid json !!!", encoding="utf-8")
        store = OwnerAliasStore(tmp_path)
        assert store.list_all() == []
        idx = load_alias_index(store)
        # No raise, and no grouping applied — degrades safely.
        assert resolve_owners("Joshua", idx) == ["Joshua"]

    def test_non_dict_json_degrades_to_no_grouping(self, tmp_path: Path):
        path = tmp_path / "owner_aliases.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        store = OwnerAliasStore(tmp_path)
        assert store.list_all() == []

    def test_load_alias_index_handles_none_store(self):
        idx = load_alias_index(None)
        assert resolve_owners("Josh", idx) == ["Josh"]

    def test_create_requires_canonical_and_members(self, tmp_path: Path):
        store = OwnerAliasStore(tmp_path)
        with pytest.raises(ValueError):
            store.create("", ["Josh"])
        with pytest.raises(ValueError):
            store.create("Josh", [])
