"""Feedback records, and the line between the two kinds of them.

The reviewer rows feed the design-weighted live-accuracy estimate; the user rows feed their
own panel. The type system is where that separation is made structural: a `source='user'`
record has no reviewer, no agreement vector, and a verdict drawn from a two-value
vocabulary, so there is no shape in which an anonymous click can arrive carrying per-label
"truth" that the estimator would then weight.
"""

import pytest

from backend.feedback import USER_VERDICTS, FeedbackRecord, derive_feedback, user_feedback
from model.labels import LABELS


def _labels(**overrides) -> dict[str, int]:
    base = {label: 0 for label in LABELS}
    base.update(overrides)
    return base


def _flags(**overrides) -> dict[str, bool]:
    base = {label: False for label in LABELS}
    base.update(overrides)
    return base


def test_full_agreement_is_an_exact_match():
    record = derive_feedback(
        "r1", _labels(toxic=1, insult=1), _flags(toxic=True, insult=True), "rock"
    )
    assert isinstance(record, FeedbackRecord)
    assert record.source == "reviewer"
    assert record.reviewer_id == "rock"
    assert record.exact_match is True
    assert record.agreement == {label: True for label in LABELS}


def test_one_disagreement_breaks_the_exact_match_and_is_localised():
    record = derive_feedback(
        "r2", _labels(toxic=1, threat=1), _flags(toxic=True), "rock"
    )
    assert record.exact_match is False
    assert record.agreement["threat"] is False
    assert record.agreement["toxic"] is True
    assert sum(1 for ok in record.agreement.values() if not ok) == 1


def test_a_false_positive_is_a_disagreement_too():
    """Agreement is symmetric. A model flag the reviewer did not confirm is as wrong as a
    label the model missed, and an implementation that only checks one direction reports a
    flag-happy model as perfectly accurate."""
    record = derive_feedback("r2b", _labels(), _flags(obscene=True), "rock")
    assert record.agreement["obscene"] is False
    assert record.exact_match is False
    assert sum(1 for ok in record.agreement.values() if not ok) == 1


def test_agreement_on_a_label_neither_side_raised_still_counts_as_agreement():
    record = derive_feedback("r2c", _labels(), _flags(), "rock")
    assert record.agreement == {label: True for label in LABELS}
    assert record.exact_match is True


def test_agreement_keys_are_the_labels_in_order():
    record = derive_feedback("r3", _labels(), _flags(), "rock")
    assert tuple(record.agreement) == LABELS


def test_missing_reviewer_label_is_rejected():
    partial = _labels()
    partial.pop("identity_hate")
    with pytest.raises(ValueError, match="identity_hate"):
        derive_feedback("r4", partial, _flags(), "rock")


def test_an_unknown_reviewer_label_is_rejected():
    """A key the model does not score means the UI and the classifier disagree about the
    label set, which silently drops whichever labels the vector actually carried."""
    extra = _labels()
    extra["spam"] = 1
    with pytest.raises(ValueError, match="spam"):
        derive_feedback("r4b", extra, _flags(), "rock")


def test_non_binary_reviewer_label_is_rejected():
    with pytest.raises(ValueError, match="toxic"):
        derive_feedback("r5", _labels(toxic=2), _flags(), "rock")


@pytest.mark.parametrize("value", [-1, 2, 0.5, "1", None, [1]])
def test_every_non_binary_reviewer_value_is_rejected(value):
    with pytest.raises(ValueError, match="toxic"):
        derive_feedback("r5b", _labels(toxic=value), _flags(), "rock")


def test_a_missing_model_flag_is_rejected_rather_than_read_as_false():
    """Defaulting an absent flag to False turns a truncated prediction into a stream of
    fabricated agreements on the negative class, which is most of the traffic."""
    partial_flags = _flags()
    partial_flags.pop("threat")
    with pytest.raises(ValueError, match="threat"):
        derive_feedback("r5c", _labels(), partial_flags, "rock")


def test_empty_reviewer_id_is_rejected():
    """A reviewer row with no attributable reviewer is not a review."""
    with pytest.raises(ValueError, match="reviewer_id"):
        derive_feedback("r6", _labels(), _flags(), "")


@pytest.mark.parametrize("who", [None, "   ", "\t"])
def test_a_blank_reviewer_id_is_rejected(who):
    with pytest.raises(ValueError, match="reviewer_id"):
        derive_feedback("r6b", _labels(), _flags(), who)


def test_user_feedback_is_a_single_bit_with_no_free_text():
    record = user_feedback("r7", "disagree")
    assert record.source == "user"
    assert record.reviewer_id is None
    assert record.agreement == {}
    assert record.exact_match is False
    assert user_feedback("r8", "agree").exact_match is True


def test_user_verdict_is_a_closed_vocabulary_so_there_is_nothing_to_size_cap():
    with pytest.raises(ValueError, match="verdict"):
        user_feedback("r9", "x" * 5000)
    with pytest.raises(ValueError, match="verdict"):
        user_feedback("r10", "AGREE ")


@pytest.mark.parametrize("verdict", ["Agree", "agree ", " agree", "", None, "yes", "1", "true"])
def test_no_near_miss_verdict_is_coerced_into_the_vocabulary(verdict):
    with pytest.raises(ValueError, match="verdict"):
        user_feedback("r11", verdict)


def test_the_user_vocabulary_is_exactly_two_values():
    """H9's whole point: rubric 3.2 grades a user-feedback mechanism, and the mechanism is
    only safe because there is no per-label channel on it."""
    assert USER_VERDICTS == frozenset({"agree", "disagree"})


def test_a_user_record_can_never_carry_a_reviewer_agreement_vector():
    """The database refuses a reviewer row with an empty agreement object
    (`feedback_reviewer_agreement_ck`); this is the other half -- a user row can never be
    constructed with a non-empty one, so no anonymous click reaches the estimator's input."""
    for verdict in sorted(USER_VERDICTS):
        record = user_feedback("r12", verdict)
        assert record.agreement == {}
        assert record.source == "user"
        assert record.reviewer_id is None


def test_records_are_immutable():
    """The record travels from the API handler to the insert. A mutable one is a place for
    `source` to be rewritten between the two."""
    import dataclasses

    record = user_feedback("r13", "agree")
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign to field 'source'"):
        record.source = "reviewer"
