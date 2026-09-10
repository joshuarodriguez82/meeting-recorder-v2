"""
One person diarized as two, and putting them back together.

THE FIELD REPORTS (2026-09-09, two separate installs)
-----------------------------------------------------
pyannote splits a single participant into two labels partway through a
meeting and the transcript alternates between them::

    [12:04 → 12:09]  Jane Doe:    ...one of the first things
    [12:09 → 12:14]  SPEAKER_03:  and then after that you'd
    [12:14 → 12:20]  Jane Doe:    determine where it goes

Naming is per-label, so the half that matched a saved profile got the
name and the half that did not stayed ``SPEAKER_03``. The reader sees a
named person talking to a stranger who is the same person.

There was no correction available. ``POST /speaker-profiles/merge``
merges entries in the global known-speakers roster and never touches a
session's segments, so merging the profiles left the transcript reading
exactly as it did before.

WHAT THESE TESTS PIN DOWN
-------------------------
Merging rewrites the speaker on every affected segment, and the two
error directions do not cost the same. Failing to merge leaves an ugly
transcript whose words are still on the right voice. Merging two people
puts one person's words in another's mouth — silently, and downstream
into the summary, the action items and the commitments.

So the automatic pass acts only on evidence that is certain by
construction (same profile, same name), similarity is a SUGGESTION, and
the owner speaker — whose spans come from the capture device rather
than from voice clustering — is never merged without the user saying so.
"""

from __future__ import annotations

import math

import pytest

from core.speaker_merge import (
    MergeGroup,
    SUGGEST_THRESHOLD,
    SpeakerFacts,
    blend_embeddings,
    choose_target,
    cosine_similarity,
    is_placeholder_name,
    normalize_name,
    plan_certain_merges,
    plan_merge,
    suggest_merges,
)

OWNER = "SPEAKER_YOU"


def _facts(speaker_id, name="", profile=None, confirmed=False,
           embedding=(), seconds=10.0, segments=5):
    return SpeakerFacts(
        speaker_id=speaker_id, display_name=name or speaker_id,
        profile_id=profile, match_confirmed=confirmed,
        embedding=embedding, seconds=seconds, segment_count=segments)


# ── What counts as a name ───────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "", "   ", "SPEAKER_03", "speaker_03", "SPEAKER 03", "SPEAKER-03",
    "SPEAKER_A4F1",
])
def test_diarizer_labels_are_not_names(name):
    """Every form the app puts in display_name when it does not know who
    someone is. Treating one as a name would merge two strangers on the
    grounds that neither has been identified."""
    assert is_placeholder_name(name, "SPEAKER_03") is True


@pytest.mark.parametrize("name", ["Jane Doe", "You", "J. Roe", "Poe"])
def test_real_names_are_names(name):
    """'You' is the owner speaker's real display name, not a
    placeholder — it is the one identity that comes from the capture
    device rather than a guess."""
    assert is_placeholder_name(name, "SPEAKER_01") is False


def test_a_first_name_does_not_match_a_full_name():
    """Two people in one meeting can share a first name, and a merge on
    that basis rewrites the transcript. Only case and whitespace fold."""
    assert normalize_name("  Jane   Doe ") == normalize_name("jane doe")
    assert normalize_name("Jane") != normalize_name("Jane Doe")


# ── Similarity ──────────────────────────────────────────────────────

def test_identical_voices_score_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_similarity_is_clamped_to_one():
    """A caller comparing against 1.0 must never see 1.0000000002 from
    accumulated rounding."""
    v = [0.1] * 192
    assert cosine_similarity(v, v) <= 1.0


@pytest.mark.parametrize("a,b", [
    ([], [1.0]),                 # nothing to compare
    ([1.0, 0.0], [1.0]),         # different widths
    ([0.0, 0.0], [1.0, 0.0]),    # zero norm
])
def test_uncomparable_embeddings_return_none_not_zero(a, b):
    """Zero is a real similarity meaning 'as different as these get'.
    Using it for 'no answer' is how a result nobody could read starts
    rendering as a confident one."""
    assert cosine_similarity(a, b) is None


# ── Blending ────────────────────────────────────────────────────────

def test_the_blend_is_weighted_by_speech_not_by_count():
    """A 9:1 split is one person whose voice looks like the 90% side. An
    unweighted mean would drag the centroid halfway toward a two-second
    fragment — often the echo that caused the split in the first
    place."""
    blended = blend_embeddings([([1.0, 0.0], 90.0), ([0.0, 1.0], 10.0)])
    assert blended[0] > blended[1] * 5


def test_the_blend_comes_back_normalized():
    """The centroid feeds cosine comparisons against the profile store,
    which assumes L2-normalized vectors."""
    blended = blend_embeddings([([3.0, 4.0], 1.0), ([0.0, 5.0], 1.0)])
    assert math.sqrt(sum(v * v for v in blended)) == pytest.approx(1.0)


@pytest.mark.parametrize("weighted", [
    [],
    [([], 5.0)],
    [([1.0, 0.0], 0.0)],              # no speech, no weight
    [([1.0, 0.0], 1.0), ([1.0], 1.0)],  # mismatched widths are dropped…
])
def test_an_unusable_blend_is_empty_not_wrong(weighted):
    """Empty means 'keep the embedding you had'. Inventing a vector here
    would poison the known-speakers profile the merge refines."""
    result = blend_embeddings(weighted)
    assert result == [] or len(result) == 2


# ── Which speaker survives ──────────────────────────────────────────

def test_a_name_the_user_confirmed_wins():
    """They told the app who this is. A merge must not quietly discard
    that in favour of whoever happened to talk longer."""
    target = choose_target([
        _facts("SPEAKER_01", "Jane Doe", confirmed=True, seconds=5.0),
        _facts("SPEAKER_02", "Jane Doe", seconds=500.0),
    ])
    assert target == "SPEAKER_01"


def test_a_named_speaker_beats_an_unnamed_one():
    """The reported case: one half named, one half not. Keeping the
    stranger's label would be a merge that fixed nothing a reader can
    see."""
    assert choose_target([
        _facts("SPEAKER_03", seconds=400.0),
        _facts("SPEAKER_01", "Jane Doe", seconds=100.0),
    ]) == "SPEAKER_01"


def test_between_two_unnamed_speakers_the_longer_one_wins():
    assert choose_target([
        _facts("SPEAKER_03", seconds=20.0),
        _facts("SPEAKER_05", seconds=300.0),
    ]) == "SPEAKER_05"


def test_the_tie_break_is_stable():
    """A merge that picks a different survivor on a re-run makes the
    same session look different for no reason."""
    a = _facts("SPEAKER_09", seconds=10.0)
    b = _facts("SPEAKER_02", seconds=10.0)
    assert choose_target([a, b]) == choose_target([b, a]) == "SPEAKER_02"


# ── Automatic merges: only what is certain ──────────────────────────

def test_two_labels_on_one_profile_are_merged():
    """The app already decided these are one person when it matched both
    to the same roster entry. Leaving them split contradicts its own
    decision."""
    groups = plan_certain_merges([
        _facts("SPEAKER_01", "Jane Doe", profile="p1", seconds=100.0),
        _facts("SPEAKER_03", "Jane Doe", profile="p1", seconds=20.0),
        _facts("SPEAKER_02", "Poe", profile="p2"),
    ], owner_label=OWNER)
    assert [g.into for g in groups] == ["SPEAKER_01"]
    assert groups[0].absorb == ("SPEAKER_03",)
    assert groups[0].reason == "same known-speaker profile"


def test_two_labels_with_one_name_are_merged():
    """The identification pass resolved both to the same person, so the
    transcript is about to render two rows carrying one name — which is
    the bug the reader actually sees."""
    groups = plan_certain_merges([
        _facts("SPEAKER_01", "Jane Doe", seconds=100.0),
        _facts("SPEAKER_04", "jane   doe", seconds=30.0),
    ], owner_label=OWNER)
    assert len(groups) == 1
    assert groups[0].into == "SPEAKER_01"
    assert groups[0].absorb == ("SPEAKER_04",)


def test_two_unnamed_speakers_are_not_merged():
    """Sharing 'no name' is not evidence of anything. This is the merge
    that would put a stranger's words in someone's mouth."""
    assert plan_certain_merges([
        _facts("SPEAKER_02"), _facts("SPEAKER_05"),
    ], owner_label=OWNER) == []


def test_similar_voices_alone_do_not_trigger_an_automatic_merge():
    """Voice similarity is a suggestion. A number this app invented is
    not permission to rewrite a transcript."""
    v = [1.0, 0.0]
    assert plan_certain_merges([
        _facts("SPEAKER_01", embedding=v),
        _facts("SPEAKER_02", embedding=v),
    ], owner_label=OWNER) == []


def test_the_owner_is_never_merged_automatically():
    """The owner's spans come from the capture DEVICE, not from voice
    clustering, and channel attribution's invariant is that far-end
    words can never be handed to the user. A name collision is weaker
    evidence than the microphone."""
    groups = plan_certain_merges([
        _facts(OWNER, "Jane Doe", seconds=200.0),
        _facts("SPEAKER_02", "Jane Doe", seconds=50.0),
    ], owner_label=OWNER)
    assert groups == []


def test_a_speaker_is_only_claimed_by_one_group():
    """Profile and name evidence overlap constantly. Emitting a speaker
    in two groups would have the caller apply one merge to a label the
    other merge just deleted."""
    groups = plan_certain_merges([
        _facts("SPEAKER_01", "Jane Doe", profile="p1", seconds=100.0),
        _facts("SPEAKER_02", "Jane Doe", profile="p1", seconds=50.0),
        _facts("SPEAKER_03", "Jane Doe", seconds=10.0),
    ], owner_label=OWNER)
    claimed = [sid for g in groups for sid in (g.into, *g.absorb)]
    assert len(claimed) == len(set(claimed)), f"a label appears twice: {groups}"


# ── Suggestions ─────────────────────────────────────────────────────

def test_a_close_voice_is_suggested():
    near = [0.99, 0.14]
    suggestions = suggest_merges([
        _facts("SPEAKER_01", "Jane Doe", embedding=[1.0, 0.0]),
        _facts("SPEAKER_03", embedding=near),
    ])
    assert len(suggestions) == 1
    assert suggestions[0].into == "SPEAKER_01"
    assert suggestions[0].similarity >= SUGGEST_THRESHOLD


def test_a_distant_voice_is_not_suggested():
    assert suggest_merges([
        _facts("SPEAKER_01", embedding=[1.0, 0.0]),
        _facts("SPEAKER_02", embedding=[0.0, 1.0]),
    ]) == []


def test_a_speaker_with_no_fingerprint_is_skipped():
    """Under 1.5s of speech leaves no centroid (core/speaker_embeddings).
    Comparing against a vector that does not exist would suggest merges
    at random."""
    assert suggest_merges([
        _facts("SPEAKER_01", embedding=[1.0, 0.0]),
        _facts("SPEAKER_02", embedding=()),
    ]) == []


def test_suggestions_are_pairs_and_never_chained():
    """A-B and B-C both scoring 0.76 does not make A and C one person.
    Chaining is how a three-person conversation collapses into one."""
    a = [1.0, 0.0, 0.0]
    b = [0.78, 0.63, 0.0]
    c = [0.10, 0.99, 0.0]
    suggestions = suggest_merges([
        _facts("A", embedding=a), _facts("B", embedding=b),
        _facts("C", embedding=c),
    ], threshold=0.7)
    for group in suggestions:
        assert len(group.absorb) == 1
    pairs = {frozenset((g.into, *g.absorb)) for g in suggestions}
    assert frozenset({"A", "C"}) not in pairs


def test_suggestions_lead_with_the_strongest():
    suggestions = suggest_merges([
        _facts("A", embedding=[1.0, 0.0, 0.0]),
        _facts("B", embedding=[0.999, 0.045, 0.0]),
        _facts("C", embedding=[0.8, 0.6, 0.0]),
    ], threshold=0.7)
    scores = [g.similarity for g in suggestions]
    assert scores == sorted(scores, reverse=True)


def test_the_owner_can_be_suggested():
    """Their own voice bleeding into a second cluster is a real split.
    A suggestion the user accepts is a decision the microphone did not
    get to make alone."""
    suggestions = suggest_merges([
        _facts(OWNER, "You", embedding=[1.0, 0.0]),
        _facts("SPEAKER_02", embedding=[0.99, 0.14]),
    ])
    assert any(OWNER in (g.into, *g.absorb) for g in suggestions)


def test_speakers_already_merged_are_not_suggested_again():
    """The certain pass runs first; re-offering what it just did would
    point at a label that no longer exists."""
    assert suggest_merges([
        _facts("SPEAKER_01", embedding=[1.0, 0.0]),
        _facts("SPEAKER_02", embedding=[0.99, 0.14]),
    ], already_merged=["SPEAKER_02"]) == []


# ── Applying one ────────────────────────────────────────────────────

def test_a_merge_keeps_the_name_of_whichever_half_had_one():
    """The reported case, in the direction the user is likeliest to
    drag it: absorb the stranger into the named half."""
    plan = plan_merge([
        _facts("SPEAKER_01", "Jane Doe", seconds=100.0, segments=12),
        _facts("SPEAKER_03", seconds=40.0, segments=6),
    ], into="SPEAKER_01", absorb=["SPEAKER_03"])
    assert plan.display_name == "Jane Doe"
    assert plan.segments_moved == 6
    assert plan.seconds == pytest.approx(140.0)


def test_merging_the_named_half_into_the_unnamed_one_keeps_the_name():
    """Same two speakers, opposite direction. Losing the name here is
    what would make the feature feel broken — the user merged two rows
    and got a stranger."""
    plan = plan_merge([
        _facts("SPEAKER_01", "Jane Doe", seconds=40.0),
        _facts("SPEAKER_03", seconds=100.0),
    ], into="SPEAKER_03", absorb=["SPEAKER_01"])
    assert plan.display_name == "Jane Doe"


def test_an_explicit_name_wins_over_both():
    plan = plan_merge([
        _facts("SPEAKER_01", "Jane Doe"), _facts("SPEAKER_03", "J. Doe"),
    ], into="SPEAKER_01", absorb=["SPEAKER_03"], display_name="Jane Roe")
    assert plan.display_name == "Jane Roe"


def test_a_confirmed_name_beats_a_guessed_one():
    plan = plan_merge([
        _facts("SPEAKER_01", "Guessed", seconds=900.0),
        _facts("SPEAKER_03", "Jane Doe", confirmed=True, seconds=5.0),
    ], into="SPEAKER_01", absorb=["SPEAKER_03"])
    # SPEAKER_01 is named, so its own name stands — the confirmed name
    # only decides when the target has none of its own.
    assert plan.display_name == "Guessed"

    plan = plan_merge([
        _facts("SPEAKER_01", seconds=900.0),
        _facts("SPEAKER_03", "Jane Doe", confirmed=True, seconds=5.0),
        _facts("SPEAKER_04", "Poe", seconds=800.0),
    ], into="SPEAKER_01", absorb=["SPEAKER_03", "SPEAKER_04"])
    assert plan.display_name == "Jane Doe"


def test_the_merged_centroid_favours_the_dominant_half():
    plan = plan_merge([
        _facts("SPEAKER_01", "Jane Doe", embedding=[1.0, 0.0], seconds=90.0),
        _facts("SPEAKER_03", embedding=[0.0, 1.0], seconds=10.0),
    ], into="SPEAKER_01", absorb=["SPEAKER_03"])
    assert plan.embedding[0] > plan.embedding[1]


@pytest.mark.parametrize("into,absorb", [
    ("SPEAKER_09", ["SPEAKER_01"]),        # target not on the session
    ("SPEAKER_01", ["SPEAKER_09"]),        # source not on the session
    ("SPEAKER_01", ["SPEAKER_01"]),        # merging a speaker into itself
    ("SPEAKER_01", []),                    # nothing asked for
])
def test_a_request_that_does_not_describe_a_merge_is_refused(into, absorb):
    """An unknown label means the caller's idea of the session and the
    session on disk have diverged. Rewriting segments on that basis is
    how a stale UI destroys a transcript."""
    with pytest.raises(ValueError):
        plan_merge([
            _facts("SPEAKER_01", "Jane Doe"), _facts("SPEAKER_03"),
        ], into=into, absorb=absorb)


def test_a_three_way_merge_moves_every_absorbed_segment():
    plan = plan_merge([
        _facts("SPEAKER_01", "Jane Doe", seconds=50.0, segments=8),
        _facts("SPEAKER_03", seconds=20.0, segments=4),
        _facts("SPEAKER_04", seconds=10.0, segments=3),
    ], into="SPEAKER_01", absorb=["SPEAKER_03", "SPEAKER_04"])
    assert plan.absorbed == ["SPEAKER_03", "SPEAKER_04"]
    assert plan.segments_moved == 7


def test_merge_group_serializes_for_the_ui():
    group = MergeGroup(into="SPEAKER_01", absorb=("SPEAKER_03",),
                       reason="similar voice", similarity=0.91)
    assert group.as_dict() == {
        "into": "SPEAKER_01", "absorb": ["SPEAKER_03"],
        "reason": "similar voice", "similarity": 0.91,
    }


# ── The owner's own voice, split (field report 2026-09-10) ──────────
#
# The single most common split: the user's voice arrives twice, once
# down the microphone and once echoed back through the meeting audio.
# Both halves end up named "You". plan_certain_merges steps over it by
# design — the microphone is stronger evidence than a name — so the
# suggestion list is the only thing that can offer the fix, and it has
# to do so WITHOUT an embedding, because the owner routinely has none.

def test_the_owner_sharing_a_name_is_suggested():
    """422 segments labelled "You" beside 212 more labelled "You", and
    nothing offered to fix it. Refusing to ACT on a name collision with
    the owner is right; refusing to MENTION it is not."""
    suggestions = suggest_merges([
        _facts(OWNER, "You", embedding=(), seconds=1800.0, segments=422),
        _facts("SPEAKER_01", "You", embedding=(), seconds=900.0, segments=212),
    ], owner_label=OWNER)
    assert len(suggestions) == 1
    assert set([suggestions[0].into, *suggestions[0].absorb]) == {
        OWNER, "SPEAKER_01"}
    assert suggestions[0].reason == "same name"


def test_the_owner_sharing_a_profile_is_suggested():
    suggestions = suggest_merges([
        _facts(OWNER, "You", profile="p-me", embedding=()),
        _facts("SPEAKER_01", "Jane Doe", profile="p-me", embedding=()),
    ], owner_label=OWNER)
    assert [g.reason for g in suggestions] == ["same known-speaker profile"]


def test_the_owner_suggestion_does_not_need_a_fingerprint():
    """The owner frequently has no centroid at all, so a suggestion list
    built only from voice similarity is empty in precisely the case the
    user is staring at."""
    suggestions = suggest_merges([
        _facts(OWNER, "You", embedding=()),
        _facts("SPEAKER_01", "You", embedding=()),
    ], owner_label=OWNER)
    assert suggestions and suggestions[0].similarity is None


def test_a_different_name_beside_the_owner_is_not_suggested():
    """Everyone else in the meeting is not the user. A suggestion per
    participant would train people to dismiss the panel."""
    assert suggest_merges([
        _facts(OWNER, "You", embedding=()),
        _facts("SPEAKER_01", "Jane Doe", embedding=()),
    ], owner_label=OWNER) == []


def test_two_unnamed_speakers_beside_the_owner_are_not_suggested():
    """Sharing "no name" with the owner is not evidence."""
    assert suggest_merges([
        _facts(OWNER, OWNER, embedding=()),
        _facts("SPEAKER_01", embedding=()),
    ], owner_label=OWNER) == []


def test_the_owner_still_never_merges_automatically():
    """The suggestion exists so the USER can decide. The automatic pass
    must stay out of it — channel attribution's invariant is that
    far-end words are never handed to the user on a guess."""
    assert plan_certain_merges([
        _facts(OWNER, "You", seconds=1800.0),
        _facts("SPEAKER_01", "You", seconds=900.0),
    ], owner_label=OWNER) == []


def test_a_pair_is_offered_once_even_when_two_kinds_of_evidence_agree():
    """A same-name owner pair whose voices also match must not appear
    twice — the user would merge one and be left staring at a duplicate
    pointing at a label that no longer exists."""
    v = [1.0, 0.0]
    suggestions = suggest_merges([
        _facts(OWNER, "You", embedding=v),
        _facts("SPEAKER_01", "You", embedding=v),
    ], owner_label=OWNER)
    assert len(suggestions) == 1


def test_name_evidence_is_offered_before_a_voice_score():
    """A shared name is a decision the app already made; a similarity is
    a measurement it took. Lead with the former."""
    v = [1.0, 0.0]
    suggestions = suggest_merges([
        _facts(OWNER, "You", embedding=()),
        _facts("SPEAKER_01", "You", embedding=()),
        _facts("SPEAKER_02", "Poe", embedding=v),
        _facts("SPEAKER_03", embedding=[0.99, 0.14]),
    ], owner_label=OWNER)
    assert suggestions[0].reason == "same name"
    assert "similar voice" in [g.reason for g in suggestions]


def test_without_an_owner_label_nothing_changes():
    """Every caller that predates the owner parameter keeps the
    behaviour it had: similarity only."""
    v = [1.0, 0.0]
    suggestions = suggest_merges([
        _facts("SPEAKER_01", "You", embedding=v),
        _facts("SPEAKER_02", "You", embedding=v),
    ])
    assert [g.reason for g in suggestions] == ["similar voice"]
