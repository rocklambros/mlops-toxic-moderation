import hashlib
from pathlib import Path

from model.normalize import (
    CORPUS_NORMALIZER_ID,
    MAX_INPUT_CHARS,
    normalize,
    normalize_for_serving,
)

GOLDEN: tuple[tuple[str, str], ...] = (
    ("You  are an   IDIOT", "you are an idiot"),
    ("  leading and trailing  ", "leading and trailing"),
    ("ＦＵＬＬＷＩＤＴＨ", "fullwidth"),
    # Escaped on purpose. The digest below is taken over the source strings, so a raw
    # accented character here would make the frozen digest depend on which Unicode form
    # the file happens to be saved in; an editor that re-normalizes the file would then
    # fire "the corpus normalizer changed" without the normalizer having changed. The
    # decomposed input is also the stronger probe: NFKC is a no-op on the composed form.
    ("Ha\u0308ndbuch", "h\u00e4ndbuch"),
    ("tabs\tand\nnewlines", "tabs and newlines"),
    ("STRASSE", "strasse"),
    ("", ""),
)
GOLDEN_SHA256 = "b9ef0fc2b3e284b9f07c92e1ec124dc418e9296db0f0e75bca18c396ca9ed589"


def _golden_digest() -> str:
    payload = "\n".join(f"{src!r}=>{normalize(src)!r}" for src, _ in GOLDEN)
    return hashlib.sha256(payload.encode()).hexdigest()


def test_corpus_normalizer_matches_its_golden_table():
    for src, expected in GOLDEN:
        assert normalize(src) == expected, src


def test_corpus_normalizer_is_frozen():
    assert _golden_digest() == GOLDEN_SHA256, (
        "the corpus normalizer changed. That moves which rows dedup collapses, which "
        "moves the locked 15% test set, which invalidates every registered model. "
        f"Bump {CORPUS_NORMALIZER_ID} and re-run `make data` deliberately, or revert."
    )


def test_serving_normalizer_is_a_strict_superset():
    cyrillic = "уou are an idiot"
    assert normalize(cyrillic) != "you are an idiot"
    assert normalize_for_serving(cyrillic) == "you are an idiot"


def test_serving_normalizer_agrees_with_corpus_normalizer_on_ascii():
    for src, expected in GOLDEN:
        if src.isascii():
            assert normalize_for_serving(src) == expected, src


def test_serving_normalizer_is_idempotent_and_composes():
    probes = ["You  are an   IDIOT", "уou are an idiоt", "  spaced  out  "]
    for probe in probes:
        once = normalize_for_serving(probe)
        assert normalize_for_serving(once) == once
        assert normalize_for_serving(normalize(probe)) == once


def test_serving_normalizer_caps_length():
    assert len(normalize_for_serving("a" * (MAX_INPUT_CHARS * 2))) == MAX_INPUT_CHARS


def test_dedup_does_not_use_the_serving_normalizer():
    source = Path("model/data/dedup.py").read_text()
    assert "normalize_for_serving" not in source, (
        "wiring the serving normalizer into dedup retroactively changes the locked split"
    )
