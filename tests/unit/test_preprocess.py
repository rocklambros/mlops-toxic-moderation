from pathlib import Path

import pytest

import backend.preprocess as preprocess
import model.data.dedup as dedup
import model.normalize as mnorm
from backend.config import MAX_INPUT_CHARS
from backend.preprocess import prepare_input


def test_prepare_input_normalizes_case_and_whitespace():
    assert prepare_input("You  are an   IDIOT") == "you are an idiot"


def test_prepare_input_rejects_text_above_the_cap():
    with pytest.raises(ValueError, match=f"exceeds {MAX_INPUT_CHARS} characters"):
        prepare_input("a" * (MAX_INPUT_CHARS + 1))


def test_prepare_input_accepts_text_at_the_cap():
    assert prepare_input("a" * MAX_INPUT_CHARS)


def test_the_serving_path_uses_the_declared_serving_normalizer():
    """H25. Phase 0 shipped `normalize_for_serving` and the model card claims it. Binding
    the serving path to the corpus normalizer instead makes that claim false and leaves the
    serving normalizer as dead code no consumer imports."""
    assert preprocess.normalize is mnorm.normalize_for_serving


def test_the_corpus_normalizer_is_still_the_one_dedup_uses():
    """The other half of H25: folding must NEVER reach dedup, because that moves
    split_version and therefore the locked 15% test set, after models were registered."""
    assert dedup.normalize is mnorm.normalize
    assert "normalize_for_serving" not in Path("model/data/dedup.py").read_text(encoding="utf-8")


def test_the_serving_path_defeats_the_trick_the_model_card_claims_it_defeats():
    assert prepare_input("уou are an idiot") == "you are an idiot"  # Cyrillic у
    assert prepare_input("You  are an   IDIOT") == "you are an idiot"
    assert dedup.normalize("уou are an idiot") != "you are an idiot"


def test_model_card_folding_claim_matches_the_serving_path():
    """The card is a graded artifact and a public one. If it claims folding, folding runs."""
    card = Path("MODEL_CARD.md").read_text(encoding="utf-8")
    if "homoglyph folding" in card:
        assert prepare_input("уou are an idiot") == "you are an idiot"


def test_the_input_cap_has_one_source_of_truth():
    """Phase 0 says 5000 in model/normalize.py; this phase said 4000 in backend/config.py,
    and both described themselves as authoritative. A cap with two values is a cap that is
    enforced twice at different places and reported wrongly at least once."""
    import backend.config

    assert backend.config.MAX_INPUT_CHARS is mnorm.MAX_INPUT_CHARS
    assert (
        "MAX_INPUT_CHARS: int = 4000" not in Path("backend/config.py").read_text(encoding="utf-8")
    )
