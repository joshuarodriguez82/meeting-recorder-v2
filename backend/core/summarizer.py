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
from typing import Dict, List, Optional, Sequence
from anthropic import AsyncAnthropic
from core._coach_text import dedup_against as _dedup_against
# One shared no-invented-precision rule, referenced by every prompt
# builder below rather than paraphrased in each of them. `_grounding_rules`
# is the date anchor plus that rule; see core/_precision.py for why.
from core._precision import (
    grounding_rules as _grounding_rules,
    json_timing_note as _json_timing_note,
    no_invented_precision as _no_invented_precision,
)
# The calendar invite as the candidate set for speaker identification.
# Same "state it once" reasoning as _precision above: the roster rules
# are text, they live in one module, and no builder paraphrases them.
from core.speaker_roster import roster_block, roster_names
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


# Prompt version + fingerprint live in a stdlib-only module so a
# request path can import them without pulling in the Anthropic
# SDK. Re-exported here because this is where the prompts live.
from core.prompt_version import (  # noqa: E402,F401
    EXTRACTOR_PROMPT_VERSION, extraction_fingerprint)


def _split_for_cache(instruction: str, transcript: str,
                     notes: str = "") -> tuple:
    """Split a prompt into (cacheable_prefix, volatile_tail).

    WHY THE ORDER CHANGED. Prompt caching is a PREFIX match: the cached
    span runs from the start of the request to the breakpoint, and any
    byte differing before it invalidates the whole thing. Every
    extractor here sends the same transcript with a different
    instruction, and `_with_user_notes` put the INSTRUCTION FIRST — so
    the shared prefix ended at character zero and nothing could ever
    cache. Field data, August 2026: 5,545,790 input tokens billed,
    cache_read_input_tokens 0 across the entire month.

    A session is analysed by five extractors (summary, structured,
    action items, decisions, requirements), each re-sending the whole
    transcript at full price — roughly 85K input tokens for a meeting
    whose transcript is 17K.

    Transcript and user notes are per-SESSION and identical across all
    five calls, so they are the prefix. The instruction is
    per-EXTRACTOR and goes after the breakpoint. Same information, and
    the same order the model wants anyway (long context first, question
    last):

        without caching   5 x T
        with caching      1.25 x T (write) + 4 x 0.1 x T (read) = 1.65 x T

    Prefixes under ~1024 tokens are not cacheable; those calls behave
    exactly as before, which is why this is safe for short transcripts.
    """
    notes = (notes or "").strip()
    parts = []
    if notes:
        parts.append(
            "=== USER NOTES (important context from the recorder — "
            "treat these as fact, they know things the transcript "
            f"doesn't capture) ===\n{notes}")
    parts.append(f"=== MEETING TRANSCRIPT ===\n{transcript}")
    return "\n\n".join(parts), instruction


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


# `_date_anchor` / `_fmt_anchor_date` moved to core/_precision.py so the
# date anchor and the general no-invented-precision rule live in one
# place and compose there (`_grounding_rules`). Imported above under
# their original names — the anchor text itself is unchanged.


def _visual_instruction(n_imgs: int) -> str:
    """Standard appendix telling the model to use any attached screenshots.

    Used by every extraction prompt so screenshot context is leveraged
    consistently — without this, Anthropic Haiku has the images in the
    request but doesn't always reference them in output, especially for
    dense technical meetings where the transcript text is rich enough
    on its own. Empty string when there are no images so the prompt
    isn't padded for nothing on a screenshot-less meeting.
    """
    if n_imgs <= 0:
        return ""
    return (
        f"\n\nThe user attached {n_imgs} screenshot"
        f"{'s' if n_imgs != 1 else ''} captured during the meeting. "
        f"Treat them as primary evidence alongside the transcript: "
        f"diagrams, slides, error screens, and shared whiteboards "
        f"frequently contain information that wasn't spoken aloud. "
        f"Reference specific screenshots inline when you draw on them "
        f"(e.g. \"as shown in screenshot 1\"). If a screenshot adds "
        f"context that doesn't fit the main sections, surface it under "
        f"a final \"## Visuals\" section that names each one in order "
        f"and describes what it shows."
    )


def _flag_truncation(text: str, truncated: bool, limit: int, model: str) -> str:
    """Make a hit token ceiling VISIBLE instead of silently returning a
    half-written artifact.

    Field repro (2026-07-31): meeting summaries stopped mid-sentence
    ("...based on KB match quality,") with no error anywhere. The
    summary call was capped at 1024 tokens (~750 words) and NOTHING in
    this module inspected `stop_reason` / `finish_reason`, so a
    truncated summary was saved, exported, and emailed as if it were
    complete. Raising the budgets fixes the common case; this makes any
    remaining case self-evident instead of a mystery the user has to
    notice by reading to the end.
    """
    if not truncated:
        return text
    logger.warning(
        f"Output TRUNCATED at max_tokens={limit} on {model} — returned "
        f"{len(text)} chars; artifact is incomplete."
    )
    return (
        f"{text.rstrip()}\n\n---\n"
        f"⚠️ **This output was cut off** — the model hit its {limit:,}-token "
        f"limit. Re-run this step to regenerate it in full."
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


# ── Co-Pilot prompt composition ──────────────────────────────────────
#
# The live co-pilot prompt is composed at tick time from three editable
# pieces (mode + meeting type + custom context) and one fixed piece
# (output rules). The fixed piece guarantees the wire format the panel
# parses, so the user editing modes / types can't accidentally break
# the JSON contract. Fallbacks below are baked-in defaults used when
# the service layer hasn't been wired or has returned empty for some
# reason — keeps coaching alive instead of erroring.
_COACH_OUTPUT_RULES = (
    "You are coaching in real time. MOST ticks should be nearly empty — "
    "silence is the correct and common output. Only surface something "
    "when it is SPECIFIC to what was just said and genuinely useful to "
    "the SA RIGHT NOW.\n\n"
    "HARD RULES:\n"
    "- Return AT MOST 2 items TOTAL across all three lists. Usually 0 or "
    "1. Never pad to fill slots.\n"
    "- Every item must name the specific system, vendor, number, person, "
    "or decision from the transcript it reacts to. If an item could be "
    "pasted into a different meeting unchanged, DROP it.\n"
    "- BANNED — never output these or anything like them: 'request an "
    "update', 'schedule a follow-up', 'request documentation', 'confirm "
    "the status', or 'vendor lock-in' as a bare standalone risk. These "
    "are noise.\n"
    "- Do NOT repeat or reword anything you or a previous tick already "
    "said.\n\n"
    "Reply with ONLY a JSON object (no markdown, no prose, no code "
    "fences) using exactly these keys, any of which may be an empty "
    "array:\n"
    '  "clarifying_questions": specific probes tied to this moment, '
    "≤120 chars each.\n"
    '  "risks": concrete named risks the room has not acknowledged, '
    "≤120 chars each.\n"
    '  "follow_ups": concrete next steps naming the artifact/person, '
    "≤120 chars each."
    # COMPRESSED form of the shared no-invented-precision rule, not the
    # full block. This prompt is re-sent in full on every tick — hot
    # ticks fire ~every 15s against a 256-token budget and a 10s
    # timeout — so ~190 tokens of instruction would be paid dozens of
    # times per meeting and would dilute the "usually output nothing"
    # bias the rest of these rules exist to create. The half the coach
    # already had is specificity ("every item must name the specific
    # system, vendor, number, person or decision from the transcript");
    # what it lacked is the prohibition, which is what the compact form
    # carries. Live output is also ≤2 bullets of ≤120 chars, so there is
    # no enumerated list to miscount and no prose to sharpen a date in —
    # the clause-by-clause detail of the full block would be spent on
    # surfaces this builder cannot produce.
    + _no_invented_precision(compact=True)
)

# Hot-tick variant: same wire format, but the rules emphasize urgency
# and bias the model toward emptiness. Hot ticks fire every ~15s on a
# narrow window so most of them SHOULD be empty — only the ones that
# catch something time-sensitive earn a render.
_COACH_OUTPUT_RULES_HOT = (
    "HOT TICK MODE — you're looking at only the last ~90 seconds of "
    "transcript. Only fire suggestions if there's something TIME-"
    "SENSITIVE the SA should act on RIGHT NOW (a question that needs "
    "asking before the topic moves on, a number that needs verifying, "
    "a risk that's actively materializing in the conversation). When "
    "in doubt, return empty arrays — a quiet hot tick is correct most "
    "of the time. The wide-context tick fires every ~45s on a much "
    "larger window and handles the slower, contextual coaching.\n\n"
    + _COACH_OUTPUT_RULES
)

_FALLBACK_MODE_PROMPT = (
    "You are a live in-call co-pilot for a Solutions Architect at a "
    "contact-center consultancy, focused on Amazon Connect / CCaaS / "
    "contact-center work. Coach in real time."
)

_FALLBACK_MEETING_TYPE_PROMPT = (
    "No specific meeting-type lens applies. Use general meeting "
    "judgment — surface what the participants seem to be working "
    "toward and what's getting in the way."
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
        # Prompt-cache accounting, per Summarizer instance. Reportable
        # so "is caching actually working" is a number, not a belief.
        self.cache_stats = {"read": 0, "write": 0, "uncached": 0,
                            "output": 0, "calls": 0}
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

    def _budget(self, base: int) -> int:
        """Output-token budget for a long-form artifact.

        Non-Anthropic providers get extra headroom: on OpenAI-compatible
        endpoints, "thinking" models (Gemini 2.5 and friends) spend
        hidden reasoning tokens against max_tokens, so an identical
        budget yields materially LESS visible output — the same effect
        that made the live co-pilot return empty results before v2.17.0.
        """
        return base if self._provider == "anthropic" else int(base * 1.5)

    def _record_cache_usage(self, usage) -> None:
        """Accumulate cache hit/miss token counts.

        A cache that is silently NOT hitting is indistinguishable from
        no cache at all — the exact defect class this project has paid
        for repeatedly. So the numbers are recorded and reportable
        rather than assumed: `cache_read_input_tokens` staying at zero
        across a session's five extractors means a silent invalidator
        broke the prefix, and that must be visible.
        """
        if usage is None:
            return
        try:
            self.cache_stats["read"] += int(
                getattr(usage, "cache_read_input_tokens", 0) or 0)
            self.cache_stats["write"] += int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0)
            self.cache_stats["uncached"] += int(
                getattr(usage, "input_tokens", 0) or 0)
            self.cache_stats["output"] += int(
                getattr(usage, "output_tokens", 0) or 0)
            self.cache_stats["calls"] += 1
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a call
            return

    async def _chat(self, prompt: str, max_tokens: int = 1024,
                    timeout: float = 60.0,
                    image_paths: Optional[List[str]] = None,
                    json_mode: bool = False,
                    cache_prefix: str = "") -> str:
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
            if cache_prefix:
                # The breakpoint sits at the END of the transcript block,
                # so everything before it — images included — is one
                # cached prefix shared by all five extractors of this
                # session. See _split_for_cache for the arithmetic.
                content: object = [
                    *imgs,
                    {"type": "text", "text": cache_prefix,
                     "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": prompt},
                ]
            elif imgs:
                content = [*imgs, {"type": "text", "text": prompt}]
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
            self._record_cache_usage(getattr(msg, "usage", None))
            return _flag_truncation(
                msg.content[0].text,
                getattr(msg, "stop_reason", None) == "max_tokens",
                max_tokens,
                self._model,
            )
        # OpenAI-compatible (OpenRouter / Ollama / LM Studio / ...).
        # Text-only — see docstring.
        #
        # Timeout: caller-supplied values (60–90s) were chosen for cloud
        # LLMs that return in <10s. Local inference on Ollama / LM Studio
        # has two costs the cloud doesn't: a 30–60s cold-load when the
        # model isn't already in VRAM, and 1–60 tok/sec generation that
        # scales with prompt length. A 12B model on a long meeting
        # transcript routinely needs minutes. Floor the timeout at 600s
        # (10 min) for local providers so a real generation isn't killed
        # mid-flight; still bounded so a genuine hang eventually fails.
        local_timeout = max(timeout, 600.0)
        # No prompt caching on the OpenAI-compat providers (local models,
        # OpenRouter): recombine into the single string they expect, in
        # the same order the Anthropic path sends it.
        combined = f"{cache_prefix}\n\n{prompt}" if cache_prefix else prompt
        kwargs = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": combined}],
        )
        # JSON mode: Gemini's OpenAI-compat endpoint (and most modern
        # servers) honor response_format to force a clean JSON object.
        # Older / smaller endpoints (some Ollama builds, a few OpenRouter
        # models) reject the param — so we try WITH it and fall back to a
        # plain call on any error rather than failing the whole tick.
        if json_mode:
            try:
                resp = await asyncio.wait_for(
                    self._openai_client.chat.completions.create(
                        **kwargs, response_format={"type": "json_object"}),
                    timeout=local_timeout,
                )
                return _flag_truncation(
                    resp.choices[0].message.content or "",
                    getattr(resp.choices[0], "finish_reason", None) == "length",
                    max_tokens,
                    self._model,
                )
            except Exception as e:
                logger.info(
                    "response_format=json_object rejected by %s (%s); "
                    "retrying without it", self._model, type(e).__name__)
        resp = await asyncio.wait_for(
            self._openai_client.chat.completions.create(**kwargs),
            timeout=local_timeout,
        )
        return _flag_truncation(
            resp.choices[0].message.content or "",
            getattr(resp.choices[0], "finish_reason", None) == "length",
            max_tokens,
            self._model,
        )

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
                         image_paths: Optional[List[str]] = None,
                         copilot_observations: str = "",
                         meeting_date: str = "") -> str:
        """
        Summarize a transcript against a caller-supplied prompt. The
        server-side templates service resolves the template name into
        a prompt so this class stays free of template storage concerns.
        `template_name` is accepted only for logging clarity.

        `image_paths` are screenshots the user captured during the
        meeting — passed to Claude as visual context so the summary can
        reference what was on screen (diagrams, dashboards, slides).

        `meeting_date` is the session's own start date (ISO date or
        datetime). It anchors relative time references in the
        transcript — see `core._precision.date_anchor`. Optional: "" drops
        the anchor half of the grounding rules (the no-invented-precision
        half ships either way).

        `copilot_observations` is the deduplicated bullet-list output
        from the live co-pilot ticks fired during the recording. It is
        the co-pilot MODEL'S OWN generated suggestions, not a record of
        anything a participant said, and the prompt below is explicit
        about that — see the provenance note there.
        """
        label = template_name or "custom"
        n_imgs = len(image_paths or [])
        logger.info(
            f"Requesting meeting summary (template={label}, "
            f"screenshots={n_imgs}, "
            f"copilot_observations={'yes' if copilot_observations.strip() else 'no'}, "
            f"meeting_date={meeting_date or 'unknown'}"
            f") via {self._provider}/{self._model}")
        instruction = prompt
        if n_imgs:
            instruction = (
                f"{prompt}\n\nThe user also attached {n_imgs} screenshot"
                f"{'s' if n_imgs != 1 else ''} captured during the meeting "
                f"(shown above). Use them as additional context — refer to "
                f"what they show where it sharpens the summary.")
        # Full block. This is the builder that produced every observed
        # instance: the invented year, the "seven candidate intents"
        # above a list of six, the "By end of meeting" on an item the
        # source never timed. Whatever the user's template says, the
        # grounding rules ride along after it — including for templates
        # the user wrote themselves, which is why this lives here and
        # not in services/template_service.py's DEFAULT_TEMPLATES.
        instruction = f"{instruction}{_grounding_rules(meeting_date)}"
        if copilot_observations.strip():
            # PROVENANCE. Everything below this line is generated text.
            #
            # Field repro (2026-08): a summary listed four "open routing
            # questions raised by co-pilot". Exactly ONE matched something
            # a participant actually asked; the other three appear nowhere
            # in the 43-minute transcript. They were the co-pilot's own
            # inventions, relayed to the reader as meeting content — in a
            # summary that gets forwarded to the customer.
            #
            # The prompt used to end with "prefer the co-pilot's phrasing
            # if it's clearer than the transcript evidence", which is
            # precisely the instruction that launders speculation into
            # record. "Do not invent details" was inert against it: the
            # invention happened upstream, in the co-pilot, and the
            # summarizer was being told to copy it out verbatim.
            #
            # The rule now is provenance, not tone: every factual claim in
            # the body of a summary must be traceable to the transcript.
            # Co-pilot output may direct ATTENTION; it may not become
            # CONTENT. Anything that survives on co-pilot authority alone
            # is quarantined in a section whose heading says so, placed
            # last, so a reader forwarding the summary cannot mistake a
            # generated question for one a participant asked.
            instruction = (
                f"{instruction}\n\n"
                "An AI co-pilot ran during this meeting. The lines below "
                "are THAT MODEL'S OWN SUGGESTIONS, generated live from "
                "partial transcript — questions it thought were worth "
                "asking, risks it thought were worth flagging, follow-ups "
                "it proposed. They are NOT a record of what anyone said. "
                "No participant necessarily raised any of them, and some "
                "of them are wrong.\n\n"
                "Treat them ONLY as a checklist pointing you at parts of "
                "the transcript you might otherwise skim past. Then:\n"
                "- A co-pilot line may inform the body of the summary "
                "ONLY where the transcript independently supports it. "
                "Verify it against the transcript first, every time.\n"
                "- Where it is supported, WRITE IT IN THE TRANSCRIPT'S "
                "OWN WORDS, at the transcript's level of detail. Follow "
                "the participants' phrasing, never the co-pilot's. Never "
                "carry over a name, number, system, date or specific "
                "that appears only in a co-pilot line.\n"
                "- A co-pilot line the transcript does NOT support must "
                "not appear anywhere that reads as a record of the "
                "meeting — not in the narrative, not among decisions, "
                "not among questions raised, not among action items.\n"
                "- If such a line is still worth the reader's time, put "
                "it at the very END of the summary, under exactly this "
                "heading:\n"
                "  ## Suggested follow-ups (AI-generated — not raised in "
                "the meeting)\n"
                "  Word each as a suggestion to the reader. Never "
                "attribute one to a participant. Omit the whole section "
                "if nothing qualifies; never pad it.\n"
                "- Never write that the co-pilot's suggestions were "
                "'raised', 'asked' or 'discussed' in the meeting.\n\n"
                "=== AI CO-PILOT SUGGESTIONS "
                "(machine-generated during the call — NOT meeting "
                "content, NOT said by anyone) ===\n"
                f"{copilot_observations.strip()}\n"
            )
        try:
            _cache_prefix, _tail = _split_for_cache(
                instruction, transcript, notes)
            summary = await self._chat(
                _tail, cache_prefix=_cache_prefix,
                max_tokens=self._budget(8192), timeout=180.0,
                image_paths=image_paths,
            )
            logger.info("Summary received.")
            return summary
        except Exception as e:
            raise RuntimeError(f"Summarization API call failed: {e}") from e

    async def coach_tick(
        self, segments: List[dict], meeting_name: str = "",
        custom_context: str = "",
        prior_ticks: Optional[List[dict]] = None,
        mode_name: str = "",
        mode_prompt: str = "",
        meeting_type_name: str = "",
        meeting_type_prompt: str = "",
        hot: bool = False,
        timeout_s: Optional[float] = None,
    ) -> dict:
        """In-call coaching tick. Given the last few minutes of live
        transcript segments, produce three short bullet lists:

          * clarifying_questions — what the user should ask to fill gaps
          * risks                — flags / assumptions the room hasn't
                                   acknowledged
          * follow_ups           — concrete next-step suggestions

        Each list is capped at three short bullets. Returns a dict with
        those three string-array keys. Tolerates a malformed model
        response by returning empty lists rather than raising — a coach
        tick is best-effort UX polish, not a critical-path call.

        Tight max_tokens (512) and a 20s timeout because this is called
        from a 45s frontend poll loop; a slow response would queue up.

        Prompt composition (three editable layers + one fixed):
          mode_prompt          — persona (SA / Sales / Executive)
          meeting_type_prompt  — meeting-type modifier (Discovery / SOW /
                                 Status / etc.) applied within the mode
          custom_context       — per-engagement pinned framing from Settings
          OUTPUT_RULES         — fixed JSON schema; appended here so the
                                 panel's parsing contract is guaranteed
                                 regardless of how the user edits the
                                 mode/meeting-type prompts

        Backwards-compatible: when mode_prompt / meeting_type_prompt
        come in empty (older callers / un-migrated state), we fall
        through to a sensible SA + General default baked into this
        method so coaching keeps working."""
        if not segments:
            return {"clarifying_questions": [], "risks": [], "follow_ups": []}

        lines = []
        for s in segments[-200:]:
            speaker = (s.get("speaker") or "?").strip()
            text = (s.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"[{speaker}] {text}")
        transcript = "\n".join(lines)
        if not transcript:
            return {"clarifying_questions": [], "risks": [], "follow_ups": []}

        header = (
            f"Meeting: {meeting_name}\n\n"
            if meeting_name else ""
        )

        # Optional custom context the SA pinned in Settings — per-
        # engagement framing the mode + meeting-type prompts can't
        # anticipate.
        custom_block = ""
        if custom_context and custom_context.strip():
            custom_block = (
                "\n\nADDITIONAL CONTEXT THE USER PINNED FOR THIS ENGAGEMENT "
                "(weight this heavily — treat as authoritative role and "
                "topic framing):\n"
                f"{custom_context.strip()}\n"
            )

        # Memory across ticks: the LAST tick's outputs get inlined so the
        # model can build on prior coaching instead of repeating it.
        # Bounded to the most recent tick only because each tick already
        # fits in a tight context window — older ticks would crowd out
        # the actual transcript.
        prior_block = ""
        if prior_ticks:
            last = prior_ticks[-1]
            prior_lines = []
            for key, label in (
                ("clarifying_questions", "asked about"),
                ("risks", "flagged"),
                ("follow_ups", "suggested following up on"),
            ):
                items = last.get(key) or []
                if items:
                    prior_lines.append(
                        f"  Already {label}: " + "; ".join(items[:3])
                    )
            if prior_lines:
                prior_block = (
                    "\n\nIN THE LAST TICK YOU ALREADY SAID:\n"
                    + "\n".join(prior_lines)
                    + "\nDo not repeat these. Build on them — note what's "
                    "evolved, what's been answered, what's gone unaddressed.\n"
                )

        # Compose mode + meeting type. Either layer empty falls through
        # to a baked-in default — keeps older callers (and broken
        # service state) from blowing up coaching.
        mode_block = (mode_prompt or "").strip() or _FALLBACK_MODE_PROMPT
        meeting_type_block = (meeting_type_prompt or "").strip()
        type_label = (meeting_type_name or "").strip() or "General"
        if not meeting_type_block:
            meeting_type_block = _FALLBACK_MEETING_TYPE_PROMPT

        output_rules = _COACH_OUTPUT_RULES_HOT if hot else _COACH_OUTPUT_RULES
        prompt = (
            f"{mode_block}\n\n"
            f"This meeting is a **{type_label}**. {meeting_type_block}\n\n"
            f"{output_rules}\n"
            f"{custom_block}"
            f"{prior_block}\n"
            f"{header}Recent transcript:\n{transcript}"
        )

        try:
            # Hot ticks have a tighter budget — narrower window, more
            # frequent firing, much higher chance the answer is empty.
            # Wide ticks keep the original 512-token budget.
            # Token budget. Anthropic returns visible text directly, so a
            # tight budget is fine. OpenAI-compat "thinking" models —
            # Gemini 2.5 in particular — spend hidden reasoning tokens
            # against max_tokens, so a small budget makes them return
            # EMPTY content (the "connection test works but the panel
            # stays blank" bug). Give non-Anthropic providers far more
            # headroom so the JSON survives after the reasoning.
            if self._provider == "anthropic":
                tokens = 256 if hot else 512
            else:
                tokens = 640 if hot else 1500
            # Timeout: caller may pass an interval-aware value (kept under
            # the poll cadence so ticks never overlap). Falls back to a
            # provider-aware default — local models (Ollama) are slower
            # than cloud, so give non-Anthropic providers more room before
            # we call it a timeout.
            if timeout_s is None:
                if self._provider == "anthropic":
                    timeout_s = 10.0 if hot else 20.0
                else:
                    timeout_s = 15.0 if hot else 35.0
            # json_mode forces a clean JSON object on providers that
            # support it (Gemini, most OpenAI-compat) — best-effort with
            # a plain-call fallback inside _chat.
            raw = await self._chat(
                prompt, max_tokens=tokens, timeout=timeout_s, json_mode=True)
        except Exception as e:
            # Include the exception type because some exceptions
            # (asyncio.TimeoutError in particular) have an empty __str__,
            # which made earlier logs look like "coach_tick chat call
            # failed:" with nothing after the colon. Useless for support.
            kind = type(e).__name__
            logger.warning(f"coach_tick chat call failed: {kind}: {e}")
            # Surface the failure so the panel can show WHY it went quiet
            # instead of looking like the meeting simply had nothing worth
            # flagging. The frontend maps these codes to a friendly line.
            if "Timeout" in kind:
                err = "timeout"
            elif "Connection" in kind or "APIConnection" in kind:
                err = "unreachable"
            else:
                err = "error"
            return {
                "clarifying_questions": [], "risks": [], "follow_ups": [],
                "error": err, "error_detail": f"{kind}: {e}"[:200],
            }

        parsed = _coerce_json(raw)
        if parsed is None:
            # Non-empty-but-unparseable OR empty output. This is the
            # Gemini-2.5 "reasoning ate the token budget → empty content"
            # case, or a model that ignored the JSON contract. Surface it
            # so the panel shows WHY it's quiet instead of looking like a
            # meeting with nothing worth flagging.
            detail = ("empty response" if not (raw or "").strip()
                      else "unparseable (non-JSON) response")
            logger.warning(
                "coach_tick: %s from %s/%s; raw[:200]=%r",
                detail, self._provider, self._model, (raw or "")[:200])
            return {
                "clarifying_questions": [], "risks": [], "follow_ups": [],
                "error": "no_output",
                "error_detail": (
                    f"Model returned {detail}. If this is a Gemini/thinking "
                    f"model, its reasoning may be exhausting the token "
                    f"budget — try a different model."),
            }

        def _clean(key: str) -> List[str]:
            items = parsed.get(key)
            if not isinstance(items, list):
                return []
            out: List[str] = []
            for item in items:
                if not isinstance(item, str):
                    continue
                t = item.strip()
                if t:
                    out.append(t[:200])
                if len(out) == 3:
                    break
            return out

        # Cross-tick dedup + filler removal. The field failure mode was
        # the model re-emitting "vendor lock-in" / "request an update"
        # every tick; drop anything that near-duplicates something from
        # ANY prior tick this meeting, and strip generic process-filler.
        prior_all = {"clarifying_questions": [], "risks": [], "follow_ups": []}
        for t in (prior_ticks or []):
            for k in prior_all:
                prior_all[k].extend(t.get(k) or [])

        cq = _dedup_against(_clean("clarifying_questions"),
                            prior_all["clarifying_questions"])
        rk = _dedup_against(_clean("risks"), prior_all["risks"])
        fu = _dedup_against(_clean("follow_ups"), prior_all["follow_ups"])

        # Total-output backstop: the prompt asks for ≤2 items total, but
        # enforce a hard cap of 3 so a misbehaving model can't firehose
        # the panel. Priority order: questions → risks → follow-ups.
        result = {"clarifying_questions": [], "risks": [], "follow_ups": []}
        budget = 3
        for key, items in (("clarifying_questions", cq),
                           ("risks", rk), ("follow_ups", fu)):
            take = items[:max(0, budget)]
            result[key] = take
            budget -= len(take)
        return result

    async def extract_action_items(
        self, transcript: str, notes: str = "",
        image_paths: Optional[List[str]] = None,
        meeting_date: str = "",
    ) -> str:
        n_imgs = len(image_paths or [])
        logger.info(
            f"Extracting action items (screenshots={n_imgs}) "
            f"via {self._provider}/{self._model}")
        instruction = (
            "Analyze this meeting transcript and extract the following "
            "in clearly structured markdown:\n\n"
            "## Action Items\n"
            "List each action item with: who is responsible, what they "
            "need to do, and by when (if mentioned). Use checkboxes.\n"
            "Format: - [ ] **[Owner]**: Action description (Due: date if mentioned)\n\n"
            # NO DECISIONS SECTION HERE, ON PURPOSE.
            #
            # This prompt used to ask for "## Decisions Made" as well,
            # while extract_decisions independently asked for every
            # decision in the same meeting. Two independent extractions
            # of one meeting do not agree: a field pair (2026-08-21)
            # produced 11 decisions in the action-items blob and 14 in
            # the decisions file, overlapping but neither a subset of
            # the other. The user sees both and cannot tell which is
            # real — and the count is what made the output feel
            # untrustworthy.
            #
            # Decisions have ONE extractor and one file.
            "## Open Questions\n"
            "List questions that were raised but not resolved.\n\n"
            "If a section has no items, write 'None identified.'\n\n"
            "If the USER NOTES mention action items the user has committed "
            "to (things like 'need to follow up on X', 'reminder to send Y'), "
            "include those as action items owned by the user."
            + _visual_instruction(n_imgs)
            # Full block. An action item is where a manufactured
            # deadline lands ("By end of meeting" on an item the source
            # never timed), and the "Due: date if mentioned" format
            # above is an open invitation to fill the slot.
            + _grounding_rules(meeting_date)
        )
        try:
            _cache_prefix, _tail = _split_for_cache(
                instruction, transcript, notes)
            result = await self._chat(
                _tail, cache_prefix=_cache_prefix,
                max_tokens=self._budget(8192), timeout=120.0,
                image_paths=image_paths,
            )
            logger.info("Action items extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Action items extraction failed: {e}") from e

    async def extract_decisions(
        self, transcript: str, notes: str = "",
        image_paths: Optional[List[str]] = None,
        meeting_date: str = "",
    ) -> str:
        """Extract decisions made with rationale — an auto-generated ADR log."""
        n_imgs = len(image_paths or [])
        logger.info(
            f"Extracting decisions (screenshots={n_imgs}) "
            f"via {self._provider}/{self._model}")
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
            "- **Evidence:** a short quote from the transcript showing "
            "the decision being made\n\n"
            "WHAT COUNTS AS A DECISION. A decision is a choice made "
            "DURING this meeting that changes what will happen. If you "
            "cannot quote the moment it was made, it is not one.\n\n"
            "These are NOT decisions — do not emit them:\n"
            "- A task, commitment or next step ('X will send Y', 'X will "
            "build the model', 'X will set up the call'). Those are "
            "action items and are extracted separately. A decision is "
            "WHAT was chosen; an action item is WHO does what next.\n"
            "- Something already true or already agreed BEFORE this "
            "meeting that a participant merely reported or restated. A "
            "roadmap someone describes is context, not a decision made "
            "here.\n"
            "- A fact, date or contract term ('the contract expires in "
            "September'). Nobody decided it in the meeting.\n"
            "- A recommendation, opinion or capability someone described "
            "that was not explicitly accepted.\n"
            "- A restatement of a decision you already listed. If two "
            "entries describe the same choice from different angles, "
            "emit ONE.\n\n"
            "Most meetings produce a HANDFUL of real decisions. A long "
            "list is a sign that action items, context and facts have "
            "been miscounted as decisions. Prefer precision: a missing "
            "borderline entry costs far less than a list nobody trusts.\n\n"
            "Only include decisions that were actually MADE, not just "
            "discussed. Skip discussions without conclusions. If the USER "
            "NOTES record additional decisions made off-audio (in hallway "
            "chat, private chat, follow-up email), include those too and "
            "annotate with **Source:** user notes.\n\n"
            "If no decisions were made, write: 'No decisions made in this "
            "meeting.'"
            + _visual_instruction(n_imgs)
            # Full block. "Alternatives considered" and "Impact" are
            # exactly the fields a model pads with plausible options
            # nobody named, and "Owner" is an attribution slot.
            + _grounding_rules(meeting_date)
        )
        try:
            _cache_prefix, _tail = _split_for_cache(
                instruction, transcript, notes)
            result = await self._chat(
                _tail, cache_prefix=_cache_prefix,
                max_tokens=self._budget(8192), timeout=120.0,
                image_paths=image_paths,
            )
            logger.info("Decisions extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Decisions extraction failed: {e}") from e

    async def extract_structured(
        self, transcript: str, notes: str = "",
        image_paths: Optional[List[str]] = None,
        meeting_date: str = "",
    ) -> dict:
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
        n_imgs = len(image_paths or [])
        logger.info(
            f"Extracting structured records (screenshots={n_imgs}) "
            f"via {self._provider}/{self._model}")
        visuals_hint = _visual_instruction(n_imgs)
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
            '"source": "transcript|notes"}],\n'
            '  "defects": [{"title": str, "ref": str, "severity": '
            '"critical|high|medium|low", "status": '
            '"open|in_progress|fixed|retest|closed|deferred|rejected", '
            '"owner": str, "due": str, "disposition": '
            '"undetermined|in_scope|out_of_scope|change_request|'
            'working_as_designed", "source": "transcript|notes"}]\n'
            "}\n"
            "Rules: include only items that genuinely occurred — empty "
            "arrays are correct when nothing applies, never invent. "
            "Decisions = actually agreed, not merely discussed. "
            "DEFECTS are specifically things reported as BROKEN in a "
            "built system — a bug, a failed test, a defect ID under "
            "triage. A task someone agreed to do is an action_item, not "
            "a defect; a capability the system lacks is a requirement, "
            "not a defect. Most meetings have no defects at all and "
            "must return []. Only set \"ref\" to an identifier actually "
            "spoken (e.g. a ticket number); never invent one. Only set "
            "\"disposition\" when the scope question was actually "
            "argued, otherwise \"undetermined\". If the "
            "USER NOTES record items decided/committed off-audio, include "
            'them with "source":"notes". Use "" for unknown string '
            'fields, [] for empty arrays. Output nothing but the JSON.'
            + visuals_hint
            # Full block. The typed records feed the engagement layer,
            # so an invented owner or requirement doesn't just read
            # wrong — it becomes a row somebody works from.
            + _grounding_rules(meeting_date)
            # Both the anchor and the general TIMINGS clause tell prose
            # extractors to SAY the timing was unspecified. This path
            # has no prose — an unknown "due" is "", and a guessed one
            # is exactly the invented-precision failure they exist to
            # stop. Unconditional now: the timing rule ships whether or
            # not we know the meeting's date, so the reconciliation has
            # to ship with it (it used to be gated on having a date,
            # because the anchor was).
            + _json_timing_note("due")
        )
        _cache_prefix, prompt = _split_for_cache(instruction, transcript, notes)
        try:
            raw = await self._chat(
                prompt, cache_prefix=_cache_prefix,
                max_tokens=self._budget(4096), timeout=120.0,
                image_paths=image_paths,
            )
            parsed = _coerce_json(raw)
            if parsed is None:
                # One repair pass — weak models often wrap JSON in prose
                # or fences despite instructions. Repair pass doesn't need
                # the image context — we're cleaning up text output.
                repair = (
                    "Return ONLY the JSON object from your previous answer, "
                    "valid and parseable, with no surrounding text or "
                    "code fences:\n\n" + raw
                )
                raw = await self._chat(repair, max_tokens=self._budget(4096), timeout=120.0)
                parsed = _coerce_json(raw)
            if parsed is None:
                # Log a truncated preview of what the model actually
                # produced so future debugging doesn't require reproducing
                # the failure to see what went wrong. Earlier this raised
                # without surfacing the raw output, making the partial
                # tracebacks in backend.log uninformative.
                preview = (raw or "").strip().replace("\n", " ")[:300]
                logger.warning(
                    f"extract_structured: model returned unparseable "
                    f"output (preview: {preview!r})")
                raise RuntimeError("model did not return parseable JSON")
            logger.info("Structured records extracted.")
            return {
                "requirements": parsed.get("requirements") or [],
                "decisions": parsed.get("decisions") or [],
                "action_items": parsed.get("action_items") or [],
                "open_questions": parsed.get("open_questions") or [],
                "defects": parsed.get("defects") or [],
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
        user_context: str = "",
        document_notes: str = "",
        today_iso: str = "",
    ) -> str:
        """Richer prep brief used by the click-from-calendar-tile flow.
        Includes hot-topics + suggested-questions sections and asks
        Claude to inline `[session_id]` citations the frontend turns
        into click-to-jump links.

        The simpler meeting_prep_brief() (string-only context) stays
        for the existing /prep-brief endpoint; this one is its
        structurally-richer cousin that knows about a specific
        upcoming meeting.

        `document_notes` is the retrieved Knowledge-Folder context
        (see services/prep_brief_context.py) — `### [DOC: <name>]`
        blocks of excerpts from this client's SOWs / requirements /
        notes. It is optional and defaults to "": when empty, the
        prompt emitted here is byte-for-byte the prompt that was
        emitted before documents existed, so a client with no
        Knowledge Folder gets exactly the brief they got before.

        Attribution matters more here than anywhere else in the app.
        "The SOW says the cutover is in October" and "they said on the
        last call the cutover is in October" are different claims with
        different authority, so when documents are present the prompt
        gains an explicit rule: document-sourced material is cited
        `[DOC: <file name>]`, meeting-sourced material `[session_id]`,
        and the two are never blended into one unattributed claim.
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
        # Optional user-provided context — anchored with explicit framing
        # so the model treats it as authoritative rather than background.
        user_context_block = ""
        if user_context.strip():
            user_context_block = (
                "\n\n=== ADDITIONAL CONTEXT FROM THE USER ===\n"
                "Treat this as authoritative — the user knows things the "
                "calendar invite and prior meetings don't capture. Weave "
                "it into the appropriate brief sections and prioritize "
                "questions / discussion points around it.\n"
                f"{user_context.strip()}\n"
            )
        # Retrieved Knowledge-Folder excerpts. Both the rule and the
        # block are conditional on there being documents, so the
        # zero-document prompt is unchanged from before this feature.
        document_notes = (document_notes or "").strip()
        doc_rule = ""
        document_block = ""
        if document_notes:
            doc_rule = (
                "Some context below comes from this client's Knowledge "
                "Folder — written documents (SOWs, requirements, notes), "
                "NOT things anyone said in a meeting. Keep the two "
                "apart: a document records what was *contracted or "
                "written down*, a meeting records what was *said*, and "
                "they carry different authority. Cite a document "
                "inline as `[DOC: <file name>]` using the literal file "
                "name from its header, exactly as you cite a meeting "
                "with `[id]`. Never attribute a document's contents to "
                "a meeting or vice versa, never merge them into one "
                "unattributed claim, and when a document and a meeting "
                "disagree, say so explicitly and cite both.\n\n"
            )
            document_block = (
                "\n\n=== CLIENT KNOWLEDGE FOLDER — DOCUMENT EXCERPTS ===\n"
                "Excerpts retrieved from documents in this client's "
                "Knowledge Folder, ranked by relevance to this meeting. "
                "Cite each as `[DOC: <file name>]`.\n\n"
                f"{document_notes}"
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
                    f"{doc_rule}"
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
                    "earn its place. Trim ruthlessly.\n"
                    # Full block. This brief is read minutes before the
                    # SA walks into the meeting and is the least
                    # verifiable artifact in the app — the reader is
                    # not going to re-listen to five prior calls to
                    # check an invented commitment or owner.
                    f"{_grounding_rules(today_iso, kind='today')}\n"
                    f"=== UPCOMING MEETING ===\n"
                    f"Subject: {upcoming_subject}\n"
                    f"When: {upcoming_when}\n"
                    f"Attendees: {attendee_blob}\n"
                    f"Account: {scope_blob}"
                    f"{agenda_block}"
                    f"{user_context_block}\n\n"
                    f"=== PRIOR MEETING NOTES ===\n{prior_notes}"
                    f"{document_block}"
                ),
                max_tokens=self._budget(4096), timeout=120.0,
            )
            logger.info("Calendar-based prep brief generated.")
            return result
        except Exception as e:
            raise RuntimeError(f"Prep brief generation failed: {e}") from e

    async def meeting_prep_brief(
        self, prior_notes: str, upcoming_subject: str,
        user_context: str = "",
        document_notes: str = "",
        today_iso: str = "",
    ) -> str:
        """Generate a prep brief from prior meeting notes for an upcoming meeting.

        user_context is free-text the SA types in to feed the LLM extra
        situational context — exec asks, customer mood, specific agenda
        items, redlines just received, etc. It's treated as authoritative
        (the SA knows things the meeting history doesn't capture) so the
        prompt instructs the model to weave it in explicitly.

        document_notes is retrieved Knowledge-Folder context — see
        meeting_prep_brief_from_calendar's docstring for the attribution
        rules. Defaults to "", in which case the prompt is identical to
        the pre-documents prompt."""
        logger.info(f"Generating prep brief for: {upcoming_subject} via {self._provider}/{self._model}")
        # Optional user-provided context block. Anchored with a heading
        # the prompt explicitly tells the model to honor; without that
        # framing the model treats it as background and underweights it.
        user_block = ""
        if user_context.strip():
            user_block = (
                "\n\n=== ADDITIONAL CONTEXT FROM THE USER ===\n"
                "Treat this as authoritative — the user knows things the "
                "meeting history below does not capture. Weave it into "
                "the appropriate brief sections and call it out under "
                "'Suggested Discussion Points' when actionable.\n"
                f"{user_context.strip()}\n"
            )
        # Knowledge-Folder excerpts, when the client has an indexed
        # folder. Conditional so the no-documents prompt is unchanged.
        document_notes = (document_notes or "").strip()
        doc_rule = ""
        document_block = ""
        if document_notes:
            doc_rule = (
                "Some of the context below comes from this client's "
                "Knowledge Folder — written documents (SOWs, requirements, "
                "notes), not things anyone said in a meeting. A document "
                "records what was contracted or written down; a meeting "
                "records what was said. Keep them distinct: cite a "
                "document inline as `[DOC: <file name>]` using the "
                "literal file name from its header, never attribute a "
                "document's contents to a meeting or vice versa, and when "
                "the two disagree, say so and cite both.\n\n"
            )
            document_block = (
                "\n\n=== CLIENT KNOWLEDGE FOLDER — DOCUMENT EXCERPTS ===\n"
                "Excerpts retrieved from documents in this client's "
                "Knowledge Folder, ranked by relevance to this meeting. "
                "Cite each as `[DOC: <file name>]`.\n\n"
                f"{document_notes}"
            )
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
                    "write 'None.'\n"
                    # Full block — same reasoning as the calendar-based
                    # cousin above.
                    f"{_grounding_rules(today_iso, kind='today')}\n"
                    f"{doc_rule}"
                    f"Upcoming meeting: {upcoming_subject}"
                    f"{user_block}\n\n"
                    f"=== PRIOR MEETING NOTES ===\n{prior_notes}"
                    f"{document_block}"
                ),
                max_tokens=self._budget(4096), timeout=120.0,
            )
            logger.info("Prep brief generated.")
            return result
        except Exception as e:
            raise RuntimeError(f"Prep brief generation failed: {e}") from e

    async def extract_requirements(
        self, transcript: str, notes: str = "",
        image_paths: Optional[List[str]] = None,
        meeting_date: str = "",
    ) -> str:
        n_imgs = len(image_paths or [])
        logger.info(
            f"Extracting requirements (screenshots={n_imgs}) "
            f"via {self._provider}/{self._model}")
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
            "WHAT COUNTS AS A REQUIREMENT. Something the solution MUST "
            "do or satisfy, stated by someone in the meeting. These are "
            "NOT requirements — do not emit them:\n"
            "- A task or next step someone committed to (that is an "
            "action item, extracted separately).\n"
            "- A decision about scope or sequencing (extracted "
            "separately).\n"
            "- A capability a vendor described, unless the customer "
            "stated they need it.\n"
            "- Background, history or a restatement of a row you "
            "already emitted. One row per requirement.\n\n"
            "A discovery call typically surfaces a modest number of real "
            "requirements. A long table is a sign that discussion has "
            "been miscounted as requirements — every row looks equally "
            "authoritative once it has an ID, so precision matters more "
            "than coverage here.\n\n"
            "Assign priority based on context clues (urgency, emphasis, "
            "stakeholder tone). If the USER NOTES list additional requirements "
            "or constraints the transcript doesn't capture, include those — "
            "annotate their source in the Owner column as 'user notes'.\n"
            "If a section has no items, write 'None identified.'"
            + _visual_instruction(n_imgs)
            # Full block. The output is ID'd tables (FR-001, NFR-001),
            # which is precision-shaped by construction: every row
            # looks equally authoritative whether or not anyone said it.
            + _grounding_rules(meeting_date)
        )
        try:
            _cache_prefix, _tail = _split_for_cache(
                instruction, transcript, notes)
            result = await self._chat(
                _tail, cache_prefix=_cache_prefix,
                max_tokens=self._budget(8192), timeout=120.0,
                image_paths=image_paths,
            )
            logger.info("Requirements extracted.")
            return result
        except Exception as e:
            raise RuntimeError(f"Requirements extraction failed: {e}") from e

    async def identify_speakers(
        self, transcript: str,
        attendees: Optional[Sequence[str]] = None,
        organizer: str = "",
    ) -> Dict[str, str]:
        """Map diarized speaker IDs to real names.

        `attendees` / `organizer` are the CALENDAR INVITE — ground truth
        for who was in the room, and the candidate set a heard first
        name is allowed to resolve to. Both are optional and additive:
        an invite-less session (or one whose attendee entries yield no
        usable name) produces a byte-identical prompt to the pre-roster
        one, so nothing changes for sessions never started from a
        calendar entry. See core/speaker_roster.py for how an address
        becomes a display name and which forms deliberately yield
        nothing.
        """
        roster = roster_names(attendees, organizer)
        logger.info(f"Requesting speaker identification via {self._provider}/{self._model}")
        if roster:
            logger.info(f"Speaker ID roster: {len(roster)} invited name(s)")
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
                    # The invite roster, when there is one. Empty
                    # string otherwise — and that emptiness is checked
                    # byte-for-byte by test_speaker_roster.py, because
                    # a session with no attendees must get exactly the
                    # prompt it got before this feature existed.
                    + roster_block(roster)
                    # DELIBERATELY PARTIAL. The output is a JSON map of
                    # speaker labels to names: there is no list to
                    # enumerate and no count to state, and no date,
                    # deadline or timing appears anywhere in it — the
                    # COUNTS and TIMINGS clauses would be instructions
                    # about surfaces this builder cannot produce, and
                    # padding a 1024-token call with them buys nothing.
                    # What DOES apply is the whole failure mode here:
                    # supplying a plausible name for a speaker nobody
                    # named (IDENTIFIERS) and pinning a name to the
                    # wrong turn (ATTRIBUTION).
                    + _no_invented_precision(counts=False, timing=False)
                    + f"\nTranscript:\n{transcript}"
                ),
                max_tokens=self._budget(1024), timeout=60.0,
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

    async def parse_daily_briefing(self, raw_text: str,
                                   today_iso: str = "") -> dict:
        """Parse the free-form daily briefing the user pastes from
        their Microsoft 365 Copilot scheduled prompt into the
        structured JSON the Today view consumes.

        The user's prompt output varies day to day (Copilot does not
        emit a fixed schema), so we hand the LLM the whole blob and ask
        for canonical fields. Returns a dict matching the shape
        DailyBriefingService._normalize_briefing expects; the service
        layer fills in ids / defaults / timestamps and merges existing
        done-state on import.

        Output schema:
          top_priority: { title, detail, why } | null
          needs_response: [{ title, detail, who, due, source }]
          agenda: [{ title, time, start_iso, end_iso, join_url,
                     duration, role, meeting_type,
                     client, attendees[], notes, status }]
          schedule_notes: [str]
          fyi: [{ title, detail, category }]
          greeting: str

        ``start_iso`` / ``end_iso`` / ``join_url`` were added so the
        Chrome-extension import can become a SECOND SOURCE for the
        Record tab's Upcoming Meetings list. The pre-existing ``time``
        field is a *display* string ("9:30 AM") with no date and no
        timezone — enough to render a row on the Today tab, useless for
        placing an event on a timeline or deduping it against a local
        Outlook invite (field report 2026-08-11). ``time`` is kept
        unchanged; the ISO fields are additive, and
        services/extension_calendar_service.py falls back to
        date + ``time`` + ``duration`` whenever the model omits them.

        ``today_iso`` (YYYY-MM-DD) anchors the date the model should
        assume for a bare clock time. Defaults to the local date.

        `meeting_type` values should align with the meeting-type tags
        used elsewhere in the app: discovery, sow, status, technical,
        demo, internal, general. `role` is one of host, attendee,
        optional.

        Tolerates malformed model output by returning a minimal
        skeleton — Today view degrades to "no items" rather than the
        whole import failing.
        """
        text = (raw_text or "").strip()
        if not text:
            return {
                "top_priority": None, "needs_response": [],
                "agenda": [], "schedule_notes": [], "fyi": [],
                "greeting": "",
            }

        logger.info(
            f"Parsing daily briefing ({len(text)} chars) "
            f"via {self._provider}/{self._model}")

        anchor_date = (today_iso or "").strip()
        if not anchor_date:
            from datetime import date as _date_cls
            anchor_date = _date_cls.today().isoformat()

        instruction = (
            "You are parsing a daily briefing the user receives every "
            "morning from a Microsoft 365 Copilot scheduled prompt. "
            "The format is conversational markdown — sections are not "
            "guaranteed. Extract the canonical structure below.\n\n"
            "Return ONLY a JSON object (no prose, no code fences) with "
            "these keys:\n\n"
            "{\n"
            '  "greeting": "short greeting line if present, else empty",\n'
            '  "top_priority": {\n'
            '    "title": "the single most important thing today",\n'
            '    "detail": "1-2 sentences of context",\n'
            '    "why": "why it matters today specifically"\n'
            '  },\n'
            '  "needs_response": [\n'
            '    { "title": "...", "detail": "...", "who": "person/team",\n'
            '      "due": "today / EOD / Friday / etc", "source": "email / Teams / etc" }\n'
            '  ],\n'
            '  "agenda": [\n'
            '    { "title": "meeting title", "time": "9:30 AM",\n'
            '      "start_iso": "YYYY-MM-DDTHH:MM:SS", "end_iso": "YYYY-MM-DDTHH:MM:SS",\n'
            '      "join_url": "Teams/Zoom/Meet link if one is visible, else empty",\n'
            '      "duration": "30 min", "role": "host | attendee | optional",\n'
            '      "meeting_type": "discovery | sow | status | technical | demo | internal | general",\n'
            '      "client": "client name if applicable, else empty",\n'
            '      "attendees": ["names"], "notes": "prep note if any",\n'
            '      "status": "scheduled | cancelled | now" }\n'
            '  ],\n'
            '  "schedule_notes": ["heads-ups about the day shape — conflicts, '
            'overruns, packed afternoons"],\n'
            '  "fyi": [\n'
            '    { "title": "headline", "detail": "1-2 sentences",\n'
            '      "category": "market | client | internal | personal" }\n'
            '  ]\n'
            "}\n\n"
            "Rules:\n"
            "- If a section is absent, return an empty array (or null for "
            "top_priority). Never invent items.\n"
            "- Keep titles short (under 100 chars). Detail under 400 chars.\n"
            "- For agenda items the briefing flags as cancelled, set "
            'status to "cancelled" — keep the entry so the user sees the '
            "schedule change.\n"
            "- Meeting type is your best guess from the title/context "
            "(e.g. 'Discovery Call' -> discovery, 'Weekly Sync' -> status, "
            "'SOW Review' -> sow, 'AWS Demo' -> demo). Default 'general'.\n"
            "- Role: 'host' if user is running the meeting, 'attendee' "
            "for normal participation, 'optional' if briefing says so.\n"
            "- Do not include any text outside the JSON object.\n"
            "- BE GENEROUS WITH needs_response. The input may contain "
            "labeled sections from the Outlook Focused Inbox and from "
            "Teams Chat — these are the canonical 'someone is waiting "
            "on me' surfaces. For each email/chat thread that has the "
            "user as a recipient and is unanswered, emit a needs_response "
            "item with: title = the subject/topic, who = sender name, "
            "due = explicit deadline if present else 'today' for emails "
            "less than 24h old, source = 'email' or 'Teams chat'. Don't "
            "skip them just because the briefing text doesn't say "
            "'needs response' literally — your job is to identify which "
            "items NEED a response based on content.\n"
            "- For agenda items, do not omit cancelled / declined ones — "
            "the user needs to see the schedule change to confirm.\n"
            f"- TODAY'S DATE IS {anchor_date} (local time). For every "
            "agenda item also emit start_iso and end_iso as LOCAL "
            "wall-clock ISO 8601 with NO timezone suffix "
            "(e.g. \"2026-08-11T09:30:00\"). Use today's date unless the "
            "text clearly says otherwise. If you genuinely cannot tell "
            "the time, emit empty strings — do NOT guess a time. These "
            "fields place the meeting on the Record tab's timeline, so a "
            "wrong timestamp is worse than a missing one.\n"
            "- Never state a year, month or day that is not either in "
            "the briefing text or derived from today's date above. A "
            "relative reference (\"Friday\", \"next week\", \"come "
            "October\") resolves FORWARD from today's date, never to an "
            "earlier one.\n"
            "- join_url: copy a Teams / Zoom / Google Meet URL verbatim "
            "if one appears with the meeting; empty string otherwise. "
            "Never construct one.\n"
            # Full block + the JSON reconciliation. This parse is the
            # LLM step that puts events on the Record tab's timeline
            # (services/extension_calendar_service.py consumes the dict
            # it returns), so an invented time here is not a wrong
            # sentence — it is a wrong meeting on the user's calendar.
            # The per-field rules above already forbid a guessed clock
            # time and a constructed join_url; the shared block is what
            # covers the fields nobody thought to enumerate.
            + _no_invented_precision()
            + _json_timing_note("due")
            + "\n\n=== BRIEFING TEXT ===\n"
            f"{text}\n"
            "=== END BRIEFING TEXT ==="
        )

        try:
            raw = await self._chat(instruction, max_tokens=self._budget(4096), timeout=120.0)
        except Exception as e:
            logger.warning(
                f"parse_daily_briefing chat failed: {type(e).__name__}: {e}")
            raise RuntimeError(f"Briefing parse failed: {e}") from e

        parsed = _coerce_json(raw) or {}
        if not isinstance(parsed, dict):
            logger.warning("Briefing parse returned non-dict; using skeleton")
            parsed = {}

        # Defensive shape: caller will normalize, but guarantee the
        # top-level keys exist so the normalizer's _list_of_dicts comp-
        # rehensions never KeyError.
        skeleton = {
            "greeting": "", "top_priority": None,
            "needs_response": [], "agenda": [],
            "schedule_notes": [], "fyi": [],
        }
        for k, default in skeleton.items():
            if k not in parsed:
                parsed[k] = default

        logger.info(
            f"Briefing parsed: agenda={len(parsed.get('agenda') or [])}, "
            f"needs_response={len(parsed.get('needs_response') or [])}, "
            f"fyi={len(parsed.get('fyi') or [])}"
        )
        return parsed