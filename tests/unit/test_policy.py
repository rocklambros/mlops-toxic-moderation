import json

import pytest

from backend.policy import BLOCK_MARGIN, REVIEW_MARGIN, decide, load_thresholds
from model.labels import LABELS

THRESHOLDS = {label: 0.50 for label in LABELS}


def probs(**overrides) -> dict[str, float]:
    values = {label: 0.01 for label in LABELS}
    values.update(overrides)
    return values


def test_probability_exactly_at_the_threshold_flags():
    result = decide(probs(insult=0.50), THRESHOLDS)
    assert result.flags["insult"] is True


def test_probability_just_below_the_threshold_does_not_flag():
    result = decide(probs(insult=0.4999), THRESHOLDS)
    assert result.flags["insult"] is False


def test_severe_toxic_forces_toxic_before_the_response_is_built():
    """H22 and delivery spec section 6.2. The contract must never carry 'severe but not
    toxic'. Enforcing it in the policy means the contract validator is a backstop that never
    fires in production rather than the only thing standing between the model and the UI."""
    result = decide(probs(toxic=0.02, severe_toxic=0.91), THRESHOLDS)
    assert result.flags["severe_toxic"] is True
    assert result.flags["toxic"] is True


def test_coherence_does_not_invent_probabilities():
    result = decide(probs(toxic=0.02, severe_toxic=0.91), THRESHOLDS)
    assert result.max_prob == pytest.approx(0.91)


def test_high_confidence_severe_label_blocks():
    result = decide(probs(threat=0.50 + BLOCK_MARGIN), THRESHOLDS)
    assert result.decision == "block"


def test_severe_label_just_over_the_threshold_reviews_rather_than_blocks():
    result = decide(probs(threat=0.51), THRESHOLDS)
    assert result.decision == "review"


def test_high_confidence_non_severe_label_reviews_rather_than_blocks():
    result = decide(probs(obscene=0.99), THRESHOLDS)
    assert result.decision == "review"


def test_near_threshold_reviews_even_without_a_flag():
    result = decide(probs(toxic=0.50 - REVIEW_MARGIN), THRESHOLDS)
    assert all(flag is False for flag in result.flags.values())
    assert result.decision == "review"


def test_clearly_benign_is_allowed():
    result = decide(probs(), THRESHOLDS)
    assert result.decision == "allow"
    assert result.max_prob == pytest.approx(0.01)


def test_flags_are_returned_in_label_order():
    assert list(decide(probs(), THRESHOLDS).flags) == list(LABELS)


def test_missing_label_is_a_hard_error():
    incomplete = {label: 0.1 for label in LABELS if label != "threat"}
    with pytest.raises(ValueError, match="threat"):
        decide(incomplete, THRESHOLDS)


def test_load_thresholds_accepts_a_complete_file(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(THRESHOLDS), encoding="utf-8")
    assert load_thresholds(path) == THRESHOLDS


def test_load_thresholds_rejects_an_incomplete_file(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"toxic": 0.5}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        load_thresholds(path)


def test_load_thresholds_rejects_an_out_of_range_value(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({**THRESHOLDS, "threat": 1.4}), encoding="utf-8")
    with pytest.raises(ValueError, match="threat"):
        load_thresholds(path)


# The tuned thresholds this project actually deploys, from the Phase 1 artifact. The suite
# above runs entirely against a uniform 0.50, which is a comfortable value that hides the
# whole class of bug below: with REVIEW_MARGIN = 0.10 a threshold of 0.50 leaves a review
# band floor of 0.40, and everything behaves. Three of the six real thresholds are 0.05.
TUNED_THRESHOLDS = {
    "toxic": 0.31,
    "severe_toxic": 0.05,
    "obscene": 0.25,
    "threat": 0.05,
    "insult": 0.28,
    "identity_hate": 0.05,
}


def test_allow_is_reachable_under_the_tuned_thresholds():
    """`allow` was dead code in production for the entire life of the deployment.

    The review band is `threshold - REVIEW_MARGIN`. REVIEW_MARGIN is 0.10 and severe_toxic,
    threat and identity_hate are all tuned to 0.05, so the band floor was -0.05. No
    probability is ever below zero, so the review branch matched unconditionally and the
    else that produces `allow` could not be reached by any input at all.

    Observed 2026-08-02 against the live backend: "Thank you for the helpful edit, I
    appreciate it." scored toxic=0.00023 with every flag false, and came back
    decision="review". Seeding 2000 held-out comments produced 2000 review rows, 0 allows,
    and seed_demo's own exit criterion failed with "the random-audit stratum is empty, so
    live accuracy stays biased" -- which is the downstream harm: the audit stratum samples
    allowed traffic, so a system that never allows anything can never measure its own
    false-negative rate."""
    result = decide({label: 0.0 for label in LABELS}, TUNED_THRESHOLDS)
    assert result.decision == "allow", (
        f"a zero-probability input is not allowed under the deployed thresholds; "
        f"got {result.decision}"
    )


@pytest.mark.parametrize("threshold", [0.01, 0.05, 0.10, 0.25, 0.50, 0.99])
def test_a_zero_probability_input_is_allowed_at_every_threshold(threshold):
    """The property the case above is one instance of. Whatever the tuning produces, an
    input the model is certain is clean must be allowed, so no future retune can quietly
    make `allow` unreachable again."""
    result = decide({label: 0.0 for label in LABELS}, {label: threshold for label in LABELS})
    assert result.decision == "allow", f"threshold {threshold} makes allow unreachable"


def test_the_review_band_never_extends_below_zero():
    """Stated directly, so the failure names the cause rather than a symptom. A band floor
    at or below zero is not a wide band -- it is a disabled branch."""
    from backend.policy import review_floor

    for label, threshold in TUNED_THRESHOLDS.items():
        assert review_floor(threshold) > 0.0, (
            f"{label}: review band floor is {review_floor(threshold)}, so every input "
            f"matches the review branch and `allow` is unreachable"
        )
