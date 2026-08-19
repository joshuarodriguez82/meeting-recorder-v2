"""
Recipient resolution and delivery reporting for follow-up drafts.

Field repro 2026-08-19, second half. `Draft follow-up emails` reported
"created 10 of 10 drafts" and returned HTTP 200. The drafts existed. Nine
of the ten had no email address, because the Outlook GAL was handed bare
first names and a GAL almost never resolves those; the one that worked
had an unusually distinctive first name. Then the drafts turned out not
to be in the Drafts folder the user was looking at either — `mail.Save()`
names no folder, so they went to whichever profile COM attached to.

Two overclaims, one shape: **a state we never established, rendered as a
finished one.** These tests pin the corrections.

  1. Before giving up on a name, try the richest form of that person we
     can prove we know — and prove it, or don't use it. An unconfirmed
     lookalike is worse than an empty To: field.
  2. A draft that can't be sent is counted separately from one that can,
     an item the mail client didn't confirm saving isn't counted at all,
     and the message says where the drafts went.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from services._follow_up_email_outlook import (
    _account_count,
    _draft_location,
    _store_account,
    save_draft,
)
from services.follow_up_owners import DraftPlan, READY
from services.follow_up_recipients import (
    DraftDelivery,
    attendee_match,
    candidate_names,
    delivery_message,
    draft_result,
    resolve_recipient,
)
from services.owner_service import OwnerAliasStore, load_alias_index
from services.session_service import SessionService


# ── 1. Which forms of the name we are willing to try ─────────────────


def test_attendee_supplies_a_fuller_form_of_a_bare_first_name():
    """The action item said "Alex"; the invite said "Alex Doe". A GAL
    resolves the second and shrugs at the first."""
    forms = candidate_names("Alex", attendees=["Alex Doe", "Pat Roe"])
    assert forms[0] == "Alex Doe"
    # ...and the label we started with survives as the fallback.
    assert forms[-1] == "Alex"


def test_two_attendees_matching_one_label_widen_to_neither():
    """THE guard. "Alex" with both an Alex Doe and an Alex Roe in the
    room is not a naming problem we get to solve by picking one —
    addressing somebody's commitments to their colleague is worse than
    an empty To: field."""
    attendees = ["Alex Doe", "Alex Roe"]
    assert attendee_match("Alex", attendees) is None
    assert candidate_names("Alex", attendees=attendees) == ["Alex"]


def test_substring_is_not_a_match():
    """Token subset, not substring: "Alex" is not a shorter spelling of
    "Alexandra"."""
    assert attendee_match("Alex", ["Alexandra Roe"]) is None
    assert candidate_names("Alex", attendees=["Alexandra Roe"]) == ["Alex"]


def test_same_length_attendee_adds_nothing():
    assert attendee_match("Jane Doe", ["Jane Doe"]) is None


def test_calendar_organiser_form_still_widens():
    """The awkward "Last, First Suffix [REGION]" shape has to keep
    working — it is the form the calendar actually hands us."""
    forms = candidate_names(
        "Pat", attendees=["Roe, Pat Jr. [US-EMEA]", "Jane Doe"])
    assert forms[0] == "Roe, Pat Jr. [US-EMEA]"


def test_a_fuller_form_is_only_tried_when_the_alias_index_groups_them(tmp_path):
    """An alias group is user-confirmed identity, so every name in it is
    fair game. A name that merely *looks* related is not — `suggest_groups`
    is advisory and never auto-applied, and this must not become a back
    door around that."""
    store = OwnerAliasStore(tmp_path)
    unconfirmed = load_alias_index(store)
    # Nothing confirmed yet: "Sam" widens to nothing.
    assert candidate_names("Sam", unconfirmed) == ["Sam"]

    store.create("Sam", ["sam", "samantha noh"])
    confirmed = load_alias_index(store)
    forms = candidate_names("Sam", confirmed)
    assert forms[0] == "Samantha Noh"   # richest, and lower-cased key
    assert "Sam" in forms               # still there as the fallback

    # A different person, same first token, NOT in the group.
    assert candidate_names("Sammy", confirmed) == ["Sammy"]


def test_alias_group_matches_on_the_canonical_name_too(tmp_path):
    """`build_draft_plan` resolves owners through `resolve_owners()`, so
    the label reaching us is usually the group's canonical display name —
    which is not required to be one of the member keys."""
    store = OwnerAliasStore(tmp_path)
    store.create("Sam Poe", ["sam", "samantha noh"])
    forms = candidate_names("Sam Poe", load_alias_index(store))
    assert "Samantha Noh" in forms


# ── 2. Resolution walks that list richest-first ──────────────────────


def _directory(*addressable: str):
    """A stand-in GAL that only knows the names given."""
    known = {n.strip().lower() for n in addressable}

    def resolve(name: str):
        key = (name or "").strip().lower()
        return f"{key.replace(' ', '.').replace(',', '')}@example.com" \
            if key in known else None

    return resolve


def test_a_first_name_that_fails_to_resolve_is_retried_against_a_fuller_form():
    """The whole point. The GAL knows "Alex Doe" and not "Alex"; the old
    code asked once, with "Alex", and gave up."""
    resolution = resolve_recipient(
        "Alex", resolver=_directory("Alex Doe"), attendees=["Alex Doe"])
    assert resolution.addressed
    assert resolution.resolved_from == "Alex Doe"
    assert resolution.address == "alex.doe@example.com"


def test_resolution_falls_back_through_shorter_forms():
    """A directory that only knows the short form still resolves — the
    ladder goes richest-first, not richest-only."""
    resolution = resolve_recipient(
        "Alex", resolver=_directory("Alex"), attendees=["Alex Doe"])
    assert resolution.address == "alex@example.com"
    assert resolution.resolved_from == "Alex"
    assert resolution.forms_tried == 2  # tried "Alex Doe" first


def test_unresolvable_name_still_yields_a_usable_to_field():
    """Not addressed, but not dropped: the body is useful and the user
    can fill in the recipient. The To: field carries the fullest form we
    know so their mail client has something to autocomplete."""
    resolution = resolve_recipient(
        "Alex", resolver=_directory(), attendees=["Alex Doe"])
    assert not resolution.addressed
    assert resolution.to_field == "Alex Doe"


def test_no_resolver_reports_itself_unaddressed():
    """macOS has no GAL. Choosing a better display name is not the same
    as having established an address, and the result must not imply it
    did."""
    resolution = resolve_recipient(
        "Alex", resolver=None, attendees=["Alex Doe"])
    assert not resolution.addressed
    assert resolution.to_field == "Alex Doe"
    assert resolution.forms_tried == 0


def test_a_raising_resolver_does_not_abort_the_ladder():
    def flaky(name: str):
        if name == "Alex Doe":
            raise RuntimeError("GAL unavailable")
        return "alex@example.com"

    resolution = resolve_recipient("Alex", resolver=flaky,
                                   attendees=["Alex Doe"])
    assert resolution.address == "alex@example.com"


# ── 3. The result distinguishes addressed from unaddressed ───────────


def _plan(owner_count: int = 3) -> DraftPlan:
    return DraftPlan(
        owners={f"Owner {i}": ["do the thing"] for i in range(owner_count)},
        state=READY, source="action_items")


def test_result_separates_addressed_from_unaddressed():
    delivery = DraftDelivery()
    delivery.note_created(addressed=True, location="Drafts",
                          account="user@example.com")
    for _ in range(9):
        delivery.note_created(addressed=False, location="Drafts",
                              account="user@example.com")

    result = draft_result(_plan(10), delivery, "your Outlook Drafts folder")
    assert result.created == 10
    assert result.addressed == 1
    assert result.unaddressed == 9
    assert result.location == "Drafts"
    assert result.account == "user@example.com"
    assert result.to_dict()["unaddressed"] == 9


def test_message_states_both_the_location_and_the_unaddressed_count():
    """The exact two facts the field report was missing: people expect a
    compose window (we only Save()), and nine of ten could not be sent."""
    delivery = DraftDelivery()
    delivery.note_created(addressed=True, location="Drafts",
                          account="user@example.com")
    for _ in range(9):
        delivery.note_created(addressed=False, location="Drafts",
                              account="user@example.com")

    message = delivery_message(delivery, "your Outlook Drafts folder")
    assert "10 drafts saved to Drafts (user@example.com)" in message
    assert "9 need an email address before you can send them" in message


def test_message_says_so_when_the_folder_could_not_be_confirmed():
    """No read-back means no claim. Naming a plausible folder we never
    checked is the defect, not the fix."""
    delivery = DraftDelivery()
    delivery.note_created(addressed=True)
    message = delivery_message(delivery, "your Outlook Drafts folder")
    assert "your Outlook Drafts folder" in message
    assert "could not be confirmed" in message


def test_message_singular_and_all_addressed():
    delivery = DraftDelivery()
    delivery.note_created(addressed=True, location="Drafts")
    message = delivery_message(delivery, "your Outlook Drafts folder")
    assert message.startswith("1 draft saved to Drafts")
    assert "ready to review" in message
    assert "email address" not in message


def test_unverified_items_are_excluded_from_created_and_reported():
    delivery = DraftDelivery()
    delivery.note_created(addressed=True, location="Drafts")
    delivery.note_unverified()
    result = draft_result(_plan(2), delivery, "your Outlook Drafts folder")
    assert result.created == 1
    assert result.unverified == 1
    assert "not counted above" in result.message


# ── 4. Outlook read-back and save verification ───────────────────────
#
# Fakes, not mocks of a mock: each one exposes exactly the COM surface
# the module touches, so a rename on our side fails the test.


class _FakeStore:
    def __init__(self, store_id: str, display: str):
        self.StoreID = store_id
        self.DisplayName = display


class _FakeAccount:
    def __init__(self, smtp: str, store: _FakeStore):
        self.SmtpAddress = smtp
        self.DeliveryStore = store


class _FakeAccounts(list):
    @property
    def Count(self) -> int:
        return len(self)


class _FakeFolder:
    def __init__(self, name: str, path: str, store: _FakeStore):
        self.Name = name
        self.FolderPath = path
        self.Store = store


class _FakeMail:
    def __init__(self, folder: _FakeFolder, entry_id: str):
        self._folder = folder
        self._entry_id = entry_id
        self.To = self.Subject = self.HTMLBody = ""
        self.EntryID = ""
        self.Parent = None
        self.displayed = False

    def Save(self):
        # A save that "worked" but did not persist leaves EntryID empty —
        # that is the case this whole check exists for.
        self.EntryID = self._entry_id
        self.Parent = self._folder if self._entry_id else None

    def Display(self):  # pragma: no cover - must never be called
        self.displayed = True


class _FakeRecipient:
    def __init__(self, name: str, directory):
        self._smtp = directory(name)

    def Resolve(self) -> bool:
        return self._smtp is not None

    @property
    def AddressEntry(self):
        smtp = self._smtp

        class _Entry:
            Address = smtp

            def GetExchangeUser(self):
                return SimpleNamespace(PrimarySmtpAddress=smtp)

        return _Entry()


class _FakeNamespace:
    def __init__(self, accounts, directory):
        self.Accounts = accounts
        self._directory = directory

    def CreateRecipient(self, name: str):
        return _FakeRecipient(name, self._directory)


class _FakeOutlook:
    def __init__(self, folder, entry_ids, namespace):
        self._folder = folder
        self._entry_ids = list(entry_ids)
        self._ns = namespace
        self.items = []

    def CreateItem(self, kind):
        assert kind == 0, "0 = olMailItem"
        entry_id = self._entry_ids.pop(0) if self._entry_ids else "entry"
        mail = _FakeMail(self._folder, entry_id)
        self.items.append(mail)
        return mail

    def GetNamespace(self, name):
        assert name == "MAPI"
        return self._ns


def _outlook(entry_ids=("e1",), directory=None, extra_accounts=0):
    store = _FakeStore("STORE-1", "user@example.com")
    folder = _FakeFolder("Drafts", "\\\\user@example.com\\Drafts", store)
    accounts = _FakeAccounts([_FakeAccount("user@example.com", store)])
    for i in range(extra_accounts):
        other = _FakeStore(f"STORE-{i + 2}", f"other{i}@example.org")
        accounts.append(_FakeAccount(f"other{i}@example.org", other))
    ns = _FakeNamespace(accounts, directory or _directory())
    return _FakeOutlook(folder, entry_ids, ns), ns


def test_save_draft_reads_back_the_folder_and_account():
    outlook, ns = _outlook()
    saved = save_draft(outlook, ns, "jane.doe@example.com", "Subject", "<p>x</p>")
    assert saved is not None
    assert saved.entry_id == "e1"
    assert saved.folder_name == "Drafts"
    assert saved.folder_path == "\\\\user@example.com\\Drafts"
    assert saved.account == "user@example.com"
    assert saved.location == "Drafts"
    # Save, never Display — ten windows is worse than not knowing.
    assert outlook.items[0].displayed is False


def test_an_item_without_an_entry_id_after_save_is_not_created():
    """COM raised nothing and reported nothing in the field log. An
    empty EntryID is the only evidence available that the save did not
    stick, so it is the one we act on."""
    outlook, ns = _outlook(entry_ids=[""])
    assert save_draft(outlook, ns, "jane.doe@example.com", "S", "<p>x</p>") is None


def test_store_account_falls_back_to_the_display_name():
    _outlook_obj, ns = _outlook()
    orphan = _FakeStore("STORE-UNKNOWN", "archive@example.com")
    assert _store_account(ns, orphan) == "archive@example.com"


def test_draft_location_degrades_when_com_will_not_say():
    class _Mute:
        @property
        def Parent(self):
            raise RuntimeError("MAPI object unavailable")

    _outlook_obj, ns = _outlook()
    assert _draft_location(ns, _Mute()) == ("", "", "")


def test_account_count_sees_every_configured_account():
    _outlook_obj, ns = _outlook(extra_accounts=2)
    assert _account_count(ns) == 3


# ── 5. End to end through the Windows backend ────────────────────────


class _FakeSummarizer:
    async def _chat(self, prompt, max_tokens=600, timeout=45.0):
        return "SUBJECT: Follow-up\nBODY:\nThanks — one item for you.\n"


def _svc(tmp_path: Path, session_id: str, session_data: dict):
    session_svc = SessionService(str(tmp_path), index_enabled=False)
    (tmp_path / f"session_{session_id}.json").write_text(
        json.dumps({"session_id": session_id, **session_data}),
        encoding="utf-8")
    return SimpleNamespace(
        session_svc=session_svc,
        commitments_svc=None,
        owner_alias_store=None,
        summarizer=_FakeSummarizer(),
    )


@pytest.fixture
def fake_win32com(monkeypatch):
    """Install a `win32com.client` the Outlook backend can import.

    `utils.com_worker.run_com` calls straight through on non-Windows, so
    with this in place the real `draft_follow_up_emails` runs end to end
    here.
    """
    created = {}

    def install(outlook):
        client = types.ModuleType("win32com.client")
        client.GetActiveObject = lambda progid: outlook
        client.Dispatch = lambda progid: outlook
        package = types.ModuleType("win32com")
        package.client = client
        monkeypatch.setitem(sys.modules, "win32com", package)
        monkeypatch.setitem(sys.modules, "win32com.client", client)
        created["outlook"] = outlook

    return install


def test_end_to_end_reports_where_the_drafts_went_and_what_is_unsendable(
        tmp_path, fake_win32com):
    from services._follow_up_email_outlook import draft_follow_up_emails

    session_id = "S1"
    svc = _svc(tmp_path, session_id, {
        "display_name": "Acme migration sync",
        "action_items": (
            "## Action Items\n"
            "- [ ] **Alex**: Send the routing map\n"
            "- [ ] **Jane Doe**: Book the Globex workshop\n"
        ),
        # The invite knows a fuller form of "Alex" than the action item did.
        "attendees": ["Alex Doe", "Jane Doe", "Pat Roe"],
    })
    # The GAL knows the full name and neither of the short/other forms.
    outlook, _ns = _outlook(entry_ids=["e1", "e2"],
                            directory=_directory("Alex Doe"))
    fake_win32com(outlook)

    result = draft_follow_up_emails(svc, session_id)

    assert result.created == 2
    assert result.addressed == 1        # "Alex" → "Alex Doe" → resolved
    assert result.unaddressed == 1      # "Jane Doe" is not in the GAL
    assert result.unverified == 0
    assert result.location == "Drafts"
    assert result.account == "user@example.com"
    assert "Drafts (user@example.com)" in result.message
    assert "1 needs an email address" in result.message

    tos = sorted(item.To for item in outlook.items)
    assert tos == ["Jane Doe", "alex.doe@example.com"]


def test_end_to_end_does_not_count_a_draft_that_did_not_persist(
        tmp_path, fake_win32com):
    from services._follow_up_email_outlook import draft_follow_up_emails

    session_id = "S2"
    svc = _svc(tmp_path, session_id, {
        "display_name": "Initech rollout",
        "action_items": (
            "## Action Items\n"
            "- [ ] **Jane Doe**: Send the routing map\n"
            "- [ ] **Pat Roe**: Book the Globex workshop\n"
        ),
        "attendees": [],
    })
    # Second Save() leaves EntryID empty — the item did not land.
    outlook, _ns = _outlook(entry_ids=["e1", ""])
    fake_win32com(outlook)

    result = draft_follow_up_emails(svc, session_id)

    assert result.created == 1
    assert result.unverified == 1
    assert "not counted above" in result.message
