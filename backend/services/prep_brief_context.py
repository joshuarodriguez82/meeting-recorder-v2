"""
Knowledge-Folder retrieval for the pre-meeting prep brief.

Until now a prep brief read *only* past call summaries: the two
prep-brief endpoints filtered sessions by client, took the 8 most
recent, and handed those to the LLM. Meanwhile the app already indexes
a per-client Knowledge Folder (SOWs, requirements docs, discovery
notes) into the same embedding space as transcript chunks — one real
client has 114 documents / 1171 chunks — and none of it reached the
brief. So the brief could tell you what was *said* about the October
cutover and nothing about what was *contracted*.

This module is the retrieval half of closing that gap. It is
retrieval-only by design: it never indexes, re-indexes, walks the
Knowledge Folder on disk, or touches the source files. The brief is
generated while the user waits (and sometimes automatically ahead of a
meeting), so the only work done here is embedding a couple of short
query strings and a dot product against the already-loaded search
matrix.


Query construction
------------------
1171 chunks cannot go in the prompt, so the query decides what the
brief sees. We build up to three *separate* probes — one per distinct
signal we know about the upcoming meeting — and merge their ranked
results round-robin:

  1. TOPIC    — "<subject> — <project>". The strongest and always-present
                signal; what this meeting is nominally about.
  2. AGENDA   — the invite body, boilerplate-stripped and head-truncated.
                Frequently names the specific thing at stake ("review
                the migration cutover plan") that a generic subject
                ("Weekly Sync") does not.
  3. USER     — the free-text context the SA typed in. Authoritative and
                highly specific ("procurement flagged the SLA section").

Separate probes rather than one concatenated string because these are
heterogeneous signals: blending a 600-char agenda into a 40-char
subject produces a vector dominated by the agenda's word count, and a
short authoritative user note gets averaged into nothing. Round-robin
over per-probe rank (rather than a merged similarity sort) is
scale-free — cosine scores from queries of very different lengths are
not directly comparable — and guarantees each signal that exists gets
representation instead of one signal monopolising every slot.

Attendees are deliberately NOT part of the query, even though we know
them. Personal names embedded against contract/requirements prose
retrieve RACI tables, approver lists and signature blocks — the parts
of a SOW that mention people — rather than the scope, dates and
obligations a prep brief needs. They would also dilute the topical
signal. Attendees already reach the model: the prompt lists them
verbatim under "Attendees:", and session-side context is person-rich.


Budget
------
The existing cap of 8 sessions is untouched. Recent calls are the spine
of a prep brief and documents must not crowd them out, so documents get
their own separate, smaller allowance rather than a share of one pool:

  sessions   : MAX_CONTEXT_SESSIONS = 8            (unchanged)
  documents  : MAX_DOCUMENT_CHUNKS = 6 chunks,
               MAX_DOCUMENT_CONTEXT_CHARS = 8000 chars total,
               MAX_CHUNKS_PER_DOCUMENT = 2

Document chunks are ~350 words (~2.3 KB) each, so the char budget
usually binds first and admits about three whole chunks — measured:
+8.3 KB of prompt, ~2,070 input tokens, a 2.1x prompt against a
full 8-session brief. Documents therefore get roughly the same
allowance as the entire meeting history, while sessions keep their
guaranteed 8 and stay at the head of the context where the model
attends most. MAX_DOCUMENT_CHUNKS binds instead when the folder is
full of short notes rather than long contracts.

MAX_CHUNKS_PER_DOCUMENT is load-bearing: without it a single long SOW
that matches the query well takes every slot and the brief sees one
document instead of the folder. The budget is also filled
breadth-first — one chunk from each distinct document before any
document gets a second — because with only ~3 slots available, three
different documents say more about an account than two paragraphs of
one.


Degradation
-----------
Every failure mode here has exactly one outcome: zero document hits,
and a brief identical to the one generated before this module existed.
No error, no "no documents found" line in the output, no partial
context. That covers: no client resolved for the meeting, no Knowledge
Folder configured, an empty or stale index, a disconnected Drive whose
sidecars can't be read, sentence-transformers not installed, and any
unexpected exception out of SearchService.
"""

from __future__ import annotations

import re
from typing import List, Sequence

from utils.logger import get_logger

logger = get_logger(__name__)


# ── Budget ──────────────────────────────────────────────────────────
# Named rather than inline so the split is reviewable in one place and
# a future change to one side is an obvious trade against the other.

#: Prior sessions fed to the brief. Unchanged from the pre-documents
#: behaviour — documents were added *beside* this, never out of it.
MAX_CONTEXT_SESSIONS = 8

#: Sessions used by /prep-brief/from-meeting's corpus-wide fallback,
#: when nothing at all is scoped to this client/project. Smaller than
#: MAX_CONTEXT_SESSIONS on purpose: unscoped material is less likely to
#: be relevant, so it gets less of the budget. Unchanged value, named.
MAX_FALLBACK_SESSIONS = 5

#: Hard cap on document chunks in the prompt.
MAX_DOCUMENT_CHUNKS = 6

#: Total characters of document text. Binds before MAX_DOCUMENT_CHUNKS
#: for typical ~350-word chunks; both are enforced.
MAX_DOCUMENT_CONTEXT_CHARS = 8000

#: Per-document chunk cap, so one well-matching SOW can't own the whole
#: document budget and hide the rest of the folder.
MAX_CHUNKS_PER_DOCUMENT = 2

#: Defensive cap on a single pathologically large chunk.
MAX_SINGLE_CHUNK_CHARS = 2500

#: Cosine floor. all-MiniLM-L6-v2 puts genuinely unrelated text around
#: 0.0-0.15 and topically-related prose around 0.3-0.5, so 0.25 keeps
#: "this document has nothing to do with the meeting" material out
#: rather than padding the budget with the least-bad chunks available.
MIN_DOCUMENT_SIMILARITY = 0.25

#: Over-fetch per probe so the per-document cap and the relevance floor
#: have alternatives to fall back on. Cheap — one dot product.
DOCUMENT_RETRIEVAL_TOP_K = 24

#: Query-side truncation. Embedding models attend to a bounded window;
#: past a few hundred characters extra text only dilutes the vector.
MAX_AGENDA_QUERY_CHARS = 600
MAX_USER_CONTEXT_QUERY_CHARS = 400


# Invite boilerplate: join links, dial-ins, separator rules, legal
# footers. Dropped from the agenda probe because it is identical across
# every meeting in the corpus and carries no topical signal.
_BOILERPLATE_LINE_RE = re.compile(
    r"(?i)("
    r"join\s+(zoom|the\s+meeting|microsoft\s+teams)"
    r"|microsoft\s+teams\s+(meeting|need\s+help)"
    r"|meeting\s+(id|link|options)\b"
    r"|(dial|call)\s*-?\s*in\b"
    r"|passcode\b|password\b"
    r"|one\s*tap\s*mobile"
    r"|find\s+a\s+local\s+number"
    r"|^\s*[_\-=*]{6,}\s*$"
    r"|learn\s+more\b.*\bmeeting\s+options"
    r")"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_WS_RE = re.compile(r"\s+")


def _clean_for_query(text: str, limit: int) -> str:
    """Strip invite boilerplate + URLs, collapse whitespace, head-truncate.

    Head rather than tail: agendas put substance first and boilerplate
    (join links, legal footers) last.
    """
    if not text:
        return ""
    kept: List[str] = []
    for line in text.splitlines():
        if _BOILERPLATE_LINE_RE.search(line):
            continue
        line = _URL_RE.sub(" ", line)
        line = line.strip()
        if line:
            kept.append(line)
    blob = _WS_RE.sub(" ", " ".join(kept)).strip()
    return blob[:limit].strip()


def build_document_queries(
    subject: str = "",
    project: str = "",
    agenda: str = "",
    user_context: str = "",
) -> List[str]:
    """Up to three retrieval probes for the upcoming meeting.

    See the module docstring for why these are separate probes and why
    attendees are deliberately excluded. Returns [] when nothing
    useful is known, which callers treat as "skip retrieval".
    """
    probes: List[str] = []

    topic = " — ".join(p for p in ((subject or "").strip(),
                                   (project or "").strip()) if p)
    if topic:
        probes.append(topic)

    agenda_probe = _clean_for_query(agenda, MAX_AGENDA_QUERY_CHARS)
    if agenda_probe:
        probes.append(agenda_probe)

    user_probe = _clean_for_query(user_context, MAX_USER_CONTEXT_QUERY_CHARS)
    if user_probe:
        probes.append(user_probe)

    # A probe that duplicates an earlier one wastes a round-robin lane.
    deduped: List[str] = []
    seen = set()
    for p in probes:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        deduped.append(p)
    return deduped


def _doc_key(hit: dict) -> str:
    """Identity of the source document for per-document capping."""
    return (hit.get("doc_path") or "").strip() or (hit.get("doc_name") or "")


def retrieve_client_documents(
    search_service,
    client: str,
    queries: Sequence[str],
    max_chunks: int = MAX_DOCUMENT_CHUNKS,
    max_chars: int = MAX_DOCUMENT_CONTEXT_CHARS,
) -> List[dict]:
    """Knowledge-Folder chunks for `client`, ranked and budgeted.

    Returns [] — never raises, never a partial error state — when the
    client is unknown, no Knowledge Folder is indexed for them, the
    index is empty/stale/unreadable, embeddings aren't available, or
    anything at all goes wrong inside SearchService. That is the whole
    contract: the brief is exactly as good as it was before, never
    worse.

    Note `project` is deliberately never passed to SearchService.
    Documents carry a client but no project, and SearchService excludes
    every document from a project-filtered query by design — passing a
    project through here would silently return nothing.
    """
    client = (client or "").strip()
    if search_service is None or not client or not queries:
        return []

    ranked_lists: List[List[dict]] = []
    for query in queries:
        try:
            hits = search_service.search(
                query=query,
                top_k=DOCUMENT_RETRIEVAL_TOP_K,
                client=client,
            ) or []
        except Exception as e:
            # Unreachable Drive, unreadable sidecar, missing embedding
            # model, anything. One dead probe must not kill the brief.
            logger.warning(
                f"prep-brief document retrieval failed for client "
                f"{client!r}: {e}")
            continue
        ranked_lists.append([
            h for h in hits
            if h.get("source") == "document"
            and float(h.get("similarity") or 0.0) >= MIN_DOCUMENT_SIMILARITY
            and (h.get("text") or "").strip()
        ])

    if not ranked_lists:
        return []

    # Merge the probes round-robin over per-probe RANK: scale-free
    # across probes of very different lengths (cosine scores from a
    # 40-char subject and a 600-char agenda are not comparable), and
    # every signal that produced hits is represented before any signal
    # gets a second slot.
    candidates: List[dict] = []
    seen_chunks = set()
    max_depth = max(len(lst) for lst in ranked_lists)
    for depth in range(max_depth):
        for lst in ranked_lists:
            if depth >= len(lst):
                continue
            hit = lst[depth]
            text = (hit.get("text") or "").strip()
            if len(text) > MAX_SINGLE_CHUNK_CHARS:
                text = text[:MAX_SINGLE_CHUNK_CHARS].rstrip() + " …(truncated)"
            key = _doc_key(hit)
            chunk_key = (key, text[:200])
            if chunk_key in seen_chunks:
                continue
            seen_chunks.add(chunk_key)
            candidates.append({
                "doc_name": hit.get("doc_name") or "document",
                "doc_path": hit.get("doc_path") or "",
                "client": hit.get("client") or client,
                "text": text,
                "similarity": float(hit.get("similarity") or 0.0),
                "_key": key,
            })

    # Fill the budget breadth-first: one chunk from each distinct
    # document in merged-rank order, and only then second chunks. With
    # real ~350-word chunks the char budget admits only about three, so
    # a plain rank walk would spend two of them on whatever single
    # document matched best. Seeing three different documents tells the
    # SA more about the account than two paragraphs of one.
    selected: List[dict] = []
    taken: set = set()
    per_doc: dict = {}
    used_chars = 0
    for round_cap in range(1, MAX_CHUNKS_PER_DOCUMENT + 1):
        for i, cand in enumerate(candidates):
            if len(selected) >= max_chunks:
                break
            if i in taken:
                continue
            key = cand["_key"]
            if per_doc.get(key, 0) >= round_cap:
                continue
            if used_chars + len(cand["text"]) > max_chars:
                # Skip rather than truncate — a half-sentence from a SOW
                # is worse than nothing — and keep going, since a later
                # (smaller) chunk may still fit.
                continue
            taken.add(i)
            per_doc[key] = per_doc.get(key, 0) + 1
            used_chars += len(cand["text"])
            selected.append({k: v for k, v in cand.items() if k != "_key"})
        if len(selected) >= max_chunks:
            break

    if selected:
        logger.info(
            f"prep-brief retrieved {len(selected)} document chunk(s) "
            f"({used_chars} chars) across "
            f"{len({_doc_key(h) for h in selected})} document(s) for "
            f"client {client!r} from {len(ranked_lists)} probe(s)")
    return selected


def format_document_context(hits: Sequence[dict]) -> str:
    """The document half of the prompt context.

    Each chunk is headed with `### [DOC: <file name>]` — the same shape
    as the session blocks' `### [<session_id>] <title>` header, so the
    model has one consistent citation convention and the two kinds of
    material are visibly distinct in the prompt. Empty string when
    there are no hits, which the summarizer treats as "emit the exact
    prompt you emitted before documents existed".
    """
    blocks: List[str] = []
    for h in hits:
        name = (h.get("doc_name") or "document").strip()
        text = (h.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"### [DOC: {name}]\n{text}")
    return "\n\n---\n\n".join(blocks)


def referenced_documents(hits: Sequence[dict]) -> List[dict]:
    """Per-document provenance for the API response / UI footer.

    One entry per distinct document (not per chunk), ordered by best
    matching chunk, mirroring what `referenced_sessions` does for
    meetings.
    """
    order: List[str] = []
    by_key: dict = {}
    for h in hits:
        key = _doc_key(h)
        sim = float(h.get("similarity") or 0.0)
        if key not in by_key:
            order.append(key)
            by_key[key] = {
                "doc_name": h.get("doc_name") or "document",
                "doc_path": h.get("doc_path") or "",
                "chunk_count": 0,
                "similarity": sim,
            }
        by_key[key]["chunk_count"] += 1
        by_key[key]["similarity"] = max(by_key[key]["similarity"], sim)
    return [by_key[k] for k in order]


def retrieve_for_brief(
    search_service,
    client: str,
    subject: str = "",
    project: str = "",
    agenda: str = "",
    user_context: str = "",
) -> List[dict]:
    """build_document_queries + retrieve_client_documents in one call.

    Convenience wrapper used by both prep-brief endpoints so the query
    construction can't drift between them. Same never-raises contract.
    """
    try:
        queries = build_document_queries(
            subject=subject, project=project,
            agenda=agenda, user_context=user_context,
        )
        return retrieve_client_documents(search_service, client, queries)
    except Exception as e:  # pragma: no cover - belt and braces
        logger.warning(f"prep-brief document retrieval aborted: {e}")
        return []


__all__ = [
    "MAX_CONTEXT_SESSIONS",
    "MAX_FALLBACK_SESSIONS",
    "MAX_DOCUMENT_CHUNKS",
    "MAX_DOCUMENT_CONTEXT_CHARS",
    "MAX_CHUNKS_PER_DOCUMENT",
    "MAX_SINGLE_CHUNK_CHARS",
    "MIN_DOCUMENT_SIMILARITY",
    "DOCUMENT_RETRIEVAL_TOP_K",
    "build_document_queries",
    "retrieve_client_documents",
    "retrieve_for_brief",
    "format_document_context",
    "referenced_documents",
]
