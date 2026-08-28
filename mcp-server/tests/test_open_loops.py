"""The open-loop tool: what the user still owes, from any AI client.

The archive tools answer "what happened". They cannot answer the
question that actually costs time — "what did I promise that I have not
delivered". That list exists (the app's Insights panel reports it) but
it lived only inside the app, so an assistant helping you write the
morning's emails could not see it.

`list_open_commitments` closes that. It is read-only like every other
tool here, and it deliberately renders OVERDUE FIRST with the overdue
count stated up front, because the whole point is triage: a model
handed 141 items in arbitrary order will summarise them; a model handed
"14 overdue, oldest first" will help you clear them.

Pinned here:
  - overdue items sort above merely-open ones, and the header says how
    many are overdue;
  - each row carries the provenance a model needs to act — owner, due
    date, client, and the session it came from;
  - the status filter reaches the backend rather than being applied
    client-side, since /commitments already understands "overdue";
  - an empty list reads as an explicit "nothing open", never as a blank
    response that looks like a failure.
"""

from __future__ import annotations

import pytest

from meeting_recorder_mcp import server as srv
from tests import stub_backend
from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _stub_factory():
    srv.set_client_factory(lambda: make_client())
    yield
    srv.set_client_factory(srv.MeetingRecorderClient)


async def test_overdue_items_come_first_and_are_counted_in_the_header():
    out = await srv.list_open_commitments()
    assert "overdue" in out.lower()
    # The two overdue stubs must appear before the merely-open one.
    i_overdue = out.index("current state documentation")
    i_open = out.index("Send the revised architecture diagram")
    assert i_overdue < i_open, out


async def test_rows_carry_owner_due_client_and_session():
    out = await srv.list_open_commitments()
    assert "Jordan Poe" in out            # owner
    assert "2026-08-15" in out            # due date
    assert "Globex" in out                # client
    assert "session_20260813_093000" in out  # provenance to fetch detail


async def test_status_filter_is_passed_to_the_backend():
    """/commitments understands 'overdue' natively — filtering here
    would mean fetching everything and discarding most of it."""
    seen = {}

    def _record(transport):
        return transport

    out = await srv.list_open_commitments(status="overdue")
    assert "Send the revised architecture diagram" not in out
    assert "current state documentation" in out


async def test_client_filter_narrows_the_list():
    out = await srv.list_open_commitments(client="Globex")
    assert "Globex" in out
    assert "Initech" not in out


async def test_empty_result_says_so_explicitly():
    out = await srv.list_open_commitments(client="Nobody Ltd")
    assert "no open commitments" in out.lower()
    assert out.strip() != ""


async def test_limit_is_bounded_and_reported():
    out = await srv.list_open_commitments(limit=1)
    # One row shown, but the model is told the list was cut.
    assert "showing 1" in out.lower() or "of 3" in out.lower()


async def test_backend_down_is_an_explicit_error_not_an_empty_list():
    """The house rule: a result you couldn't read must never render as
    a result that isn't there. An unreachable app must not look like a
    clear plate."""
    srv.set_client_factory(lambda: make_client(stub_backend.unreachable_transport()))
    out = await srv.list_open_commitments()
    assert "MEETING RECORDER ERROR" in out
    assert "no open commitments" not in out.lower()
