"""
Follow-up delivery when the user has no usable desktop mail client.

Field repro 2026-08-19, third instalment. `calendar_source =
"extension"` is presented to the user as **"Never contacts Outlook"**,
and the calendar side honours that. The *drafting* side did not: **Draft
follow-up emails** called `win32com.client.Dispatch("Outlook.Application")`,
which launches classic desktop Outlook and files drafts into whichever
profile COM attaches to. The user was on the Outlook PWA — that is *why*
they had set "extension" — and got ten drafts they could never see, in a
client they do not use.

macOS had the same defect in AppleScript: Mail.app, then *Microsoft
Outlook*, then `.eml` files on disk.

The fix routes both platforms to Outlook Web compose deeplinks when the
setting says no mail client. These tests pin four things:

  1. **"extension" never reaches a mail client** — not COM on Windows,
     not AppleScript on macOS, not the `.eml` fallback. Enforced with
     stubs that fail the test if they are so much as touched.
  2. **Every other value behaves exactly as before**, on both platforms.
  3. **The URLs survive real content** — `&`, `#`, `+`, newlines and
     non-ASCII in both subject and body — and an over-budget body is
     shortened *in the URL only*, with the full text still returned.
  4. **The message does not claim a draft exists.** A compose link is a
     different artifact: close the tab and it is gone, and no mailbox
     ever held it. Saying "saved to your Drafts folder" about one would
     be the same overclaim `test_follow_up_recipients.py` exists to stop.

Section 5 is a guard rail rather than a feature test: the calendar feed
must be bit-for-bit unaffected by all of the above, for every one of the
four `calendar_source` values. Calendar display and auto-record were
broken for weeks and only fixed across v2.30.0–v2.34.0; a regression
there has to fail loudly here.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from services import follow_up_email
from services._follow_up_email_web import (
    COMPOSE_ENDPOINT,
    TRUNCATION_NOTICE,
    URL_BUDGET_CHARS,
    ComposeLink,
    build_compose_url,
    compose_message,
)
from services.follow_up_owners import (
    ARTIFACT_COMPOSE_LINK,
    ARTIFACT_DRAFT,
    READY,
    UNREADABLE_FORMAT,
)
from services.session_service import SessionService


# ── Helpers ──────────────────────────────────────────────────────────


ACTION_ITEMS = (
    "## Action Items\n"
    "- [ ] **[Jane Doe]**: Send the routing map to Acme\n"
    "- [ ] **[Pat Roe]**: Confirm the Globex pricing model\n"
)


class _FakeSummarizer:
    """Stands in for `Summarizer`. `_compose_body` only ever calls
    `_chat`, and only ever parses SUBJECT:/BODY: out of the reply."""

    def __init__(self, body: str = "Thanks for the time today.\n\n- One thing\n"):
        self.body = body
        self.calls = 0

    async def _chat(self, prompt: str, max_tokens: int = 600,
                    timeout: float = 45.0) -> str:
        self.calls += 1
        return f"SUBJECT: Follow-up — Acme sync\nBODY:\n{self.body}"


def _make_svc(tmp_path: Path, session_id: str = "s1",
              session_data: dict | None = None,
              summarizer: _FakeSummarizer | None = None):
    """Minimal stand-in for server.py's service container — same shape
    as test_follow_up_owners.py's."""
    session_svc = SessionService(str(tmp_path), index_enabled=False)
    data = session_data if session_data is not None else {
        "display_name": "Acme sync",
        "action_items": ACTION_ITEMS,
        "attendees": ["Jane Doe", "Pat Roe"],
    }
    (tmp_path / f"session_{session_id}.json").write_text(
        json.dumps({"session_id": session_id, **data}), encoding="utf-8")
    from services.commitments_service import CommitmentsService
    return SimpleNamespace(
        session_svc=session_svc,
        commitments_svc=CommitmentsService(session_svc),
        owner_alias_store=None,
        summarizer=summarizer or _FakeSummarizer(),
    )


def _set_calendar_source(monkeypatch, value: str) -> None:
    """Point the router's live settings read at `value`.

    Patches `Settings.from_env` rather than the router's own helper, so
    the real reader in `follow_up_email._calendar_source` is under test
    too — that function is the entire gate.
    """
    from config import settings as settings_mod
    monkeypatch.setattr(
        settings_mod.Settings, "from_env",
        classmethod(lambda cls: SimpleNamespace(calendar_source=value)),
    )


def _sabotage_every_mail_client(monkeypatch) -> None:
    """Make any attempt to reach a mail client an immediate test failure.

    Not "assert it returned []" — the point is that the call is never
    made at all. Attaching to Outlook is itself what trips a locked-down
    tenant's conditional-access challenge, and `osascript` against
    Mail.app is what puts a draft somewhere a PWA user will never look.
    """
    def _boom(*args, **kwargs):
        raise AssertionError(
            "a mail client was contacted while calendar_source == 'extension'")

    # Windows: the COM worker and the pywin32 import behind it.
    from services import _follow_up_email_outlook as win_mod
    monkeypatch.setattr(win_mod, "run_com", _boom)
    fake_win32com = types.ModuleType("win32com")
    fake_client = types.ModuleType("win32com.client")
    fake_client.Dispatch = _boom
    fake_client.GetActiveObject = _boom
    fake_win32com.client = fake_client
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)

    # macOS: AppleScript, and the .eml-on-disk fallback underneath it.
    from services import _follow_up_email_macos as mac_mod
    monkeypatch.setattr(mac_mod, "_osascript", _boom)
    monkeypatch.setattr(mac_mod, "subprocess", SimpleNamespace(run=_boom))
    monkeypatch.setattr(mac_mod, "_write_eml_fallback", _boom)


# ── 1. "extension" never reaches a mail client ───────────────────────


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_extension_source_never_touches_a_mail_client(
        tmp_path, monkeypatch, platform):
    """The whole point. On every platform, including the one that used
    to have no drafting path at all."""
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", platform)

    svc = _make_svc(tmp_path)
    result = follow_up_email.draft_follow_up_emails(svc, "s1")

    assert result.artifact == ARTIFACT_COMPOSE_LINK
    assert result.state == READY
    assert result.created == 2
    assert len(result.compose_links) == 2
    for link in result.compose_links:
        assert link["url"].startswith(COMPOSE_ENDPOINT + "?")


def test_extension_source_names_no_folder_and_no_account(tmp_path, monkeypatch):
    """Nothing was written anywhere, so there is nothing to name. The
    macOS backend's `_default_account_address()` read-back and the
    Outlook backend's folder read-back both describe a mailbox that this
    path never wrote to; reporting one here would be fiction."""
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")

    result = follow_up_email.draft_follow_up_emails(_make_svc(tmp_path), "s1")

    assert result.location == ""
    assert result.account == ""
    assert "folder" not in result.message.lower()
    assert "mailbox" in result.message.lower()


def test_extension_source_writes_no_eml_files(tmp_path, monkeypatch):
    """The macOS `.eml` fallback is right where it lives — it is the last
    resort before producing nothing. Here it would be a third artifact
    kind (a file in ~/Downloads is not a draft either) offered to a user
    whose situation is precisely "working browser, no working mail
    client". So: not used, and nothing lands on disk."""
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", "darwin")
    downloads = tmp_path / "home" / "Downloads"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    follow_up_email.draft_follow_up_emails(_make_svc(tmp_path), "s1")

    assert not downloads.exists()


def test_extension_source_still_reports_empty_states(tmp_path, monkeypatch):
    """The three empty states are the shared plan's, not the delivery
    path's, so they must survive the reroute — with the artifact kind
    still set, because the UI branches on it before it reads `state`."""
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    svc = _make_svc(tmp_path, session_data={
        "display_name": "Acme sync",
        "action_items": "## Action Items\n- [ ] Send the deck onwards\n",
    })
    result = follow_up_email.draft_follow_up_emails(svc, "s1")

    assert result.state == UNREADABLE_FORMAT
    assert result.created == 0
    assert result.artifact == ARTIFACT_COMPOSE_LINK


# ── 2. Every other value behaves exactly as before ───────────────────


@pytest.mark.parametrize("source", ["auto", "outlook", "off", "garbage"])
@pytest.mark.parametrize(
    "platform,module_name",
    [("win32", "services._follow_up_email_outlook"),
     ("darwin", "services._follow_up_email_macos")],
)
def test_other_sources_keep_the_platform_backend(
        tmp_path, monkeypatch, source, platform, module_name):
    """"auto", "outlook", "off" and an unrecognised value all keep
    today's behaviour: the OS's own mail client, exactly as shipped.
    Other users depend on it and it works.

    "off" is deliberately in this list. It means "no calendar data",
    which says nothing about whether the user's Outlook works — silently
    moving their drafts somewhere else would be its own surprise.
    """
    _set_calendar_source(monkeypatch, source)
    monkeypatch.setattr(sys, "platform", platform)

    called = {}

    def _impl(svc, session_id, tone="friendly-professional"):
        called["module"] = module_name
        called["tone"] = tone
        from services.follow_up_owners import DraftResult
        return DraftResult(created=3, state=READY)

    module = sys.modules[module_name] if module_name in sys.modules else \
        __import__(module_name, fromlist=["draft_follow_up_emails"])
    monkeypatch.setattr(module, "draft_follow_up_emails", _impl)

    result = follow_up_email.draft_follow_up_emails(
        _make_svc(tmp_path), "s1", tone="brisk")

    assert called["module"] == module_name
    assert called["tone"] == "brisk"
    # The pre-existing paths report saved drafts, unchanged.
    assert result.artifact == ARTIFACT_DRAFT
    assert result.created == 3


def test_unreadable_settings_fall_back_to_auto(monkeypatch):
    """A settings read that raises must not crash a drafting run, and
    must not silently become "extension" either — the fallback is the
    pre-existing behaviour."""
    from config import settings as settings_mod

    def _raise(cls):
        raise OSError("config.env is unreadable")

    monkeypatch.setattr(settings_mod.Settings, "from_env", classmethod(_raise))
    assert follow_up_email._calendar_source() == "auto"


# ── 3. The URLs survive real content ─────────────────────────────────


def _body_param(url: str) -> str:
    return parse_qs(urlparse(url).query, keep_blank_values=True)["body"][0]


def _subject_param(url: str) -> str:
    return parse_qs(urlparse(url).query, keep_blank_values=True)["subject"][0]


def test_ampersand_hash_and_plus_survive_the_round_trip():
    """The three characters that quietly destroy a hand-built query
    string: `&` splits it into extra parameters, `#` truncates it at the
    fragment, and `+` reads back as a space under form decoding."""
    subject = "Pricing & scope — item #4 + follow-up"
    body = "Q3 & Q4 budget\n#1 priority\nC++ migration"
    url, truncated = build_compose_url("jane.doe@example.com", subject, body)

    assert not truncated
    assert "#" not in url          # would truncate the URL at the fragment
    assert _subject_param(url) == subject
    assert _body_param(url) == body
    # Space is %20, not "+", so a literal plus is unambiguous.
    assert "%20" in url and "+" not in url


def test_newlines_and_non_ascii_survive_the_round_trip():
    """Bodies are multi-line with bullets, and names carry diacritics."""
    subject = "Suivi — réunion Zorg"
    body = "Bonjour Ana van der Noh,\n\n- Réviser le plan\n- Envoyer le devis\n"
    url, truncated = build_compose_url("ana@example.com", subject, body)

    assert not truncated
    assert "%0A" in url            # newlines encoded, not dropped
    assert _subject_param(url) == subject
    assert _body_param(url) == body


def test_an_unaddressed_recipient_still_gets_a_link():
    """A bare first name resolves to no address. The compose window just
    opens with an empty To: — that is a usable outcome, and dropping the
    recipient entirely is not."""
    url, _ = build_compose_url("", "Follow-up", "Hello there")

    assert url.startswith(COMPOSE_ENDPOINT + "?to=&")
    assert parse_qs(urlparse(url).query, keep_blank_values=True)["to"] == [""]


# ── 4. Over-budget bodies ────────────────────────────────────────────


def test_a_long_body_is_shortened_in_the_url_only():
    body = "Thanks for the time today. " * 200          # ~5 400 characters
    url, truncated = build_compose_url(
        "jane.doe@example.com", "Follow-up — Acme sync", body)

    assert truncated
    assert len(url) <= URL_BUDGET_CHARS
    in_url = _body_param(url)
    assert len(in_url) < len(body)
    # The user can SEE that this is not the whole message.
    assert in_url.endswith(TRUNCATION_NOTICE)
    assert in_url.startswith("Thanks for the time today.")


def test_the_full_body_is_never_lost_to_truncation(tmp_path, monkeypatch):
    """Truncation is a property of the URL, not of the message. The
    complete text comes back on the result so the UI can offer it."""
    long_body = "Recap of the Globex migration. " * 200
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    svc = _make_svc(tmp_path, summarizer=_FakeSummarizer(long_body))
    result = follow_up_email.draft_follow_up_emails(svc, "s1")

    for link in result.compose_links:
        assert link["truncated"] is True
        assert link["body"].strip() == long_body.strip()
        assert len(link["url"]) <= URL_BUDGET_CHARS
        assert len(link["body"]) > len(_body_param(link["url"]))


def test_the_budget_is_measured_on_the_encoded_form():
    """A body of newlines costs three URL characters each and one of
    emoji up to twelve. Counting source characters would blow the budget
    on exactly the bodies most likely to be near it."""
    body = "\n".join("ligne é" for _ in range(400))
    url, truncated = build_compose_url("a@example.com", "Suivi", body)

    assert truncated
    assert len(url) <= URL_BUDGET_CHARS

    emoji = "🙂" * 800
    url2, truncated2 = build_compose_url("a@example.com", "Hi", emoji)
    assert truncated2
    assert len(url2) <= URL_BUDGET_CHARS


def test_subject_and_recipient_are_never_truncated():
    """A mangled subject or a mangled address is worse than a shortened
    body, so the body absorbs the whole shortfall — and when even that
    is not enough, the link still opens against the right mailbox."""
    subject = "S" * 2500
    url, truncated = build_compose_url(
        "jane.doe@example.com", subject, "Some body text here.")

    assert truncated
    assert _subject_param(url) == subject
    assert parse_qs(urlparse(url).query, keep_blank_values=True)["to"] == \
        ["jane.doe@example.com"]
    assert _body_param(url) == ""


def test_a_body_that_fits_is_left_exactly_alone():
    body = "Short and complete.\n\n- One item\n"
    url, truncated = build_compose_url("a@example.com", "Follow-up", body)

    assert not truncated
    assert _body_param(url) == body
    assert TRUNCATION_NOTICE not in _body_param(url)


# ── 5. The message must not claim a draft exists ─────────────────────


def _link(addressed: bool = True, truncated: bool = False) -> ComposeLink:
    return ComposeLink(
        owner="Jane Doe",
        display_name="Jane Doe",
        address="jane.doe@example.com" if addressed else "",
        subject="Follow-up",
        body="Hello",
        url=COMPOSE_ENDPOINT + "?to=",
        truncated=truncated,
    )


def test_the_message_says_nothing_was_saved():
    msg = compose_message([_link(), _link()])

    assert "compose link" in msg.lower()
    assert "nothing has been saved to any mailbox" in msg.lower()
    # The saved-draft vocabulary from `delivery_message` must not appear:
    # "N drafts saved to <folder>" is a different artifact.
    assert "saved to your" not in msg.lower()
    assert "drafts folder" not in msg.lower()
    assert "drafts saved" not in msg.lower()


def test_the_message_does_not_borrow_the_saved_draft_wording():
    """Guards the exact sentence shape that would confuse the two.
    `follow_up_recipients.delivery_message` says "N drafts saved to
    <folder>"; nothing here may read like that."""
    from services.follow_up_recipients import DraftDelivery, delivery_message

    delivery = DraftDelivery()
    delivery.note_created(addressed=True, location="Drafts",
                          account="user@example.com")
    saved = delivery_message(delivery, "your Outlook Drafts folder")
    links = compose_message([_link()])

    # The saved-draft path makes the claim...
    assert re.search(r"\bdrafts? saved to\b", saved.lower())
    # ...and the compose-link path makes the opposite one. The only
    # "saved" here is the negation ("nothing has been saved") and the
    # conditional ("becomes a draft only once you save it").
    assert not re.search(r"\bdrafts? saved to\b", links.lower())
    assert "nothing has been saved to any mailbox" in links.lower()
    assert links != saved


def test_the_message_counts_unaddressed_and_truncated_separately():
    msg = compose_message([
        _link(addressed=True),
        _link(addressed=False),
        _link(addressed=True, truncated=True),
    ])

    assert "1 opens with an empty To:" in msg
    assert "1 message is" in msg and "shortened" in msg


def test_no_links_says_so_without_implying_a_draft():
    msg = compose_message([])

    assert "no compose links" in msg.lower()
    assert "no draft exists" in msg.lower()


def test_the_result_dict_carries_the_artifact_kind(tmp_path, monkeypatch):
    """The wire contract the UI branches on. `drafts_created` keeps its
    key for compatibility, but `artifact` is what says what those things
    actually are."""
    _set_calendar_source(monkeypatch, "extension")
    _sabotage_every_mail_client(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")

    payload = follow_up_email.draft_follow_up_emails(
        _make_svc(tmp_path), "s1").to_dict()

    assert payload["artifact"] == ARTIFACT_COMPOSE_LINK
    assert payload["location"] == "" and payload["account"] == ""
    assert len(payload["compose_links"]) == 2
    first = payload["compose_links"][0]
    assert set(first) >= {"owner", "display_name", "address", "subject",
                          "body", "url", "truncated", "addressed"}


def test_the_drafting_prompt_block_is_unchanged():
    """Only DELIVERY changed. The wording, the owners and the
    no-invented-precision guard all still come from the one place."""
    from services import _follow_up_email_outlook as win_mod
    from services import _follow_up_email_web as web_mod
    from services import follow_up_owners

    assert web_mod._compose_body is win_mod._compose_body
    assert web_mod.build_draft_plan is follow_up_owners.build_draft_plan
    # No second copy of the prompt: the web module defines no drafting
    # function of its own and never calls the model directly.
    tree = ast.parse(Path(web_mod.__file__).read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_compose_body" not in defined
    assert "no_invented_precision" not in defined
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_chat" not in called


# ── 6. The calendar feed is unaffected ───────────────────────────────
#
# Guard rail, not a feature test. Calendar display and auto-record were
# broken for weeks and only fixed across v2.30.0–v2.34.0; this change
# reads `calendar_source` and must not have altered what any value means
# for the feed. Both windows, all four values, spies on both fetchers.


def _meeting(subject: str, minutes_ahead: int) -> dict:
    start = datetime.now() + timedelta(minutes=minutes_ahead)
    return {"subject": subject, "start": start,
            "end": start + timedelta(minutes=30)}


class _Spy:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return list(self.events)


@pytest.mark.parametrize("window", ["upcoming", "today"])
@pytest.mark.parametrize(
    "source,expect_local_call,expect_extension_call,expect_subjects",
    [
        # "off" consults NEITHER source and returns nothing.
        ("off", False, False, set()),
        # "extension" must never make the local call — that call is what
        # re-triggers the tenant sign-in prompt — but still merges the
        # extension store.
        ("extension", False, True, {"Globex standup"}),
        # "outlook" and "auto" both call both fetchers today; the local
        # copy wins the dedup. Pinned as-is so a change is visible.
        ("outlook", True, True, {"Acme sync", "Globex standup"}),
        ("auto", True, True, {"Acme sync", "Globex standup"}),
    ],
)
def test_calendar_feed_is_unchanged_for_every_source(
        window, source, expect_local_call, expect_extension_call,
        expect_subjects):
    from services import calendar_feed

    local = _Spy([_meeting("Acme sync", 30)])
    extension = _Spy([_meeting("Globex standup", 60)])
    extension_svc = SimpleNamespace(get_events=extension)

    if window == "upcoming":
        got = asyncio.run(calendar_feed.merged_upcoming(
            168, source=source, fetch_local=local,
            extension_svc=extension_svc))
    else:
        got = asyncio.run(calendar_feed.merged_today(
            source=source, fetch_local=local, extension_svc=extension_svc))

    assert (local.calls > 0) is expect_local_call
    assert (extension.calls > 0) is expect_extension_call
    assert {m["subject"] for m in got} == expect_subjects


def test_drafting_does_not_import_or_patch_any_calendar_module():
    """The drafting router reads `calendar_source` and nothing else. If
    it ever grows a call into a calendar service, the Record and Today
    tabs are back in the blast radius."""
    from services import _follow_up_email_web as web_mod

    forbidden = ("calendar_service", "calendar_feed", "auto_record_service",
                 "extension_calendar_service", "_calendar_outlook",
                 "_calendar_eventkit")
    # Parsed, not grepped — the modules' docstrings legitimately explain
    # WHY the calendar paths are off limits, and a prose mention is not
    # a dependency.
    for mod in (follow_up_email, web_mod):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module or ''}.{a.name}"
                                for a in node.names)
        for name in forbidden:
            assert not any(name in imp for imp in imported), \
                f"{mod.__name__} imports {name}"
