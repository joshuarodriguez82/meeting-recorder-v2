"""
Cross-meeting Q&A.

User asks "what did we decide about pricing in the last few ACME meetings"
and Claude answers using top-K chunks retrieved from the semantic search
index, citing the specific session + timestamp where each claim came from.

Flow:
  1. SearchService.search(query, top_k, client?, project?) → ranked chunks
  2. Build a prompt that:
     - Tells Claude to use ONLY the supplied excerpts
     - Asks for inline citations in [session_id @ mm:ss] form
     - Forbids speculation beyond the excerpts
  3. Stream the LLM's response token-by-token via Summarizer.stream_chat()

The QA endpoint exposes this as SSE so the user sees the answer type out
in real time — a 5-second wait for a finished answer feels much worse
than a 5-second wait while watching it form.

Citations:
  Inline `[ABC123 @ 12:34]` references. The frontend regex-detects them
  and renders as click-to-jump buttons opening the source session at
  that timestamp. We use IDs (not display names) because IDs are stable
  + short — names can be renamed and contain spaces.
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional

from utils.logger import get_logger

logger = get_logger(__name__)


# Cap on chunks retrieved per question. More context = potentially
# better answer, but also more tokens (cost) and a higher chance of
# Claude getting confused by tangentially-related material. 8 chunks
# of 400 words ≈ 3200 words ≈ 4000 tokens — comfortable for any model.
DEFAULT_TOP_K = 8


def _format_time(seconds: float) -> str:
    """Format seconds as mm:ss to match how citations appear in answers."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


class QAService:
    """Orchestrates retrieve + prompt + stream-completion for the
    cross-meeting Q&A endpoint."""

    def __init__(self, search_service, summarizer):
        # Both can be None at construct time (settings not configured) —
        # the endpoint checks before calling.
        self._search = search_service
        self._summarizer = summarizer

    @property
    def is_ready(self) -> bool:
        return self._search is not None and self._summarizer is not None

    def retrieve(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        client: Optional[str] = None,
        project: Optional[str] = None,
    ) -> List[dict]:
        """Pull the top-K chunks for `query`. Synchronous (SearchService
        is CPU-bound numpy)."""
        if self._search is None:
            return []
        return self._search.search(
            query=query, top_k=top_k, client=client, project=project,
        )

    async def stream_inline_answer(
        self,
        query: str,
        context: str,
    ) -> AsyncIterator[str]:
        """Stream a Claude answer grounded in a single inline context
        blob — no semantic retrieval. Used by the in-call search panel
        when the user wants Claude to reason over the LIVE transcript
        of the current meeting ("what was that number she just said?").

        We bypass `stream_answer` because there are no citation-bearing
        sources to format, and the user does not need the "[ID @ mm:ss]"
        link infrastructure during a live call — they're listening to
        the source themselves.
        """
        if self._summarizer is None:
            yield (
                "AI search needs a provider configured (Anthropic, "
                "OpenRouter, Ollama, Groq, Gemini, …) — open Settings → "
                "AI Provider, save a key, and try again."
            )
            return
        if not (context or "").strip():
            yield (
                "Nothing has been transcribed yet in this call. Wait "
                "for the live preview to capture some speech, then ask."
            )
            return

        prompt = (
            "You answer questions about a live meeting in progress. "
            "Use ONLY the transcript excerpt below — no outside "
            "knowledge, no speculation. The excerpt is a rolling live "
            "transcript with You / Them speaker tags; recent text is "
            "at the bottom.\n\n"
            "Rules:\n"
            "1. Be concise — 1-3 sentences usually. The user is in a "
            "meeting and reading mid-conversation.\n"
            "2. Quote the exact wording when the transcript answers "
            "the question verbatim.\n"
            "3. If the transcript doesn't cover it, say so plainly: "
            "\"I don't see that in this call yet.\"\n"
            "4. Don't add any [citation @ mm:ss] markers — the user "
            "can scroll the live transcript themselves.\n\n"
            f"USER QUESTION: {query}\n\n"
            f"LIVE TRANSCRIPT:\n{context}\n\n"
            "ANSWER:"
        )
        async for fragment in self._summarizer.stream_chat(
            prompt, max_tokens=512,
        ):
            yield fragment

    async def stream_answer(
        self,
        query: str,
        sources: List[dict],
    ) -> AsyncIterator[str]:
        """Stream Claude's answer text given the pre-retrieved sources.

        Yields raw text fragments (str). Caller wraps in SSE on its way
        to the browser. Empty `sources` short-circuits with a single
        "no relevant material" answer so we don't waste an LLM call —
        and so the user gets a clear "your search returned nothing"
        signal rather than a hallucinated answer.
        """
        if not sources:
            yield (
                "I couldn't find anything in the indexed meetings that "
                "matches that question. Try different wording, broaden "
                "the client/project filter, or run the semantic-index "
                "backfill in Settings if you have older sessions that "
                "aren't indexed yet."
            )
            return

        if self._summarizer is None:
            yield (
                "Q&A needs an AI provider configured (Anthropic or "
                "OpenAI-compatible) — open Settings → AI Provider, "
                "save a key, and try again."
            )
            return

        prompt = self._build_prompt(query, sources)
        async for fragment in self._summarizer.stream_chat(
            prompt, max_tokens=1024,
        ):
            yield fragment

    def _build_prompt(self, query: str, sources: List[dict]) -> str:
        """Format the retrieved chunks into a citation-aware prompt.

        Claude is instructed to:
          - Use ONLY the supplied excerpts (no outside knowledge)
          - Cite each claim with [session_id @ mm:ss]
          - Quote exact wording when the excerpt has it
          - Say "not found" rather than speculate
          - Stay concise (most user questions take 2-5 sentences)
        """
        excerpts: List[str] = []
        for s in sources:
            sid = s.get("session_id", "")
            display = s.get("display_name") or "Untitled meeting"
            ts = _format_time(float(s.get("start_s", 0.0)))
            text = (s.get("text") or "").strip()
            if not text:
                continue
            excerpts.append(
                f"[{sid} @ {ts}] (from \"{display}\")\n{text}"
            )
        excerpts_str = "\n\n---\n\n".join(excerpts)

        return (
            "You answer questions about the user's meetings using ONLY the "
            "excerpts below. The excerpts come from a vector-similarity "
            "search against their meeting transcripts.\n\n"
            "Rules:\n"
            "1. Cite each claim with the inline form [session_id @ mm:ss] "
            "exactly as it appears in the excerpt headers — don't invent "
            "new IDs or timestamps. The frontend turns these into "
            "click-to-jump links so the user can verify each claim "
            "against the source recording.\n"
            "2. Quote the exact wording when the excerpt has the answer "
            "verbatim. Paraphrase only when synthesising across excerpts.\n"
            "3. If the excerpts don't cover the question, say so plainly: "
            "\"I don't see this in the indexed meetings.\" Do NOT speculate "
            "or rely on general knowledge.\n"
            "4. Be concise — 2-5 sentences usually suffices. Use bullet "
            "points only if the user asked for a list.\n"
            "5. If multiple meetings disagree or evolved over time, say so "
            "and cite each.\n\n"
            f"USER QUESTION: {query}\n\n"
            f"EXCERPTS:\n{excerpts_str}\n\n"
            "ANSWER:"
        )
