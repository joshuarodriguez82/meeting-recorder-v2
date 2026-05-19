"""
Provider-agnostic meeting summarizer and speaker identifier.

Supports two provider families:

- **Anthropic** (native SDK): Claude Haiku / Sonnet / Opus. Requires an
  `anthropic_api_key`.
- **OpenAI-compatible** (via `openai` SDK): any service that speaks the
  OpenAI Chat Completions protocol. Covers:
    * OpenRouter (https://openrouter.ai/api/v1) — gateway that exposes
      free-tier Llama/Qwen/Gemini/Mistral models among paid options.
    * Ollama (http://localhost:11434/v1) — local-only, no API key needed.
    * LM Studio, LocalAI, self-hosted vLLM, etc.

The active provider is selected via the `ai_provider` setting. For
OpenAI-compatible targets, `openai_base_url` points at the server and
`openai_api_key` carries the credential (Ollama accepts any non-empty
string — "ollama" by convention).
"""

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from anthropic import AsyncAnthropic
from utils.logger import get_logger

logger = get_logger(__name__)

# Screenshots the user grabs mid-meeting are PNG (macOS screencapture /
# Windows GDI / Linux grim all default to PNG). Cap how many we attach
# so a screenshot-happy meeting doesn't blow the request size / cost.
_MAX_SCREENSHOTS = 8
_IMG_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def _model_supports_vision(model: str) -> bool:
    """Claude 3.5 Haiku and the original Claude 3 Haiku are text-only —
    sending image blocks to them is a hard 400 from the Anthropic API.
    Every Claude 4.x model (and Sonnet/Opus 3.x) is vision-capable, so
    default to True and only deny the known text-only Haiku ids."""
    m = (model or "").lower()
    return not ("haiku-3" in m or "3-5-haiku" in m or "3.5-haiku" in m
                or "3-haiku" in m)


def _image_blocks(image_paths: List[str]) -> list:
    """Read screenshot files into Anthropic image content blocks.
    Skips anything missing/oversized/unknown rather than failing the
    whole summary — a broken screenshot shouldn't cost the user their
    meeting notes."""
    blocks: list = []
    for p in (image_paths or [])[:_MAX_SCREENSHOTS]:
        try:
            fp = Path(p)
            media = _IMG_MEDIA_TYPES.get(fp.suffix.lower())
            if not media or not fp.is_file():
                continue
            raw = fp.read_bytes()
            if not raw or len(raw) > 5 * 1024 * 1024:  # Anthropic per-image cap
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media,
                    "data": base64.standard_b64encode(raw).decode("ascii"),
                },
            })
        except Exception as e:
            logger.warning(f"Skipping screenshot {p}: {e}")
    return blocks


def _markdown_to_html(text: str) -> str:
    """Convert basic markdown to HTML for email display."""
    lines = text.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        # Headers
        if line.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h3 style="color:#1a1a1a;font-size:15px;margin:16px 0 6px;">'
                f'{line[4:]}</h3>')
        elif line.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h2 style="color:#003a57;font-size:17px;margin:20px 0 8px;'
                f'border-bottom:1px solid #ddd;padding-bottom:4px;">'
                f'{line[3:]}</h2>')
        elif line.startswith("# "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(
                f'<h1 style="color:#003a57;font-size:20px;margin:20px 0 10px;">'
                f'{line[2:]}</h1>')
        # Bullet points
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                html_lines.append(
                    '<ul style="margin:6px 0;padding-left:20px;">')
                in_list = True
            content = _inline_markdown(line[2:])
            html_lines.append(
                f'<li style="margin:4px 0;color:#333;">{content}</li>')
        # Numbered list
        elif re.match(r"^\d+\. ", line):
            if not in_list:
                html_lines.append(
                    '<ol style="margin:6px 0;padding-left:20px;">')
                in_list = True
            content = _inline_markdown(re.sub(r"^\d+\. ", "", line))
            html_lines.append(
                f'<li style="margin:4px 0;color:#333;">{content}</li>')
        # Empty line
        elif line.strip() == "":
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append('<div style="height:8px;"></div>')
        # Regular paragraph
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            content = _inline_markdown(line)
            html_lines.append(
                f'<p style="margin:4px 0;color:#333;line-height:1.6;">'
                f'{content}</p>')

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def _inline_markdown(text: str) -> str:
    """Convert inline markdown (bold, italic, code) to HTML."""
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r'<strong>\1</strong>', text)
    text = re.sub(r"__(.+?)__",     r'<strong>\1</strong>', text)
    # Italic
    text = re.sub(r"\*(.+?)\*",     r'<em>\1</em>', text)
    text = re.sub(r"_(.+?)_",       r'<em>\1</em>', text)
    # Inline code
    text = re.sub(r"`(.+?)`",
                  r'<code style="background:#f0f0f0;padding:1px 4px;'
                  r'border-radius:3px;font-family:monospace;">\1</code>',
                  text)
    return text


# Note: the prompt library has moved to `services.template_service` —
# the server reads it at request time so the user can edit templates
# without restarting the backend. This module no longer owns the dict.


DEFAULT_MODEL = "claude-haiku-4-5"


def _with_user_notes(instruction: str, transcript: str, notes: str = "") -> str:
    """
    Compose the final prompt by prepending the user's own session notes —
    things that aren't on the audio (off-call context, hallway conversation,
    reminders, implicit follow-ups). Claude is told to weight these heavily
    so AI extractions reflect the SA's perspective, not just the transcript.
    """
    notes = (notes or "").strip()
    if not notes:
        return f"{instruction}\n\n{transcript}"
    return (
        f"{instruction}\n\n"
        f"=== USER NOTES (important context from the recorder — "
        f"treat these as fact, they know things the transcript doesn't "
        f"capture) ===\n{notes}\n\n"
        f"=== MEETING TRANSCRIPT ===\n{transcript}"
    )


def _coerce_json(text: str) -> Optional[dict]:
    """Best-effort 'model text → dict'. Tolerates code fences and a
    little surrounding prose by falling back to the outermost {...}
    slice. Returns None when nothing parseable is found so the caller
    can trigger a repair retry."""
    if not text:
        return None
    s = text.strip()
    # Strip a leading ```json / ``` fence and its closer if present.
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


class Summarizer:

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        provider: str = "anthropic",
        base_url: str = "",
        openai_api_key: str = "",
    ):
        """
        Args:
            api_key: Anthropic API key (used when provider == "anthropic").
            model: Model identifier. Interpretation depends on provider —
                e.g. "claude-haiku-4-5" for Anthropic,
                "meta-llama/llama-3.3-70b-instruct:free" for OpenRouter,
                "llama3" for Ollama.
            provider: "anthropic" (default) or "openai" (OpenAI-compatible).
            base_url: Override endpoint for OpenAI-compatible providers.
                Ignored when provider == "anthropic".
            openai_api_key: Credential for OpenAI-compatible providers.
                For Ollama any non-empty string works.
        """
        self._provider = (provider or "anthropic").strip().lower()
        from config.settings import _normalize_model
        requested = model or DEFAULT_MODEL
        self._model = _normalize_model(requested)
        if self._model != requested:
            logger.warning(
                "Model id %r is not resolvable (Anthropic 404s it); "
                "using %r instead", requested, self._model)
        self._anthropic_client: Optional[AsyncAnthropic] = None
        self._openai_client = None  # lazily imported so the openai SDK
        # isn't a hard dep when the user stays on Anthropic
        if self._provider == "anthropic":
            self._anthropic_client = AsyncAnthropic(api_key=api_key)
        else:
            # Import here so users who never switch off Anthropic don't
            # need the openai wheel installed. Any error surfaces at
            # first call rather than at Summarizer construction.
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise RuntimeError(
                    "The 'openai' package is required for non-Anthropic "
                    "providers. Install with: pip install openai"
                ) from e
            # Default to OpenRouter if nothing was configured, since that's
            # the easiest "free models" entry point and doesn't require
            # anything running on the user's machine.
            effective_base = (base_url or "").strip() or "https://openrouter.ai/api/v1"
            # Ollama accepts any non-empty key. OpenRouter / OpenAI need a
            # real one. We pass a literal placeholder so the client can
            # construct even when the user forgot to paste a key — the
            # HTTP 401 surface message is clearer than a ValueError.
            effective_key = (openai_api_key or "").strip() or "MISSING_KEY"
            self._openai_client = AsyncOpenAI(
                api_key=effective_key,
                base_url=effective_base,
            )

    async def _chat(self, prompt: str, max_tokens: int = 1024,
                    timeout: float = 60.0,
                    image_paths: Optional[List[str]] = None) -> str:
        """
        Provider-agnostic "one-shot user prompt → assistant text" helper.

        Both Anthropic and OpenAI-compat providers get the same user
        content string; the SDK differences are isolated to this method
        so the extractors above stay identical.

        `image_paths` (optional) attaches meeting screenshots as visual
        context. Only honoured on the Anthropic provider — Claude is
        vision-capable; the OpenAI-compat path covers local/text-only
        models (Ollama etc.) where blindly sending images would error,
        so there we silently fall back to text-only.
        """
        if self._provider == "anthropic":
            imgs = (
                _image_blocks(image_paths)
                if image_paths and _model_supports_vision(self._model)
                else []
            )
            if imgs:
                content: object = [*imgs, {"type": "text", "text": prompt}]
            else:
                content = prompt
            msg = await asyncio.wait_for(
                self._anthropic_client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": content}],
                ),
                timeout=timeout,
            )
            return msg.content[0].text
        # OpenAI-compatible (OpenRouter / Ollama / LM Studio / ...).
        # Text-only — see docstring.
        resp = await asyncio.wait_for(
            self._openai_client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout,
        )
        return resp.choices[0].message.content or ""

    async def stream_chat(self, prompt: str, max_tokens: int = 2048):
        """
        Provider-agnostic streaming generator: yields text fragments
        (str) as the model produces them, so the SSE endpoint can pipe
        them straight to the browser. Used by QAService for the
        cross-meeting Q&A "watch the answer type out" UX.

        Both providers' streaming APIs deliver text in unpredictable
        chunk sizes — sometimes a single token, sometimes a sentence.
        Caller should accumulate fragments rather than treating each
        as a sentence boundary.
        """
        if self._provider == "anthropic":
            # Anthropic SDK's async streaming returns an async context
            # manager; text_stream is an async iterator of deltas.
            async with self._anthropic_client.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield text
            return

        # OpenAI-compatible streaming. Each chunk has a delta.content
        # which is None on bookkeeping events (role, finish_reason)
        # — skip those and yield only text.
        stream = await self._openai_client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except (AttributeError, IndexError):
                continue
            if delta:
                yield delta

    async def summarize(self, transcript: str, prompt: str,
                         notes: str = "", template_name: str = "",
                         image_paths: Optional[List[str]] = None) -> str:
        """
        Summarize a transcript against a caller-supplied prompt. The
        server-side templates service resolves the template name into
        a prompt so this class stays free of template storage concerns.
        `template_name` is accepted only for logging clarity.

        `image_paths` are screenshots the user captured during the
        meeting — passed to Claude as visual context so the summary can
        reference what was on screen (diagrams, dashboards, slides).
        """
        label = template_name or "custom"
        n_imgs = len(image_paths or [])
        logger.info(
            f"Requesting meeting summary (template={label}, "
            f"screenshots={n_imgs}) via {self._provider}/{self._model}")
        instruction = prompt
        if n_imgs:
            instruction = (
                f"{prompt}\n\nThe user also attached {n_imgs} screenshot"
                f"{'s' if n_imgs != 1 else ''} captured during the meeting "
                f"(shown above). Use them as additional context — refer to "
                f"what they show where it sharpens the summary.")
        try:
            summary = await self._chat(
                _with_user_notes(instruction, transcript, notes),
                max_tokens=1024, timeout=90.0,
                image_paths=image_paths,
            )
            logger.info("Summary received.")
            return summary
        except Exception as e:
            raise RuntimeError(f"Summarization API call failed: {e}") from e

    async def extract_action_items(self, transcript: str, notes: str = "") -> str:
        logger.info(f"Extracting action items via {self._provider}/{self._model}")
        instruction = (
            "Analyze this meeting transcript and extract the following "
            "in clearly structured markdown:\n\n"
            "## Action Items\n"
            "List each action item with: who is responsible, what they "
            "need to do, and by when (if mentioned). Use checkboxes.\n"
            "Format: - [ ] **[Owner]**: Action description (Due: date if mentioned)\n\n"
            "## Decisions Made\n"
            "List each decision that was agreed upon in the meeting.\n\n"
            "## Open Questions\n"
            "List questions that were raised but not resolved.\n\n"
            "If a section has no items, write 'None identified.'\n\n"
            "If the USER NOTES mention action items the user has committed "
            "to (things like 'need to follow up on X', 'reminder to send Y'), "
            "include those as action items owned by the user."
        )
        try:
            result = await self._chat(
                _with_user_notes(instruction, transcript, notes),
                max_tokens=1024, timeout=60.0,
            )
            logger.info("Action items extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Action items extraction failed: {e}") from e

    async def extract_decisions(self, transcript: str, notes: str = "") -> str:
        """Extract decisions made with rationale — an auto-generated ADR log."""
        logger.info(f"Extracting decisions via {self._provider}/{self._model}")
        instruction = (
            "Analyze this meeting transcript and extract every DECISION "
            "made. Return structured markdown with one entry per decision "
            "in this format:\n\n"
            "## Decision: [short title]\n"
            "- **Decided:** what was agreed upon\n"
            "- **Rationale:** why (context, drivers)\n"
            "- **Alternatives considered:** options that were rejected "
            "(if any mentioned)\n"
            "- **Owner:** who made the call (if identifiable)\n"
            "- **Impact:** systems/teams/clients affected\n\n"
            "Only include decisions that were actually MADE, not just "
            "discussed. Skip discussions without conclusions. If the USER "
            "NOTES record additional decisions made off-audio (in hallway "
            "chat, private chat, follow-up email), include those too and "
            "annotate with **Source:** user notes.\n\n"
            "If no decisions were made, write: 'No decisions made in this "
            "meeting.'"
        )
        try:
            result = await self._chat(
                _with_user_notes(instruction, transcript, notes),
                max_tokens=1024, timeout=60.0,
            )
            logger.info("Decisions extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Decisions extraction failed: {e}") from e

    async def extract_structured(self, transcript: str,
                                 notes: str = "") -> dict:
        """One call → typed records for the engagement layer.

        Returns {"requirements": [...], "decisions": [...],
        "action_items": [...], "open_questions": [...]} where each item
        is a plain dict of content fields (the server stamps id /
        session_id / status / created_at — the model never owns those).

        Deliberately NOT Anthropic tool-use: `_chat` also serves
        OpenRouter / Ollama where tool-calling isn't guaranteed. A
        JSON-only instruction plus a tolerant parser and one repair
        retry keeps a single code path across every provider. On hard
        failure this raises — callers run it best-effort so the markdown
        extractors still populate the per-session UI.
        """
        logger.info(
            f"Extracting structured records via {self._provider}/{self._model}")
        instruction = (
            "Extract the meeting's content as STRICT JSON. Output ONLY a "
            "single JSON object, no prose, no markdown fences. Schema:\n"
            "{\n"
            '  "requirements": [{"text": str, "kind": '
            '"functional|nonfunctional|constraint|assumption", '
            '"source": "transcript|notes"}],\n'
            '  "decisions": [{"title": str, "decided": str, '
            '"rationale": str, "alternatives": str, "owner": str, '
            '"impact": str, "source": "transcript|notes"}],\n'
            '  "action_items": [{"text": str, "owner": str, '
            '"due": str, "source": "transcript|notes"}],\n'
            '  "open_questions": [{"text": str, '
            '"source": "transcript|notes"}]\n'
            "}\n"
            "Rules: include only items that genuinely occurred — empty "
            "arrays are correct when nothing applies, never invent. "
            "Decisions = actually agreed, not merely discussed. If the "
            "USER NOTES record items decided/committed off-audio, include "
            'them with "source":"notes". Use "" for unknown string '
            'fields, [] for empty arrays. Output nothing but the JSON.'
        )
        prompt = _with_user_notes(instruction, transcript, notes)
        try:
            raw = await self._chat(prompt, max_tokens=2048, timeout=90.0)
            parsed = _coerce_json(raw)
            if parsed is None:
                # One repair pass — weak models often wrap JSON in prose
                # or fences despite instructions.
                repair = (
                    "Return ONLY the JSON object from your previous answer, "
                    "valid and parseable, with no surrounding text or "
                    "code fences:\n\n" + raw
                )
                raw = await self._chat(repair, max_tokens=2048, timeout=90.0)
                parsed = _coerce_json(raw)
            if parsed is None:
                raise RuntimeError("model did not return parseable JSON")
            logger.info("Structured records extracted.")
            return {
                "requirements": parsed.get("requirements") or [],
                "decisions": parsed.get("decisions") or [],
                "action_items": parsed.get("action_items") or [],
                "open_questions": parsed.get("open_questions") or [],
            }
        except Exception as e:
            raise RuntimeError(f"Structured extraction failed: {e}") from e

    async def meeting_prep_brief_from_calendar(
        self,
        upcoming_subject: str,
        upcoming_attendees: list[str],
        upcoming_when: str,
        identified_client: str,
        identified_project: str,
        prior_notes: str,
        agenda: str = "",
    ) -> str:
        """Richer prep brief used by the click-from-calendar-tile flow.
        Includes hot-topics + suggested-questions sections and asks
        Claude to inline `[session_id]` citations the frontend turns
        into click-to-jump links.

        The simpler meeting_prep_brief() (string-only context) stays
        for the existing /prep-brief endpoint; this one is its
        structurally-richer cousin that knows about a specific
        upcoming meeting.
        """
        logger.info(
            f"Generating calendar-based prep brief: "
            f"\"{upcoming_subject}\" via {self._provider}/{self._model}")
        attendee_blob = ", ".join(upcoming_attendees) or "(no attendees listed)"
        scope_blob = identified_client or "(no client identified)"
        if identified_project:
            scope_blob += f" / {identified_project}"
        agenda = (agenda or "").strip()
        # Keep the invite body bounded so it can't crowd out the prior
        # notes in the context window.
        if len(agenda) > 4000:
            agenda = agenda[:4000] + "\n…(truncated)"
        agenda_block = (
            f"\n\n=== MEETING INVITE / AGENDA ===\n{agenda}" if agenda else ""
        )
        try:
            result = await self._chat(
                (
                    "You're preparing a Solutions Architect for a specific "
                    "upcoming meeting on their calendar. Ground the brief "
                    "in the prior meeting notes below (and the meeting "
                    "invite/agenda if one is provided); don't invent "
                    "context. When an agenda is present, let it steer the "
                    "'Hot topics' and 'Questions' sections toward what "
                    "this specific meeting is actually about. Output "
                    "concise actionable markdown. When you reference a "
                    "specific prior meeting, INLINE the citation as "
                    "`[ABC123]` using the literal session ID from the "
                    "header of that meeting's notes — the frontend "
                    "turns those into click-to-jump links.\n\n"
                    "Sections (use these exact headers):\n\n"
                    "## The story so far\n"
                    "3-5 bullets. The arc across these prior meetings — "
                    "what was decided, what shifted, what's unresolved. "
                    "Cite specific meetings with `[id]`.\n\n"
                    "## Hot topics likely to come up\n"
                    "Themes that recurred across multiple prior meetings, "
                    "or topics that are still open. List with one-line "
                    "context per item. Cite the meetings where each was "
                    "raised.\n\n"
                    "## Open commitments to / from this account\n"
                    "Action items still outstanding from prior meetings, "
                    "by owner. Format: `- [Owner]: <task> (from [id])`. "
                    "Skip ones that look already-delivered or trivial. "
                    "If none are open, write 'None.'\n\n"
                    "## Questions to drive this meeting\n"
                    "3-5 questions calibrated to unblock open commitments "
                    "and pressure-test decisions. Specific to the people "
                    "in the room.\n\n"
                    "Keep ALL sections tight — every bullet should "
                    "earn its place. Trim ruthlessly.\n\n"
                    f"=== UPCOMING MEETING ===\n"
                    f"Subject: {upcoming_subject}\n"
                    f"When: {upcoming_when}\n"
                    f"Attendees: {attendee_blob}\n"
                    f"Account: {scope_blob}"
                    f"{agenda_block}\n\n"
                    f"=== PRIOR MEETING NOTES ===\n{prior_notes}"
                ),
                max_tokens=1500, timeout=90.0,
            )
            logger.info("Calendar-based prep brief generated.")
            return result
        except Exception as e:
            raise RuntimeError(f"Prep brief generation failed: {e}") from e

    async def meeting_prep_brief(self, prior_notes: str, upcoming_subject: str) -> str:
        """Generate a prep brief from prior meeting notes for an upcoming meeting."""
        logger.info(f"Generating prep brief for: {upcoming_subject} via {self._provider}/{self._model}")
        try:
            result = await self._chat(
                (
                    "You're preparing a Solutions Architect for an upcoming meeting. "
                    "Based on the summaries, decisions, action items, and requirements "
                    "from previous related meetings, generate a concise pre-meeting "
                    "brief in markdown with these sections:\n\n"
                    "## Recent Context\n"
                    "Key topics discussed in recent meetings — 3-5 bullets.\n\n"
                    "## Open Action Items\n"
                    "Outstanding action items (especially for this person). "
                    "Status and owner.\n\n"
                    "## Open Questions / Risks\n"
                    "Unresolved questions or risks raised previously.\n\n"
                    "## Suggested Discussion Points\n"
                    "What you should raise or follow up on in this meeting.\n\n"
                    "Keep it tight and actionable. If a section has no content, "
                    "write 'None.'\n\n"
                    f"Upcoming meeting: {upcoming_subject}\n\n"
                    f"=== PRIOR MEETING NOTES ===\n{prior_notes}"
                ),
                max_tokens=1024, timeout=60.0,
            )
            logger.info("Prep brief generated.")
            return result
        except Exception as e:
            raise RuntimeError(f"Prep brief generation failed: {e}") from e

    async def extract_requirements(self, transcript: str, notes: str = "") -> str:
        logger.info(f"Extracting requirements via {self._provider}/{self._model}")
        instruction = (
            "Analyze this meeting transcript and extract all requirements "
            "discussed. Return structured markdown with:\n\n"
            "## Functional Requirements\n"
            "| ID | Requirement | Priority | Owner |\n"
            "|---|---|---|---|\n"
            "| FR-001 | Description | High/Med/Low | Person if mentioned |\n\n"
            "## Non-Functional Requirements\n"
            "Same table format with IDs like NFR-001.\n\n"
            "## Constraints\n"
            "List any technical, business, or timeline constraints mentioned.\n\n"
            "## Assumptions\n"
            "List assumptions made during the discussion.\n\n"
            "Assign priority based on context clues (urgency, emphasis, "
            "stakeholder tone). If the USER NOTES list additional requirements "
            "or constraints the transcript doesn't capture, include those — "
            "annotate their source in the Owner column as 'user notes'.\n"
            "If a section has no items, write 'None identified.'"
        )
        try:
            result = await self._chat(
                _with_user_notes(instruction, transcript, notes),
                max_tokens=2048, timeout=90.0,
            )
            logger.info("Requirements extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Requirements extraction failed: {e}") from e

    async def identify_speakers(self, transcript: str) -> Dict[str, str]:
        logger.info(f"Requesting speaker identification via {self._provider}/{self._model}")
        try:
            raw = (await self._chat(
                (
                    "Analyze this meeting transcript and identify the real "
                    "name of each speaker. Return ONLY a JSON object mapping "
                    "speaker IDs to their real names.\n\n"
                    "Use these two high-confidence signals:\n"
                    "1. SELF-INTRODUCTION — a speaker states their own name: "
                    "'Hi, I'm X', 'My name is X', 'This is X', 'X here', or "
                    "in a round of intros 'I'm X from <company>'.\n"
                    "2. DIRECT ADDRESS — someone calls a person by name and "
                    "that person then immediately speaks or responds. e.g. "
                    "SPEAKER_00: 'Sarah, what do you think?' followed by "
                    "SPEAKER_02: 'I think…' → SPEAKER_02 is Sarah. Also "
                    "'Thanks, Mike.', 'Over to you, Priya.', 'Go ahead "
                    "Dave.' — attribute the name to the speaker who takes "
                    "the turn that was handed to them.\n\n"
                    "Only include a speaker when one of these signals makes "
                    "you confident. Use the most complete form of the name "
                    "stated (prefer 'Sarah Jones' over 'Sarah' if both "
                    "appear for the same speaker). Do NOT guess from topic "
                    "or role. If nothing qualifies, return {}.\n\n"
                    "Example response: "
                    "{\"SPEAKER_00\": \"John Smith\", \"SPEAKER_02\": \"Sarah Jones\"}\n\n"
                    f"Transcript:\n{transcript}"
                ),
                max_tokens=512, timeout=30.0,
            )).strip()
            logger.info(f"Speaker identification response: {raw}")

            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(
                    line for line in lines
                    if not line.startswith("```")
                ).strip()

            result = json.loads(raw)
            if not isinstance(result, dict):
                return {}

            filtered = {
                k: v for k, v in result.items()
                if isinstance(k, str) and isinstance(v, str)
                and k.startswith("SPEAKER") and v.strip()
            }
            logger.info(f"Identified {len(filtered)} speakers by name")
            return filtered

        except json.JSONDecodeError:
            logger.warning("Speaker ID response was not valid JSON")
            return {}
        except Exception as e:
            logger.warning(f"Speaker identification failed: {e}")
            return {}

    def summary_to_html(self, summary: str) -> str:
        """Convert a markdown summary to formatted HTML for email."""
        return _markdown_to_html(summary)