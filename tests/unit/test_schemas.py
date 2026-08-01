import pytest
from pydantic import ValidationError

from backend.config import MAX_INPUT_CHARS
from backend.schemas import PredictRequest


def test_valid_text_parses():
    request = PredictRequest(text="you are wrong about this")
    assert request.text == "you are wrong about this"


def test_text_at_the_cap_is_accepted():
    assert len(PredictRequest(text="a" * MAX_INPUT_CHARS).text) == MAX_INPUT_CHARS


def test_oversize_text_is_rejected():
    """REG-6.3a. Jigsaw comments top out around 5k characters; a moderation endpoint that
    accepts a megabyte of text per request is free CPU for anyone who asks."""
    with pytest.raises(ValidationError, match=f"at most {MAX_INPUT_CHARS}"):
        PredictRequest(text="a" * (MAX_INPUT_CHARS + 1))


def test_empty_text_is_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(text="")


def test_whitespace_only_text_is_rejected():
    with pytest.raises(ValidationError, match="must not be blank"):
        PredictRequest(text="   \n\t  ")


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(text="hello", reviewer_id="not-yours")
