"""`get_portal_binding` — the tool that makes the boundary crossable.

Agreed with the SA Tools Portal session (docs/mcp-tool-spec.md §3).
Without it, an assistant holding both servers' tools has to guess which
portal opportunity a recorder client corresponds to, and portal
opportunity *names* are neither unique nor stable — so guessing produces
confident wrong answers right before something gets filed against the
wrong opportunity. That has already happened once in this system.

The properties pinned here are the ones where being wrong is expensive:

  - a parent-company binding is FLAGGED, not silently treated as an
    opportunity. The portal is adding `isParentCompany` to the
    connection block precisely so this is detectable; a mis-pasted
    parent block in a per-project binding is the failure it prevents;
  - `tokenPresent` is about THIS machine, not about health — bindings
    roam between the user's laptops in the recordings dir, keychains do
    not, so a roamed binding with no local token is normal, not broken;
  - an unbound client says so explicitly rather than returning nothing;
  - a client with several bound projects returns the set.
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


async def test_exact_scope_returns_the_opportunity_identity():
    out = await srv.get_portal_binding(client="ACME", project="CCaaS Migration")
    assert "cus_acme_ccaas" in out
    assert "ACME CCaaS Migration" in out


async def test_the_opportunity_name_is_marked_a_label():
    out = await srv.get_portal_binding(client="ACME", project="CCaaS Migration")
    assert "label" in out.lower()


async def test_a_parent_company_binding_is_flagged_not_silently_accepted():
    """The whole reason the portal is adding isParentCompany. A parent
    block in a per-project binding means anything filed against this
    customerId goes to the account, not the engagement."""
    out = await srv.get_portal_binding(client="Umbrella", project="Rollout")
    assert "PARENT" in out.upper()
    assert "cus_umbrella_parent" in out


async def test_token_present_is_described_as_per_machine():
    """A roamed binding whose token is on the other laptop is normal.
    Rendering it as "broken" sent a previous version of this system
    chasing a non-problem."""
    out = await srv.get_portal_binding(client="ACME", project="Support Retainer")
    assert "this machine" in out.lower() or "this device" in out.lower()


async def test_unbound_client_says_so_explicitly():
    out = await srv.get_portal_binding(client="Nobody Ltd")
    assert "not bound" in out.lower()
    assert "MEETING RECORDER ERROR" not in out


async def test_client_with_several_projects_returns_the_set():
    out = await srv.get_portal_binding(client="ACME")
    assert "cus_acme_ccaas" in out
    assert "cus_acme_retainer" in out


async def test_backend_down_is_an_error_not_an_unbound_answer():
    """"Couldn't read it" must never render as "there is no binding" —
    they lead to opposite actions."""
    srv.set_client_factory(
        lambda: make_client(stub_backend.unreachable_transport()))
    out = await srv.get_portal_binding(client="ACME")
    assert "MEETING RECORDER ERROR" in out
    assert "not bound" not in out.lower()
