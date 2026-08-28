# Contract for the SA Tools Portal MCP server

**Audience:** whoever is building the portal's AI integration.
**Purpose:** so the portal's tools and Meeting Recorder's tools compose
into one coherent surface instead of two overlapping ones.

The prize is a question neither tool can answer alone:

> "What did I commit to on the Globex migration, what's the opportunity
> state, and what's still outstanding?"

That requires one assistant holding both tool sets at once. It only
works if they were designed to sit side by side — which is what this
document is for. It states what the recorder side already does, and the
decisions the portal side should make deliberately rather than by
copying.

---

## 1. Namespace the tools

An assistant sees one flat list of tool names. `list_clients` from two
servers is ambiguous to a model and to a human reading a transcript.

The recorder's tools are **unprefixed** and already shipped:
`search_meetings`, `ask_knowledge_base`, `list_open_commitments`,
`list_clients`, `list_sessions`, `get_session`.

**So the portal should prefix: `portal_*`.** `portal_get_opportunity`,
`portal_list_opportunities`, `portal_get_customer`. This costs nothing
and removes the whole class of collision — including future ones, since
the recorder will keep adding unprefixed tools in its own namespace.

Do not name a portal tool `list_clients`, `list_sessions`, or anything
else in the list above.

---

## 2. Transport and auth are NOT the same as the recorder's

The recorder runs **stdio on localhost** because the app is on the
user's machine and owns a local token file. Copying that shape would be
wrong for a hosted portal. Decide these explicitly:

| Concern | Recorder (for reference) | Portal (decide) |
| --- | --- | --- |
| Transport | stdio, spawned by the client | stdio via a thin local shim, **or** streamable HTTP |
| Reachability | localhost only; app must be running | already remote — no tunnel needed |
| Auth | 64-hex token from a local file | the portal's own auth — see below |
| Multi-user | single user by construction | **many users; this is the real difference** |

The recorder can be relaxed about auth because it never leaves the
laptop. The portal is shared infrastructure: whatever it exposes, it
exposes to whoever holds the credential, and the tools must scope to the
caller's own opportunities. That's a real access-control design, not a
token check.

If in doubt, start with a **local stdio shim that holds the user's
portal credential and calls the portal's HTTPS API**. It reuses the
recorder's proven client shape, keeps secrets on the user's machine, and
defers hosting an MCP endpoint until it's actually needed.

---

## 3. Match on identity, not on strings

The recorder's `client` field is a free-text name the user typed
("Globex", "Globex Corp", "globex"). Portal opportunities have real IDs.
Joining these on display name will produce confident wrong answers.

The binding already exists and should be the join key: the connection
block the portal hands out —
`{portal, api, opportunity, customerId, editToken}` — is stored by the
recorder per client/project. So:

- **Portal tools should accept and return `opportunity` and
  `customerId`**, never rely on a display name as the key.
- **The recorder should surface its binding** so an assistant can go
  from "the Globex meetings" to "opportunity `abc123`" without guessing.
  If that's missing when you get there, ask — it's a small addition on
  the recorder side and belongs there, not in a portal heuristic.

The rule: a tool may *accept* a friendly name for lookup, but every
cross-tool reference travels as an ID.

---

## 4. Read-only first, and say so in the annotations

The recorder's tools are all `readOnlyHint: true`, which lets a client
show them as safe and lets a user auto-approve them. That property is
worth keeping across both servers, because the moment one tool can
write, the whole set gets treated as dangerous by cautious users.

Write tools (update an opportunity, upload an artifact) are legitimate
and probably wanted — but ship them **separately, named clearly, and
annotated as non-read-only**, after the read side is proven. A tool that
silently mutates a customer record because a model misread a prompt is
the failure mode that ends the experiment.

---

## 5. Errors must be legible to a model

Both servers hand their output to a language model, so an error is text
the model will reason about. The recorder's convention:

```
MEETING RECORDER ERROR — the app isn't running. Start Meeting Recorder
and try again.
```

Prefix, plain cause, and the action that fixes it. Use
`SA PORTAL ERROR — ...` and the same structure. Specifically:

- **An empty result must say it is empty**, never return an empty
  string. "No open opportunities for Globex" and a blank response look
  identical to a model, and one of them makes it invent an answer.
- **Distinguish "couldn't reach it" from "there's nothing there."**
  This is the single defect this codebase has re-learned most often.

---

## 6. Suggested first tool set

Enough to be useful, small enough to review:

| Tool | Why it earns a slot |
| --- | --- |
| `portal_list_opportunities(customer?, stage?, mine_only?)` | the "what am I working on" question |
| `portal_get_opportunity(opportunity)` | full state for one — stage, value, dates, owner |
| `portal_list_artifacts(opportunity)` | what has already been delivered, so an assistant doesn't re-offer it |
| `portal_get_customer(customerId)` | the account context behind an opportunity |

Deliberately **not** in a first cut: anything that writes, anything that
enumerates every customer in the tenant, and anything returning
unbounded document bodies (paginate and truncate — see the recorder's
`truncate` usage; a model that gets 400KB of text loses the plot).

---

## 7. Test it the way the recorder does

`mcp-server/tests/` is worth reading before you start. The shape that
has held up:

- a **stub backend** over `httpx.MockTransport` — every tool exercised
  against real HTTP without a live server;
- assertions on **the text a model actually sees**, not on internal
  return values, because the rendering *is* the product;
- a **real stdio handshake check** (`scripts/handshake_check.py`) so
  protocol breakage is caught separately from tool logic.

One caution from experience: as of 2026-08-28 that suite was running in
no CI job at all, and a genuinely failing test sat unnoticed on main.
**Wire the portal's MCP tests into its CI from the first commit.**

---

## Open questions for the two of us

1. Does the portal already have an API the MCP server can sit on, or
   does that need building too? (The recorder's MCP server is a thin
   adapter over an API that already existed — that's why it was cheap.)
2. Who is the audience — just you, or the whole SA team? That decides
   multi-user auth on day one versus later.
3. Should the recorder gain a `get_portal_binding` tool so an assistant
   can cross the boundary without guessing at names? I think yes, and
   it's small.
