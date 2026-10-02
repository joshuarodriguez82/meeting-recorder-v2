"""
Reading a transcript someone else's meeting tool already made.

WHY
---
A Teams or Zoom recording usually comes with the platform's own
transcript, and that transcript knows something the audio can't tell
us: who each speaker is, by name, from the meeting roster. Importing it
with (or instead of) the video turns "Speaker 1 / Speaker 2" into real
names, skips transcription and speaker separation entirely, and lets a
meeting arrive as a transcript alone when there is no recording.

FORMATS
-------
* WebVTT (.vtt) — what Teams and Zoom both download.
  - Teams names the speaker in a voice span:  ``<v Jane Doe>text</v>``
  - Zoom prefixes the cue text:               ``Jane Doe: text``
* SubRip (.srt) — the same cue blocks with comma milliseconds.
* Word (.docx) — Teams' "Download as .docx". Two layouts are read:
  - a speaker line with the time after it, then what they said:
        Jane Doe   0:03
        Good morning everyone.
  - the older cue layout: a ``start --> end`` line, the speaker's name
    on the next line, then the text.
  A .docx is a zip of XML, read here with the standard library so this
  needs no Word library.

Timestamps are accepted in every form these produce: ``00:01:02.500``,
``01:02.500``, ``0:1:2.5`` and ``00:01:02,500``.

Pure — no I/O beyond reading the one file it is handed.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from html import unescape

TRANSCRIPT_EXTS = (".vtt", ".srt", ".docx", ".txt")

#: Consecutive cues from the same speaker closer than this are one
#: utterance. Teams splits a sentence across several short cues.
MERGE_GAP_S = 1.0
MERGE_MAX_S = 30.0
#: A "Name: text" prefix is only a speaker name when it looks like one.
MAX_NAME_WORDS = 6
MAX_NAME_CHARS = 60
#: A four-hour meeting's document.xml is a few MB; anything near this
#: is not a transcript.
MAX_DOCX_XML_BYTES = 50 * 1024 * 1024


class TranscriptError(ValueError):
    """The file isn't a transcript this can read."""


@dataclass
class Cue:
    start: float
    end: float
    speaker: str      # "" when the file doesn't say
    text: str


def is_transcript(path) -> bool:
    return Path(path).suffix.lower() in TRANSCRIPT_EXTS


# ── timestamps ───────────────────────────────────────────────────────

_TS = r"\d{1,2}(?::\d{1,2}){1,2}(?:[.,]\d{1,3})?"
_CUE_RE = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")


def parse_timestamp(text: str) -> float:
    """Seconds from ``h:m:s.f`` / ``m:s.f`` / ``h:m:s,f``."""
    parts = text.strip().replace(",", ".").split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


# ── speaker names ────────────────────────────────────────────────────

_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_PREFIX_RE = re.compile(r"^([^:]{1,%d}):\s+(.+)$" % MAX_NAME_CHARS, re.S)


def _looks_like_name(text: str) -> bool:
    words = text.split()
    return (0 < len(words) <= MAX_NAME_WORDS
            and not any(ch.isdigit() for ch in text)
            and not text.rstrip().endswith((".", "?", "!")))


def _clean(text: str) -> str:
    return " ".join(_TAG_RE.sub("", text).split())


# ── WebVTT / SRT ─────────────────────────────────────────────────────


def parse_cues(text: str) -> List[Cue]:
    """Cue blocks (VTT, SRT, or the older Teams .docx layout) into cues.
    The speaker comes from a voice span, a 'Name: text' prefix used
    consistently through the file, or — the .docx layout — a name alone
    on the first line after the timing."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    raw: List[tuple] = []          # (start, end, voice, lines)
    i = 0
    while i < len(lines):
        m = _CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = parse_timestamp(m.group(1)), parse_timestamp(m.group(2))
        body: List[str] = []
        i += 1
        while i < len(lines) and lines[i].strip() and not _CUE_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        raw.append((start, end, body))

    # Zoom-style prefixes: trust them only when the same name prefixes
    # more than one cue (or the file is tiny) — "Note: ..." once is text.
    # The same goes for a name alone on the first line of the cue (the
    # older Teams .docx layout).
    prefix_counts: Dict[str, int] = {}
    line_counts: Dict[str, int] = {}
    for _, _, body in raw:
        joined = " ".join(body)
        if _VOICE_RE.search(joined):
            continue
        pm = _PREFIX_RE.match(_clean(joined))
        if pm and _looks_like_name(pm.group(1)):
            name = pm.group(1).strip()
            prefix_counts[name] = prefix_counts.get(name, 0) + 1
        if len(body) >= 2 and _looks_like_name(_clean(body[0])):
            name = _clean(body[0])
            line_counts[name] = line_counts.get(name, 0) + 1
    small = len(raw) <= 3
    trusted = {n for n, c in prefix_counts.items() if c >= 2 or small}
    own_line = {n for n, c in line_counts.items() if c >= 2 or small}

    cues: List[Cue] = []
    for start, end, body in raw:
        joined = " ".join(body)
        speaker = ""
        text = _clean(joined)
        vm = _VOICE_RE.search(joined)
        if vm:
            speaker = vm.group(1).strip()
        else:
            pm = _PREFIX_RE.match(text)
            if pm and pm.group(1).strip() in trusted:
                speaker, text = pm.group(1).strip(), pm.group(2).strip()
            elif len(body) >= 2 and _clean(body[0]) in own_line:
                speaker, text = _clean(body[0]), _clean(" ".join(body[1:]))
        if text:
            cues.append(Cue(start, max(end, start), speaker, text))
    return cues


# ── Teams .docx, newer layout ────────────────────────────────────────

_HEADER_RE = re.compile(
    r"^(?P<name>.{1,%d}?)\s+(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s*$"
    % MAX_NAME_CHARS)


def parse_speaker_blocks(paragraphs: List[str]) -> List[Cue]:
    """'Jane Doe   0:03' followed by what they said.

    The layout alternates speaker line, then words, so the line after a
    speaker line is always what was said — even when it looks like a
    speaker line itself ("We start at 9:30"). A speaker line's time
    never goes backwards, which keeps such a remark from being taken
    for one further on in the transcript."""
    lines = [" ".join(p.split()) for p in paragraphs]
    lines = [ln for ln in lines if ln]
    cues: List[Cue] = []
    current: Optional[Cue] = None
    last_t = -1.0
    expecting_words = False
    for ln in lines:
        if not expecting_words:
            m = _HEADER_RE.match(ln)
            if m and _looks_like_name(m.group("name")):
                t = parse_timestamp(m.group("ts"))
                if t >= last_t:
                    if current is not None and current.text:
                        cues.append(current)
                    current = Cue(t, t, m.group("name").strip(), "")
                    last_t = t
                    expecting_words = True
                    continue
        expecting_words = False
        if current is not None:
            current.text = f"{current.text} {ln}".strip()
    if current is not None and current.text:
        cues.append(current)
    # Each utterance runs until the next one starts; the last gets a
    # length from its word count (about 150 words a minute).
    for a, b in zip(cues, cues[1:]):
        a.end = max(a.start, b.start)
    if cues:
        cues[-1].end = cues[-1].start + max(
            2.0, len(cues[-1].text.split()) / 2.5)
    return cues


# The handful of WordprocessingML tags a transcript needs. Matching
# "w:p" with a lookahead keeps it from matching w:pPr, w:r from w:rPr.
_DOCX_TAG = re.compile(r"<(/?)w:(p|r|t|tab|br|cr)(?=[\s/>])[^>]*?(/?)>")


def _docx_paragraphs(path: Path) -> List[str]:
    """The paragraphs of a .docx, as text.

    The file is untrusted, so it is not handed to an XML parser at all:
    a transcript is flat paragraphs of runs of text, and the few tags
    that carry it are read directly. With no parser there is no entity
    expansion and no external entity to resolve — the attacks an XML
    parser would have to be hardened against do not exist here."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            # A zip bomb: refuse before inflating it.
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise TranscriptError(
                    f"{path.name} is too large to be a meeting transcript.")
            raw = z.read(info)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise TranscriptError(f"{path.name} isn't a readable Word file: {e}")
    # Word never writes a DOCTYPE; a document that has one was not made
    # by Word, and is refused rather than half-read.
    if b"<!doctype" in raw.lower():
        raise TranscriptError(
            f"{path.name} isn't a Word transcript (it declares a DOCTYPE).")
    xml = raw.decode("utf-8", errors="replace")

    paragraphs: List[str] = []
    bits: List[str] = []
    in_run = False
    text_from: Optional[int] = None
    for m in _DOCX_TAG.finditer(xml):
        closing, name, empty = m.group(1) == "/", m.group(2), m.group(3) == "/"
        if name == "t":
            if closing and text_from is not None:
                bits.append(unescape(xml[text_from:m.start()]))
                text_from = None
            elif not closing and not empty:
                text_from = m.end()
        elif name == "r":
            in_run = not closing and not empty
        elif name == "tab" and in_run and not closing:
            bits.append("\t")        # a w:tab outside a run is a tab STOP
        elif name in ("br", "cr") and in_run and not closing:
            bits.append("\n")
        elif name == "p":
            if closing or empty:
                # A soft line break inside a paragraph separates lines too.
                paragraphs.extend("".join(bits).split("\n"))
                bits = []
            else:
                bits = []
    return paragraphs


# ── entry point ──────────────────────────────────────────────────────


def merge(cues: List[Cue]) -> List[Cue]:
    """Join consecutive cues from the same named speaker that run on
    from each other."""
    out: List[Cue] = []
    for c in sorted(cues, key=lambda c: c.start):
        prev = out[-1] if out else None
        # Only a NAMED speaker's cues are joined: two unattributed
        # lines may well be two different people.
        if (prev is not None and c.speaker and prev.speaker == c.speaker
                and c.start - prev.end <= MERGE_GAP_S
                and c.end - prev.start <= MERGE_MAX_S):
            prev.text = f"{prev.text} {c.text}"
            prev.end = max(prev.end, c.end)
        else:
            out.append(Cue(c.start, c.end, c.speaker, c.text))
    return out


def read_transcript(path) -> List[Cue]:
    """The utterances in a transcript file, in time order. Raises
    TranscriptError when the file has none this can read."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".docx":
        paragraphs = _docx_paragraphs(path)
        joined = "\n".join(paragraphs)
        cues = parse_cues(joined) if "-->" in joined \
            else parse_speaker_blocks(paragraphs)
    elif suffix in (".vtt", ".srt", ".txt"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="cp1252", errors="replace")
        cues = parse_cues(text) if "-->" in text \
            else parse_speaker_blocks(text.split("\n"))
    else:
        raise TranscriptError(
            f"Unsupported transcript type: {suffix or '(none)'}. "
            f"Use .vtt, .srt or .docx.")
    cues = merge(cues)
    if not cues:
        raise TranscriptError(
            f"No timed lines found in {path.name}. Use the transcript "
            f"download from Teams or Zoom (.vtt or .docx).")
    return cues


def speaker_names(cues: List[Cue]) -> List[str]:
    """Distinct named speakers, in order of first appearance."""
    seen: List[str] = []
    for c in cues:
        if c.speaker and c.speaker not in seen:
            seen.append(c.speaker)
    return seen
