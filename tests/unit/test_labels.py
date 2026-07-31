from model.labels import LABELS


def test_labels_exact_order_and_count():
    assert LABELS == ("toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate")
    assert len(LABELS) == 6


def test_labels_is_immutable_tuple():
    assert isinstance(LABELS, tuple)
