"""`compact_vocabularies` is a serialisation fix, so the tests measure serialisation.

A test that only asserted "the values are now ints" would pass against a no-op that cast a
vocabulary which was already plain ints, and would say nothing about the thing that actually
hurt: a 300,000-entry archive that took over an hour to write. These cases pin the cause
(`max_features` produces numpy scalars), the effect (entry count and dump time collapse), and
the invariant that must not move (`transform()` output is unchanged).
"""

import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
import skops.io as sio

from model.pipeline import build_classical_pipeline, compact_vocabularies

DOCS = [
    "you are an idiot and everyone knows it",
    "have a nice day friend, hope you are well",
    "i will find you and hurt you badly",
    "this article needs a citation for the third claim",
    "completely worthless garbage written by a moron",
    "thanks for fixing the typo in the introduction",
] * 12


def _fit(max_features: int | None):
    pipeline = build_classical_pipeline(
        word_max_features=max_features, char_max_features=max_features
    )
    union = pipeline.named_steps["features"]
    union.fit(DOCS)
    return pipeline, dict(union.transformer_list)


def test_max_features_is_what_makes_the_values_numpy_scalars():
    """The premise. sklearn's `_limit_features` reassigns vocabulary_ out of a numpy array, so
    the values come back as np.int64 -- but only when a cap is in force. Without this case the
    fix below could be a no-op on a vocabulary that was already plain ints, and the suite could
    not tell the difference."""
    _, capped = _fit(40)
    values = list(capped["word"].vocabulary_.values())
    assert isinstance(values[0], np.generic), (
        "max_features no longer yields numpy scalars; if sklearn changed this, "
        "compact_vocabularies may be unnecessary -- confirm before deleting it"
    )

    _, uncapped = _fit(None)
    uncapped_values = list(uncapped["word"].vocabulary_.values())
    assert not isinstance(uncapped_values[0], np.generic)


def test_every_vocabulary_value_becomes_a_python_int():
    pipeline, vectorizers = _fit(40)
    rewritten = compact_vocabularies(pipeline)

    assert set(rewritten) == {"word", "char"}, f"a vectoriser was skipped: {rewritten}"
    assert all(count > 0 for count in rewritten.values()), (
        f"nothing was rewritten, so the fix did not reach the vocabularies: {rewritten}"
    )
    for name, vectorizer in vectorizers.items():
        offenders = [v for v in vectorizer.vocabulary_.values() if not isinstance(v, int)]
        assert not offenders, f"{name} still holds {len(offenders)} non-int indices"
        assert not any(isinstance(v, np.generic) for v in vectorizer.vocabulary_.values())


def test_the_mapping_itself_is_untouched():
    """The container changes; the mapping must not. A fix that renumbered a single term would
    silently permute the feature matrix and every coefficient trained against it."""
    pipeline, vectorizers = _fit(40)
    before = {name: dict(v.vocabulary_) for name, v in vectorizers.items()}
    compact_vocabularies(pipeline)
    for name, vectorizer in vectorizers.items():
        assert {k: int(v) for k, v in before[name].items()} == vectorizer.vocabulary_


def test_transform_output_is_bit_identical_after_compaction():
    pipeline, _ = _fit(40)
    union = pipeline.named_steps["features"]
    before = union.transform(DOCS[:20]).toarray()
    compact_vocabularies(pipeline)
    after = union.transform(DOCS[:20]).toarray()
    np.testing.assert_array_equal(before, after)


def test_compaction_collapses_the_archive_that_made_serialisation_quadratic(tmp_path: Path):
    """The finding this exists for. skops writes every numpy object as its own archive member
    and guards each write with `namelist()`, which rebuilds the entry list each call -- so a
    vocabulary of numpy scalars is O(n^2) in entries. On the real 300,000-term model that was
    300,190 entries and 70-87 minutes.
    """
    fat, _ = _fit(40)
    lean, _ = _fit(40)
    compact_vocabularies(lean)

    fat_path, lean_path = tmp_path / "fat.skops", tmp_path / "lean.skops"
    start = time.perf_counter()
    sio.dump(fat, fat_path)
    fat_seconds = time.perf_counter() - start
    start = time.perf_counter()
    sio.dump(lean, lean_path)
    lean_seconds = time.perf_counter() - start

    with zipfile.ZipFile(fat_path) as archive:
        fat_entries = len(archive.namelist())
    with zipfile.ZipFile(lean_path) as archive:
        lean_entries = len(archive.namelist())

    assert lean_entries < fat_entries / 2, (
        f"compaction did not reduce the archive: {fat_entries} -> {lean_entries} entries. "
        "If skops stopped writing one member per numpy object this fix may be obsolete"
    )
    assert lean_seconds <= fat_seconds, (
        f"the lean dump was slower ({lean_seconds:.2f}s vs {fat_seconds:.2f}s), which "
        "contradicts the entry-count reduction above"
    )


def test_the_compacted_model_still_round_trips_through_skops(tmp_path: Path):
    """A smaller artifact that cannot be loaded is not a fix."""
    pipeline, _ = _fit(40)
    compact_vocabularies(pipeline)
    path = tmp_path / "model.skops"
    sio.dump(pipeline.named_steps["features"], path)

    union = pipeline.named_steps["features"]
    loaded = sio.load(path, trusted=sio.get_untrusted_types(file=path))
    np.testing.assert_array_equal(
        union.transform(DOCS[:20]).toarray(), loaded.transform(DOCS[:20]).toarray()
    )


def test_compaction_is_idempotent():
    pipeline, _ = _fit(40)
    first = compact_vocabularies(pipeline)
    second = compact_vocabularies(pipeline)
    assert all(count > 0 for count in first.values())
    assert all(count == 0 for count in second.values()), (
        f"a second pass claims to have rewritten terms again: {second}"
    )


def test_an_unfitted_pipeline_is_not_an_error():
    """The helper runs in a release path that may be handed a pipeline that never fitted."""
    pipeline = build_classical_pipeline()
    assert compact_vocabularies(pipeline) == {}


@pytest.mark.parametrize("cap", [None, 40])
def test_compaction_is_safe_whether_or_not_a_cap_is_in_force(cap):
    pipeline, vectorizers = _fit(cap)
    compact_vocabularies(pipeline)
    for vectorizer in vectorizers.values():
        assert all(isinstance(v, int) for v in vectorizer.vocabulary_.values())
