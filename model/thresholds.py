"""Per-label decision thresholds, tuned on out-of-fold validation predictions only.

Three normative facts from the delivery spec (section 6.2) live in this file.

**The tuner never sees the held-out test set.** A threshold is a fitted parameter: pick it on
the rows you later score and the reported precision/recall pair is the tuner's own optimum
read back to itself. So ``tune_thresholds`` accepts :class:`~model.oof.OofPredictions` and
nothing else, and it additionally requires every row to carry a validation-fold id. The type
makes tuning on the test set a deliberate construction rather than a mistyped variable; the
fold ids make concatenating test rows require forging their provenance.

**The cost is asymmetric.** A missed ``threat`` costs far more than a false flag on ``toxic``,
so the objective is F-beta with a *per-label* beta rather than F1 everywhere. ``threat`` gets
beta 5 (recall weighted 25x precision), ``severe_toxic`` and ``identity_hate`` beta 3, and the
three high-prevalence labels stay at beta 1. Ties on the grid resolve to the lower threshold,
which is the recall-favouring direction and therefore the right default here.

**A label with no out-of-fold positives falls back to a neutral 0.5.** F-beta is 0.0 at every
grid point in that case, and a plain ``argmax`` over an all-zero vector returns index 0 -- the
*lowest* threshold on the grid, which flags nearly every comment. Shipping a flag-everything
threshold for the rarest label is the worst available failure, so it is named and neutral.

The second half of this module writes ``baseline_flag_rates.json``: the per-label flag rate on
the **held-out test set** at the promoted thresholds. Rubric 3.2 grades a predicted-class
distribution as target drift, drift is a comparison, and premortem C5 is the observation that
the Phase 3 dashboard has nothing to compare against. This is the only phase that holds the
held-out predictions, so the reference is written here.

Note the deliberate asymmetry between the two entry points. ``tune_thresholds`` refuses raw
arrays, because its input must be out-of-fold. ``compute_baseline_flag_rates`` refuses
``OofPredictions``, because its input must be the held-out test set -- out-of-fold probabilities
come from five different models and encode a distribution no deployed model ever produces.
Each door only opens the right way.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from model.labels import LABELS
from model.oof import OofPredictions

RECALL_WEIGHTS: dict[str, float] = {
    "toxic": 1.0,
    "severe_toxic": 3.0,
    "obscene": 1.0,
    "threat": 5.0,
    "insult": 1.0,
    "identity_hate": 3.0,
}
"""F-beta beta per label. beta weights recall beta-squared times as heavily as precision."""

GRID: np.ndarray = np.round(np.arange(0.05, 0.96, 0.01), 2)
"""Candidate thresholds. Rounded so the written JSON carries no float-repr noise."""

DEFAULT_THRESHOLD: float = 0.5
"""Used only when a label has no out-of-fold positives, or no grid point scores above zero."""

TUNED_ON = "out-of-fold validation predictions"


@dataclass(frozen=True)
class LabelTuning:
    """What the grid search decided for one label, and the evidence it decided on."""

    label: str
    threshold: float
    beta: float
    n_pos: int
    n_neg: int
    f_beta: float
    precision: float
    recall: float
    fell_back: bool


@dataclass(frozen=True)
class ThresholdReport:
    """Thresholds plus the provenance a reviewer needs to check they were tuned honestly.

    ``n_tuning_rows`` is the auditable number: it must match the training-split row count, not
    the deduped corpus count. A reviewer cannot re-derive from the thresholds alone whether the
    held-out test set was involved, so the row and fold counts are written down beside them.
    """

    thresholds: dict[str, float]
    per_label: dict[str, LabelTuning]
    data_version: str
    n_tuning_rows: int
    n_tuning_folds: int
    grid_lo: float
    grid_hi: float


def oof_version(oof: object) -> str:
    """The split identity an OofPredictions carries.

    Phase 0's ``DatasetBundle`` names the realized-split hash ``split_version`` and exposes
    ``data_version`` as a composite of ``raw_sha256``, ``split_version`` and ``env_version``;
    the Phase 1 interface block predates that and names only ``data_version``. Preferring
    ``split_version`` keys the audit record on the realized split, which is the thing thresholds
    are actually specific to. ``model/evaluate.py`` resolves it the same way, and the two must
    not disagree about what identifies a split.
    """
    for name in ("split_version", "data_version"):
        value = getattr(oof, name, None)
        if isinstance(value, str) and value:
            return value
    raise ValueError(
        f"{type(oof).__name__} exposes neither split_version nor data_version, so the "
        "thresholds could not be tied to the split they were tuned on"
    )


def _validated_oof(oof: object) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Accept out-of-fold predictions and nothing else, then check they really are that."""
    if not isinstance(oof, OofPredictions):
        raise TypeError(
            "tune_thresholds only accepts OofPredictions produced by cross_val_probabilities; "
            "accepting raw arrays would let the held-out test set be tuned on. Got "
            f"{type(oof).__name__}"
        )
    y_true = np.asarray(oof.y_true)
    y_prob = np.asarray(oof.y_prob, dtype=float)
    row_fold = np.asarray(oof.row_fold)
    expected = (y_true.shape[0], len(LABELS))
    if y_true.ndim != 2 or y_true.shape != expected:
        raise ValueError(f"y_true must have shape (n, {len(LABELS)}), got {y_true.shape}")
    if y_prob.shape != expected:
        raise ValueError(
            f"y_prob shape {y_prob.shape} does not match y_true shape {y_true.shape}"
        )
    if row_fold.shape != (expected[0],):
        raise ValueError(
            f"row_fold shape {row_fold.shape} does not match {expected[0]} rows of y_true"
        )
    unscored = int(np.count_nonzero(row_fold < 0))
    if unscored:
        raise ValueError(
            f"{unscored} rows never appeared in a validation fold, so no out-of-fold model "
            "produced their probabilities; they cannot be tuned on"
        )
    if not np.isfinite(y_prob).all():
        raise ValueError(
            f"{int(np.count_nonzero(~np.isfinite(y_prob)))} out-of-fold probabilities are "
            "non-finite; cross_val_probabilities leaves NaN where no fold scored a row"
        )
    if y_prob.min() < 0.0 or y_prob.max() > 1.0:
        raise ValueError(
            f"out-of-fold probabilities range [{y_prob.min()}, {y_prob.max()}], which is "
            "outside [0, 1]; these are not calibrated probabilities"
        )
    return y_true.astype(int), y_prob, row_fold.astype(int)


def _validated_weights(recall_weights: dict[str, float]) -> dict[str, float]:
    if set(recall_weights) != set(LABELS):
        raise ValueError(f"recall_weights keys must equal {LABELS}, got {sorted(recall_weights)}")
    out: dict[str, float] = {}
    for label in LABELS:
        beta = float(recall_weights[label])
        if not np.isfinite(beta) or beta <= 0.0:
            raise ValueError(
                f"recall_weights[{label!r}] = {beta} must be a positive finite F-beta weight"
            )
        out[label] = beta
    return out


def _f_beta(tp: float, fp: float, fn: float, beta: float) -> tuple[float, float, float]:
    """F-beta with its precision and recall, defined as 0.0 when nothing true was caught."""
    if tp == 0.0:
        return 0.0, 0.0, 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    b2 = beta * beta
    return (1.0 + b2) * precision * recall / (b2 * precision + recall), precision, recall


def _tune_one(
    label: str, y_true: np.ndarray, y_prob: np.ndarray, beta: float, grid: np.ndarray
) -> LabelTuning:
    n_pos = int(np.count_nonzero(y_true == 1))
    n_neg = int(y_true.shape[0] - n_pos)
    if n_pos == 0:
        return LabelTuning(label, DEFAULT_THRESHOLD, beta, 0, n_neg, 0.0, 0.0, 0.0, True)

    positives = y_prob[y_true == 1]
    negatives = y_prob[y_true == 0]
    best: tuple[float, float, float, float] | None = None
    for threshold in grid:
        tp = float(np.count_nonzero(positives >= threshold))
        fp = float(np.count_nonzero(negatives >= threshold))
        score, precision, recall = _f_beta(tp, fp, n_pos - tp, beta)
        # Strict `>` keeps the FIRST (lowest) threshold among ties, which is the
        # recall-favouring direction and the correct default under asymmetric cost.
        if best is None or score > best[0]:
            best = (score, float(threshold), precision, recall)

    score, threshold, precision, recall = best
    if score <= 0.0:
        # Every grid point missed every positive. Flagging the whole corpus is not the answer.
        return LabelTuning(label, DEFAULT_THRESHOLD, beta, n_pos, n_neg, 0.0, 0.0, 0.0, True)
    return LabelTuning(label, threshold, beta, n_pos, n_neg, score, precision, recall, False)


def threshold_report(
    oof: OofPredictions,
    *,
    recall_weights: dict[str, float] = RECALL_WEIGHTS,
    grid: np.ndarray = GRID,
) -> ThresholdReport:
    """Tune every label and return the thresholds together with their provenance."""
    y_true, y_prob, row_fold = _validated_oof(oof)
    weights = _validated_weights(recall_weights)
    grid = np.asarray(grid, dtype=float)
    if grid.ndim != 1 or grid.size == 0:
        raise ValueError(f"grid must be a non-empty 1-D array of thresholds, got {grid.shape}")

    per_label = {
        label: _tune_one(label, y_true[:, j], y_prob[:, j], weights[label], grid)
        for j, label in enumerate(LABELS)
    }
    return ThresholdReport(
        thresholds={label: float(per_label[label].threshold) for label in LABELS},
        per_label=per_label,
        data_version=oof_version(oof),
        n_tuning_rows=int(y_true.shape[0]),
        n_tuning_folds=int(np.unique(row_fold).size),
        grid_lo=float(grid.min()),
        grid_hi=float(grid.max()),
    )


def tune_thresholds(
    oof: OofPredictions,
    *,
    recall_weights: dict[str, float] = RECALL_WEIGHTS,
    grid: np.ndarray = GRID,
) -> dict[str, float]:
    """Per-label thresholds in LABELS order, tuned on validation folds only.

    One data channel, by design: a second positional parameter is how ``y_test`` would
    eventually get passed in.
    """
    return threshold_report(oof, recall_weights=recall_weights, grid=grid).thresholds


def _validated_thresholds(thresholds: dict[str, float]) -> dict[str, float]:
    if set(thresholds) != set(LABELS):
        raise ValueError(f"thresholds keys must equal {LABELS}, got {sorted(thresholds)}")
    out: dict[str, float] = {}
    for label in LABELS:
        value = float(thresholds[label])
        if not 0.0 < value < 1.0:
            raise ValueError(
                f"thresholds[{label!r}] = {value} is outside (0, 1); 0.0 flags every comment "
                "and 1.0 flags none"
            )
        out[label] = value
    return out


def write_thresholds(
    path: Path,
    thresholds: dict[str, float],
    *,
    data_version: str,
    report: ThresholdReport | None = None,
) -> None:
    """Write thresholds.json plus a sidecar thresholds.meta.json audit record.

    ``thresholds.json`` stays a bare ``{label: float}`` because Phase 2's policy layer loads it
    directly. The provenance goes in the sidecar so the artifact contract never changes shape.

    Without a ``report`` the sidecar records only what this function can actually observe. It
    would be easy to stamp the module-level ``RECALL_WEIGHTS`` in unconditionally, but a caller
    that tuned with custom weights would then ship an audit record that quietly disagrees with
    the numbers beside it, which is worse than an absent field.
    """
    ordered = _validated_thresholds(thresholds)
    if report is not None and report.data_version != data_version:
        raise ValueError(
            f"report.data_version {report.data_version!r} != data_version {data_version!r}; "
            "thresholds tuned on one split must not be filed under another"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ordered, indent=2) + "\n")

    meta: dict = {
        "data_version": data_version,
        "tuned_on": TUNED_ON,
        "generated_at_utc": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
    }
    if report is not None:
        meta["recall_weights"] = {
            label: float(report.per_label[label].beta) for label in LABELS
        }
        meta["n_tuning_rows"] = report.n_tuning_rows
        meta["n_tuning_folds"] = report.n_tuning_folds
        meta["grid"] = {"lo": report.grid_lo, "hi": report.grid_hi}
        meta["per_label"] = {
            label: {
                "threshold": float(tuning.threshold),
                "beta": float(tuning.beta),
                "n_pos": tuning.n_pos,
                "n_neg": tuning.n_neg,
                "f_beta": float(tuning.f_beta),
                "precision": float(tuning.precision),
                "recall": float(tuning.recall),
                "fell_back": tuning.fell_back,
            }
            for label, tuning in ((label, report.per_label[label]) for label in LABELS)
        }
    (path.parent / "thresholds.meta.json").write_text(json.dumps(meta, indent=2) + "\n")


class BaselineFlagRates(BaseModel):
    """The reference distribution the Phase 3 target-drift panel drifts FROM.

    Stamped with the model version, artifact digest and ``data_version`` that produced it, so a
    later mismatch between the deployed model and the baseline is visible rather than silent.
    """

    model_config = ConfigDict(protected_namespaces=())

    # schema_version is REQUIRED by the consumer (monitoring/baseline.py::load_baseline,
    # SUPPORTED_SCHEMA_VERSIONS = {1}) and was absent here, and the consumer reads `n` where
    # this emitted `n_test`. Producer and consumer had never met: both sides test against
    # fixtures in tests/fixtures/, so neither noticed that the real file this writes could
    # never load. Discovered when the first deploy fetched it.
    schema_version: int = 1
    data_version: str
    model_version: str
    model_digest: str
    n: int = Field(ge=1)
    thresholds: dict[str, float]
    flag_rates: dict[str, float]
    generated_at_utc: str

    @model_validator(mode="after")
    def _keys_and_ranges(self) -> "BaselineFlagRates":
        for name, mapping in (("thresholds", self.thresholds), ("flag_rates", self.flag_rates)):
            if set(mapping) != set(LABELS):
                raise ValueError(f"{name} keys must equal {LABELS}")
            for label, value in mapping.items():
                if not 0.0 <= value <= 1.0:
                    raise ValueError(f"{name}[{label}] = {value} is outside [0, 1]")
        return self


def compute_baseline_flag_rates(
    y_prob,
    *,
    data_version: str,
    model_version: str,
    model_digest: str,
    thresholds: dict[str, float],
) -> BaselineFlagRates:
    """Per-label flag rate on the held-out test set at the promoted thresholds."""
    if isinstance(y_prob, OofPredictions):
        raise TypeError(
            "compute_baseline_flag_rates needs held-out test probabilities, not OofPredictions: "
            "out-of-fold scores come from five different models and encode a distribution no "
            "deployed model ever produces, so Phase 3 would drift against a reference that "
            "never existed"
        )
    probs = np.asarray(y_prob, dtype=float)
    if probs.ndim != 2 or probs.shape[1] != len(LABELS):
        raise ValueError(f"expected an (n, {len(LABELS)}) probability matrix, got {probs.shape}")
    if not np.isfinite(probs).all() or probs.min() < 0.0 or probs.max() > 1.0:
        raise ValueError("held-out probabilities contain values outside [0, 1] or non-finite")
    ordered = _validated_thresholds(thresholds)
    thr = np.array([ordered[label] for label in LABELS], dtype=float)
    flags = probs >= thr
    return BaselineFlagRates(
        data_version=data_version,
        model_version=model_version,
        model_digest=model_digest,
        n=int(probs.shape[0]),
        thresholds=ordered,
        flag_rates={label: float(flags[:, j].mean()) for j, label in enumerate(LABELS)},
        generated_at_utc=dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
    )


def write_baseline_flag_rates(path: Path, rates: BaselineFlagRates) -> None:
    """Write baseline_flag_rates.json, the artifact Phase 3 loads as its drift reference."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json.loads(rates.model_dump_json()), indent=2) + "\n")
