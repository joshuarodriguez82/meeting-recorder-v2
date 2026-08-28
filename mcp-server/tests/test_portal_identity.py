"""Portal identity on the tool surface — the cross-system join.

Agreed with the SA Tools Portal session (docs/agent-channel/,
docs/mcp-tool-spec.md §3). With both servers mounted in one assistant,
Claude could previously list this app's clients and the portal's
opportunities with **no way to connect them**: the backend knew the
binding and the tool surface threw it away.

The correction that shaped this: the recorder binds per **(client,
project)** pair, not per client, and each binding carries `customerId`
AND `opportunity`. So a meeting or a commitment resolves exactly, while
a *client* may span several bound projects.

The rule that matters most is what happens when it cannot resolve
cleanly. One client holding two projects bound to two DIFFERENT portal
customers is a real state — a mis-paste at bind time produces it — and
picking one would hand an assistant a confident wrong answer right
before it files something against an opportunity. So it reports the
ambiguity instead.

Three distinct outcomes, never collapsed:
  - resolved  → customerId (+ opportunity where singular)
  - unbound   → explicit null, because "not bound" is information
  - ambiguous → named as ambiguous, with the count
"""

from __future__ import annotations

import re

import pytest

from meeting_recorder_mcp import server as srv
from meeting_recorder_mcp.formatting import portal_id_line, portal_ids_for
from tests import stub_backend
from tests.conftest import make_client

BINDINGS = stub_backend.PORTAL_BINDINGS


@pytest.fixture(autouse=True)
def _stub_factory():
    srv.set_client_factory(lambda: make_client())
    yield
    srv.set_client_factory(srv.MeetingRecorderClient)


# ── the resolver ────────────────────────────────────────────────────

def test_exact_scope_resolves_the_opportunitys_customer_id():
    ids = portal_ids_for(BINDINGS, "ACME", "CCaaS Migration")
    assert ids["customerId"] == "cus_acme_ccaas"
    assert ids["opportunityName"] == "ACME CCaaS Migration"


def test_client_level_with_several_opportunities_resolves_to_none_but_lists_them():
    """THE one that matters, and the one I had inverted.

    customerId is per-OPPORTUNITY, so a client with two bound projects
    has two different customerIds. That is normal, not a fault. There is
    no single right answer at client level, so return none and list the
    options rather than picking."""
    ids = portal_ids_for(BINDINGS, "ACME")
    assert ids["customerId"] is None
    assert {b["customerId"] for b in ids["bound"]} == {
        "cus_acme_ccaas", "cus_acme_retainer"}


def test_single_bound_project_resolves_at_client_level():
    ids = portal_ids_for(BINDINGS, "Initech")
    assert ids["customerId"] == "cus_initech_rollout"


def test_unbound_client_is_explicit_null():
    ids = portal_ids_for(BINDINGS, "Nobody Ltd")
    assert ids["customerId"] is None
    assert ids["bound"] == []


def test_scope_slug_matches_the_backends_rule():
    """The slug must agree with scope_key() in
    backend/services/portal_push_service.py, or a binding silently never
    matches its meetings and every identifier comes back null.

    The two are NOT written the same way and that is the risk: the
    backend joins first and slugs the whole string, while this package
    slugs each part and joins — it cannot import the backend, which is
    deliberate isolation (mcp-server ships standalone). They agree only
    because `_` is itself an allowed character, so the `__` separator
    survives slugging from either direction.

    So rather than hardcode a key and prove nothing, this recomputes the
    expected key the BACKEND's way and asserts the resolver finds it.
    Awkward inputs included, because those are where the two rules would
    diverge if anyone edited one of them.
    """
    def backend_scope_key(client: str, project: str) -> str:
        raw = f"{client}__{project}" if project else client
        return re.sub(r"[^A-Za-z0-9._-]+", "_", raw.strip().lower())

    for client, project in [
        ("ACME", "CCaaS Migration"),
        ("Globex Corp!", "Genesys  Migration"),
        ("Zorg & Co.", "Phase 1 — Discovery"),
        ("A!", "B"),
        ("A", "!B"),
    ]:
        bindings = {backend_scope_key(client, project): {
            "customer_id": "cus_x", "opportunity_name": "opp_x"}}
        ids = portal_ids_for(bindings, client, project)
        assert ids["customerId"] == "cus_x", (
            f"the scope slug diverged from the backend's for "
            f"{client!r}/{project!r} — bindings will never match")


# ── the rendered line a model reads ─────────────────────────────────

def test_the_line_states_the_client_customer_equivalence():
    """A model holding both servers' tools must not have to infer that a
    recorder `client` and a portal `customer` are the same company."""
    line = portal_id_line(portal_ids_for(BINDINGS, "ACME", "CCaaS Migration"),
                          "ACME")
    assert "client/customer: ACME" in line
    assert "customerId: cus_acme_ccaas" in line


def test_the_opportunity_name_is_marked_as_a_label_not_a_key():
    """Portal opportunity names are neither unique nor stable, and the
    portal's own `opportunityId` is unrelated CRM free text. Nothing
    must join on either, so the surface says so in the text."""
    line = portal_id_line(portal_ids_for(BINDINGS, "ACME", "CCaaS Migration"),
                          "ACME")
    assert "opportunityName:" in line
    assert "label, not a key" in line


def test_no_field_named_bare_opportunity_is_emitted():
    """Agreed with the portal session: `opportunity` would collide with
    their `opportunityId` (user-typed CRM text). Both names read as
    synonyms and are not."""
    ids = portal_ids_for(BINDINGS, "ACME", "CCaaS Migration")
    assert "opportunity" not in ids
    line = portal_id_line(ids, "ACME")
    assert "opportunity:" not in line


def test_multiple_opportunities_are_listed_with_how_to_pick_one():
    line = portal_id_line(portal_ids_for(BINDINGS, "ACME"), "ACME")
    assert "2 bound opportunities" in line
    assert "pass a project to resolve one" in line
    assert "cus_acme_ccaas" in line


def test_unbound_says_null_and_why():
    line = portal_id_line(portal_ids_for(BINDINGS, "Nobody"), "Nobody")
    assert "null" in line
    assert "not bound" in line


# ── through the actual tool ─────────────────────────────────────────

async def test_list_clients_emits_portal_identity():
    out = await srv.list_clients()
    assert "cus_acme_ccaas" in out


async def test_a_portal_outage_does_not_break_the_client_listing():
    """Binding is an optional feature and the portal layer may be
    absent. Losing the identity line must degrade that one line — not
    fail the tool the user actually asked for."""
    class _NoPortal:
        def __getattr__(self, name):
            real = getattr(make_client(), name)
            if name == "portal_bindings":
                async def _boom(*a, **k):
                    raise RuntimeError("portal layer unavailable")
                return _boom
            return real

    srv.set_client_factory(_NoPortal)
    out = await srv.list_clients()
    assert "client(s) configured" in out
    assert "MEETING RECORDER ERROR" not in out
