"""
Domain terminology — biases transcription toward the user's jargon and
fixes known mis-hears after the fact.

The SA works in one of the most acronym- and proper-noun-dense domains
there is (Amazon Connect, Genesys, CCaaS, MEDDIC, ...). Stock Whisper
mangles these constantly — "Genesys" → "Genesis", "UCCX" → "you see
ex", "CCaaS" → "see-cass" — and every mistranscription then poisons the
downstream summary / action-item / decision extraction.

Two mechanisms, both fed from one editable glossary:

  1. **initial_prompt bias** — faster-whisper accepts an `initial_prompt`
     string that conditions the decoder toward those tokens. We build it
     from the canonical term list. Cheap, no extra pass, and especially
     good for proper nouns the model has simply never seen.

  2. **post-transcription correction** — a dict of {wrong → canonical}
     applied with word-boundary, case-insensitive regex after
     transcription. Catches the specific, repeatable mis-hears the bias
     prompt doesn't fully fix.

Storage / atomic-write / seed-and-reset semantics mirror
CoPilotModeService so behavior is predictable for the user.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, List

from utils.logger import get_logger

logger = get_logger(__name__)


# Canonical terms — fed into Whisper's initial_prompt to bias decoding.
# Curated for a [scrubbed] Digital Solutions / Cloud / Enterprise Architect
# working Amazon Connect + CCaaS migrations, plus the sales vocabulary
# that shows up in pre-sales calls.
DEFAULT_TERMS: List[str] = [
    # AWS / cloud
    "Amazon Connect", "AWS", "Amazon Web Services", "AWS Lambda",
    "Amazon Lex", "Amazon Bedrock", "Amazon Q", "Amazon Q in Connect",
    "Contact Lens", "Amazon Polly", "Amazon Transcribe",
    "Amazon Comprehend", "Amazon Kinesis", "Amazon S3", "DynamoDB",
    "Amazon CloudFront", "Amazon API Gateway", "AWS IAM",
    "Amazon Cognito", "Amazon EventBridge", "AWS Step Functions",
    "Amazon CloudWatch", "Amazon Route 53", "AWS CloudFormation",
    "AWS CDK", "Terraform", "Customer Profiles", "Voice ID",
    "Amazon Connect Cases",
    # CCaaS / contact center
    "CCaaS", "Genesys", "Genesys Cloud", "NICE", "NICE CXone",
    "Cisco", "UCCX", "Cisco UCCX", "Five9", "Webex Contact Center",
    "Twilio", "Verint", "IVR", "DTMF", "ACD", "CTI", "WFM",
    "Workforce Management", "contact flow", "routing profile",
    "queue", "omnichannel", "screen pop", "after-call work",
    "wrap-up", "agent desktop", "softphone", "SIP", "WebRTC",
    "chatbot", "virtual agent", "self-service", "deflection",
    # SA / architecture / [scrubbed]
    "[scrubbed] Digital", "Solutions Architect", "Enterprise Architect",
    "Statement of Work", "SOW", "Contact Flow Design Document", "CFDD",
    "Solution Design Document", "SDD", "discovery", "proof of concept",
    "POC", "MVP", "RFP", "RFI", "professional services",
    # Sales / commercial
    "MEDDIC", "MEDDPICC", "BANT", "champion", "economic buyer",
    "decision criteria", "ARR", "MRR", "TCO", "Total Cost of Ownership",
    "ROI", "net-new", "upsell", "cross-sell", "procurement",
    "redlines", "Master Services Agreement", "MSA", "pipeline",
    "opportunity", "qualification", "close date", "renewal",
]


# Known mis-hears → canonical. Keys are matched case-insensitively on
# word boundaries; the canonical value preserves the correct casing.
# Conservative on purpose — only unambiguous corrections so we never
# "fix" a word the user actually meant.
DEFAULT_CORRECTIONS: Dict[str, str] = {
    "genesis": "Genesys",
    "jenesys": "Genesys",
    "jenesis": "Genesys",
    "genesys cloud": "Genesys Cloud",
    "see cass": "CCaaS",
    "see-cass": "CCaaS",
    "c cass": "CCaaS",
    "seacas": "CCaaS",
    "you see ex": "UCCX",
    "u c c x": "UCCX",
    "five nine": "Five9",
    "five9": "Five9",
    "nice cx one": "NICE CXone",
    "cx one": "CXone",
    "amazon connect": "Amazon Connect",
    "connect contact flow": "Connect contact flow",
    "lex bot": "Lex bot",
    "bedrock agent": "Bedrock agent",
    "medic": "MEDDIC",
    "meddic": "MEDDIC",
    "bant": "BANT",
    "d t m f": "DTMF",
    "i v r": "IVR",
    "a c d": "ACD",
    "s o w": "SOW",
    "sow": "SOW",
    "t c o": "TCO",
    "r o i": "ROI",
    "webex": "Webex",
    "twilio": "Twilio",
    "verint": "Verint",
    "polly": "Polly",
}

# faster-whisper conditions on the last ~224 tokens of initial_prompt.
# Keep the built prompt under a safe character budget so the glossary
# doesn't crowd out the conditioning. ~1000 chars ≈ well within budget.
_MAX_PROMPT_CHARS = 1000


class TerminologyService:
    """Thread-safe JSON-on-disk glossary: canonical terms (for Whisper
    bias) + correction map (for post-transcription fixes)."""

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "terminology.json"
        self._lock = threading.Lock()
        self._ensure_seeded()

    def _ensure_seeded(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._write_locked({
                    "terms": list(DEFAULT_TERMS),
                    "corrections": dict(DEFAULT_CORRECTIONS),
                })

    def _read_locked(self) -> dict:
        if not self._path.exists():
            return {"terms": [], "corrections": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"terminology.json unreadable ({e}); using empty")
            return {"terms": [], "corrections": {}}
        terms = data.get("terms")
        corr = data.get("corrections")
        return {
            "terms": [str(t).strip() for t in terms if str(t).strip()]
            if isinstance(terms, list) else [],
            "corrections": {
                str(k).strip().lower(): str(v).strip()
                for k, v in corr.items()
                if str(k).strip() and str(v).strip()
            } if isinstance(corr, dict) else {},
        }

    def _write_locked(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── Read API ──────────────────────────────────────────────────────

    def get_all(self) -> dict:
        with self._lock:
            return self._read_locked()

    def build_initial_prompt(self) -> str:
        """Whisper `initial_prompt` biasing string built from canonical
        terms. Returns '' when the glossary is empty (so transcription
        behaves exactly as before for users who clear it)."""
        with self._lock:
            terms = self._read_locked()["terms"]
        if not terms:
            return ""
        # A short natural-language frame helps the decoder treat these as
        # vocabulary rather than a transcript continuation.
        prompt = "Glossary of terms used in this meeting: " + ", ".join(terms) + "."
        if len(prompt) > _MAX_PROMPT_CHARS:
            # Trim term-by-term from the end until under budget.
            kept: List[str] = []
            running = len("Glossary of terms used in this meeting: .")
            for t in terms:
                add = len(t) + 2
                if running + add > _MAX_PROMPT_CHARS:
                    break
                kept.append(t)
                running += add
            prompt = "Glossary of terms used in this meeting: " + ", ".join(kept) + "."
        return prompt

    def apply_corrections(self, text: str) -> str:
        """Replace known mis-hears with canonical forms. Word-boundary,
        case-insensitive. No-op when text is empty or no corrections are
        configured."""
        if not text:
            return text
        with self._lock:
            corrections = self._read_locked()["corrections"]
        if not corrections:
            return text
        out = text
        # Longest keys first so multi-word corrections win over any
        # single-word substring (e.g. "genesys cloud" before "genesys").
        for wrong in sorted(corrections, key=len, reverse=True):
            canonical = corrections[wrong]
            pattern = r"\b" + re.escape(wrong) + r"\b"
            out = re.sub(pattern, canonical, out, flags=re.IGNORECASE)
        return out

    # ── Write API ─────────────────────────────────────────────────────

    def set_all(self, terms: List[str], corrections: Dict[str, str]) -> dict:
        clean_terms = []
        seen = set()
        for t in terms or []:
            t = str(t).strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                clean_terms.append(t)
        clean_corr: Dict[str, str] = {}
        for k, v in (corrections or {}).items():
            k = str(k).strip().lower()
            v = str(v).strip()
            if k and v:
                clean_corr[k] = v
        with self._lock:
            self._write_locked({"terms": clean_terms, "corrections": clean_corr})
            return self._read_locked()

    def reset(self) -> dict:
        with self._lock:
            self._write_locked({
                "terms": list(DEFAULT_TERMS),
                "corrections": dict(DEFAULT_CORRECTIONS),
            })
            return self._read_locked()
