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
