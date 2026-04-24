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
import json
import re
from typing import Dict, Optional
from anthropic import AsyncAnthropic
from utils.logger import get_logger

logger = get_logger(__name__)


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


MEETING_TEMPLATES = {
    "General": (
        "Please summarize this meeting transcript. "
        "Include: key topics discussed, decisions made, "
        "action items, and any follow-ups needed."
    ),
    "Requirements Gathering": (
        "This is a requirements gathering meeting. Summarize with focus on: "
        "1) Business context and problem statement discussed, "
        "2) Functional requirements identified (what the system should do), "
        "3) Non-functional requirements (performance, security, scalability), "
        "4) Constraints and assumptions mentioned, "
        "5) Open questions that need follow-up, "
        "6) Stakeholder priorities and any conflicts between requirements."
    ),
    "Design Review": (
        "This is a design/architecture review meeting. Summarize with focus on: "
        "1) Solution overview and architecture discussed, "
        "2) Design decisions made and their rationale, "
        "3) Trade-offs considered, "
        "4) Risks and concerns raised, "
        "5) Feedback and requested changes, "
        "6) Next steps and action items."
    ),
    "Sprint Planning": (
        "This is a sprint planning meeting. Summarize with focus on: "
        "1) Sprint goal agreed upon, "
        "2) Stories/tasks committed to with owners, "
        "3) Capacity concerns or blockers raised, "
        "4) Dependencies identified, "
        "5) Carry-over items from previous sprint, "
        "6) Key risks to sprint delivery."
    ),
    "Stakeholder Update": (
        "This is a stakeholder update meeting. Summarize with focus on: "
        "1) Project status and progress reported, "
        "2) Milestones achieved or missed, "
        "3) Risks and issues escalated, "
        "4) Decisions requested from stakeholders, "
        "5) Decisions made by stakeholders, "
        "6) Next steps and timeline updates."
    ),
}


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
        self._model = model or DEFAULT_MODEL
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
                    timeout: float = 60.0) -> str:
        """
        Provider-agnostic "one-shot user prompt → assistant text" helper.

        Both Anthropic and OpenAI-compat providers get the same user
        content string; the SDK differences are isolated to this method
        so the extractors above stay identical.
        """
        if self._provider == "anthropic":
            msg = await asyncio.wait_for(
                self._anthropic_client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
            return msg.content[0].text
        # OpenAI-compatible (OpenRouter / Ollama / LM Studio / ...)
        resp = await asyncio.wait_for(
            self._openai_client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout,
        )
        return resp.choices[0].message.content or ""

    async def summarize(self, transcript: str, template: str = "General",
                         notes: str = "") -> str:
        prompt = MEETING_TEMPLATES.get(template, MEETING_TEMPLATES["General"])
        logger.info(f"Requesting meeting summary (template={template}) via {self._provider}/{self._model}")
        try:
            summary = await self._chat(
                _with_user_notes(prompt, transcript, notes),
                max_tokens=1024, timeout=60.0,
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
                    "Analyze this meeting transcript and identify any speakers "
                    "who introduced themselves by name. Return ONLY a JSON object "
                    "mapping speaker IDs to their real names. "
                    "Only include speakers where you are confident of their name "
                    "from an explicit introduction like 'Hi I'm X', 'My name is X', "
                    "'This is X speaking', etc. "
                    "If no introductions are found, return an empty JSON object {}.\n\n"
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