# Shared MCP tool spec — recorder ⇄ SA Tools Portal

**Status:** agreed 2026-08-28 between the two Claude Code sessions building
these systems (see `docs/agent-channel/`). This file exists in **both**
repos, each enforcing its own half with a test. A naming document with
nothing enforcing it is exactly how the two surfaces diverged in the
first place.

Both servers are meant to be mounted **at the same time**, in one
assistant. Everything here follows from that.

---

## 1. Namespacing

| Server | Prefix |
| --- | --- |
| Meeting Recorder | none (unprefixed — shipped first) |
| SA Tools Portal | `portal_` |

The portal prefixes because the recorder's names were already shipped and
configured. A portal tool must never take a name in the recorder's list
below.

## 2. Vocabulary

| Concept | Recorder | Portal | Notes |
| --- | --- | --- | --- |
| the company | `client` | parent company (`parentCustomerId`, `isParentCompany`) | Same thing. The recorder keeps `client`: it is persisted across session JSONs, the REST API, the register, the exports and the UI, and renaming it would be a migration whose only payoff is matching a noun. Interop depends on the ID, not the noun. |
| the engagement | `project` | opportunity record (its own `customerId`) | 1:1 — a recorder *project* maps onto exactly one portal opportunity record. |
| a recorded meeting | `meeting` | — | Was `session`; renamed because "session" also means a Claude Code session, and an ambiguous tool name is one a model gets right only sometimes. |

Because the nouns differ, every recorder return that names a client
states the equivalence in the text the model reads:

```
client/customer: Globex (customerId: 7f3e…, opportunityName: 'Globex Genesys Migration' (label, not a key))
```

## 3. The join

**`customerId` is the portal's PER-OPPORTUNITY primary key** — a UUID
minted per opportunity record (`lambda-api/index.js:1447`) and used as
the DynamoDB partition key. It is *not* a company identifier. The
company level is `parentCustomerId` / `isParentCompany`.

That makes `customerId` the single cross-system key. It is 1:1 with a
recorder project, and it is a UUID rather than an editable label.

The recorder binds per **(client, project)** pair — `scope_key()` in
`backend/services/portal_push_service.py` — and stores the connection
block's `customerId` **verbatim apart from `.strip()`**: no re-casing,
no normalisation, nothing derived. A join on it cannot silently miss.

Rules:

1. **`customerId` is the only cross-reference.** Both sides store and
   echo it verbatim; neither invents an identifier.
2. **Never emit a field named `opportunity`.** The portal already has
   `opportunityId`, which is user-typed CRM free text — optional, often
   blank, not unique, not validated, not a key to anything
   (`index.js:2746`). `opportunity` and `opportunityId` read as
   synonyms and are not, which makes the collision worse than
   `client`/`customer`.
3. **`opportunityName` is a label, never a key.** Portal opportunity
   names are neither unique nor stable. Where a return carries one it is
   marked as a label inline.
4. **A meeting or commitment resolves exactly**, because it carries
   client AND project.
5. **A client normally does not, and that is not a fault.** Since
   `customerId` is per-opportunity, a client with several bound projects
   legitimately has several different `customerId`s. A client-level
   return therefore reports the *set* — each project with its own
   `customerId` — and never collapses it to one value. A single bound
   project resolves; none is an explicit `null`, because "not bound" is
   information.

### Known gap: the recorder cannot tell a parent from an opportunity

The connection block carries `{portal, api, opportunity, customerId,
editToken}` — no `isParentCompany` and no `parentCustomerId`. The
recorder stores a `parentName` **label** but no parent ID. So if an SA
pastes a parent-company block, its ID lands in the `customerId` slot and
nothing on the recorder side can detect it.

Closing this needs the portal to add `isParentCompany` (and
`parentCustomerId` where applicable) to the connection block; the
recorder can then record it, and refuse or warn at bind time. Until
then, treat a recorder-supplied `customerId` as *probably* an
opportunity, and validate portal-side before writing anything against it.

## 4. Tool naming

`verb_noun`, both servers, no exceptions.

### Recorder surface (this repo)

| Tool | Carries identifiers? |
| --- | --- |
| `search_meetings` | per-hit, where the hit is a meeting |
| `ask_knowledge_base` | in cited sources |
| `list_open_commitments` | yes — exact |
| `list_clients` | yes — set-valued per §3.5 (a client may hold several) |
| `list_meetings` | yes — exact |
| `get_meeting` | yes — exact |

The identifier *parameter* remains `session_id`, not `meeting_id`: it is
the key in `/sessions/{id}`, in every stored session JSON, and in the
exports. The tool name removes the model-facing ambiguity; renaming the
field would ripple through the backend and buy nothing further. So the
surface reads `get_meeting(session_id=…)` — mildly awkward, deliberate.

### Portal surface (sa-portal)

Named by that session; recorded here so both repos can see one list.
Known at time of writing: `portal_list_opportunities`,
`portal_get_opportunity`, `portal_list_artifacts`, `portal_get_customer`,
`list_assumptions` (renamed from `assumptions` to satisfy verb_noun).

## 5. Read-only

Every tool on both surfaces is annotated read-only. Write tools are
legitimate and probably wanted later, but ship separately, named
clearly, and annotated as non-read-only — the moment one tool in a set
can write, cautious users treat the whole set as dangerous.

## 6. Errors

Both servers hand their output to a language model, so an error is text
the model reasons about.

```
MEETING RECORDER ERROR — the app isn't running. Start Meeting Recorder and try again.
SA PORTAL ERROR — …
```

Prefix, plain cause, the action that fixes it. And the rule this
codebase has re-learned most often:

> **An empty result must say it is empty**, and must be distinguishable
> from a failure to read. "No open commitments for Globex" and a blank
> response look identical to a model, and one of them makes it invent an
> answer.

## 7. Enforcement

Each repo has a test asserting **its own half** of this file:

- recorder: `mcp-server/tests/test_tool_spec.py` — parses this document,
  asserts the registered tool names match §4 exactly (no extras, none
  missing), and that the identifier fields in §3 are emitted.
- portal: the equivalent on its side.

Neither test asserts the other repo's half — neither can see it.
