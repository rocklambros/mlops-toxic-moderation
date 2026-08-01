"""Classical TF-IDF + one-vs-rest calibrated logistic regression pipeline.

Three normative constraints from the delivery spec section 6.2 live here, and all three were
established by measurement rather than by preference:

- ``solver='liblinear'``. ``saga`` was measured on this build box at 493 s for n=15,000 while
  hitting ``max_iter`` WITHOUT converging, against 5.7 s for liblinear converging in 6
  iterations. Extrapolated to six labels by five folds at the real 180,633-row training split
  that is roughly 50 hours against a two-day budget, and the result would still carry a
  ConvergenceWarning -- meaning the coefficients are wherever the optimiser happened to stop.
  ``assert_converged`` turns that from a warning into a test failure.
- ``max_features`` caps on both vectorizers, with the resulting memory measured rather than
  assumed. The measurement below is the real corpus, not a fixture, and it moves the
  justification for the caps: they buy very little design-matrix memory and a great deal of
  *serving* memory.
- Calibration nests INSIDE the one-vs-rest wrapper. The outer nesting
  ``CalibratedClassifierCV(OneVsRestClassifier(...))`` raises
  ``ValueError: y should be a 1d array, got an array of shape (n, 6) instead.`` because
  ``CalibratedClassifierCV.fit`` calls ``LabelEncoder().fit(y)``. ``method='sigmoid'``, never
  isotonic: ``threat`` carries roughly 80 per-fold positives and isotonic overfits that badly.

The vectorizers are constructed unfitted and live inside the ``Pipeline``. That placement is
the whole point: scikit-learn clones the pipeline per cross-validation fold, so the vocabulary
and the IDF are relearned from each fold's training rows. Fitting TF-IDF once on the full
corpus and then cross-validating the classifier alone is the classic silent leak -- it does not
raise, it does not warn, it just inflates every held-out number.

Measured feature footprint. 180,633 rows of real ``comment_text`` -- the size of the deduped
training split -- fitted through ``measure_feature_footprint`` on this build box, 2026-07-31:

===========================  ==================  ==================
quantity                     capped (shipped)    uncapped
===========================  ==================  ==================
word features                           200,000             733,962
char features                           100,000             489,928
total features                          300,000           1,223,890
stored non-zeros                    102,117,351         106,747,387
CSR design matrix                       1.23 GB             1.28 GB
bytes per row                             6,788               7,096
peak RSS during the fit                 4.34 GB             4.49 GB
OvR coefficients, 6 x 5 folds           0.072 GB            0.294 GB
===========================  ==================  ==================

Two conclusions, both of which correct the received wisdom this project inherited:

1. **The cap barely touches the design matrix.** CSR memory is driven by stored non-zeros,
   not by column count, and the terms the cap removes are rare ones contributing few non-zeros
   each -- ``min_df=2``/``min_df=3`` already did the heavy pruning. 1.23 GB against 1.28 GB is
   a 4% saving. The premortem's "~4.7M features and a ~1.7 GB matrix at 135k rows" does not
   reproduce with these ``min_df`` settings on this corpus.
2. **The cap is load-bearing where it was always claimed to matter: EC2 #1.** One-vs-rest keeps
   one coefficient vector per label per calibration fold, so serving memory is linear in the
   feature count: 30 vectors of 300,000 float64 is 72 MB, and uncapped it is 294 MB, on top of
   a vocabulary of 1.2M rather than 300k Python strings. That is the 4 GB instance's budget,
   not the training box's.

Training itself needs ~4.4 GB of RSS for the vectorizer fit alone, so it belongs on the build
box and not on EC2 -- which is where the design already puts it.
"""

from dataclasses import dataclass

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import FeatureUnion, Pipeline

from model.labels import LABELS

WORD_MAX_FEATURES: int = 200_000
CHAR_MAX_FEATURES: int = 100_000
# 2000 rather than the 1000 the plan drafted: liblinear converges on this problem in single
# digits of iterations, so the cap costs nothing when it is not reached, and headroom is what
# keeps `assert_converged` a signal about the data rather than about the cap.
MAX_ITER: int = 2000
CALIBRATION_FOLDS: int = 5
SOLVER: str = "liblinear"
CALIBRATION_METHOD: str = "sigmoid"


def build_classical_pipeline(
    *,
    word_max_features: int | None = WORD_MAX_FEATURES,
    char_max_features: int | None = CHAR_MAX_FEATURES,
    C: float = 1.0,
    calibration_folds: int = CALIBRATION_FOLDS,
    method: str = CALIBRATION_METHOD,
    max_iter: int = MAX_ITER,
    seed: int = 42,
) -> Pipeline:
    """Build the unfitted classical pipeline.

    Returns a fresh, unfitted ``Pipeline`` on every call, which is what lets it be used as the
    per-fold factory in ``model.oof.cross_val_probabilities`` without state crossing folds.
    """
    features = FeatureUnion(
        [
            (
                "word",
                TfidfVectorizer(
                    ngram_range=(1, 2),
                    min_df=2,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    max_features=word_max_features,
                ),
            ),
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    min_df=3,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    max_features=char_max_features,
                ),
            ),
        ]
    )
    base = LogisticRegression(
        solver=SOLVER,
        class_weight="balanced",
        C=C,
        max_iter=max_iter,
        random_state=seed,
    )
    calibrated = CalibratedClassifierCV(base, cv=calibration_folds, method=method)
    # n_jobs=1 deliberately: each parallel job copies the sparse design matrix, and the
    # memory budget measured by measure_feature_footprint assumes one copy.
    return Pipeline([("features", features), ("clf", OneVsRestClassifier(calibrated, n_jobs=1))])


class ConvergenceError(RuntimeError):
    """A fitted inner estimator hit max_iter without converging."""


class DegenerateLabelError(RuntimeError):
    """A label was fitted as a constant predictor, so it is neither trained nor calibrated."""


def inner_logistic_regressions(fitted: Pipeline) -> list[LogisticRegression]:
    """Every fitted base estimator inside the OvR-of-calibrated stack.

    Path: ``Pipeline["clf"]`` -> ``OneVsRestClassifier.estimators_`` (one
    ``CalibratedClassifierCV`` per label) -> ``.calibrated_classifiers_`` (one per calibration
    fold) -> ``.estimator``. The ``.estimator`` attribute name is correct for the pinned
    scikit-learn 1.5.2; it was ``base_estimator`` before 1.2.

    Raises ``DegenerateLabelError`` if any label collapsed to a ``_ConstantPredictor``, which
    is what ``OneVsRestClassifier`` substitutes when a label has a single class in the training
    rows. Skipping those silently would let a model that returns a constant, uncalibrated
    probability for a whole label pass the convergence gate.
    """
    out: list[LogisticRegression] = []
    per_label = fitted.named_steps["clf"].estimators_
    for index, estimator in enumerate(per_label):
        label = LABELS[index] if index < len(LABELS) else f"column {index}"
        calibrated_folds = getattr(estimator, "calibrated_classifiers_", None)
        if calibrated_folds is None:
            raise DegenerateLabelError(
                f"label {label!r} was fitted as {type(estimator).__name__}, not a "
                "CalibratedClassifierCV: OneVsRestClassifier substitutes a constant predictor "
                "when a label has only one class in the training rows, so that column is "
                "neither trained nor calibrated"
            )
        for per_fold in calibrated_folds:
            out.append(per_fold.estimator)
    return out


def assert_converged(fitted: Pipeline) -> None:
    """Raise unless every inner estimator converged before max_iter.

    A ConvergenceWarning-tainted Production artifact is a correctness failure, not a style
    complaint: the coefficients are wherever the optimiser happened to stop.
    """
    inner = inner_logistic_regressions(fitted)
    bad = []
    for i, lr in enumerate(inner):
        n_iter = int(np.max(np.atleast_1d(lr.n_iter_)))
        if n_iter >= lr.max_iter:
            bad.append((i, n_iter, lr.max_iter))
    if bad:
        raise ConvergenceError(
            f"{len(bad)} of {len(inner)} inner estimators hit max_iter without converging "
            f"(first: index={bad[0][0]} n_iter={bad[0][1]} max_iter={bad[0][2]}, solver={SOLVER})"
        )


@dataclass(frozen=True)
class FeatureFootprint:
    n_rows: int
    n_word_features: int
    n_char_features: int
    n_features: int
    nnz: int
    matrix_bytes: int
    bytes_per_row: float


class FeatureBudgetError(RuntimeError):
    """The projected design matrix does not fit the target instance."""


def measure_feature_footprint(
    texts,
    *,
    word_max_features: int | None = WORD_MAX_FEATURES,
    char_max_features: int | None = CHAR_MAX_FEATURES,
) -> FeatureFootprint:
    """Fit only the FeatureUnion and report the real CSR byte counts.

    The bytes are the three real CSR arrays -- ``data``, ``indices``, ``indptr`` -- not an
    estimate derived from the feature count. That distinction is the whole point: the estimate
    everyone reaches for scales with columns, and the measurement scales with stored non-zeros,
    which is why capping ``max_features`` turned out to save 4% of this matrix and 75% of the
    serving-side coefficient memory. See the module docstring for the measured table.
    """
    pipe = build_classical_pipeline(
        word_max_features=word_max_features, char_max_features=char_max_features
    )
    union = pipe.named_steps["features"]
    matrix = union.fit_transform(list(texts))
    word = dict(union.transformer_list)["word"]
    char = dict(union.transformer_list)["char"]
    nbytes = int(matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes)
    return FeatureFootprint(
        n_rows=matrix.shape[0],
        n_word_features=len(word.vocabulary_),
        n_char_features=len(char.vocabulary_),
        n_features=matrix.shape[1],
        nnz=int(matrix.nnz),
        matrix_bytes=nbytes,
        bytes_per_row=nbytes / matrix.shape[0],
    )


def assert_feature_budget(
    footprint: FeatureFootprint, *, n_rows_full: int, max_bytes: int
) -> int:
    """Project the measured bytes-per-row to the full corpus and enforce the budget."""
    projected = int(footprint.bytes_per_row * n_rows_full)
    if projected > max_bytes:
        raise FeatureBudgetError(
            f"projected design matrix {projected / 1e9:.2f} GB at {n_rows_full} rows exceeds the "
            f"{max_bytes / 1e9:.2f} GB budget (measured {footprint.bytes_per_row:.0f} B/row over "
            f"{footprint.n_features} features); lower max_features"
        )
    return projected
