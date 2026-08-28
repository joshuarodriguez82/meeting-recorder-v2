"""The tool surface must match the agreed spec — enforced, not asserted.

`docs/mcp-tool-spec.md` is a contract negotiated with the session
building the SA Tools Portal, because the two servers are meant to be
mounted in one assistant at the same time. The two surfaces had already
drifted apart once without either side noticing, which is the whole
reason the spec exists.

A naming document nothing enforces is how that drift happened. So this
test reads the spec file itself and holds the code to it: the registered
tool names must match §4 exactly — no extras, none missing — and the
join identifiers from §3 must actually be emitted.

If you are renaming a tool or adding one, the spec is the thing to edit
first; this test is what tells you the other repo needs to hear about it.
"""

from __future__ import annotations

import re
from pathlib import Path

from meeting_recorder_mcp import server as srv

SPEC = Path(__file__).resolve().parents[2] / "docs" / "mcp-tool-spec.md"


def _spec_text() -> str:
    assert SPEC.exists(), f"the agreed spec is missing: {SPEC}"
    return SPEC.read_text(encoding="utf-8")


def _spec_tool_names() -> set:
    """Tool names from the recorder table in §4.

    Read from the backticked first column between the recorder heading
    and the portal heading, so the portal's table cannot leak in.
    """
    text = _spec_text()
    start = text.index("### Recorder surface")
    end = text.index("### Portal surface")
    section = text[start:end]
    return set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", section, re.M))


def _registered_tool_names() -> set:
    """Every tool actually registered on the MCP server.

    Read off the decorators in the source rather than the SDK's private
    registry, so this keeps working across SDK versions — the spec is
    about the names we publish, and the decorator is where we publish
    them.
    """
    src = (Path(srv.__file__)).read_text(encoding="utf-8")
    return set(re.findall(r'@server\.tool\(\s*\n\s*name="([a-z_]+)"', src))


def test_the_spec_file_exists_and_is_parseable():
    """Guard the guard: if the parsing rots, every assertion below
    passes vacuously and this file silently stops testing anything."""
    names = _spec_tool_names()
    assert len(names) >= 5, f"parsed too few tool names from the spec: {names}"
    assert "get_meeting" in names


def test_registered_tools_match_the_spec_exactly():
    spec = _spec_tool_names()
    live = _registered_tool_names()
    assert live == spec, (
        "the MCP tool surface has drifted from docs/mcp-tool-spec.md — "
        "the portal session builds against that file, so update it and "
        "tell them.\n"
        f"  registered but not in the spec: {sorted(live - spec) or 'none'}\n"
        f"  in the spec but not registered: {sorted(spec - live) or 'none'}")


def test_no_tool_reuses_the_overloaded_session_noun():
    """`session` also means a Claude Code session; a tool named for it is
    one a model gets right only sometimes. Agreed rename, §2."""
    live = _registered_tool_names()
    offenders = [n for n in live if "session" in n]
    assert not offenders, (
        f"these tool names still use the overloaded noun 'session': "
        f"{offenders}. Meetings are 'meeting' on this surface.")


def test_portal_prefixed_names_are_not_claimed_here():
    """The portal owns `portal_*`. Taking one from this side would
    collide in an assistant with both servers mounted."""
    live = _registered_tool_names()
    assert not [n for n in live if n.startswith("portal_")], live


def test_every_tool_is_verb_noun():
    """§4. `assumptions` on the portal side was the violation that
    prompted the rule; this end must not add one."""
    verbs = ("list", "get", "search", "ask", "find", "create", "update")
    bad = [n for n in _registered_tool_names()
           if not n.startswith(tuple(v + "_" for v in verbs))]
    assert not bad, f"tool names that are not verb_noun: {bad}"


def test_the_join_identifiers_are_named_in_the_spec():
    """The identifiers are the portal's, echoed verbatim. If these
    spellings change, both repos have to move together."""
    text = _spec_text()
    assert "customerId" in text
    assert "opportunityName" in text
    assert "parentCustomerId" in text, (
        "the spec must record that the company level is parentCustomerId, "
        "not customerId — getting this backwards is the error that cost a "
        "round trip with the portal session")
    assert "No MCP *tool* emits a field named `opportunity`" in text, (
        "the spec must keep the `opportunity` vs `opportunityId` collision "
        "warning — both read as synonyms and are not")
    assert "connection block is a different contract" in text, (
        "the spec must keep wire format and tool surface separate: the "
        "block's `opportunity` key is frozen by deployment, the tool "
        "surface is free to be correct. Conflating them either breaks "
        "installed bindings or re-imports the collision")
    assert "session_id" in text, (
        "the spec must keep stating that the identifier parameter stays "
        "session_id even though the tool is get_meeting")
