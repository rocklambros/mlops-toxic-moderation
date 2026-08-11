"""Panel 3 renders the one number a grader reads off the screenshot, and had no floor.

Panel 1 requires 20 samples per bucket. Panel 2 requires 30 plus an exact binomial tail
test. Panel 3 gated its headline st.metric on `point is not None` and nothing else, so a
single reviewed row scored correct renders 100.0% in the largest type on the page. The
Wilson interval in the caption bounds that honestly, but a caption is not what a screenshot
shows.
"""

from monitoring.dashboard import (
    MIN_REVIEWED_FOR_ESTIMATE,
    accuracy_floor_notice,
    accuracy_is_reportable,
)
from monitoring.stats import AccuracyReport, StratumStat


def _report(n: int, point: float | None = 1.0) -> AccuracyReport:
    return AccuracyReport(
        n=n,
        point=point,
        lo=0.207 if point is not None else None,
        hi=1.0 if point is not None else None,
        effective_n=float(n),
        strata=[
            StratumStat(
                stratum="flagged",
                n=n,
                correct=n if point else 0,
                sample_rate=1.0,
                accuracy=point,
                lo=0.207 if point is not None else None,
                hi=1.0 if point is not None else None,
            )
        ],
    )


def test_one_perfect_review_does_not_render_a_headline_accuracy():
    assert accuracy_is_reportable(_report(n=1)) is False


def test_the_floor_matches_the_drift_panel_beside_it():
    assert MIN_REVIEWED_FOR_ESTIMATE == 30


def test_exactly_the_floor_is_reportable():
    assert accuracy_is_reportable(_report(n=MIN_REVIEWED_FOR_ESTIMATE)) is True


def test_one_below_the_floor_is_not():
    assert accuracy_is_reportable(_report(n=MIN_REVIEWED_FOR_ESTIMATE - 1)) is False


def test_the_current_live_volume_is_unaffected():
    """643 reviewed items on 2026-08-11. This guard must not change what is on screen now."""
    assert accuracy_is_reportable(_report(n=643)) is True


def test_no_estimate_at_all_is_not_reportable():
    assert accuracy_is_reportable(_report(n=0, point=None)) is False


def test_the_notice_names_both_numbers_so_the_gap_is_actionable():
    notice = accuracy_floor_notice(_report(n=7))
    assert "7" in notice
    assert str(MIN_REVIEWED_FOR_ESTIMATE) in notice
