"""Pure live-co-pilot text helpers — dedup + filler removal.

The field failure was the co-pilot repeating itself ("vendor lock-in",
"request an update") every tick. These guard the logic that kills the
broken record without touching the LLM.
"""

from core._coach_text import (
    dedup_against,
    is_filler,
    is_near_duplicate,
    normalize,
)


def test_normalize_strips_punct_and_case():
    assert normalize("Vendor Lock-In, RISK!!") == "vendor lock in risk"


def test_near_duplicate_catches_rewordings():
    assert is_near_duplicate("Vendor lock-in risk", "vendor lock in risk!")
    assert is_near_duplicate(
        "Request an update on the status", "request an update on status")
    assert not is_near_duplicate(
        "India IDT in-country carrier rule", "Philippines Connect call latency")


def test_near_duplicate_containment():
    # The model loves re-emitting a slightly-expanded version of a prior.
    assert is_near_duplicate(
        "vendor lock-in", "vendor lock-in risk due to [scrubbed] being the sole gateway")


def test_is_filler():
    assert is_filler("Request an update")
    assert is_filler("Schedule a follow-up")
    assert is_filler("Request detailed documentation")
    # Specific, artifact-anchored items are NOT filler.
    assert not is_filler(
        "Request TYR's IDT carrier commitment before the SOW locks")
    assert not is_filler(
        "Confirm SCV telephony model: BYOT vs Amazon-provided")


def test_dedup_against_drops_priors_and_filler():
    new = [
        "Vendor lock-in risk",                              # dup of prior
        "Confirm SCV telephony model (BYOT vs Amazon)",     # keep
        "Request an update",                                # filler
    ]
    prior = ["vendor lock in risk"]
    assert dedup_against(new, prior) == [
        "Confirm SCV telephony model (BYOT vs Amazon)"]


def test_dedup_within_new_batch():
    new = [
        "Ask about India IDT domestic-carriage rules",
        "ask about india idt domestic carriage rules please",  # near-dup
    ]
    assert len(dedup_against(new, [])) == 1


def test_dedup_preserves_distinct_items():
    new = [
        "Confirm Connect region for Philippines agents",
        "Clarify [scrubbed] rate limits and failover",
        "Pin Bedrock agent-core to a phase in the SOW",
    ]
    assert dedup_against(new, []) == new
