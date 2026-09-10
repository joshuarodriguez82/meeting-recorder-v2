"""
Collapsing one person who was diarized as two.

THE DEFECT (field reports 2026-09-09, two separate installs)
------------------------------------------------------------
pyannote splits one person into two labels partway through a meeting —
an echo, a headset swap, someone unmuting into a different audio path —
and the transcript then alternates between them::

    [12:04 → 12:09]  Jane Doe:    ...one of the first things
    [12:09 → 12:14]  SPEAKER_03:  and then after that you'd
    [12:14 → 12:20]  Jane Doe:    determine where it goes

Worse than the split itself is what follows it. Naming is per-label, so
the half that matched a saved profile gets the name and the half that
did not stays ``SPEAKER_03``. The reader sees a conversation between a
named person and a stranger who is the same person. And the transcript
is the input to the summary, the action items and the commitments, so
half of what someone committed to is attributed to nobody.

There was no way to correct it. ``POST /speaker-profiles/merge`` merges
entries in the GLOBAL known-speakers roster; it does not touch a
session's segments, so merging the two profiles left the transcript
reading exactly as before.

WHY THIS IS A DECISION MODULE
-----------------------------
Merging is destructive — it rewrites the speaker on every affected
segment — and the cost of the two error directions is wildly
asymmetric:

  * Failing to merge one person leaves a transcript that is UGLY. The
    words are still attached to the right voice.
  * Merging two people puts one person's words in another's mouth.
    That is the exact defect ``DiarizationEngine.assign_speakers``
    exists to prevent, and it is silent — nothing downstream can tell.

So the automatic pass here merges ONLY on evidence that is certain by
construction, and everything short of certain is offered to the user as
a suggestion rather than applied. Voice similarity is a suggestion. A
number this module invented is not permission to rewrite a transcript.

The logic is pure — no torch, no audio, no session objects — because
the thresholds and the tie-breaks are the part worth testing, and the
speechbrain import makes the rest of the speaker stack untestable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Cosine similarity at or above which two centroids in the SAME
#: session are offered to the user as "probably the same person".
#:
#: This is a SUGGESTION threshold, never an action threshold. ECAPA
#: scores the same person 0.7–0.95 and different people 0.3–0.5 (see
#: core/speaker_embeddings.py), and 0.75 is what the known-speakers
#: store already uses to propose a cross-session match that the user
#: then confirms. Reusing that number keeps one calibration story in
#: the app instead of two, and the cost of a wrong suggestion is a
#: dismissed prompt.
SUGGEST_THRESHOLD = 0.75

#: Names that mean "unnamed", not a person. ``Speaker.__post_init__``
#: defaults ``display_name`` to the label itself, and the diarizer emits
#: both the numbered pyannote form and the app's own hex form.
_PLACEHOLDER_RE = re.compile(r"^speaker[_\s-]?[0-9a-f]+$", re.IGNORECASE)


@dataclass(frozen=True)
class SpeakerFacts:
    """Everything a merge decision needs about one session-local speaker.

    Deliberately not the ``Speaker`` model: this module is imported by
    tests that must not pay for torch, and the model carries persistence
    concerns that have nothing to do with the decision.
    """

    speaker_id: str
    display_name: str = ""
    profile_id: Optional[str] = None
    match_confirmed: bool = False
    embedding: Sequence[float] = ()
    seconds: float = 0.0
    segment_count: int = 0

    @property
    def is_named(self) -> bool:
        """True when a human would read ``display_name`` as a name."""
        return not is_placeholder_name(self.display_name, self.speaker_id)


@dataclass(frozen=True)
class MergeGroup:
    """One merge: ``absorb`` disappear, their segments become ``into``."""

    into: str
    absorb: Tuple[str, ...]
    reason: str
    #: Present only on similarity-derived suggestions.
    similarity: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "into": self.into,
            "absorb": list(self.absorb),
            "reason": self.reason,
            "similarity": self.similarity,
        }


def is_placeholder_name(display_name: str, speaker_id: str = "") -> bool:
    """Is this a stand-in rather than a person's name?

    ``SPEAKER_03`` is a placeholder. So is a ``display_name`` that is
    still just the label. ``You`` is NOT — it is the owner speaker's
    real display name (core/channel_attribution.py) and the one person
    in the room whose identity is known from the capture device rather
    than guessed from a voice.
    """
    name = (display_name or "").strip()
    if not name:
        return True
    if speaker_id and name == speaker_id:
        return True
    return bool(_PLACEHOLDER_RE.match(name))


def normalize_name(display_name: str) -> str:
    """Fold a display name for equality comparison only.

    Case and whitespace vary between the LLM speaker-identification pass
    and a name the user typed; nothing else is folded. In particular a
    first name is NOT treated as matching a full name — "Jane" and
    "Jane Doe" may well be two different people in the same meeting, and
    guessing wrong here rewrites the transcript.
    """
    return " ".join((display_name or "").split()).casefold()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """Cosine of two embeddings, or None when either cannot be compared.

    Returns None rather than 0.0 for an empty, mismatched or zero-norm
    vector. Zero is a real similarity value meaning "as different as
    these get"; using it for "no answer" is how an unreadable result
    starts rendering as a confident one.
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return None
    value = dot / math.sqrt(norm_a * norm_b)
    # Guard the float edges so a caller comparing against 1.0 never sees
    # 1.0000000002 from an accumulated rounding error.
    return max(-1.0, min(1.0, value))


def blend_embeddings(
    weighted: Iterable[Tuple[Sequence[float], float]],
) -> List[float]:
    """Duration-weighted mean of several centroids, L2-normalized.

    Weighted by SPEECH SECONDS, not by segment count. A speaker split
    9:1 is one person whose centroid should look like the 90% side; an
    unweighted mean would drag it halfway toward a two-second fragment
    that may be mostly the echo that caused the split.

    Vectors that are empty, mismatched in length, or carry a
    non-positive weight are skipped. Returns [] when nothing usable
    remains, which callers read as "keep the embedding you had".
    """
    vectors = [(list(v), float(w)) for v, w in weighted if v and w > 0]
    if not vectors:
        return []
    width = len(vectors[0][0])
    vectors = [(v, w) for v, w in vectors if len(v) == width]
    if not vectors:
        return []
    total = sum(w for _, w in vectors)
    if total <= 0:
        return []
    summed = [0.0] * width
    for vector, weight in vectors:
        share = weight / total
        for i, value in enumerate(vector):
            summed[i] += value * share
    norm = math.sqrt(sum(v * v for v in summed))
    if norm <= 0:
        return []
    return [v / norm for v in summed]


def choose_target(candidates: Sequence[SpeakerFacts]) -> str:
    """Which of a merge group survives, and keeps its name.

    Order, most decisive first:

    1. **A name the user confirmed.** They told the app who this is; a
       merge must not quietly discard that.
    2. **A name at all**, over a bare ``SPEAKER_04``. The merged speaker
       should read as the person, and this is the whole point of the
       merge for the reported case — one half was named and one was not.
    3. **A linked profile**, so the merged speaker stays connected to
       the known-speakers roster.
    4. **The most speech.** The dominant half of a lopsided split is the
       one whose identity the rest of the app has already reasoned
       about.
    5. **The label, alphabetically.** Never a coin flip: a merge that
       picks a different survivor on a re-run makes the same session
       look different for no reason.
    """
    if not candidates:
        raise ValueError("choose_target needs at least one speaker")
    return sorted(
        candidates,
        key=lambda s: (
            not (s.match_confirmed and s.is_named),
            not s.is_named,
            s.profile_id is None,
            -s.seconds,
            s.speaker_id,
        ),
    )[0].speaker_id


def _group(members: Sequence[SpeakerFacts], reason: str,
           similarity: Optional[float] = None) -> MergeGroup:
    target = choose_target(members)
    absorb = tuple(sorted(s.speaker_id for s in members
                          if s.speaker_id != target))
    return MergeGroup(into=target, absorb=absorb, reason=reason,
                      similarity=similarity)


def plan_certain_merges(
    speakers: Sequence[SpeakerFacts],
    owner_label: str = "",
) -> List[MergeGroup]:
    """Merges that need no threshold, because they are true by
    construction.

    Two cases, both of which the app has ALREADY decided elsewhere and
    then failed to act on:

    * **Same linked profile.** Two labels matched to one entry in the
      known-speakers roster are one person by the app's own definition
      of the roster. Leaving them split contradicts a decision it
      already made.
    * **Same name.** The speaker-identification pass resolved both
      labels to the same person, or the user typed the same name twice.
      Either way the transcript is about to render two rows with one
      name on them, which is the bug the reader sees.

    Placeholder names are never matched to each other — ``SPEAKER_02``
    and ``SPEAKER_05`` sharing "no name" is not evidence of anything.

    THE OWNER IS NEVER TOUCHED HERE. The owner speaker's spans come
    from the capture device, not from voice clustering
    (core/channel_attribution.py), and that module's invariant is that
    far-end words can never be handed to the user. A name collision is
    weaker evidence than the microphone, so an owner merge is offered
    as a suggestion and applied only when the user asks for it.
    """
    mergeable = [s for s in speakers
                 if not (owner_label and s.speaker_id == owner_label)]

    groups: List[MergeGroup] = []
    claimed: set[str] = set()

    by_profile: Dict[str, List[SpeakerFacts]] = {}
    for speaker in mergeable:
        if speaker.profile_id:
            by_profile.setdefault(speaker.profile_id, []).append(speaker)
    for profile_id, members in sorted(by_profile.items()):
        if len(members) < 2:
            continue
        groups.append(_group(members, "same known-speaker profile"))
        claimed.update(s.speaker_id for s in members)

    by_name: Dict[str, List[SpeakerFacts]] = {}
    for speaker in mergeable:
        if speaker.speaker_id in claimed or not speaker.is_named:
            continue
        by_name.setdefault(normalize_name(speaker.display_name), []).append(
            speaker)
    for _, members in sorted(by_name.items()):
        if len(members) < 2:
            continue
        groups.append(_group(members, "same name"))

    return groups


def suggest_merges(
    speakers: Sequence[SpeakerFacts],
    threshold: float = SUGGEST_THRESHOLD,
    already_merged: Iterable[str] = (),
) -> List[MergeGroup]:
    """Pairs that SOUND like one person, for the user to decide on.

    Every pair scoring at or above ``threshold`` is returned, most
    similar first, as a two-speaker group. Deliberately pairwise rather
    than transitively clustered: A-B and B-C both scoring 0.76 does not
    make A and C the same person, and chaining them is how a
    three-person conversation collapses into one.

    The owner speaker IS included here. Their own voice bleeding into a
    second cluster is a real and common split, and a suggestion the
    user accepts is a decision the microphone did not get to make
    alone.
    """
    skip = set(already_merged)
    pool = [s for s in speakers
            if s.speaker_id not in skip and len(s.embedding) > 0]

    scored: List[Tuple[float, MergeGroup]] = []
    for i, left in enumerate(pool):
        for right in pool[i + 1:]:
            similarity = cosine_similarity(left.embedding, right.embedding)
            if similarity is None or similarity < threshold:
                continue
            scored.append(
                (similarity, _group([left, right], "similar voice",
                                    similarity=round(similarity, 4))))
    scored.sort(key=lambda pair: (-pair[0], pair[1].into))
    return [group for _, group in scored]


@dataclass
class MergeResult:
    """What a merge did, in terms the caller can log and report."""

    into: str
    absorbed: List[str] = field(default_factory=list)
    segments_moved: int = 0
    display_name: str = ""
    embedding: List[float] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "into": self.into,
            "absorbed": list(self.absorbed),
            "segments_moved": self.segments_moved,
            "display_name": self.display_name,
            "seconds": round(self.seconds, 3),
        }


def plan_merge(
    speakers: Sequence[SpeakerFacts],
    into: str,
    absorb: Sequence[str],
    display_name: Optional[str] = None,
) -> MergeResult:
    """Work out the surviving speaker's name, centroid and duration.

    Raises ValueError when the request does not describe a merge — an
    unknown label, or a target that is also in ``absorb``. Both mean the
    caller's idea of the session and the session on disk have diverged,
    and rewriting segments on that basis is how a stale UI destroys a
    transcript.

    The name is settled here rather than left to the caller: an explicit
    ``display_name`` wins, else the target's own name if it has one,
    else the best name among the absorbed. That last clause is the
    reported case — the named half is the one the user can see is wrong,
    so they merge the stranger into it; but they may equally merge the
    named half into the longer-speaking unnamed one, and losing the name
    at that point would make the feature feel broken.
    """
    by_id = {s.speaker_id: s for s in speakers}
    if into not in by_id:
        raise ValueError(f"unknown speaker {into!r}")
    absorb = [a for a in absorb if a != into]
    if not absorb:
        raise ValueError("nothing to absorb")
    missing = [a for a in absorb if a not in by_id]
    if missing:
        raise ValueError(f"unknown speaker(s): {', '.join(sorted(missing))}")

    target = by_id[into]
    members = [target] + [by_id[a] for a in absorb]

    chosen = (display_name or "").strip()
    if not chosen and target.is_named:
        chosen = target.display_name.strip()
    if not chosen:
        named = [s for s in members if s.is_named]
        if named:
            # Most speech wins among equally-valid names; a confirmed
            # one wins outright.
            chosen = sorted(
                named,
                key=lambda s: (not s.match_confirmed, -s.seconds,
                               s.speaker_id),
            )[0].display_name.strip()
    if not chosen:
        chosen = target.display_name or into

    return MergeResult(
        into=into,
        absorbed=sorted(absorb),
        segments_moved=sum(by_id[a].segment_count for a in absorb),
        display_name=chosen,
        embedding=blend_embeddings(
            (s.embedding, s.seconds) for s in members),
        seconds=sum(s.seconds for s in members),
    )
