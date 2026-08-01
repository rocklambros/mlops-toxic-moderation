"""Cross-validated evaluation, stratified bootstrap intervals, and the once-only held-out guard.

Three normative constraints from the delivery spec live here, and each one exists because the
obvious implementation fails quietly rather than loudly.

**Intervals are stratified.** `threat` is under 0.3% of the corpus. A naive bootstrap of a small
stratum loses every positive some of the time, and `average_precision_score` then returns 0.0
with only a `UserWarning` -- it does *not* crash. The interval's lower bound is dragged to the
floor and a promote decision happens inside noise. `stratified_bootstrap_ci` resamples the
positive and negative strata separately, so every resample carries exactly the observed number
of positives. `multilabel_stratified_bootstrap_ci` does the same job for the aggregate metrics by
stratifying on the exact label *pattern*: that preserves every column's positive count at once
while keeping rows intact, so the label correlations survive the resample.

**Accuracy is computed and never promoted on.** Rubric 1.2 lists accuracy among the metrics each
run must log and rubric 3.2 puts live accuracy on the dashboard, so it must exist. An all-negative
predictor scores about 90% on this corpus while catching nothing, so it must never select. The
ban is `select_best_run` raising, not a sentence in a document.

**The held-out test set is evaluated once per `split_version`, and the guard is a file.** A
module-level `_already_evaluated = False` guards nothing: RunPod pods are ephemeral by design and
every fresh interpreter starts with the flag clear. The guard is a git-tracked markdown ledger
that survives a new process, a new pod, and a fresh clone. The refusal lands *before* the rows are
scored, and the touch is recorded *before* the intervals are computed, so neither a repeat call
nor a crash downstream can leave the test set quietly re-runnable.

The test set evaluates the ONE model cross-validation already chose. It never *chooses* between
models: picking the better of two test numbers is selection on the test set and biases the winner
upward.
"""

import datetime as dt
import json
import math
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score

from model.contract import probs_to_dict

# Module scope is safe in this direction only: `model.fairness` defers its import back to this
# module to call time, so the cycle never closes whichever entrypoint loads first.
from model.fairness import identity_fairness_report
from model.labels import LABELS

LEDGER_PATH = Path("docs/test-set-touch-log.md")

PROMOTION_METRIC = "macro_f1"
FORBIDDEN_PROMOTION_KEYS = frozenset({"accuracy", "subset_accuracy", "macro_accuracy"})

#: Below this many positives an interval is reported as under-powered rather than trusted.
LOW_POWER_BELOW = 30

_HEADER = """# Held-out test-set touch log

The held-out test set is evaluated **once** per `split_version`, on the single model that
cross-validation already chose. It never *chooses* between candidate models: picking the better
of two test numbers is selection on the test set and biases the winner upward.

This file is the guard. It is tracked in git, so it survives an ephemeral RunPod pod, a fresh
interpreter, and a fresh clone. A second entry for the same `split_version` is refused by
`model.evaluate.record_touch`.

"""

_ROW = re.compile(r"^\|\s*split_version\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$", re.MULTILINE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

_HEADLINE_KEYS = ("macro_f1", "macro_pr_auc", "accuracy")


class ForbiddenPromotionMetric(ValueError):
    """Run selection was attempted on a metric the design bans for selection."""


class TestSetAlreadyTouched(RuntimeError):
    """The held-out test set was already evaluated for this split_version."""

    # pytest collects any class named Test*; this is an exception, not a test case.
    __test__ = False


class LedgerNotTracked(RuntimeError):
    """The ledger is not tracked by git, so the guard would not be durable."""


@dataclass(frozen=True)
class CIResult:
    """A point estimate and its bootstrap interval, with the power it was computed at.

    `lo`/`hi` are `None` when no interval could be formed -- too few positives, or every
    replicate non-finite. `low_power` and `reason` exist so an under-powered number is reported
    as under-powered rather than dropped or silently trusted.
    """

    point: float
    lo: float | None
    hi: float | None
    n_pos: int
    n_neg: int
    n_boot: int
    low_power: bool
    reason: str | None


# --------------------------------------------------------------------------------------
# per-label metric primitives
# --------------------------------------------------------------------------------------


def _threshold_vector(thresholds: Mapping[str, float]) -> np.ndarray:
    if set(thresholds) != set(LABELS):
        raise ValueError(f"threshold keys must equal {LABELS}")
    return np.array([float(thresholds[label]) for label in LABELS], dtype=float)


def _per_label(values: Iterable[float]) -> dict[str, float]:
    """Positional array -> per-label dict through the one authoritative adapter (H23).

    `model.contract.probs_to_dict` is the project's single array->dict converter. Re-deriving it
    with `zip(LABELS, row)` here would mislabel every per-label metric silently if the column
    order ever drifted, and a key-membership check is order-blind.
    """
    return probs_to_dict(np.asarray(list(values), dtype=float))


def _f1_binary(y_true_col: np.ndarray, y_flag_col: np.ndarray) -> float:
    tp = int(np.count_nonzero((y_true_col == 1) & (y_flag_col == 1)))
    fp = int(np.count_nonzero((y_true_col == 0) & (y_flag_col == 1)))
    fn = int(np.count_nonzero((y_true_col == 1) & (y_flag_col == 0)))
    denominator = 2 * tp + fp + fn
    return 0.0 if denominator == 0 else 2.0 * tp / denominator


def _precision_binary(y_true_col: np.ndarray, y_flag_col: np.ndarray) -> float:
    tp = int(np.count_nonzero((y_true_col == 1) & (y_flag_col == 1)))
    fp = int(np.count_nonzero((y_true_col == 0) & (y_flag_col == 1)))
    return 0.0 if tp + fp == 0 else tp / (tp + fp)


def _recall_binary(y_true_col: np.ndarray, y_flag_col: np.ndarray) -> float:
    tp = int(np.count_nonzero((y_true_col == 1) & (y_flag_col == 1)))
    fn = int(np.count_nonzero((y_true_col == 1) & (y_flag_col == 0)))
    return 0.0 if tp + fn == 0 else tp / (tp + fn)


def _pr_auc(y_true_col: np.ndarray, y_prob_col: np.ndarray) -> float:
    """Average precision, or NaN when the column carries no positives.

    `average_precision_score` returns 0.0 with only a `UserWarning` on an all-negative column.
    Reporting that as a score understates the model and hides the gap; NaN says "undefined".
    """
    if not np.any(y_true_col == 1):
        return float("nan")
    return float(average_precision_score(y_true_col, y_prob_col))


def _macro_f1(y_true: np.ndarray, y_flag: np.ndarray) -> float:
    return float(np.mean([_f1_binary(y_true[:, j], y_flag[:, j]) for j in range(y_true.shape[1])]))


def _macro_pr_auc(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    values = [_pr_auc(y_true[:, j], y_prob[:, j]) for j in range(y_true.shape[1])]
    defined = [v for v in values if math.isfinite(v)]
    return float(np.mean(defined)) if defined else float("nan")


def compute_metrics(y_true, y_prob, thresholds: Mapping[str, float]) -> dict[str, float]:
    """Per-label and aggregate metrics at the supplied thresholds.

    `accuracy` is present on purpose: rubric 1.2 lists it among the metrics each run must log and
    rubric 3.2 puts live accuracy on the dashboard. `select_best_run` is what stops it becoming a
    decision input.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    if y_true.ndim != 2 or y_true.shape[1] != len(LABELS):
        raise ValueError(f"y_true must be (n, {len(LABELS)}), got {y_true.shape}")
    if y_prob.shape != y_true.shape:
        raise ValueError(f"y_prob {y_prob.shape} does not match y_true {y_true.shape}")
    y_flag = (y_prob >= _threshold_vector(thresholds)).astype(int)

    columns = range(len(LABELS))
    out: dict[str, float] = {}
    for prefix, values in (
        ("f1", [_f1_binary(y_true[:, j], y_flag[:, j]) for j in columns]),
        ("precision", [_precision_binary(y_true[:, j], y_flag[:, j]) for j in columns]),
        ("recall", [_recall_binary(y_true[:, j], y_flag[:, j]) for j in columns]),
        ("pr_auc", [_pr_auc(y_true[:, j], y_prob[:, j]) for j in columns]),
        ("accuracy", [float(np.mean(y_true[:, j] == y_flag[:, j])) for j in columns]),
        ("support", [float(y_true[:, j].sum()) for j in columns]),
    ):
        for label, value in _per_label(values).items():
            out[f"{prefix}/{label}"] = value

    out["macro_f1"] = float(np.mean([out[f"f1/{label}"] for label in LABELS]))
    out["macro_pr_auc"] = _macro_pr_auc(y_true, y_prob)
    out["accuracy"] = float(np.mean([out[f"accuracy/{label}"] for label in LABELS]))
    out["subset_accuracy"] = float((y_flag == y_true).all(axis=1).mean())
    return out


# --------------------------------------------------------------------------------------
# stratified bootstrap
# --------------------------------------------------------------------------------------


def _draw(strata: Sequence[np.ndarray], rng: np.random.Generator) -> np.ndarray:
    """One resample: with replacement *within* each stratum, so stratum sizes are preserved."""
    return np.concatenate(
        [stratum[rng.integers(0, stratum.size, stratum.size)] for stratum in strata]
    )


def _bootstrap(
    strata: Sequence[np.ndarray],
    statistics: Mapping[str, Callable[[np.ndarray], float]],
    n_boot: int,
    seed: int,
) -> dict[str, np.ndarray]:
    """Every statistic is evaluated on the *same* resamples, which is both cheaper and coherent."""
    rng = np.random.default_rng(seed)
    non_empty = [np.asarray(s) for s in strata if np.asarray(s).size]
    out = {name: np.empty(n_boot, dtype=float) for name in statistics}
    for b in range(n_boot):
        idx = _draw(non_empty, rng)
        for name, statistic in statistics.items():
            out[name][b] = statistic(idx)
    return out


def _finish(
    point: float,
    replicates: np.ndarray,
    *,
    n_pos: int,
    n_neg: int,
    n_requested: int,
    alpha: float,
    low_power: bool,
    low_power_reason: str | None,
) -> CIResult:
    usable = replicates[np.isfinite(replicates)]
    dropped = int(replicates.size - usable.size)
    reasons: list[str] = []
    if low_power and low_power_reason:
        reasons.append(low_power_reason)
    if dropped:
        reasons.append(
            f"{dropped} of {n_requested} bootstrap replicates were non-finite and were dropped"
        )
    if usable.size == 0:
        reasons.append("no usable bootstrap replicates")
        return CIResult(point, None, None, n_pos, n_neg, 0, True, "; ".join(reasons))
    lo, hi = (float(v) for v in np.quantile(usable, [alpha / 2.0, 1.0 - alpha / 2.0]))
    return CIResult(
        point, lo, hi, n_pos, n_neg, int(usable.size), bool(low_power),
        "; ".join(reasons) if reasons else None,
    )


def _binary_ci_many(
    y_true_col: np.ndarray,
    statistics: Mapping[str, Callable[[np.ndarray], float]],
    points: Mapping[str, float],
    *,
    n_boot: int,
    seed: int,
    alpha: float,
    min_positives: int,
    low_power_below: int,
) -> dict[str, CIResult]:
    pos = np.flatnonzero(y_true_col == 1)
    neg = np.flatnonzero(y_true_col == 0)
    n_pos, n_neg = int(pos.size), int(neg.size)
    if n_pos < min_positives:
        reason = f"only {n_pos} positives; need at least {min_positives}"
        return {
            name: CIResult(points[name], None, None, n_pos, n_neg, 0, True, reason)
            for name in statistics
        }
    replicates = _bootstrap([pos, neg], statistics, n_boot, seed)
    low_power = n_pos < low_power_below
    reason = f"only {n_pos} positives; the interval is wide" if low_power else None
    return {
        name: _finish(
            points[name], replicates[name], n_pos=n_pos, n_neg=n_neg, n_requested=n_boot,
            alpha=alpha, low_power=low_power, low_power_reason=reason,
        )
        for name in statistics
    }


def stratified_bootstrap_ci(
    y_true,
    y_score,
    metric_fn: Callable[[np.ndarray, np.ndarray], float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    min_positives: int = 1,
    low_power_below: int = LOW_POWER_BELOW,
) -> CIResult:
    """Percentile interval for a binary-label metric, resampled within the positive/negative strata.

    Every resample carries exactly the observed number of positives, so no resample can be
    all-negative and score 0.0 on a warning. A slice with fewer than `min_positives` positives
    gets no interval and says why, rather than returning a number that looks like a measurement.
    """
    y_true = np.asarray(y_true).astype(int).ravel()
    y_score = np.asarray(y_score, dtype=float).ravel()
    if y_true.shape != y_score.shape:
        raise ValueError(
            f"y_true and y_score must have the same length, got {y_true.shape} and {y_score.shape}"
        )
    pos = np.flatnonzero(y_true == 1)
    if pos.size < min_positives:
        return CIResult(
            float("nan"), None, None, int(pos.size), int(y_true.size - pos.size), 0, True,
            f"only {int(pos.size)} positives; need at least {min_positives}",
        )
    point = float(metric_fn(y_true, y_score))

    def statistic(idx: np.ndarray) -> float:
        return float(metric_fn(y_true[idx], y_score[idx]))

    return _binary_ci_many(
        y_true, {"metric": statistic}, {"metric": point},
        n_boot=n_boot, seed=seed, alpha=alpha, min_positives=min_positives,
        low_power_below=low_power_below,
    )["metric"]


def _pattern_strata(y_true: np.ndarray) -> list[np.ndarray]:
    """Row indices grouped by the exact label-combination pattern.

    Resampling within these strata preserves the count of every label at once -- a row can only
    be replaced by a row carrying an identical label vector -- while keeping rows intact, so the
    correlations between labels are not destroyed the way independent per-label resampling would
    destroy them.
    """
    weights = (1 << np.arange(y_true.shape[1], dtype=np.int64)).astype(np.int64)
    codes = y_true.astype(np.int64) @ weights
    order = np.argsort(codes, kind="stable")
    boundaries = np.flatnonzero(np.diff(codes[order])) + 1
    return np.split(order, boundaries)


def _limiting_label(y_true: np.ndarray) -> tuple[str, int]:
    per_label = y_true.sum(axis=0)
    j = int(np.argmin(per_label))
    name = LABELS[j] if y_true.shape[1] == len(LABELS) else f"column {j}"
    return name, int(per_label[j])


def _multilabel_ci_many(
    y_true: np.ndarray,
    statistics: Mapping[str, Callable[[np.ndarray], float]],
    *,
    n_boot: int,
    seed: int,
    alpha: float,
    low_power_below: int,
) -> dict[str, CIResult]:
    y_true = np.asarray(y_true).astype(int)
    if y_true.ndim != 2:
        raise ValueError(f"y_true must be a 2-D indicator matrix, got shape {y_true.shape}")
    n = y_true.shape[0]
    points = {name: float(fn(np.arange(n))) for name, fn in statistics.items()}
    replicates = _bootstrap(_pattern_strata(y_true), statistics, n_boot, seed)
    any_positive = int(np.count_nonzero(y_true.any(axis=1)))
    label, count = _limiting_label(y_true)
    low_power = count < low_power_below
    reason = (
        f"the rarest label {label} has only {count} positives; the interval is wide"
        if low_power
        else None
    )
    return {
        name: _finish(
            points[name], replicates[name], n_pos=any_positive, n_neg=n - any_positive,
            n_requested=n_boot, alpha=alpha, low_power=low_power, low_power_reason=reason,
        )
        for name in statistics
    }


def multilabel_stratified_bootstrap_ci(
    y_true,
    statistic: Callable[[np.ndarray], float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    low_power_below: int = LOW_POWER_BELOW,
) -> CIResult:
    """Percentile interval for an aggregate metric, resampled within label-pattern strata.

    `statistic` receives the row indices of one resample and returns the metric on those rows.
    Every label's positive count is preserved exactly, which is what stops a macro average from
    inheriting a rare label's zero-positive resample.
    """
    return _multilabel_ci_many(
        y_true, {"metric": statistic},
        n_boot=n_boot, seed=seed, alpha=alpha, low_power_below=low_power_below,
    )["metric"]


def _label_statistics(col_true, col_flag, col_prob):
    return {
        "f1": lambda idx: _f1_binary(col_true[idx], col_flag[idx]),
        "pr_auc": lambda idx: _pr_auc(col_true[idx], col_prob[idx]),
        "accuracy": lambda idx: float(np.mean(col_true[idx] == col_flag[idx])),
    }


def _aggregate_statistics(y_true, y_flag, y_prob):
    return {
        "macro_f1": lambda idx: _macro_f1(y_true[idx], y_flag[idx]),
        "macro_pr_auc": lambda idx: _macro_pr_auc(y_true[idx], y_prob[idx]),
        # mean per-label accuracy == the mean over every (row, label) cell
        "accuracy": lambda idx: float(np.mean(y_true[idx] == y_flag[idx])),
        "subset_accuracy": lambda idx: float((y_true[idx] == y_flag[idx]).all(axis=1).mean()),
    }


def compute_intervals(
    y_true,
    y_prob,
    thresholds: Mapping[str, float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    low_power_below: int = LOW_POWER_BELOW,
) -> dict[str, CIResult]:
    """A stratified bootstrap interval for every headline metric, per label and aggregate."""
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    y_flag = (y_prob >= _threshold_vector(thresholds)).astype(int)

    out: dict[str, CIResult] = {}
    for j, label in enumerate(LABELS):
        col_true, col_flag, col_prob = y_true[:, j], y_flag[:, j], y_prob[:, j]
        points = {
            "f1": _f1_binary(col_true, col_flag),
            "pr_auc": _pr_auc(col_true, col_prob),
            "accuracy": float(np.mean(col_true == col_flag)),
        }
        per_metric = _binary_ci_many(
            col_true, _label_statistics(col_true, col_flag, col_prob), points,
            n_boot=n_boot, seed=seed, alpha=alpha, min_positives=1,
            low_power_below=low_power_below,
        )
        for name, result in per_metric.items():
            out[f"{name}/{label}"] = result

    out.update(
        _multilabel_ci_many(
            y_true, _aggregate_statistics(y_true, y_flag, y_prob),
            n_boot=n_boot, seed=seed, alpha=alpha, low_power_below=low_power_below,
        )
    )
    return out


# --------------------------------------------------------------------------------------
# the promotion-metric ban
# --------------------------------------------------------------------------------------


def select_best_run(runs: Sequence[Mapping[str, float]], key: str = PROMOTION_METRIC) -> Mapping:
    """Pick the run to promote. Refuses to decide on accuracy."""
    if key in FORBIDDEN_PROMOTION_KEYS or key.startswith("accuracy"):
        raise ForbiddenPromotionMetric(
            f"{key!r} is logged for rubric 1.2 and 3.2 but is banned as a promotion metric: an "
            f"all-negative predictor scores about 90% on this corpus while catching nothing. "
            f"Use {PROMOTION_METRIC!r}"
        )
    if not runs:
        raise ValueError("no runs to select from")
    return max(runs, key=lambda run: run[key])


# --------------------------------------------------------------------------------------
# the durable once-only ledger
# --------------------------------------------------------------------------------------


def read_touched_versions(path: Path = LEDGER_PATH) -> set[str]:
    """Every `split_version` already recorded. Anchored on the field row, so prose cannot match."""
    path = Path(path)
    if not path.exists():
        return set()
    return set(_ROW.findall(path.read_text()))


def assert_ledger_is_git_tracked(path: Path = LEDGER_PATH) -> None:
    """Raise unless the ledger is under version control, since that is what makes it durable."""
    path = Path(path)
    cwd = path.parent if path.parent.is_dir() else Path.cwd()
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path.name],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise LedgerNotTracked(
            f"{path} is not tracked by git, so the once-only guard would vanish with the pod. "
            f"Run: git add {path} && git commit"
        ) from exc


def _json_safe(metrics: Mapping[str, float]) -> dict[str, float | None]:
    """NaN and infinity are not valid JSON. An undefined metric is recorded as null, not as 0."""
    out: dict[str, float | None] = {}
    for key, value in metrics.items():
        number = float(value)
        out[key] = number if math.isfinite(number) else None
    return out


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file and rename.

    A half-written ledger is worse than no ledger: a truncated file loses an earlier entry, and
    losing an entry silently re-opens the once-only guard for a split that was already evaluated.
    `os.replace` is atomic within a filesystem, so a reader sees either the old file or the new
    one and never a prefix of the new one.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    number = float(value)
    return f"{number:.4f}" if math.isfinite(number) else "n/a"


def record_touch(
    split_version: str,
    *,
    git_sha: str,
    run_id: str,
    metrics: Mapping[str, float],
    path: Path = LEDGER_PATH,
) -> None:
    """Append one held-out evaluation to the ledger, or refuse if this split was already touched."""
    path = Path(path)
    if not _SHA256.fullmatch(str(split_version)):
        raise ValueError(
            f"split_version {split_version!r} is not a 64-character lowercase hex sha256. The "
            f"ledger row regex would not match it back out, so the guard would be silently inert"
        )
    if split_version in read_touched_versions(path):
        raise TestSetAlreadyTouched(
            f"split_version {split_version} already appears in {path}. The held-out test set is "
            f"evaluated exactly once per split_version, on the model cross-validation already "
            f"chose. Read the recorded numbers, or change the data (which changes split_version) "
            f"if a genuinely new split is intended."
        )
    touched_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()
    headline = "\n".join(f"| {key} | {_fmt(metrics.get(key))} |" for key in _HEADLINE_KEYS)
    payload = json.dumps(_json_safe(metrics), indent=2, sort_keys=True)
    entry = (
        f"## `{split_version}` — run `{run_id}`\n"
        f"\n"
        f"| field | value |\n"
        f"|---|---|\n"
        f"| split_version | `{split_version}` |\n"
        f"| git_sha | `{git_sha}` |\n"
        f"| run_id | `{run_id}` |\n"
        f"| touched_at_utc | `{touched_at}` |\n"
        f"{headline}\n"
        f"\n"
        f"Accuracy is recorded because rubric 1.2 and 3.2 name it. It is never a promotion or\n"
        f"comparison metric; promotion is decided on `{PROMOTION_METRIC}`.\n"
        f"\n"
        f"```json\n{payload}\n```\n"
        f"\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = path.read_text() if path.exists() else _HEADER
    _atomic_write(path, body + entry)


# --------------------------------------------------------------------------------------
# evaluation entry points
# --------------------------------------------------------------------------------------


def _version_of(obj, *, required: bool = True) -> str | None:
    """`split_version` is the key. `data_version` is accepted as the composite fallback.

    Phase 0's `DatasetBundle` carries `raw_sha256` / `split_version` / `env_version` and exposes
    `data_version` as a composite of the three; older drafts of the interface block named only
    `data_version`. Preferring `split_version` keeps the guard keyed on the realized split, which
    is the thing that must not be evaluated twice.
    """
    for name in ("split_version", "data_version"):
        value = getattr(obj, name, None)
        if value:
            return str(value)
    if required:
        raise AttributeError(
            f"{type(obj).__name__} exposes neither split_version nor data_version; the "
            f"once-only held-out guard is keyed on split_version and cannot run without it"
        )
    return None


def evaluate_cross_validated(
    oof,
    thresholds: Mapping[str, float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
) -> dict:
    """Score out-of-fold predictions. No ledger: only the held-out set is once-only.

    `oof` supplies `y_true` (n, 6), `y_prob` (n, 6) -- every row scored by a model that never saw
    it -- and a version string. Re-running this is free and expected; it is how the model is
    chosen, and it is the only sanctioned place a comparison happens.
    """
    y_true = np.asarray(oof.y_true).astype(int)
    y_prob = np.asarray(oof.y_prob, dtype=float)
    return {
        "split_version": _version_of(oof, required=False),
        "n": int(y_true.shape[0]),
        "metrics": compute_metrics(y_true, y_prob, thresholds),
        "cis": compute_intervals(
            y_true, y_prob, thresholds, n_boot=n_boot, seed=seed, alpha=alpha
        ),
        "thresholds": {label: float(thresholds[label]) for label in LABELS},
    }


def evaluate_on_test(
    *,
    bundle,
    model,
    thresholds: Mapping[str, float],
    git_sha: str,
    run_id: str,
    ledger_path: Path = LEDGER_PATH,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    require_tracked_ledger: bool | None = None,
) -> dict:
    """The single guarded evaluation of the held-out test set.

    Ordering is the control, not a detail:

    1. The ledger is checked **before** the rows are scored, so a repeat call never touches the
       held-out data at all.
    2. The touch is recorded **before** the intervals are computed, so a crash in the bootstrap
       cannot leave the test set quietly re-runnable.

    `require_tracked_ledger` defaults to "true when writing the real committed ledger, false for
    an explicitly supplied path", so tests may use a temporary file while the production path
    must be under version control.
    """
    ledger_path = Path(ledger_path)
    if require_tracked_ledger is None:
        require_tracked_ledger = ledger_path == LEDGER_PATH
    if require_tracked_ledger:
        assert_ledger_is_git_tracked(ledger_path)

    split_version = _version_of(bundle)
    _threshold_vector(thresholds)
    if split_version in read_touched_versions(ledger_path):
        raise TestSetAlreadyTouched(
            f"split_version {split_version} already appears in {ledger_path}. The held-out test "
            f"set is evaluated exactly once per split_version; the rows were not scored again."
        )

    test_df = bundle.test_df
    texts = test_df["comment_text"].tolist()
    y_true = test_df[list(LABELS)].to_numpy().astype(int)
    y_prob = np.asarray(model.predict_proba(texts), dtype=float)
    if y_prob.shape != (len(texts), len(LABELS)):
        raise ValueError(
            f"model.predict_proba returned {y_prob.shape}; expected (n, {len(LABELS)}) = "
            f"{(len(texts), len(LABELS))} in LABELS order"
        )

    metrics = compute_metrics(y_true, y_prob, thresholds)
    record_touch(
        split_version, git_sha=git_sha, run_id=run_id, metrics=metrics, path=ledger_path
    )
    cis = compute_intervals(y_true, y_prob, thresholds, n_boot=n_boot, seed=seed, alpha=alpha)
    y_flag = (y_prob >= _threshold_vector(thresholds)).astype(int)
    # Full (n, 6) matrices, not just the primary column: that is what turns on the per-label
    # F1 table inside the report. Jigsaw's documented failure is over-flagging comments that
    # merely *mention* an identity group, so the slice metric that matters is the FPR among the
    # non-toxic rows of each slice (premortem H31).
    fairness = identity_fairness_report(
        texts, y_true, y_flag, y_prob, n_boot=n_boot, seed=seed
    )
    return {
        "split_version": split_version,
        "git_sha": git_sha,
        "run_id": run_id,
        "n_test": len(texts),
        "metrics": metrics,
        "cis": cis,
        "fairness": fairness,
        "y_true": y_true,
        "y_prob": y_prob,
        "y_flag": y_flag,
        "thresholds": {label: float(thresholds[label]) for label in LABELS},
        "ledger_path": str(ledger_path),
    }
