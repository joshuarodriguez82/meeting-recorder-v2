"""Extraction prompt version + input fingerprint.

Deliberately its own module with stdlib imports only. `core.summarizer`
imports the Anthropic SDK at module load, so anything that pulls the
fingerprint in from a request path would drag that dependency along —
which broke a test that fakes the summarizer entirely and never
installs the SDK. The fingerprint is a pure function of text; it has no
business needing a model client to be importable.
"""

from __future__ import annotations

import hashlib

# Bump when any extractor's PROMPT text changes. It is part of the
# fingerprint, so a prompt edit correctly invalidates every session's
# cached extraction — exactly what forced the reprocessing runs of
# August 2026 — while an unchanged prompt lets reprocessing skip work
# that would produce byte-identical output.
EXTRACTOR_PROMPT_VERSION = "2026-08-27.1"


def extraction_fingerprint(transcript: str, notes: str = "",
                           template: str = "") -> str:
    """Stable hash of everything an extraction's output depends on.

    Deliberately NOT a timestamp or a file mtime: the question is
    "would re-running produce different text", and that turns only on
    the transcript, the user's notes, the chosen template, and the
    prompt version.
    """
    h = hashlib.sha256()
    for part in (EXTRACTOR_PROMPT_VERSION, template or "",
                 notes or "", transcript or ""):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()
