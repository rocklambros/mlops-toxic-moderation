import pytest

import backend.preprocess as preprocess
import model.data.dedup as dedup
from backend.config import MAX_INPUT_CHARS
from backend.preprocess import prepare_input

SKEW_CORPUS = [
    "You  are an   IDIOT",
    "ＦＵＬＬＷＩＤＴＨ text",
    "  leading and trailing  ",
    "line\nbreaks\tand\ttabs",
    "Ünicode combining áccent",
    "f*ck this garbage",
    "MiXeD CaSe WoRdS",
]


def test_serving_normalizer_is_the_dedup_normalizer_itself():
    """H25. The delivery spec described the serving normalizer as dedup's plus homoglyph
    folding; that is train/serve skew by construction. One function object, asserted, so an
    'improvement' to either side breaks the build instead of silently shifting the input
    distribution the model was fitted on."""
    assert preprocess.normalize is dedup.normalize


def test_no_serving_side_normalization_diverges_from_training():
    for text in SKEW_CORPUS:
        assert prepare_input(text) == dedup.normalize(text)


def test_prepare_input_normalizes_case_and_whitespace():
    assert prepare_input("You  are an   IDIOT") == "you are an idiot"


def test_prepare_input_rejects_text_above_the_cap():
    with pytest.raises(ValueError, match="exceeds 4000 characters"):
        prepare_input("a" * (MAX_INPUT_CHARS + 1))


def test_prepare_input_accepts_text_at_the_cap():
    assert prepare_input("a" * MAX_INPUT_CHARS)
