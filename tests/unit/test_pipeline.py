"""Tests for the classical pipeline factory.

Four properties are load-bearing here, and each one is a premortem finding rather than a
style preference:

- the fitted pipeline emits an ``(n, 6)`` probability matrix whose columns are in ``LABELS``
  order (delivery spec section 6.2, the output contract);
- every inner logistic regression converges strictly before ``max_iter``, because
  ``solver='saga'`` was measured at 493 s for n=15,000 while hitting the cap WITHOUT
  converging (premortem C3);
- the TF-IDF vectorizers are fitted INSIDE the pipeline, so every cross-validation fold
  relearns the vocabulary and the IDF from its own training rows only. Fitting TF-IDF on the
  full corpus first is the classic silent leak: it does not raise, it does not warn, it just
  inflates every held-out number;
- the outer nesting ``CalibratedClassifierCV(OneVsRestClassifier(...))`` is a hard crash on a
  multi-label target (premortem C4). It is pinned by a test rather than by a comment, because
  the predicted 2 a.m. repair for that crash is to drop calibration, which silently voids the
  output contract's central promise.
"""

import warnings

import numpy as np
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import KFold, cross_validate
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline

from model.contract import probs_to_dict
from model.labels import LABELS
from model.pipeline import (
    CALIBRATION_FOLDS,
    CHAR_MAX_FEATURES,
    MAX_ITER,
    WORD_MAX_FEATURES,
    ConvergenceError,
    DegenerateLabelError,
    FeatureBudgetError,
    assert_converged,
    assert_feature_budget,
    build_classical_pipeline,
    inner_logistic_regressions,
    measure_feature_footprint,
)

# _CUES is imported rather than re-typed: a local copy would drift from the fixture and the
# column-alignment test below would then pass while asserting nothing.
from tests.fixtures.synthetic import _CUES, make_corpus

# Two marker tokens that appear in exactly one half of the corpus each. They are the probe
# used to prove the vectorizer only ever learned from the rows it was fitted on. They are
# repeated across many documents on purpose: min_df=2 would drop a once-only token anyway,
# which would make the leak test pass for the wrong reason.
_MARKER_A = "zqmarkeralpha"
_MARKER_B = "zqmarkerbravo"


def _vectorizers(pipe: Pipeline) -> dict[str, TfidfVectorizer]:
    return dict(pipe.named_steps["features"].transformer_list)


def _word_vocabulary(pipe: Pipeline) -> dict:
    return _vectorizers(pipe)["word"].vocabulary_


def _marked_corpus(n: int = 300, seed: int = 5):
    """A corpus split into two halves, each carrying its own marker token."""
    texts, y = make_corpus(n=n, seed=seed)
    half = n // 2
    marked = [
        f"{text} {_MARKER_A if i < half else _MARKER_B}" for i, text in enumerate(texts)
    ]
    return marked, y, half


@pytest.fixture(scope="module")
def fitted_default():
    """One default-configuration fit, shared by the tests that only read from it."""
    texts, y = make_corpus()
    pipe = build_classical_pipeline()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        pipe.fit(texts, y)
    return pipe, texts, y


# --------------------------------------------------------------------------------------
# Structure: what the factory builds
# --------------------------------------------------------------------------------------


def test_factory_returns_a_pipeline_with_the_vectorizers_inside_it():
    pipe = build_classical_pipeline()
    assert isinstance(pipe, Pipeline)
    vecs = _vectorizers(pipe)
    assert isinstance(vecs["word"], TfidfVectorizer)
    assert isinstance(vecs["char"], TfidfVectorizer)
    # Not fitted at construction time: the vocabulary and the IDF are learned inside each CV
    # fold, which is the classic silent leak this asserts against.
    assert not hasattr(vecs["word"], "vocabulary_")
    assert not hasattr(vecs["char"], "vocabulary_")


def test_both_vectorizers_are_capped_at_the_documented_values():
    vecs = _vectorizers(build_classical_pipeline())
    assert vecs["word"].max_features == WORD_MAX_FEATURES == 200_000
    assert vecs["char"].max_features == CHAR_MAX_FEATURES == 100_000


def test_word_and_char_ngram_ranges_match_the_design():
    vecs = _vectorizers(build_classical_pipeline())
    assert vecs["word"].analyzer == "word"
    assert vecs["word"].ngram_range == (1, 2)
    assert vecs["char"].analyzer == "char_wb"
    assert vecs["char"].ngram_range == (3, 5)


def test_solver_is_liblinear_and_never_saga():
    pipe = build_classical_pipeline()
    base = pipe.named_steps["clf"].estimator.estimator
    assert isinstance(base, LogisticRegression)
    assert base.solver == "liblinear", (
        "saga was measured at 493 s for n=15,000 while hitting max_iter without converging, "
        "against 5.7 s for liblinear converging in 6 iterations (premortem C3)"
    )
    assert base.class_weight == "balanced"
    # The cap has to be high enough that reaching it means something is wrong with the data,
    # not with the budget. liblinear converges on this problem in single-digit iterations.
    assert base.max_iter == MAX_ITER >= 1000


def test_the_factory_returns_a_fresh_unfitted_pipeline_on_every_call():
    """model.oof.cross_val_probabilities uses this as its per-fold factory.

    If two calls shared a vectorizer instance, fold k's fit would overwrite fold k-1's
    vocabulary in place and every fold after the first would score rows against a vectorizer
    fitted on other folds' rows.
    """
    first, second = build_classical_pipeline(), build_classical_pipeline()
    assert first is not second
    assert _vectorizers(first)["word"] is not _vectorizers(second)["word"]
    assert _vectorizers(first)["char"] is not _vectorizers(second)["char"]
    assert first.named_steps["clf"] is not second.named_steps["clf"]
    assert first.named_steps["clf"].estimator is not second.named_steps["clf"].estimator


def test_the_shipped_nesting_is_calibration_inside_one_vs_rest():
    clf = build_classical_pipeline().named_steps["clf"]
    assert isinstance(clf, OneVsRestClassifier)
    assert isinstance(clf.estimator, CalibratedClassifierCV)
    assert isinstance(clf.estimator.estimator, LogisticRegression)
    # sigmoid, never isotonic: `threat` carries roughly 80 per-fold positives on the real
    # corpus and isotonic overfits that badly.
    assert clf.estimator.method == "sigmoid"
    assert clf.estimator.cv == CALIBRATION_FOLDS == 5


# --------------------------------------------------------------------------------------
# The outer nesting is a hard crash, pinned by a test rather than by a comment [C4]
# --------------------------------------------------------------------------------------


def test_the_outer_calibration_nesting_raises_on_a_multi_label_target():
    """CalibratedClassifierCV(OneVsRestClassifier(...)) cannot take an (n, 6) target.

    CalibratedClassifierCV.fit calls LabelEncoder().fit(y). Reproduced on the pinned
    scikit-learn 1.5.2 (premortem C4). This pins the reason so nobody re-derives the
    "obvious" outer wrap at 2 a.m. and then drops calibration when it crashes.
    """
    rng = np.random.default_rng(0)
    x = rng.random((96, 10))
    y = (rng.random((96, len(LABELS))) > 0.7).astype(int)
    wrong = CalibratedClassifierCV(
        OneVsRestClassifier(LogisticRegression(solver="liblinear")), cv=3, method="sigmoid"
    )
    with pytest.raises(
        ValueError, match=r"y should be a 1d array, got an array of shape \(96, 6\)"
    ):
        wrong.fit(x, y)


def test_the_shipped_nesting_fits_the_same_multi_label_target_the_outer_one_rejects():
    """Same (n, 6) target, same data: the difference is the nesting, not the problem."""
    rng = np.random.default_rng(0)
    x = [f"row {i} " + ("idiot " * int(v)) for i, v in enumerate(rng.integers(0, 3, 96))]
    y = (rng.random((96, len(LABELS))) > 0.7).astype(int)
    pipe = build_classical_pipeline(calibration_folds=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(x, y)
    assert pipe.predict_proba(x).shape == (96, len(LABELS))


# --------------------------------------------------------------------------------------
# Output shape and column alignment
# --------------------------------------------------------------------------------------


def test_fitted_pipeline_emits_six_calibrated_columns(fitted_default):
    pipe, texts, _ = fitted_default
    probs = pipe.predict_proba(texts)
    assert probs.shape == (len(texts), len(LABELS))
    assert probs.min() >= 0.0 and probs.max() <= 1.0
    assert np.isfinite(probs).all()


def test_probability_columns_are_in_labels_order(fitted_default):
    """Shape (n, 6) is necessary and not sufficient: the columns must also be the right ones.

    Each synthetic label has a unique cue token, so a probe carrying only that cue must move
    that label's column and no other's. A transposed column order passes every shape check
    and fails this one. probs_to_dict is the single authoritative adapter (premortem H23);
    re-deriving zip(LABELS, row) here would make this test blind to the drift it exists to
    catch.
    """
    pipe, _, _ = fitted_default
    clean_text = "thanks for the edit comment 7"
    clean = probs_to_dict(pipe.predict_proba([clean_text])[0])
    assert list(clean.keys()) == list(LABELS)
    for label, cue in _CUES.items():
        probe = probs_to_dict(pipe.predict_proba([f"{clean_text} {cue}"])[0])
        assert probe[label] > clean[label], f"cue {cue!r} did not move column {label!r}"
        assert max(probe, key=probe.get) == label, (
            f"cue {cue!r} moved {max(probe, key=probe.get)!r} more than {label!r}; "
            "the probability columns are not in LABELS order"
        )


# --------------------------------------------------------------------------------------
# TF-IDF is fitted inside the pipeline, so it is refit inside every CV fold
# --------------------------------------------------------------------------------------


def test_vocabulary_holds_only_terms_from_the_rows_the_pipeline_was_fitted_on():
    texts, y, half = _marked_corpus()
    pipe = build_classical_pipeline(calibration_folds=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(texts[:half], y[:half])
    vocab = _word_vocabulary(pipe)
    assert _MARKER_A in vocab
    assert _MARKER_B not in vocab, (
        "the vectorizer learned a term that appears only in rows it was never fitted on, "
        "which means the vocabulary was built outside the pipeline"
    )


def test_every_cross_validation_fold_refits_the_vectorizer_on_its_own_rows_only():
    """The leak test that matters: a pre-fitted TF-IDF would carry one shared vocabulary.

    cross_validate clones the estimator per fold, so if the vectorizer lives inside the
    pipeline each fold relearns the vocabulary and the IDF from its own training rows. Fold 0
    trains on the second half and must not know the first half's marker, and vice versa.
    """
    texts, y, half = _marked_corpus()
    pipe = build_classical_pipeline(calibration_folds=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cross_validate(
            pipe, texts, y, cv=KFold(n_splits=2, shuffle=False), return_estimator=True
        )
    held_out_marker = {0: _MARKER_A, 1: _MARKER_B}
    trained_on_marker = {0: _MARKER_B, 1: _MARKER_A}
    for fold, estimator in enumerate(result["estimator"]):
        vocab = _word_vocabulary(estimator)
        assert trained_on_marker[fold] in vocab
        assert held_out_marker[fold] not in vocab, (
            f"fold {fold}'s vectorizer knows a term that appears only in its validation "
            "rows; the TF-IDF was fitted outside the cross-validation loop and every "
            "held-out score from this configuration is inflated"
        )
    # The object handed to cross_validate is never mutated, so the factory's pipeline cannot
    # smuggle a full-corpus vocabulary into a later fit.
    assert not hasattr(_vectorizers(pipe)["word"], "vocabulary_")
    assert not hasattr(_vectorizers(pipe)["char"], "vocabulary_")
    assert len({len(_word_vocabulary(est)) for est in result["estimator"]}) >= 1


def test_the_two_folds_learn_different_vocabularies():
    """A shared, leaked vocabulary would be identical across folds; disjoint rows are not."""
    texts, y, _ = _marked_corpus()
    pipe = build_classical_pipeline(calibration_folds=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = cross_validate(
            pipe, texts, y, cv=KFold(n_splits=2, shuffle=False), return_estimator=True
        )
    vocabs = [set(_word_vocabulary(est)) for est in result["estimator"]]
    assert vocabs[0] != vocabs[1]


# --------------------------------------------------------------------------------------
# Convergence [C3]
# --------------------------------------------------------------------------------------


def test_inner_estimators_are_reachable_and_counted():
    texts, y = make_corpus(n=400)
    pipe = build_classical_pipeline(calibration_folds=3).fit(texts, y)
    inner = inner_logistic_regressions(pipe)
    # six labels x three calibration folds
    assert len(inner) == len(LABELS) * 3
    assert all(isinstance(lr, LogisticRegression) for lr in inner)


def test_every_inner_estimator_converged_strictly_before_max_iter(fitted_default):
    """The fixture already fits with ConvergenceWarning promoted to an error.

    n_iter_ < max_iter is asserted directly as well, because a solver can stop at the cap
    without emitting a warning if the warning category is ever filtered upstream.
    """
    pipe, _, _ = fitted_default
    inner = inner_logistic_regressions(pipe)
    assert inner
    for lr in inner:
        n_iter = int(np.max(np.atleast_1d(lr.n_iter_)))
        assert n_iter < lr.max_iter, f"solver stopped at the cap: {n_iter} >= {lr.max_iter}"
    assert_converged(pipe)


def test_assert_converged_raises_when_iterations_hit_the_cap():
    texts, y = make_corpus(n=400)
    pipe = build_classical_pipeline(calibration_folds=3, max_iter=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(texts, y)
    with pytest.raises(ConvergenceError, match="hit max_iter"):
        assert_converged(pipe)


def test_a_label_that_collapsed_to_a_constant_predictor_is_not_reported_as_converged():
    """A label with no positives in the training rows is not calibrated at all.

    OneVsRestClassifier substitutes a _ConstantPredictor for that column, which has no
    inner estimator and no calibrator. Silently skipping it would let assert_converged pass
    on a model that returns a constant, uncalibrated probability for a whole label.
    """
    texts, y = make_corpus(n=300, seed=2)
    y[:, LABELS.index("threat")] = 0
    pipe = build_classical_pipeline(calibration_folds=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(texts, y)
    with pytest.raises(DegenerateLabelError, match="threat"):
        assert_converged(pipe)


# --------------------------------------------------------------------------------------
# Measured feature footprint [C11 / H11 cap]
# --------------------------------------------------------------------------------------


def test_footprint_reports_real_measured_bytes_and_the_cap_binds_both_vectorizers():
    texts, _ = make_corpus(n=600)
    capped = measure_feature_footprint(texts, word_max_features=50, char_max_features=30)
    uncapped = measure_feature_footprint(
        texts, word_max_features=None, char_max_features=None
    )
    assert capped.n_rows == 600
    assert capped.n_word_features == 50
    assert capped.n_char_features == 30
    assert capped.n_features == capped.n_word_features + capped.n_char_features
    assert capped.nnz > 0
    # CSR bytes are the sum of the three real arrays, so the number is measured, not modelled.
    assert capped.matrix_bytes >= capped.nnz * (8 + 4)
    assert capped.bytes_per_row == pytest.approx(capped.matrix_bytes / capped.n_rows)
    assert uncapped.n_features > capped.n_features, (
        "the corpus must exceed the cap for this test to prove the cap does anything"
    )


def test_budget_check_raises_with_the_projection_in_the_message():
    texts, _ = make_corpus(n=600)
    fp = measure_feature_footprint(texts, word_max_features=50, char_max_features=30)
    assert assert_feature_budget(fp, n_rows_full=180_633, max_bytes=2_000_000_000) > 0
    with pytest.raises(FeatureBudgetError, match="exceeds the"):
        assert_feature_budget(fp, n_rows_full=180_633, max_bytes=1_000)
