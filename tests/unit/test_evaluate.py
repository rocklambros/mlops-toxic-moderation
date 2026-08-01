"""Cross-validated evaluation, stratified bootstrap intervals, and the durable once-only guard.

Three properties are load-bearing here and each has a test that fails if it regresses:

1. Every headline number carries a **stratified** bootstrap interval. A naive resample of a
   small stratum loses all positives sometimes, and `average_precision_score` then returns
   0.0 with only a `UserWarning` -- it does not crash. That silently drags the lower bound to
   the floor and a promote decision happens inside noise.
2. `accuracy` is computed and returned, because rubric 1.2 and 3.2 name it, and refuses to be
   a selection input.
3. The held-out test set is evaluated **once per split_version**, and the guard is a file, not
   process state, because RunPod pods are ephemeral by design.
"""

import ast
import json
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline

from model.evaluate import (
    LEDGER_PATH,
    PROMOTION_METRIC,
    CIResult,
    ForbiddenPromotionMetric,
    LedgerNotTracked,
    TestSetAlreadyTouched,
    assert_ledger_is_git_tracked,
    compute_intervals,
    compute_metrics,
    evaluate_cross_validated,
    evaluate_on_test,
    multilabel_stratified_bootstrap_ci,
    read_touched_versions,
    record_touch,
    select_best_run,
    stratified_bootstrap_ci,
)
from model.fairness import IDENTITY_TERMS
from model.labels import LABELS

DV_A = "a" * 64
DV_B = "b" * 64
DV_C = "c" * 64


# --------------------------------------------------------------------------------------
# fixtures and doubles
# --------------------------------------------------------------------------------------


def _thresholds(value: float = 0.5) -> dict[str, float]:
    return {label: value for label in LABELS}


def _rare_slice(n: int = 3000, n_pos: int = 20, seed: int = 0):
    """One label at `threat`-like prevalence, with a genuinely separating but noisy score."""
    rng = np.random.default_rng(seed)
    y = np.zeros(n, dtype=int)
    y[:n_pos] = 1
    scores = rng.random(n)
    scores[:n_pos] += 0.6
    return y, scores


def _multilabel(n: int = 800, seed: int = 0):
    """A six-column target whose rarest column mimics `threat`, plus informative scores."""
    rng = np.random.default_rng(seed)
    rates = np.array([0.30, 0.10, 0.20, 0.025, 0.25, 0.08])
    y = (rng.random((n, len(LABELS))) < rates).astype(int)
    for j in range(len(LABELS)):
        if y[:, j].sum() < 12:
            y[rng.choice(n, 12, replace=False), j] = 1
    y_prob = np.clip(0.12 + 0.65 * y + rng.normal(0.0, 0.14, y.shape), 0.001, 0.999)
    return y, y_prob


_CLEAN = (
    "thanks for the edit",
    "great work on the article",
    "i disagree politely",
    "nice sourcing here",
    "please add a citation",
)
_CUES = {
    "toxic": "idiot",
    "severe_toxic": "vile",
    "obscene": "filth",
    "threat": "killyou",
    "insult": "moron",
    "identity_hate": "yourkind",
}
_RATES = {
    "toxic": 0.30,
    "severe_toxic": 0.14,
    "obscene": 0.20,
    "threat": 0.12,
    "insult": 0.25,
    "identity_hate": 0.14,
}


def _learnable_corpus(n: int = 300, seed: int = 0):
    """A tiny learnable corpus so a real sklearn estimator can be fitted in a unit test."""
    rng = np.random.default_rng(seed)
    texts: list[str] = []
    y = np.zeros((n, len(LABELS)), dtype=int)
    for i in range(n):
        parts = [_CLEAN[i % len(_CLEAN)], f"comment {i}"]
        for j, label in enumerate(LABELS):
            if rng.random() < _RATES[label]:
                y[i, j] = 1
                parts.append(_CUES[label])
        rng.shuffle(parts)
        texts.append(" ".join(parts))
    return texts, y


class _Bundle:
    """The Phase 0 `DatasetBundle` fields `evaluate_on_test` actually reads."""

    def __init__(self, texts, y, split_version: str):
        frame = pd.DataFrame(
            {"id": [f"c{i}" for i in range(len(texts))], "comment_text": list(texts)}
        )
        for j, label in enumerate(LABELS):
            frame[label] = y[:, j]
        self.test_df = frame
        self.split_version = split_version


class _Oof:
    """The `OofPredictions` fields `evaluate_cross_validated` actually reads."""

    def __init__(self, y_true, y_prob, split_version: str):
        self.y_true = y_true
        self.y_prob = y_prob
        self.split_version = split_version


class _SpyModel:
    """Counts how many times the held-out rows were scored."""

    def __init__(self, y_prob):
        self.y_prob = np.asarray(y_prob, dtype=float)
        self.calls = 0

    def predict_proba(self, texts):
        self.calls += 1
        return self.y_prob[: len(texts)]


# --------------------------------------------------------------------------------------
# compute_metrics
# --------------------------------------------------------------------------------------


def test_metrics_cover_every_label_and_the_rubric_named_accuracy():
    y_true, y_prob = _multilabel()
    out = compute_metrics(y_true, y_prob, _thresholds())
    for label in LABELS:
        for prefix in ("f1", "pr_auc", "precision", "recall", "accuracy"):
            assert f"{prefix}/{label}" in out
    for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy"):
        assert key in out, f"{key} is named by rubric 1.2/3.2 or is the promotion metric"


def test_macro_f1_is_exactly_the_mean_of_the_per_label_f1s():
    y_true, y_prob = _multilabel()
    out = compute_metrics(y_true, y_prob, _thresholds())
    assert out["macro_f1"] == pytest.approx(
        float(np.mean([out[f"f1/{label}"] for label in LABELS]))
    )
    assert out["accuracy"] == pytest.approx(
        float(np.mean([out[f"accuracy/{label}"] for label in LABELS]))
    )


def test_a_perfect_prediction_scores_one_everywhere():
    y_true = np.array([[1, 0, 1, 0, 1, 0], [0, 1, 0, 1, 0, 1]])
    out = compute_metrics(y_true, y_true.astype(float), _thresholds())
    assert out["macro_f1"] == pytest.approx(1.0)
    assert out["accuracy"] == pytest.approx(1.0)
    assert out["subset_accuracy"] == pytest.approx(1.0)


def test_per_label_keys_are_not_transposed():
    """A column transposition is invisible to an order-blind key-membership check (H23)."""
    n = 400
    rng = np.random.default_rng(3)
    y_true = np.zeros((n, len(LABELS)), dtype=int)
    threat = LABELS.index("threat")
    y_true[:40, threat] = 1
    y_prob = rng.uniform(0.0, 0.4, (n, len(LABELS)))
    y_prob[:40, threat] = 0.99
    out = compute_metrics(y_true, y_prob, _thresholds())
    assert out["f1/threat"] == pytest.approx(1.0)
    for label in LABELS:
        if label != "threat":
            assert out[f"f1/{label}"] == pytest.approx(0.0)


def test_a_label_with_no_positives_reports_pr_auc_as_nan_rather_than_a_silent_zero():
    y_true = np.zeros((50, len(LABELS)), dtype=int)
    y_true[:10, 0] = 1
    y_prob = np.full((50, len(LABELS)), 0.2)
    y_prob[:10, 0] = 0.9
    out = compute_metrics(y_true, y_prob, _thresholds())
    assert np.isnan(out["pr_auc/threat"]), (
        "average_precision_score returns 0.0 with only a UserWarning when a column has no "
        "positives; reporting that as a score understates the model and hides the gap"
    )
    assert out["pr_auc/toxic"] == pytest.approx(1.0)
    assert np.isfinite(out["macro_pr_auc"])


def test_thresholds_must_name_every_label():
    y_true, y_prob = _multilabel(n=100)
    with pytest.raises(ValueError, match="must equal"):
        compute_metrics(y_true, y_prob, {"toxic": 0.5})


def test_the_module_never_re_derives_the_label_zip():
    """Premortem H23: independent `zip(LABELS, row)` re-derivations mislabel silently.

    Parsed rather than grepped, so a docstring that *names* the banned pattern in order to
    explain why it is banned does not itself trip the guard.
    """
    tree = ast.parse(Path("model/evaluate.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "zip":
            args = {getattr(arg, "id", None) for arg in node.args}
            assert "LABELS" not in args, (
                "model/evaluate.py re-derives the array->dict mapping; call "
                "model.contract.probs_to_dict, which is the single authoritative adapter"
            )
    used = {getattr(node, "id", None) for node in ast.walk(tree)}
    assert "probs_to_dict" in used, "the canonical adapter in model/contract.py must be used"


# --------------------------------------------------------------------------------------
# stratified bootstrap
# --------------------------------------------------------------------------------------


def test_naive_bootstrap_silently_produces_zero_positive_resamples():
    """The failure this module exists to remove. Documented here, fixed below."""
    y, scores = _rare_slice(n=120, n_pos=4)
    rng = np.random.default_rng(1)
    zero_positive = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(2000):
            idx = rng.integers(0, len(y), len(y))
            if y[idx].sum() == 0:
                zero_positive += 1
                assert average_precision_score(y[idx], scores[idx]) == 0.0
    assert zero_positive > 0, "expected the naive resampler to lose all positives sometimes"


def test_every_stratified_resample_retains_exactly_the_observed_positives():
    y, scores = _rare_slice(n=120, n_pos=4)
    seen: list[int] = []

    def spy(y_true, y_score):
        seen.append(int(y_true.sum()))
        assert len(y_true) == len(y), "the resample must preserve the sample size too"
        return float(average_precision_score(y_true, y_score))

    stratified_bootstrap_ci(y, scores, spy, n_boot=500, seed=7)
    assert min(seen) == 4 and max(seen) == 4, "a resample moved the positive count"
    assert len(seen) == 501, "500 resamples plus the point estimate"


def test_the_interval_is_ordered_and_never_dragged_to_zero_by_an_empty_stratum():
    y, scores = _rare_slice(n=120, n_pos=4)
    ci = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=500, seed=7)
    assert isinstance(ci, CIResult)
    assert ci.lo is not None and ci.hi is not None
    assert ci.lo <= ci.point <= ci.hi
    assert ci.lo > 0.0
    assert (ci.n_pos, ci.n_neg) == (4, 116)
    assert ci.low_power is True and "4 positives" in ci.reason


def test_a_label_with_no_positives_returns_no_interval_and_does_not_raise():
    ci = stratified_bootstrap_ci(
        np.zeros(50, dtype=int),
        np.random.default_rng(0).random(50),
        average_precision_score,
        n_boot=100,
    )
    assert ci.lo is None and ci.hi is None
    assert ci.low_power is True and ci.n_boot == 0
    assert "0 positives" in ci.reason


def test_a_single_positive_is_reported_low_power_rather_than_crashed():
    y, scores = _rare_slice(n=200, n_pos=1)
    ci = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=50, seed=2)
    assert ci.n_pos == 1 and ci.low_power is True
    assert ci.lo is not None and ci.hi is not None


def test_intervals_are_deterministic_for_a_fixed_seed():
    y, scores = _rare_slice()
    first = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=1)
    second = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=1)
    third = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=2)
    assert first == second
    assert first != third


def test_a_well_powered_label_is_not_flagged_low_power():
    rng = np.random.default_rng(2)
    y = np.zeros(4000, dtype=int)
    y[:400] = 1
    scores = rng.random(4000)
    scores[:400] += 0.5
    ci = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=3)
    assert ci.low_power is False and ci.reason is None


def test_non_finite_replicates_are_dropped_rather_than_poisoning_the_quantile():
    y, scores = _rare_slice(n=400, n_pos=40)
    calls = {"n": 0}

    def flaky(y_true, y_score):
        calls["n"] += 1
        if calls["n"] % 5 == 0:
            return float("nan")
        return float(average_precision_score(y_true, y_score))

    ci = stratified_bootstrap_ci(y, scores, flaky, n_boot=100, seed=4)
    assert ci.n_boot < 100
    assert ci.lo is not None and np.isfinite(ci.lo) and np.isfinite(ci.hi)
    assert "non-finite" in ci.reason


def test_mismatched_score_length_is_refused():
    with pytest.raises(ValueError, match="same length"):
        stratified_bootstrap_ci(np.array([0, 1]), np.array([0.1]), average_precision_score)


# --------------------------------------------------------------------------------------
# multilabel (aggregate) stratified bootstrap
# --------------------------------------------------------------------------------------


def test_aggregate_resamples_preserve_every_labels_positive_count():
    """Stratifying on the exact label pattern preserves each column's positives exactly,
    while keeping rows intact so the label correlations survive."""
    y_true, _ = _multilabel(n=600, seed=5)
    observed = y_true.sum(axis=0)
    seen: list[np.ndarray] = []

    def statistic(idx):
        seen.append(y_true[idx].sum(axis=0))
        return float(len(idx))

    multilabel_stratified_bootstrap_ci(y_true, statistic, n_boot=200, seed=9)
    stacked = np.array(seen)
    assert (stacked == observed).all(), (
        "a resample moved a label's positive count; the rare-label interval would then be "
        "measuring resample noise rather than sampling uncertainty"
    )


def test_the_macro_f1_interval_brackets_its_own_point_estimate():
    y_true, y_prob = _multilabel(n=800, seed=6)
    thr = np.array([_thresholds()[label] for label in LABELS])
    y_flag = (y_prob >= thr).astype(int)

    def statistic(idx):
        return float(f1_score(y_true[idx], y_flag[idx], average="macro", zero_division=0))

    ci = multilabel_stratified_bootstrap_ci(y_true, statistic, n_boot=200, seed=11)
    assert ci.lo <= ci.point <= ci.hi
    assert ci.lo > 0.0


def test_the_aggregate_low_power_flag_names_the_limiting_label():
    y_true, _ = _multilabel(n=800, seed=7)
    threat = LABELS.index("threat")
    y_true[:, threat] = 0
    y_true[:9, threat] = 1
    ci = multilabel_stratified_bootstrap_ci(
        y_true, lambda idx: float(idx.size), n_boot=20, seed=1
    )
    assert ci.low_power is True
    assert "threat" in ci.reason and "9" in ci.reason


# --------------------------------------------------------------------------------------
# compute_intervals
# --------------------------------------------------------------------------------------


def test_every_headline_metric_carries_an_interval():
    y_true, y_prob = _multilabel(n=600, seed=8)
    cis = compute_intervals(y_true, y_prob, _thresholds(), n_boot=60, seed=3)
    for label in LABELS:
        for prefix in ("f1", "pr_auc", "accuracy"):
            assert isinstance(cis[f"{prefix}/{label}"], CIResult)
    for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy"):
        assert isinstance(cis[key], CIResult)


def test_interval_point_estimates_agree_with_compute_metrics():
    y_true, y_prob = _multilabel(n=600, seed=8)
    metrics = compute_metrics(y_true, y_prob, _thresholds())
    cis = compute_intervals(y_true, y_prob, _thresholds(), n_boot=40, seed=3)
    for key in ("macro_f1", "macro_pr_auc", "accuracy", "subset_accuracy"):
        assert cis[key].point == pytest.approx(metrics[key])
    for label in LABELS:
        assert cis[f"f1/{label}"].point == pytest.approx(metrics[f"f1/{label}"])
        assert cis[f"pr_auc/{label}"].point == pytest.approx(metrics[f"pr_auc/{label}"])


# --------------------------------------------------------------------------------------
# the promotion-metric ban
# --------------------------------------------------------------------------------------


def test_promotion_on_accuracy_is_refused():
    with pytest.raises(ForbiddenPromotionMetric, match="banned as a promotion metric"):
        select_best_run([{"accuracy": 0.99}, {"accuracy": 0.98}], key="accuracy")
    with pytest.raises(ForbiddenPromotionMetric):
        select_best_run([{"accuracy/threat": 0.99}], key="accuracy/threat")
    with pytest.raises(ForbiddenPromotionMetric):
        select_best_run([{"subset_accuracy": 0.9}], key="subset_accuracy")


def test_promotion_on_macro_f1_is_the_default_and_works():
    assert PROMOTION_METRIC == "macro_f1"
    winner = select_best_run([{"macro_f1": 0.10}, {"macro_f1": 0.30}, {"macro_f1": 0.20}])
    assert winner["macro_f1"] == pytest.approx(0.30)


def test_selecting_from_no_runs_is_an_error_not_a_silent_none():
    with pytest.raises(ValueError, match="no runs"):
        select_best_run([])


# --------------------------------------------------------------------------------------
# cross-validated evaluation
# --------------------------------------------------------------------------------------


def test_cross_validated_evaluation_returns_points_and_intervals():
    y_true, y_prob = _multilabel(n=600, seed=12)
    out = evaluate_cross_validated(_Oof(y_true, y_prob, DV_A), _thresholds(), n_boot=40, seed=1)
    assert out["split_version"] == DV_A
    assert out["n"] == 600
    assert out["metrics"]["macro_f1"] == pytest.approx(
        out["cis"]["macro_f1"].point
    )
    assert "accuracy" in out["metrics"]
    for label in LABELS:
        assert f"pr_auc/{label}" in out["cis"]


def test_cross_validated_evaluation_touches_no_ledger(tmp_path, monkeypatch):
    """Out-of-fold data may be re-evaluated freely; only the held-out set is once-only."""
    monkeypatch.chdir(tmp_path)
    y_true, y_prob = _multilabel(n=200, seed=13)
    evaluate_cross_validated(_Oof(y_true, y_prob, DV_A), _thresholds(), n_boot=10)
    evaluate_cross_validated(_Oof(y_true, y_prob, DV_A), _thresholds(), n_boot=10)
    assert not (tmp_path / LEDGER_PATH).exists()


# --------------------------------------------------------------------------------------
# the durable ledger
# --------------------------------------------------------------------------------------


def test_a_fresh_ledger_has_touched_nothing(tmp_path):
    assert read_touched_versions(tmp_path / "absent.md") == set()


def test_first_touch_records_git_sha_split_version_timestamp_and_metrics(tmp_path):
    path = tmp_path / "log.md"
    record_touch(
        DV_A,
        git_sha="9f1c2ab",
        run_id="run-1",
        metrics={"macro_f1": 0.7412, "macro_pr_auc": 0.6810, "accuracy": 0.9721},
        path=path,
    )
    assert read_touched_versions(path) == {DV_A}
    body = path.read_text()
    assert DV_A in body and "9f1c2ab" in body and "run-1" in body
    assert "0.7412" in body
    assert "touched_at_utc" in body
    payload = json.loads(body.split("```json")[1].split("```")[0])
    assert payload["macro_pr_auc"] == pytest.approx(0.6810)
    assert payload["accuracy"] == pytest.approx(0.9721)


def test_second_touch_of_the_same_split_version_is_refused(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", metrics={"macro_f1": 0.74}, path=path)
    with pytest.raises(TestSetAlreadyTouched, match="evaluated exactly once"):
        record_touch(DV_A, git_sha="9f1c", run_id="run-2", metrics={"macro_f1": 0.99}, path=path)
    assert read_touched_versions(path) == {DV_A}
    assert "0.99" not in path.read_text(), "the refused entry must not be written"


def test_a_different_split_version_is_allowed(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", metrics={"macro_f1": 0.74}, path=path)
    record_touch(DV_B, git_sha="9f1c", run_id="run-2", metrics={"macro_f1": 0.75}, path=path)
    assert read_touched_versions(path) == {DV_A, DV_B}


def test_prose_and_headings_are_not_mistaken_for_a_split_version(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", metrics={"macro_f1": 0.74}, path=path)
    assert read_touched_versions(path) == {DV_A}


def test_a_short_or_non_hex_version_is_refused_rather_than_silently_unguarded(tmp_path):
    """A version the row regex cannot match back out would leave the guard inert."""
    path = tmp_path / "log.md"
    with pytest.raises(ValueError, match="64-character"):
        record_touch("dv", git_sha="9f1c", run_id="run-1", metrics={"macro_f1": 0.1}, path=path)
    assert not path.exists()


def test_a_failed_ledger_write_leaves_the_previous_entries_intact(tmp_path, monkeypatch):
    """A truncated ledger silently re-opens the once-only guard, so the write is atomic."""
    import model.evaluate as evaluate_module

    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", metrics={"macro_f1": 0.74}, path=path)
    before = path.read_text()

    class _BrokenOs:
        @staticmethod
        def replace(src, dst):
            raise OSError("disk full")

    monkeypatch.setattr(evaluate_module, "os", _BrokenOs)
    with pytest.raises(OSError, match="disk full"):
        record_touch(DV_B, git_sha="9f1c", run_id="run-2", metrics={"macro_f1": 0.75}, path=path)
    assert path.read_text() == before
    assert read_touched_versions(path) == {DV_A}
    assert list(tmp_path.iterdir()) == [path], "a partial temporary file was left behind"


def test_the_guard_survives_a_fresh_interpreter(tmp_path):
    """The test a module-level boolean cannot pass: RunPod pods are ephemeral by design."""
    path = tmp_path / "log.md"
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from model.evaluate import TestSetAlreadyTouched, record_touch
        try:
            record_touch({DV_A!r}, git_sha="9f1c", run_id=sys.argv[1],
                         metrics={{"macro_f1": 0.5}}, path=Path({str(path)!r}))
            print("WROTE")
        except TestSetAlreadyTouched:
            print("REFUSED")
        """
    )
    first = subprocess.run(
        [sys.executable, "-c", script, "run-1"], capture_output=True, text=True
    )
    second = subprocess.run(
        [sys.executable, "-c", script, "run-2"], capture_output=True, text=True
    )
    assert first.stdout.strip() == "WROTE", first.stderr
    assert second.stdout.strip() == "REFUSED", second.stderr


def test_an_untracked_ledger_is_refused(tmp_path):
    with pytest.raises(LedgerNotTracked, match="not tracked by git"):
        assert_ledger_is_git_tracked(tmp_path / "scratch-copy.md")


def test_a_git_tracked_file_passes_the_durability_check():
    assert_ledger_is_git_tracked(Path("model/labels.py"))  # must not raise


def test_the_default_ledger_path_is_the_git_tracked_doc():
    assert LEDGER_PATH == Path("docs/test-set-touch-log.md")


# --------------------------------------------------------------------------------------
# evaluate_on_test
# --------------------------------------------------------------------------------------


def test_held_out_evaluation_returns_metrics_intervals_and_the_scored_probabilities(tmp_path):
    texts, y = _learnable_corpus(n=200)
    _, y_prob = _multilabel(n=200, seed=21)
    out = evaluate_on_test(
        bundle=_Bundle(texts, y, DV_A),
        model=_SpyModel(y_prob),
        thresholds=_thresholds(),
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=40,
    )
    assert out["split_version"] == DV_A
    assert out["n_test"] == 200
    assert "macro_f1" in out["metrics"] and "accuracy" in out["metrics"]
    for label in LABELS:
        assert f"pr_auc/{label}" in out["cis"]
    assert out["y_prob"].shape == (200, len(LABELS))


def test_the_second_held_out_evaluation_is_refused_without_scoring_the_rows_again(tmp_path):
    texts, y = _learnable_corpus(n=120)
    _, y_prob = _multilabel(n=120, seed=22)
    model = _SpyModel(y_prob)
    kwargs = dict(
        bundle=_Bundle(texts, y, DV_A),
        model=model,
        thresholds=_thresholds(),
        git_sha="9f1c",
        ledger_path=tmp_path / "log.md",
        n_boot=20,
    )
    evaluate_on_test(run_id="run-1", **kwargs)
    assert model.calls == 1
    with pytest.raises(TestSetAlreadyTouched):
        evaluate_on_test(run_id="run-2", **kwargs)
    assert model.calls == 1, (
        "the refusal must land before the held-out rows are scored a second time"
    )


def test_the_touch_is_recorded_before_the_intervals_are_computed(tmp_path, monkeypatch):
    """A crash after scoring must not leave the test set silently re-runnable."""
    import model.evaluate as evaluate_module

    def boom(*args, **kwargs):
        raise RuntimeError("bootstrap exploded")

    monkeypatch.setattr(evaluate_module, "compute_intervals", boom)
    texts, y = _learnable_corpus(n=120)
    _, y_prob = _multilabel(n=120, seed=23)
    ledger = tmp_path / "log.md"
    with pytest.raises(RuntimeError, match="bootstrap exploded"):
        evaluate_on_test(
            bundle=_Bundle(texts, y, DV_C),
            model=_SpyModel(y_prob),
            thresholds=_thresholds(),
            git_sha="9f1c",
            run_id="run-1",
            ledger_path=ledger,
            n_boot=10,
        )
    assert read_touched_versions(ledger) == {DV_C}


def test_the_real_ledger_path_is_required_to_be_git_tracked(tmp_path):
    texts, y = _learnable_corpus(n=60)
    _, y_prob = _multilabel(n=60, seed=24)
    with pytest.raises(LedgerNotTracked):
        evaluate_on_test(
            bundle=_Bundle(texts, y, DV_A),
            model=_SpyModel(y_prob),
            thresholds=_thresholds(),
            git_sha="9f1c",
            run_id="run-1",
            ledger_path=tmp_path / "log.md",
            require_tracked_ledger=True,
            n_boot=10,
        )
    assert not (tmp_path / "log.md").exists()


def test_a_bundle_without_a_split_version_is_refused(tmp_path):
    texts, y = _learnable_corpus(n=60)
    _, y_prob = _multilabel(n=60, seed=25)
    bundle = _Bundle(texts, y, DV_A)
    del bundle.split_version
    with pytest.raises(AttributeError, match="split_version"):
        evaluate_on_test(
            bundle=bundle,
            model=_SpyModel(y_prob),
            thresholds=_thresholds(),
            git_sha="9f1c",
            run_id="run-1",
            ledger_path=tmp_path / "log.md",
            n_boot=10,
        )


def test_a_model_returning_the_wrong_shape_is_refused(tmp_path):
    texts, y = _learnable_corpus(n=60)

    class _Wrong:
        def predict_proba(self, texts):
            return np.zeros((len(texts), 2))

    with pytest.raises(ValueError, match=r"\(n, 6\)"):
        evaluate_on_test(
            bundle=_Bundle(texts, y, DV_A),
            model=_Wrong(),
            thresholds=_thresholds(),
            git_sha="9f1c",
            run_id="run-1",
            ledger_path=tmp_path / "log.md",
            n_boot=10,
        )


def test_the_held_out_accuracy_is_reported_but_still_cannot_select_a_run(tmp_path):
    texts, y = _learnable_corpus(n=120)
    _, y_prob = _multilabel(n=120, seed=26)
    out = evaluate_on_test(
        bundle=_Bundle(texts, y, DV_B),
        model=_SpyModel(y_prob),
        thresholds=_thresholds(),
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=20,
    )
    assert 0.0 <= out["metrics"]["accuracy"] <= 1.0
    with pytest.raises(ForbiddenPromotionMetric):
        select_best_run([out["metrics"]], key="accuracy")


def test_the_held_out_evaluation_attaches_the_identity_fairness_slice(tmp_path):
    """Rubric H31: Jigsaw's documented failure is over-flagging mere *mentions* of a group.

    `model/fairness.py` states that `model.evaluate` imports it, and defers its own import back
    so the cycle cannot close. This is the call that makes that direction real.
    """
    texts, y = _learnable_corpus(n=200)
    texts = [
        f"{text} {term}" for text, term in zip(texts, IDENTITY_TERMS * 20, strict=False)
    ]
    _, y_prob = _multilabel(n=200, seed=27)
    out = evaluate_on_test(
        bundle=_Bundle(texts, y, DV_A),
        model=_SpyModel(y_prob),
        thresholds=_thresholds(),
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=30,
    )
    fairness = out["fairness"]
    assert "background_fpr" in fairness
    assert fairness["n_rows"] == 200
    assert fairness["n_terms_present"] > 0
    assert fairness["primary_label"] == "toxic"
    assert fairness["labels"] == list(LABELS), "the per-label table needs the full (n, 6) matrices"


def test_the_returned_flags_are_the_thresholded_probabilities(tmp_path):
    texts, y = _learnable_corpus(n=120)
    _, y_prob = _multilabel(n=120, seed=28)
    out = evaluate_on_test(
        bundle=_Bundle(texts, y, DV_B),
        model=_SpyModel(y_prob),
        thresholds=_thresholds(0.4),
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=10,
    )
    assert ((out["y_prob"] >= 0.4).astype(int) == out["y_flag"]).all()


def test_evaluation_works_against_a_real_multi_label_sklearn_estimator(tmp_path):
    """One end-to-end check against a genuine calibrated one-vs-rest pipeline.

    Also pins the two measured constraints this phase inherits: `liblinear` converges (saga
    was measured at 493 s for n=15,000 while hitting max_iter WITHOUT converging), and the
    calibration nests INSIDE the one-vs-rest wrapper -- the outer nesting raises ValueError
    on a multi-label target because the calibrator label-encodes y.
    """
    texts, y = _learnable_corpus(n=300, seed=1)
    base = LogisticRegression(
        solver="liblinear", class_weight="balanced", max_iter=1000, random_state=42
    )
    pipe = Pipeline(
        [
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
            (
                "clf",
                OneVsRestClassifier(
                    CalibratedClassifierCV(base, cv=3, method="sigmoid"), n_jobs=1
                ),
            ),
        ]
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        pipe.fit(texts, y)
    for calibrated in pipe.named_steps["clf"].estimators_:
        for per_fold in calibrated.calibrated_classifiers_:
            lr = per_fold.estimator
            assert int(np.max(np.atleast_1d(lr.n_iter_))) < lr.max_iter

    out = evaluate_on_test(
        bundle=_Bundle(texts, y, DV_C),
        model=pipe,
        thresholds=_thresholds(),
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=30,
    )
    assert out["y_prob"].shape == (300, len(LABELS))
    assert out["y_prob"].min() >= 0.0 and out["y_prob"].max() <= 1.0
    assert out["metrics"]["macro_f1"] > 0.5, "the cue words make this corpus learnable"
    assert read_touched_versions(tmp_path / "log.md") == {DV_C}
