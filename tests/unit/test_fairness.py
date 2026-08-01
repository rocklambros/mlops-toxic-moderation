"""Per-identity-term fairness slice of the held-out test set (premortem H31).

The corpus fixtures below encode Jigsaw's best-documented failure mode: comments that merely
MENTION an identity group are over-flagged even when they are not toxic. Every assertion here
is about that shape, not about a "fair / not fair" verdict, which this module deliberately
refuses to issue.
"""

import ast
import inspect
import pathlib

import numpy as np
import pytest
from sklearn.metrics import f1_score

from model import fairness as fairness_module
from model.fairness import (
    IDENTITY_TERMS,
    _rate_ci,
    identity_fairness_report,
    render_fairness_markdown,
    term_mask,
    write_fairness_report,
)
from model.labels import LABELS


def _corpus():
    """400 ordinary non-toxic rows, 60 non-toxic rows that merely MENTION an identity group and
    are over-flagged, 6 genuinely toxic rows mentioning it, 20 low-power rows, 40 toxic rows."""
    rng = np.random.default_rng(0)
    texts, y_true, y_flag, y_prob = [], [], [], []
    for i in range(400):
        texts.append(f"a perfectly ordinary comment number {i}")
        flag = int(rng.random() < 0.05)
        y_true.append(0)
        y_flag.append(flag)
        y_prob.append(0.6 if flag else 0.05)
    for i in range(60):
        texts.append(f"i am a muslim and i edited paragraph {i}")
        flag = int(rng.random() < 0.50)
        y_true.append(0)
        y_flag.append(flag)
        y_prob.append(0.7 if flag else 0.10)
    for i in range(6):
        texts.append(f"muslim people are scum {i}")
        y_true.append(1)
        y_flag.append(1)
        y_prob.append(0.90)
    for i in range(20):
        texts.append(f"my sikh neighbour helped with source {i}")
        y_true.append(0)
        y_flag.append(0)
        y_prob.append(0.02)
    for i in range(40):
        texts.append(f"you are an idiot number {i}")
        y_true.append(1)
        y_flag.append(1)
        y_prob.append(0.95)
    return texts, y_true, y_flag, y_prob


def _multilabel_corpus():
    """The same shape widened to all six labels.

    `identity_hate` is where a term-mention model degrades hardest: inside the `muslim` slice
    the model flags identity_hate on most of the merely-mentioning rows and misses half of the
    genuine ones, so both the slice F1 and the slice false-positive rate must move against the
    overall numbers. Elsewhere the model is close to perfect, so any measured gap is attributable.
    """
    rng = np.random.default_rng(7)
    texts: list[str] = []
    rows_true: list[list[int]] = []
    rows_flag: list[list[int]] = []
    rows_prob: list[list[float]] = []
    toxic = LABELS.index("toxic")
    ihate = LABELS.index("identity_hate")
    insult = LABELS.index("insult")

    def _row():
        return [0] * len(LABELS), [0] * len(LABELS), [0.02] * len(LABELS)

    for i in range(400):  # background: non-toxic, occasionally false-flagged
        texts.append(f"a perfectly ordinary comment number {i}")
        t, f, p = _row()
        if rng.random() < 0.05:
            f[toxic] = 1
            p[toxic] = 0.60
        rows_true.append(t)
        rows_flag.append(f)
        rows_prob.append(p)
    for i in range(80):  # merely mentioning: heavily over-flagged on toxic and identity_hate
        texts.append(f"i am a muslim and i edited paragraph {i}")
        t, f, p = _row()
        if rng.random() < 0.50:
            f[toxic] = 1
            p[toxic] = 0.70
        if rng.random() < 0.45:
            f[ihate] = 1
            p[ihate] = 0.66
        rows_true.append(t)
        rows_flag.append(f)
        rows_prob.append(p)
    for i in range(20):  # genuine identity_hate inside the slice, half of them missed
        texts.append(f"muslim people are scum {i}")
        t, f, p = _row()
        t[toxic] = t[ihate] = 1
        f[toxic] = 1
        p[toxic] = 0.92
        if i % 2 == 0:
            f[ihate] = 1
            p[ihate] = 0.88
        rows_true.append(t)
        rows_flag.append(f)
        rows_prob.append(p)
    for i in range(120):  # background positives the model handles well
        texts.append(f"you are an idiot number {i}")
        t, f, p = _row()
        t[toxic] = t[insult] = f[toxic] = f[insult] = 1
        p[toxic] = p[insult] = 0.95
        rows_true.append(t)
        rows_flag.append(f)
        rows_prob.append(p)
    for i in range(30):  # background identity_hate the model handles well
        texts.append(f"go back to your own country number {i}")
        t, f, p = _row()
        t[toxic] = t[ihate] = f[toxic] = f[ihate] = 1
        p[toxic] = p[ihate] = 0.91
        rows_true.append(t)
        rows_flag.append(f)
        rows_prob.append(p)

    y_true = np.array(rows_true, dtype=int)
    y_flag = np.array(rows_flag, dtype=int)
    y_prob = np.array(rows_prob, dtype=float)
    return texts, y_true, y_flag, y_prob


# --------------------------------------------------------------------------------------------
# Term matching
# --------------------------------------------------------------------------------------------


def test_term_matching_respects_word_boundaries():
    assert term_mask(["a woman spoke"], "man").tolist() == [False]
    assert term_mask(["a man spoke"], "man").tolist() == [True]
    assert term_mask(["MUSLIM readers"], "muslim").tolist() == [True]
    assert term_mask(["anti-muslim graffiti"], "muslim").tolist() == [True]


def test_term_matching_does_not_fire_on_a_longer_word_that_contains_the_term():
    assert term_mask(["transgender rights"], "trans").tolist() == [False]
    assert term_mask(["trans rights"], "trans").tolist() == [True]
    assert term_mask(["the jewellery shop"], "jew").tolist() == [False]


def test_term_matching_survives_non_string_and_missing_text():
    assert term_mask([None, 12345, "a muslim reader"], "muslim").tolist() == [False, False, True]


# --------------------------------------------------------------------------------------------
# The headline finding: over-flagged identity mentions
# --------------------------------------------------------------------------------------------


def test_over_flagged_identity_mentions_are_detected_as_a_material_gap():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=300)
    assert report["worst_term"] == "muslim"
    assert report["max_fpr_gap"] > 0.10
    assert report["material"] is True
    slices = {s["term"]: s for s in report["slices"]}
    assert slices["muslim"]["fpr"] > 4 * report["background_fpr"]
    assert slices["muslim"]["fpr_ratio_to_background"] > 4.0


def test_every_scored_slice_carries_a_bootstrap_interval():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=300)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert muslim["fpr_lo"] is not None and muslim["fpr_hi"] is not None
    assert muslim["fpr_lo"] <= muslim["fpr"] <= muslim["fpr_hi"]
    assert muslim["pr_auc"] is not None and muslim["pr_auc_lo"] is not None


def test_small_groups_are_flagged_low_power_and_never_dropped():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=200)
    slices = {s["term"]: s for s in report["slices"]}
    assert "sikh" in slices, "a small group was silently dropped instead of reported"
    assert slices["sikh"]["n"] == 20
    assert slices["sikh"]["low_power"] is True
    assert report["n_terms_low_power"] >= 1


def test_terms_absent_from_the_corpus_are_omitted_not_reported_as_zero():
    report = identity_fairness_report(
        ["nothing to see here"], [0], [0], [0.01], terms=("muslim",), n_boot=50
    )
    assert report["slices"] == []
    assert report["n_terms_present"] == 0
    assert report["terms_absent"] == ["muslim"], (
        "a term with no rows must still be named, so a missing group is distinguishable from a "
        "group that was never searched for"
    )
    assert "muslim" in render_fairness_markdown(report)


def test_the_term_list_covers_the_documented_bias_axes():
    for term in ("muslim", "jewish", "gay", "transgender", "black", "female", "disabled"):
        assert term in IDENTITY_TERMS
    assert len(IDENTITY_TERMS) >= 40


def test_the_term_list_is_lowercase_and_free_of_duplicates():
    assert len(set(IDENTITY_TERMS)) == len(IDENTITY_TERMS)
    assert all(term == term.lower().strip() for term in IDENTITY_TERMS)
    assert all(term for term in IDENTITY_TERMS)


def test_a_slice_with_no_non_toxic_rows_reports_no_fpr_instead_of_crashing():
    report = identity_fairness_report(
        ["muslim people are scum", "muslim people are scum too"],
        [1, 1],
        [1, 1],
        [0.9, 0.8],
        terms=("muslim",),
        n_boot=20,
    )
    muslim = report["slices"][0]
    assert muslim["fpr"] is None and muslim["fpr_lo"] is None
    assert muslim["fpr_ratio_to_background"] is None
    assert muslim["tpr"] == pytest.approx(1.0)
    assert report["worst_term"] is None and report["material"] is False


def test_mismatched_input_lengths_are_refused():
    with pytest.raises(ValueError, match="same number of rows"):
        identity_fairness_report(["a", "b"], [0], [0], [0.1], terms=("muslim",), n_boot=10)


def test_the_report_is_deterministic_for_a_fixed_seed():
    texts, y_true, y_flag, y_prob = _corpus()
    first = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=100, seed=3)
    second = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=100, seed=3)
    assert first == second


# --------------------------------------------------------------------------------------------
# Per-label F1 within each slice, against the overall rate
# --------------------------------------------------------------------------------------------


def test_a_six_column_target_produces_per_label_f1_for_every_label():
    texts, y_true, y_flag, y_prob = _multilabel_corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=100)
    assert report["labels"] == list(LABELS)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert set(muslim["per_label"]) == set(LABELS)
    for label in LABELS:
        cell = muslim["per_label"][label]
        assert {"n_pos", "flag_rate", "fpr", "f1", "f1_overall", "f1_gap"} <= set(cell)


def test_per_label_f1_matches_sklearn_on_the_slice_rows():
    texts, y_true, y_flag, y_prob = _multilabel_corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    mask = term_mask(texts, "muslim")
    j = LABELS.index("identity_hate")
    expected = f1_score(y_true[mask, j], y_flag[mask, j], zero_division=0)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert muslim["per_label"]["identity_hate"]["f1"] == pytest.approx(expected)
    assert report["overall"]["identity_hate"]["f1"] == pytest.approx(
        f1_score(y_true[:, j], y_flag[:, j], zero_division=0)
    )


def test_the_identity_slice_f1_is_measurably_worse_than_the_overall_f1():
    texts, y_true, y_flag, y_prob = _multilabel_corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    cell = muslim["per_label"]["identity_hate"]
    assert cell["f1"] < cell["f1_overall"]
    assert cell["f1_gap"] < -0.10, "the per-label F1 gap must be visible, not rounded away"
    assert cell["fpr"] > report["overall"]["identity_hate"]["fpr"]
    assert muslim["macro_f1_gap"] < 0.0
    assert report["worst_f1_term"] == "muslim"
    assert report["max_f1_drop"] > 0.0


def test_no_f1_drop_is_reported_when_every_slice_does_at_least_as_well_as_the_whole_set():
    """A "worst term" that is not actually worse is an invented finding."""
    texts = [f"a muslim reader commented {i}" for i in range(60)]
    texts += [f"you are an idiot number {i}" for i in range(60)]
    y_true = np.zeros((120, len(LABELS)), dtype=int)
    y_flag = np.zeros((120, len(LABELS)), dtype=int)
    toxic = LABELS.index("toxic")
    y_true[:30, toxic] = y_flag[:30, toxic] = 1  # the slice is scored perfectly
    y_true[60:, toxic] = 1
    y_flag[60:90, toxic] = 1  # the background half of the corpus is scored worse
    y_prob = y_flag.astype(float)
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert muslim["macro_f1_gap"] > 0.0
    assert report["worst_f1_term"] is None
    assert report["max_f1_drop"] == 0.0


def test_a_label_with_no_positives_in_a_slice_reports_none_not_a_misleading_zero():
    texts, y_true, y_flag, y_prob = _multilabel_corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    threat = muslim["per_label"]["threat"]
    assert threat["n_pos"] == 0
    assert threat["f1"] is None and threat["f1_gap"] is None
    assert muslim["macro_f1"] is not None, "labels without positives must not sink the macro"


def test_a_one_dimensional_target_still_reports_per_label_f1_for_the_primary_label():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    assert report["labels"] == ["toxic"]
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert muslim["per_label"]["toxic"]["f1"] is not None
    assert muslim["per_label"]["toxic"]["n_pos"] == 6


# --------------------------------------------------------------------------------------------
# Interval machinery and the import cycle
# --------------------------------------------------------------------------------------------


def test_the_rate_interval_matches_a_naive_resampling_bootstrap():
    """The binomial draw IS the nonparametric bootstrap of a proportion, not a shortcut past it.

    Resampling n Bernoulli values with replacement and averaging is distributed exactly
    Binomial(n, p_hat) / n, so the two intervals must agree to within resampling noise.
    """
    rng = np.random.default_rng(4)
    indicator = (rng.random(240) < 0.35).astype(int)
    lo, hi = _rate_ci(indicator, n_boot=20_000, seed=11)
    naive = np.array(
        [indicator[rng.integers(0, indicator.size, indicator.size)].mean() for _ in range(20_000)]
    )
    naive_lo, naive_hi = np.quantile(naive, [0.025, 0.975])
    assert lo == pytest.approx(naive_lo, abs=0.01)
    assert hi == pytest.approx(naive_hi, abs=0.01)
    assert lo < indicator.mean() < hi


def test_the_rate_interval_of_an_empty_stratum_is_absent_not_zero():
    assert _rate_ci(np.array([]), n_boot=50, seed=1) == (None, None)


def test_the_evaluate_import_stays_deferred_so_the_cycle_cannot_close():
    """`model.evaluate` imports this module; a top-level import back is an ImportError waiting
    for the first entrypoint that happens to load the two modules in the other order."""
    tree = ast.parse(pathlib.Path(inspect.getfile(fairness_module)).read_text())
    top_level = [node for node in tree.body if isinstance(node, ast.ImportFrom | ast.Import)]
    targets = []
    for node in top_level:
        if isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
        else:
            targets.extend(alias.name for alias in node.names)
    assert not any(t.startswith("model.evaluate") for t in targets), (
        f"model.fairness must not import model.evaluate at module scope; found {targets}"
    )


# --------------------------------------------------------------------------------------------
# The rendered report
# --------------------------------------------------------------------------------------------


def test_markdown_renders_the_background_rate_and_every_slice():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=200)
    md = render_fairness_markdown(report)
    assert "background non-toxic flag rate" in md
    assert "| muslim |" in md
    assert "| sikh |" in md
    assert "low power" in md
    assert "no fair / not fair verdict" in md


def test_markdown_summarises_a_long_absent_term_list_instead_of_dumping_it():
    import re

    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    md = render_fairness_markdown(report)
    assert re.search(r"\+\d+ more", md), "the absent-term line must be summarised, not a wall"
    assert len(report["terms_absent"]) == len(IDENTITY_TERMS) - report["n_terms_present"]


def test_markdown_states_the_term_presence_proxy_limitation():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    md = render_fairness_markdown(report)
    assert "no identity annotations" in md
    assert "proxy" in md


def test_markdown_carries_a_per_label_f1_table_when_all_six_labels_are_supplied():
    texts, y_true, y_flag, y_prob = _multilabel_corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    md = render_fairness_markdown(report)
    assert "## Per-label F1 inside each slice" in md
    for label in LABELS:
        assert f"| {label} " in md or f" {label} |" in md
    assert "identity_hate" in md


def test_the_report_file_is_written_and_round_trips(tmp_path):
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=50)
    path = write_fairness_report(tmp_path / "nested" / "fairness-report.md", report)
    assert path.exists()
    body = path.read_text()
    assert body == render_fairness_markdown(report)
    assert body.endswith("\n")
