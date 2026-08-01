"""Estimation primitives, checked against hand-computable arithmetic.

Every expected value in this file was derived independently (exact rationals where the
arithmetic allows) rather than read back off the implementation. A test that asserted only
"a float in [0, 1]" would pass on the biased pooled ratio this module exists to replace.
"""

import math

import pytest

from monitoring.stats import (
    AccuracyReport,
    horvitz_thompson_accuracy,
    js_divergence,
    psi,
    wilson_interval,
)


def test_wilson_matches_published_value():
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4901, abs=1e-4)
    assert hi == pytest.approx(0.9433, abs=1e-4)


def test_wilson_on_zero_denominator_is_the_unit_interval_not_a_crash():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_is_clamped_to_zero_one():
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0
    assert 0.0 < hi < 1.0


def test_wilson_upper_bound_stays_inside_one_where_wald_would_overrun():
    """The reason Wilson and not the normal approximation. At 5 of 5 the Wald interval is
    1.0 +/- 0, and at 19 of 20 it is 0.95 +/- 0.0955, i.e. an upper bound above 1 printed on
    a graded screenshot."""
    lo, hi = wilson_interval(5, 5)
    assert hi <= 1.0
    assert lo < 1.0, "a 5-of-5 stratum must not read as a certain 100%"
    lo20, hi20 = wilson_interval(19, 20)
    assert hi20 <= 1.0
    assert 0.95 + 1.959963984540054 * math.sqrt(0.95 * 0.05 / 20) > 1.0, "Wald would overrun"


def test_wilson_does_not_let_three_of_four_read_as_a_confident_seventy_five_percent():
    """The small-stratum failure this interval exists to stop."""
    lo, hi = wilson_interval(3, 4)
    assert lo < 0.35 and hi > 0.95
    assert hi - lo > 0.6, "a four-observation stratum must report a wide interval"


def _stratified_rows():
    # flagged: pi = 1.0, n = 200, 120 correct (0.600)
    # random-audit: pi = 0.05, n = 20, 19 correct (0.950)
    return (
        [("flagged", 1.0, True)] * 120
        + [("flagged", 1.0, False)] * 80
        + [("random-audit", 0.05, True)] * 19
        + [("random-audit", 0.05, False)] * 1
    )


def test_horvitz_thompson_differs_from_the_unweighted_pool():
    """The whole point of H8. The unweighted pool is 0.6318; the design-weighted estimate
    is 0.8333. A pooled implementation fails this test."""
    rows = _stratified_rows()
    pooled = sum(1 for _, _, c in rows if c) / len(rows)
    report = horvitz_thompson_accuracy(rows)
    assert pooled == pytest.approx(0.63182, abs=1e-5)
    assert report.point == pytest.approx(0.83333, abs=1e-5)
    assert abs(report.point - pooled) > 0.15


def test_the_estimate_is_the_hand_computed_ratio_of_weighted_sums():
    """Nine rows, worked by hand: one flagged correct at pi=1 contributes weight 1, and two
    audited rows at pi=0.25 contribute weight 4 each. Numerator 1*1 + 4*1 + 4*0 = 5,
    denominator 1 + 4 + 4 = 9, so the estimate is exactly 5/9 = 0.5555... The unweighted
    pool over the same rows is 2/3."""
    rows = [
        ("flagged", 1.0, True),
        ("random-audit", 0.25, True),
        ("random-audit", 0.25, False),
    ]
    report = horvitz_thompson_accuracy(rows)
    assert report.point == pytest.approx(5 / 9, abs=1e-12)
    assert report.point != pytest.approx(2 / 3, abs=1e-3)
    # n_eff = (1 + 4 + 4)^2 / (1 + 16 + 16) = 81 / 33
    assert report.effective_n == pytest.approx(81 / 33, abs=1e-12)


def test_a_uniform_design_reduces_exactly_to_the_unweighted_proportion():
    """When every inclusion probability is equal the weighting must be a no-op: the point
    estimate is the plain proportion, n_eff is the raw n, and the interval is the plain
    Wilson interval. An implementation that double-weights fails here even though it passes
    'the number moved' tests."""
    rows = [("random-audit", 0.5, True)] * 8 + [("random-audit", 0.5, False)] * 2
    report = horvitz_thompson_accuracy(rows)
    assert report.point == pytest.approx(0.8, abs=1e-12)
    assert report.effective_n == pytest.approx(10.0, abs=1e-12)
    assert (report.lo, report.hi) == pytest.approx(wilson_interval(8, 10), abs=1e-12)


def test_the_estimate_does_not_depend_on_row_order():
    rows = _stratified_rows()
    forward = horvitz_thompson_accuracy(rows)
    backward = horvitz_thompson_accuracy(list(reversed(rows)))
    assert forward.point == pytest.approx(backward.point, abs=1e-12)
    assert forward.effective_n == pytest.approx(backward.effective_n, abs=1e-12)
    assert [s.stratum for s in forward.strata] == [s.stratum for s in backward.strata]


def test_report_carries_per_stratum_n_and_intervals_not_a_bare_point():
    report = horvitz_thompson_accuracy(_stratified_rows())
    by_name = {s.stratum: s for s in report.strata}
    assert by_name["flagged"].n == 200
    assert by_name["flagged"].accuracy == pytest.approx(0.60)
    assert by_name["flagged"].lo == pytest.approx(0.5308, abs=1e-4)
    assert by_name["flagged"].hi == pytest.approx(0.6654, abs=1e-4)
    assert by_name["random-audit"].n == 20
    assert by_name["random-audit"].sample_rate == pytest.approx(0.05)
    assert report.effective_n == pytest.approx(43.9024, abs=1e-3)
    assert report.lo == pytest.approx(0.6975, abs=1e-3)
    assert report.hi == pytest.approx(0.9156, abs=1e-3)
    assert report.lo < report.point < report.hi


def test_the_interval_is_widened_by_the_design_effect_not_taken_at_raw_n():
    """220 observations drawn under two inclusion probabilities do not carry the information
    of 220 independent ones. Evaluating Wilson at the raw n would print a narrow interval
    around a number the design says is far less certain."""
    report = horvitz_thompson_accuracy(_stratified_rows())
    naive_lo, naive_hi = wilson_interval(139, 220)
    assert report.effective_n < 220
    assert (report.hi - report.lo) > (naive_hi - naive_lo)
    assert report.hi - report.lo > 0.2


def test_empty_input_returns_none_not_nan_and_not_zero_division():
    report = horvitz_thompson_accuracy([])
    assert isinstance(report, AccuracyReport)
    assert report.n == 0
    assert report.point is None
    assert report.lo is None and report.hi is None
    assert report.strata == []


def test_a_missing_sample_rate_is_an_error_not_a_default():
    # NB: this test cannot be named after the retired `review_queue` column. Phase 2 ships a
    # repo-wide scan that bans that identifier anywhere in the tree, and a test name is a
    # hit like any other -- the plan's own name for this test tripped it.
    with pytest.raises(ValueError, match="sample_rate"):
        horvitz_thompson_accuracy([("flagged", None, True)])
    with pytest.raises(ValueError, match="sample_rate"):
        horvitz_thompson_accuracy([("flagged", 0.0, True)])


def test_a_sample_rate_above_one_is_an_error():
    """pi > 1 is not a probability. It would silently shrink a row's weight below 1 and pull
    the estimate toward whichever stratum carried the bad value."""
    with pytest.raises(ValueError, match="sample_rate"):
        horvitz_thompson_accuracy([("flagged", 1.5, True)])


def test_one_stratum_sampled_at_two_rates_is_reported_as_two_design_cells():
    """`RANDOM_AUDIT_RATE` is deploy configuration and configuration changes, which is why
    the rate is stored per row. Two rates under one name are two different designs; folding
    them into a single StratumStat would print one of the rates as if it applied to all of
    the rows."""
    rows = [
        ("random-audit", 0.10, True),
        ("random-audit", 0.10, True),
        ("random-audit", 0.02, False),
    ]
    report = horvitz_thompson_accuracy(rows)
    audit = [s for s in report.strata if s.stratum == "random-audit"]
    assert len(audit) == 2
    assert {s.n for s in audit} == {2, 1}
    assert sorted(s.sample_rate for s in audit) == pytest.approx([0.02, 0.10])
    assert sum(s.n for s in audit) == report.n
    # 10 + 10 + 0 over 10 + 10 + 50
    assert report.point == pytest.approx(20 / 70, abs=1e-12)


def test_psi_flags_a_known_shift_and_stays_quiet_on_none():
    assert psi(0.10, 0.30) == pytest.approx(0.26999, abs=1e-5)
    assert psi(0.10, 0.10) == 0.0
    assert psi(0.10, 0.13) == pytest.approx(0.00889, abs=1e-5)


def test_psi_bands_separate_a_moderate_shift_from_a_major_one():
    """The dashboard alerts at 0.2. These two calls sit either side of it."""
    assert psi(0.10, 0.13) < 0.1
    assert psi(0.10, 0.30) >= 0.2


def test_psi_is_symmetric():
    assert psi(0.10, 0.30) == pytest.approx(psi(0.30, 0.10), abs=1e-12)


def test_psi_is_finite_at_the_boundaries():
    assert math.isfinite(psi(0.0, 0.5))
    assert math.isfinite(psi(0.5, 0.0))
    assert math.isfinite(psi(0.0, 0.0))


def test_js_divergence_is_bounded_and_zero_on_identity():
    assert js_divergence(0.10, 0.10) == 0.0
    assert js_divergence(0.10, 0.30) == pytest.approx(0.04678, abs=1e-5)
    assert 0.0 <= js_divergence(0.0, 1.0) <= 1.0


def test_js_divergence_is_symmetric_and_maximal_on_disjoint_support():
    assert js_divergence(0.10, 0.30) == pytest.approx(js_divergence(0.30, 0.10), abs=1e-12)
    assert js_divergence(0.0, 1.0) == pytest.approx(1.0, abs=1e-9)
