"""Out-of-fold probabilities, and the type that carries them.

Threshold tuning must never see the held-out test set, and it must never see a fold's own
training rows either -- probabilities from a model that fitted the row it is scoring are
optimistic, and a threshold tuned on them sits too high. The only honest input is
out-of-fold: every row scored by a model that did not see it.

`OofPredictions` exists so that constraint is carried by the type rather than by a comment.
`tune_thresholds` accepts nothing else, so passing test-set probabilities is a TypeError at
the call site instead of a silently inflated threshold.

The `split_version` field ties the probabilities to the realized split that produced them.
Phase 0's `DatasetBundle` computes that hash over train/test/fold membership plus a per-id
label fingerprint plus the pinned split-library versions, so a relabel, a reseed or a
dependency bump all produce a different value. Thresholds tuned against one split cannot be
silently reused against another.
"""

from dataclasses import dataclass

import numpy as np

from model.labels import LABELS


@dataclass(frozen=True, eq=False)
class OofPredictions:
    """Every training row scored exactly once, by a fold that excluded it.

    eq=False because the arrays make a generated __eq__ raise rather than compare.
    """

    y_true: np.ndarray  # (n, 6) int, columns ordered by LABELS
    y_prob: np.ndarray  # (n, 6) float in [0, 1], same ordering
    row_fold: np.ndarray  # (n,) int, which fold produced each row's probability
    split_version: str

    @property
    def data_version(self) -> str:
        """Alias for split_version, mirroring Phase 0's DatasetBundle.

        DatasetBundle exposes both: split_version names the realized split, data_version is
        the composite used for single-string display. Consumers reach for either, so both
        resolve here rather than each call site guessing which exists.
        """
        return self.split_version

    def __post_init__(self) -> None:
        # Only the invariant this type exists to carry. Shape, dtype and range checks live in
        # thresholds._validated_oof, which owns them and produces the error messages its own
        # tests assert on -- duplicating them here would make a deliberately malformed object
        # unconstructable and those tests unwritable.
        if not self.split_version:
            raise ValueError("split_version is required so thresholds cannot outlive their split")

def cross_val_probabilities(pipeline_factory, bundle) -> OofPredictions:
    """Fit one model per fold and score only that fold's validation rows.

    The factory is called fresh per fold rather than a single estimator being refit, so no
    vectoriser vocabulary or calibration survives across folds. Fitting TF-IDF once over the
    whole corpus is the classic silent leak this guards against.
    """
    train_df = bundle.train_df
    y = train_df[list(LABELS)].to_numpy()
    texts = train_df["comment_text"].to_numpy()

    y_prob = np.zeros((len(train_df), len(LABELS)), dtype=float)
    row_fold = np.full(len(train_df), -1, dtype=int)

    for fold, (tr_idx, va_idx) in enumerate(bundle.fold_indices):
        model = pipeline_factory()
        model.fit(texts[tr_idx], y[tr_idx])
        y_prob[va_idx] = model.predict_proba(texts[va_idx])
        row_fold[va_idx] = fold

    if (row_fold < 0).any():
        raise ValueError(
            f"{int((row_fold < 0).sum())} rows were never in a validation fold, so their "
            "probabilities would be in-sample; the fold indices do not cover the training set"
        )

    return OofPredictions(
        y_true=y, y_prob=y_prob, row_fold=row_fold, split_version=bundle.split_version
    )
