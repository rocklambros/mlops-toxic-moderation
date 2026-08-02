"""Per-label threshold tuning, and the drift reference the promoted thresholds produce.

The central claim this file has to prove is negative: the tuner never sees the held-out test
set. A negative claim cannot be proved by exercising the happy path, so it is attacked from
four directions:

- **Type.** ``tune_thresholds`` accepts ``OofPredictions`` and nothing else, so tuning on the
  test set requires constructing a deliberate lie rather than passing the wrong array.
- **Provenance.** Every row must carry a validation-fold id, so smuggling test rows in means
  forging fold membership for them.
- **Structure.** The module's own import graph and call signature are inspected, so no second
  data channel can appear without a test noticing.
- **Consequence.** Leaked rows are shown to *change the answer*, which is what makes the three
  guards above load-bearing rather than decorative.

The second half covers ``baseline_flag_rates.json``: the per-label flag rate on the held-out
test set at the promoted thresholds. Phase 3's target-drift panel (rubric 3.2) has nothing else
to drift *from*, and premortem C5 is exactly that missing reference. Note the deliberate
asymmetry between the two doors in this module -- the tuner refuses test-derived input, and the
baseline refuses out-of-fold input. Each one only opens the right way.
"""

import ast
import inspect
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from model import thresholds as thresholds_module
from model.labels import LABELS
from model.oof import OofPredictions
from model.thresholds import (
    DEFAULT_THRESHOLD,
    GRID,
    RECALL_WEIGHTS,
    BaselineFlagRates,
    compute_baseline_flag_rates,
    oof_version,
    threshold_report,
    tune_thresholds,
    write_baseline_flag_rates,
    write_thresholds,
)

THREAT_COLUMN = LABELS.index("threat")


def _overlapping_oof(seed: int = 11, n: int = 6000, n_folds: int = 5) -> OofPredictions:
    """Background and positive score distributions that genuinely overlap.

    Without overlap every beta picks the same separating threshold and the asymmetric-cost
    test proves nothing. `threat` gets a tenth of the positives the others get, which is the
    shape of the real corpus (roughly 0.3% prevalence).
    """
    rng = np.random.default_rng(seed)
    y_true = np.zeros((n, len(LABELS)), dtype=int)
    y_prob = np.clip(rng.beta(2, 12, size=(n, len(LABELS))), 0.001, 0.999)
    for j, _label in enumerate(LABELS):
        prevalence = 0.01 if j == THREAT_COLUMN else 0.10
        n_pos = max(1, int(round(n * prevalence)))
        idx = rng.choice(n, n_pos, replace=False)
        y_true[idx, j] = 1
        y_prob[idx, j] = np.clip(rng.beta(6, 5, size=n_pos), 0.001, 0.999)
    row_fold = np.arange(n) % n_folds
    return OofPredictions(y_true, y_prob, row_fold, "dv")


def _with_smuggled_rows(oof: OofPredictions, n_leak: int = 3000) -> OofPredictions:
    """The same out-of-fold data with held-out-shaped rows concatenated onto the end.

    Deliberately extreme: a block of positives scored low. If the tuner ever ingested rows it
    was not supposed to, thresholds would collapse toward the block. That is the observable
    consequence the guards exist to prevent.
    """
    y_true = np.vstack([oof.y_true, np.ones((n_leak, len(LABELS)), dtype=int)])
    y_prob = np.vstack([oof.y_prob, np.full((n_leak, len(LABELS)), 0.10)])
    row_fold = np.concatenate([oof.row_fold, np.zeros(n_leak, dtype=int)])
    return OofPredictions(y_true, y_prob, row_fold, oof.data_version)


def _recall(y_true: np.ndarray, y_prob: np.ndarray, threshold: float) -> float:
    flag = y_prob >= threshold
    tp = int(np.count_nonzero(flag & (y_true == 1)))
    return tp / max(int(y_true.sum()), 1)


def _precision(y_true: np.ndarray, y_prob: np.ndarray, threshold: float) -> float:
    flag = y_prob >= threshold
    tp = int(np.count_nonzero(flag & (y_true == 1)))
    return tp / max(int(np.count_nonzero(flag)), 1)


# --------------------------------------------------------------------------------------
# Shape and asymmetric cost
# --------------------------------------------------------------------------------------


def test_thresholds_cover_every_label_in_order_and_are_probabilities():
    thresholds = tune_thresholds(_overlapping_oof())
    assert list(thresholds.keys()) == list(LABELS)
    assert all(0.0 < v < 1.0 for v in thresholds.values())
    assert all(type(v) is float for v in thresholds.values())


def test_rare_severe_labels_get_a_lower_threshold_than_symmetric_f1_would_pick():
    oof = _overlapping_oof()
    symmetric = tune_thresholds(oof, recall_weights=dict.fromkeys(LABELS, 1.0))
    asymmetric = tune_thresholds(oof)
    for label in ("threat", "severe_toxic", "identity_hate"):
        assert asymmetric[label] < symmetric[label], (
            f"{label} carries recall weight {RECALL_WEIGHTS[label]} and must flag more freely"
        )
    for label in ("toxic", "obscene", "insult"):
        assert asymmetric[label] == symmetric[label], f"{label} has weight 1.0 and must not move"


def test_recall_weights_name_every_label_and_prioritise_threat_most():
    assert set(RECALL_WEIGHTS) == set(LABELS)
    assert RECALL_WEIGHTS["threat"] == max(RECALL_WEIGHTS.values())
    assert RECALL_WEIGHTS["toxic"] == 1.0
    assert all(w >= 1.0 for w in RECALL_WEIGHTS.values())


def test_the_recall_weighted_threshold_actually_catches_more_threats():
    """The weight has to buy recall, not just move a number.

    A missed `threat` costs more than a false flag on `toxic`, so the accepted trade is
    strictly more recall for strictly less precision on the severe labels.
    """
    oof = _overlapping_oof()
    symmetric = tune_thresholds(oof, recall_weights=dict.fromkeys(LABELS, 1.0))
    asymmetric = tune_thresholds(oof)
    y_true = oof.y_true[:, THREAT_COLUMN]
    y_prob = oof.y_prob[:, THREAT_COLUMN]
    assert _recall(y_true, y_prob, asymmetric["threat"]) > _recall(
        y_true, y_prob, symmetric["threat"]
    )
    assert _precision(y_true, y_prob, asymmetric["threat"]) < _precision(
        y_true, y_prob, symmetric["threat"]
    )


def test_recall_weights_must_name_every_label():
    oof = _overlapping_oof()
    with pytest.raises(ValueError, match="recall_weights keys must equal"):
        tune_thresholds(oof, recall_weights={"toxic": 1.0})
    with pytest.raises(ValueError, match="recall_weights keys must equal"):
        tune_thresholds(oof, recall_weights={**RECALL_WEIGHTS, "profanity": 2.0})


def test_a_non_positive_recall_weight_is_refused():
    oof = _overlapping_oof()
    with pytest.raises(ValueError, match="must be a positive finite"):
        tune_thresholds(oof, recall_weights={**RECALL_WEIGHTS, "threat": 0.0})


def test_tuning_is_deterministic():
    assert tune_thresholds(_overlapping_oof()) == tune_thresholds(_overlapping_oof())


def test_tuning_does_not_mutate_the_out_of_fold_arrays():
    oof = _overlapping_oof()
    before_true = oof.y_true.copy()
    before_prob = oof.y_prob.copy()
    tune_thresholds(oof)
    assert np.array_equal(oof.y_true, before_true)
    assert np.array_equal(oof.y_prob, before_prob)


def test_a_label_with_no_out_of_fold_positives_falls_back_to_the_neutral_default():
    """Never 0.05.

    F-beta is 0.0 at every grid point when a label has no positives, and a plain argmax over
    an all-zero vector returns index 0 -- the lowest threshold on the grid, which flags almost
    every comment. Silently shipping a flag-everything threshold for the rarest label is the
    worst available failure, so the degenerate case is named and neutral instead.
    """
    oof = _overlapping_oof()
    y_true = oof.y_true.copy()
    y_true[:, THREAT_COLUMN] = 0
    empty = OofPredictions(y_true, oof.y_prob, oof.row_fold, oof.data_version)
    report = threshold_report(empty)
    assert report.thresholds["threat"] == DEFAULT_THRESHOLD
    assert report.per_label["threat"].fell_back is True
    assert report.per_label["threat"].n_pos == 0
    # the other five labels are unaffected: the fallback is per-label, not a global bail-out
    assert report.per_label["toxic"].fell_back is False
    assert report.thresholds["toxic"] == tune_thresholds(oof)["toxic"]


def test_ties_on_the_grid_resolve_toward_recall():
    """Perfect separation makes a whole band of thresholds score identically.

    Every grid point in (0.105, 0.855) produces the same confusion matrix here, so the tie-break
    rule alone decides the answer. Under asymmetric cost the correct direction is the lower
    threshold -- flag more, miss fewer -- and a `>=` comparison in the search loop would silently
    pick the top of the band instead, shipping the least sensitive of six equally optimal
    thresholds for the label where sensitivity matters most.
    """
    n = 400
    y_true = np.zeros((n, len(LABELS)), dtype=int)
    y_true[:40] = 1
    y_prob = np.full((n, len(LABELS)), 0.105)
    y_prob[:40] = 0.855
    tuned = tune_thresholds(OofPredictions(y_true, y_prob, np.arange(n) % 5, "dv"))
    for label in LABELS:
        assert tuned[label] == pytest.approx(0.11), (
            f"{label}: ties must resolve to the lowest equally optimal threshold, "
            f"not {tuned[label]}"
        )


def test_the_grid_spans_the_documented_range():
    assert GRID.min() == pytest.approx(0.05)
    assert GRID.max() == pytest.approx(0.95)
    assert 0.0 < DEFAULT_THRESHOLD < 1.0


# --------------------------------------------------------------------------------------
# The tuner never sees the held-out test set
# --------------------------------------------------------------------------------------


def test_raw_arrays_are_refused_so_the_test_set_cannot_be_tuned_on():
    oof = _overlapping_oof()
    with pytest.raises(TypeError, match="only accepts OofPredictions"):
        tune_thresholds(oof.y_prob)


def test_a_bundle_shaped_object_is_refused():
    """The realistic slip is `tune_thresholds(bundle)` or `tune_thresholds(bundle.test_df)`."""

    class _Bundle:
        train_df = None
        test_df = None
        data_version = "dv"

    with pytest.raises(TypeError, match="only accepts OofPredictions"):
        tune_thresholds(_Bundle())
    with pytest.raises(TypeError, match="only accepts OofPredictions"):
        tune_thresholds(None)


def test_rows_that_never_appeared_in_a_validation_fold_are_refused():
    """`cross_val_probabilities` leaves -1 in row_fold for any row it did not score.

    Concatenating held-out rows onto an out-of-fold matrix is the concrete leak; the forged
    fold id is what this makes necessary, and a forged id is a lie rather than a slip.
    """
    oof = _overlapping_oof()
    row_fold = oof.row_fold.copy()
    row_fold[7] = -1
    smuggled = OofPredictions(oof.y_true, oof.y_prob, row_fold, oof.data_version)
    with pytest.raises(ValueError, match="never appeared in a validation fold"):
        tune_thresholds(smuggled)


def test_unscored_out_of_fold_cells_are_refused():
    """NaN is what an unfilled out-of-fold row looks like; it must not be tuned through."""
    oof = _overlapping_oof()
    y_prob = oof.y_prob.copy()
    y_prob[3, THREAT_COLUMN] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        tune_thresholds(OofPredictions(oof.y_true, y_prob, oof.row_fold, oof.data_version))


def test_probabilities_outside_the_unit_interval_are_refused():
    oof = _overlapping_oof()
    y_prob = oof.y_prob.copy()
    y_prob[3, 0] = 1.4
    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        tune_thresholds(OofPredictions(oof.y_true, y_prob, oof.row_fold, oof.data_version))


def test_mismatched_array_shapes_are_refused():
    oof = _overlapping_oof(n=200)
    with pytest.raises(ValueError, match="shape"):
        tune_thresholds(
            OofPredictions(oof.y_true[:100], oof.y_prob, oof.row_fold, oof.data_version)
        )


def test_smuggled_rows_would_change_every_threshold():
    """The consequence proof. Without it the three guards above are untested ceremony.

    The smuggled block is 3,000 positives scored 0.10. If the tuner ingested them, every
    per-label threshold would collapse to at most 0.10 -- observably different from the clean
    answer. So the guards are load-bearing: they are the only thing standing between the
    held-out test set and a different set of promoted thresholds.
    """
    clean = tune_thresholds(_overlapping_oof())
    leaked = tune_thresholds(_with_smuggled_rows(_overlapping_oof()))
    assert clean != leaked
    for label in LABELS:
        assert leaked[label] <= 0.10 < clean[label], (
            f"{label}: smuggled rows must visibly move the answer, else the guards prove nothing"
        )


def test_the_tuner_imports_nothing_that_can_reach_the_held_out_test_set():
    """Static, not behavioural: a future import is the way test data would arrive.

    `model.oof` and `model.labels` are the whole permitted surface. Anything from the data
    pipeline (`model.data.split`, `model.data.prepare`) or the evaluator (`model.evaluate`)
    would put the held-out rows one attribute access away.
    """
    tree = ast.parse(Path(inspect.getfile(thresholds_module)).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    project = {name for name in imported if name == "model" or name.startswith("model.")}
    assert project == {"model.labels", "model.oof"}, (
        f"model/thresholds.py may only import model.labels and model.oof; found {project}"
    )


def test_the_tuner_never_names_a_held_out_attribute():
    tree = ast.parse(Path(inspect.getfile(thresholds_module)).read_text())
    forbidden = {"test_df", "test_idx", "test_index", "y_test", "x_test", "held_out"}
    named = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert not (named & forbidden), f"the tuner reaches for held-out state: {named & forbidden}"


def test_the_signature_offers_exactly_one_data_channel():
    """A second positional parameter is how `y_test` would eventually get passed in."""
    params = list(inspect.signature(tune_thresholds).parameters.values())
    assert [p.name for p in params] == ["oof", "recall_weights", "grid"]
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[1:])
    assert all(p.default is not inspect.Parameter.empty for p in params[1:])


# --------------------------------------------------------------------------------------
# The audit trail
# --------------------------------------------------------------------------------------


def test_the_report_records_the_tuning_provenance():
    oof = _overlapping_oof()
    report = threshold_report(oof)
    assert report.thresholds == tune_thresholds(oof)
    assert report.data_version == "dv"
    assert report.n_tuning_rows == 6000
    assert report.n_tuning_folds == 5
    assert set(report.per_label) == set(LABELS)
    threat = report.per_label["threat"]
    assert threat.n_pos == 60
    assert threat.n_neg == 5940
    assert threat.beta == RECALL_WEIGHTS["threat"]
    assert 0.0 < threat.f_beta <= 1.0
    assert threat.recall > 0.0


def test_the_split_identity_prefers_split_version_over_the_composite():
    """Phase 0 names the realized-split hash `split_version`; the Phase 1 interface block
    predates that and names only `data_version`. Whichever `model/oof.py` ships, the audit
    record must key on the realized split, and it must agree with `model/evaluate.py`, which
    resolves the same two names in the same order."""

    class _Both:
        split_version = "realized-split"
        data_version = "composite"

    class _OnlyComposite:
        data_version = "composite"

    class _Neither:
        pass

    assert oof_version(_Both()) == "realized-split"
    assert oof_version(_OnlyComposite()) == "composite"
    with pytest.raises(ValueError, match="neither split_version nor data_version"):
        oof_version(_Neither())


def test_written_thresholds_round_trip_in_labels_order(tmp_path):
    thresholds = tune_thresholds(_overlapping_oof())
    path = tmp_path / "thresholds.json"
    write_thresholds(path, thresholds, data_version="dv")
    loaded = json.loads(path.read_text())
    assert list(loaded.keys()) == list(LABELS)
    assert loaded == thresholds
    meta = json.loads((tmp_path / "thresholds.meta.json").read_text())
    assert meta["data_version"] == "dv"


def test_the_meta_pins_the_row_count_so_a_leak_is_auditable(tmp_path):
    """180,633 training rows, not 212,510. The row count is the auditable evidence.

    A reviewer six months out cannot re-derive whether the tuner saw the test set from the
    thresholds alone, so the count of rows it tuned on and the folds they came from are
    written down next to the answer.
    """
    report = threshold_report(_overlapping_oof())
    path = tmp_path / "thresholds.json"
    write_thresholds(path, report.thresholds, data_version="dv", report=report)
    meta = json.loads((tmp_path / "thresholds.meta.json").read_text())
    assert meta["n_tuning_rows"] == 6000
    assert meta["n_tuning_folds"] == 5
    assert meta["tuned_on"] == "out-of-fold validation predictions"
    assert meta["recall_weights"] == RECALL_WEIGHTS
    assert meta["per_label"]["threat"]["n_pos"] == 60


def test_the_meta_never_claims_weights_it_cannot_observe(tmp_path):
    """An audit record that quietly disagrees with the numbers beside it is worse than a gap.

    ``write_thresholds`` is handed a plain dict; it cannot know which betas produced it. If it
    stamped the module default in unconditionally, a run tuned with custom weights would ship a
    sidecar asserting weights that never ran.
    """
    custom = {**RECALL_WEIGHTS, "threat": 2.0}
    tuned = tune_thresholds(_overlapping_oof(), recall_weights=custom)
    write_thresholds(tmp_path / "thresholds.json", tuned, data_version="dv")
    bare = json.loads((tmp_path / "thresholds.meta.json").read_text())
    assert "recall_weights" not in bare
    assert bare["tuned_on"] == "out-of-fold validation predictions"

    report = threshold_report(_overlapping_oof(), recall_weights=custom)
    write_thresholds(tmp_path / "thresholds.json", tuned, data_version="dv", report=report)
    full = json.loads((tmp_path / "thresholds.meta.json").read_text())
    assert full["recall_weights"]["threat"] == 2.0


def test_a_report_from_a_different_split_cannot_be_filed_under_this_data_version(tmp_path):
    report = threshold_report(_overlapping_oof())
    with pytest.raises(ValueError, match="data_version"):
        write_thresholds(
            tmp_path / "thresholds.json",
            report.thresholds,
            data_version="a-different-split",
            report=report,
        )


def test_writing_an_incomplete_threshold_map_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must equal"):
        write_thresholds(tmp_path / "t.json", {"toxic": 0.5}, data_version="dv")


def test_writing_a_threshold_outside_the_unit_interval_is_refused(tmp_path):
    bad = {**dict.fromkeys(LABELS, 0.5), "threat": 1.0}
    with pytest.raises(ValueError, match=r"outside \(0, 1\)"):
        write_thresholds(tmp_path / "t.json", bad, data_version="dv")


# --------------------------------------------------------------------------------------
# baseline_flag_rates.json -- the Phase 3 drift reference (premortem C5)
# --------------------------------------------------------------------------------------


def _baseline_args(**over):
    base = dict(
        data_version="d" * 64,
        model_version="toxic-clf:v1",
        model_digest="sha256:" + "e" * 64,
        thresholds=dict.fromkeys(LABELS, 0.5),
    )
    base.update(over)
    return base


def test_flag_rates_are_computed_at_the_promoted_thresholds():
    # column 0 (`toxic`) flags on 3 of 4 rows at threshold 0.5; column 3 (`threat`) on 1 of 4
    probs = np.array(
        [
            [0.9, 0.1, 0.1, 0.9, 0.1, 0.1],
            [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.7, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.2, 0.1, 0.1, 0.1, 0.1, 0.1],
        ]
    )
    out = compute_baseline_flag_rates(probs, **_baseline_args())
    assert out.flag_rates["toxic"] == pytest.approx(0.75)
    assert out.flag_rates["threat"] == pytest.approx(0.25)
    assert out.flag_rates["insult"] == pytest.approx(0.0)
    assert out.n == 4


def test_per_label_thresholds_are_applied_per_label_not_globally():
    probs = np.full((10, len(LABELS)), 0.40)
    thresholds = {**dict.fromkeys(LABELS, 0.5), "threat": 0.30}
    out = compute_baseline_flag_rates(probs, **_baseline_args(thresholds=thresholds))
    assert out.flag_rates["threat"] == pytest.approx(1.0)
    assert out.flag_rates["toxic"] == pytest.approx(0.0)


def test_the_baseline_records_the_thresholds_it_was_measured_at():
    """A flag rate without its threshold is uninterpretable six months later."""
    probs = np.random.default_rng(0).random((50, len(LABELS)))
    thresholds = {**dict.fromkeys(LABELS, 0.5), "threat": 0.18}
    out = compute_baseline_flag_rates(probs, **_baseline_args(thresholds=thresholds))
    assert out.thresholds == thresholds
    assert list(out.flag_rates.keys()) == list(LABELS)
    assert list(out.thresholds.keys()) == list(LABELS)
    assert out.model_digest.startswith("sha256:")


def test_the_baseline_refuses_out_of_fold_predictions():
    """The mirror image of the tuner's guard.

    The drift reference has to be the held-out test distribution. Out-of-fold probabilities
    come from five different models and would encode a distribution no deployed model ever
    produces, so Phase 3 would then drift against a reference that never existed.
    """
    oof = _overlapping_oof(n=200)
    with pytest.raises(TypeError, match="held-out test"):
        compute_baseline_flag_rates(oof, **_baseline_args())


def test_the_baseline_rejects_a_matrix_of_the_wrong_width():
    with pytest.raises(ValueError, match=r"\(n, 6\)"):
        compute_baseline_flag_rates(
            np.zeros((10, 3)) + 0.5, **_baseline_args()
        )
    with pytest.raises(ValueError, match=r"\(n, 6\)"):
        compute_baseline_flag_rates(np.zeros(6) + 0.5, **_baseline_args())


def test_the_baseline_rejects_non_probabilities():
    with pytest.raises(ValueError, match="outside"):
        compute_baseline_flag_rates(np.full((5, len(LABELS)), 1.2), **_baseline_args())


def test_the_baseline_rejects_an_incomplete_threshold_map():
    with pytest.raises(ValueError, match="must equal"):
        compute_baseline_flag_rates(
            np.full((5, len(LABELS)), 0.5), **_baseline_args(thresholds={"toxic": 0.5})
        )


def test_the_baseline_schema_rejects_a_missing_label():
    with pytest.raises(ValidationError):
        BaselineFlagRates(
            data_version="d" * 64,
            model_version="toxic-clf:v1",
            model_digest="sha256:" + "e" * 64,
            n=10,
            thresholds=dict.fromkeys(LABELS, 0.5),
            flag_rates={"toxic": 0.1},
            generated_at_utc="2026-08-02T00:00:00+00:00",
        )


def test_the_baseline_schema_rejects_a_rate_outside_zero_to_one():
    with pytest.raises(ValidationError):
        BaselineFlagRates(
            data_version="d" * 64,
            model_version="toxic-clf:v1",
            model_digest="sha256:" + "e" * 64,
            n=10,
            thresholds=dict.fromkeys(LABELS, 0.5),
            flag_rates={label: (1.5 if label == "toxic" else 0.1) for label in LABELS},
            generated_at_utc="2026-08-02T00:00:00+00:00",
        )


def test_the_baseline_schema_rejects_an_empty_test_set():
    with pytest.raises(ValidationError):
        BaselineFlagRates(
            data_version="d" * 64,
            model_version="toxic-clf:v1",
            model_digest="sha256:" + "e" * 64,
            n=0,
            thresholds=dict.fromkeys(LABELS, 0.5),
            flag_rates=dict.fromkeys(LABELS, 0.1),
            generated_at_utc="2026-08-02T00:00:00+00:00",
        )


def test_the_baseline_json_round_trips_for_phase_three(tmp_path):
    probs = np.random.default_rng(0).random((50, len(LABELS)))
    out = compute_baseline_flag_rates(probs, **_baseline_args())
    path = tmp_path / "baseline_flag_rates.json"
    write_baseline_flag_rates(path, out)
    reloaded = BaselineFlagRates.model_validate(json.loads(path.read_text()))
    assert reloaded == out
    assert reloaded.model_digest.startswith("sha256:")
    assert list(json.loads(path.read_text())["flag_rates"].keys()) == list(LABELS)


def test_the_promoted_thresholds_flow_straight_into_the_drift_reference(tmp_path):
    """End to end: tune on out-of-fold rows, then measure the rate on held-out rows.

    This is the seam Phase 3 depends on. The thresholds written to thresholds.json and the
    thresholds stamped into baseline_flag_rates.json must be the same object, or the dashboard
    drifts today's flag rate against a rate produced by a different decision boundary.
    """
    oof = _overlapping_oof()
    thresholds = tune_thresholds(oof)
    write_thresholds(tmp_path / "thresholds.json", thresholds, data_version="dv")

    held_out_probs = np.clip(np.random.default_rng(99).beta(2, 12, (800, len(LABELS))), 0, 1)
    rates = compute_baseline_flag_rates(
        held_out_probs,
        data_version="dv",
        model_version="toxic-clf:v1",
        model_digest="sha256:" + "a" * 64,
        thresholds=thresholds,
    )
    write_baseline_flag_rates(tmp_path / "baseline_flag_rates.json", rates)

    written_thresholds = json.loads((tmp_path / "thresholds.json").read_text())
    written_rates = json.loads((tmp_path / "baseline_flag_rates.json").read_text())
    assert written_rates["thresholds"] == written_thresholds
    assert written_rates["data_version"] == "dv"
    assert written_rates["n"] == 800
    assert all(0.0 <= v <= 1.0 for v in written_rates["flag_rates"].values())
