"""The dashboard reports the system, not the hour it was opened.

Every panel used to be bounded by one 14-day `since`. Only drift needs a bound. The other
six are time series whose x-axis already carries recency, or totals a reader expects to be
project-wide, and bounding them had a dated failure: every prediction in this project was
made between 2026-07-27 and 2026-08-02, so from 2026-08-17 a 14-day window contained zero
rows and every panel rendered empty -- on the due date, with no error and nothing to say the
data was still there.

The second half of this file is about a different lie the same chart told. Latency p50 sat
at 17.0 ms on all seven days that carried traffic. Then eight idle days, then a single
cold-start request at 114 ms. A line chart joined the last real day to that one sample and
drew a smooth 5x ramp across a week in which nothing happened at all.

The third is the drift window's turn at the same failure. That window stayed bounded on
purpose, so it is the one that still runs dry -- and a caption that says "no label exceeds
the threshold" or "PSI >= 0.2 on: toxic, obscene, insult" over three hand-typed comments is
reporting the window again. `tests/unit/test_drift_small_sample.py` holds the guard itself;
these are the sentences a reader sees.
"""

import datetime as dt

import pytest

from monitoring.dashboard import (
    BEGINNING_OF_TIME,
    DEFAULT_DRIFT_WINDOW_DAYS,
    MIN_DRIFT_SAMPLES,
    MIN_SAMPLES_PER_BUCKET,
    Snapshot,
    drift_caption,
    drift_window_days,
    latency_caption,
    window_caption,
    window_days,
)
from monitoring.queries import LatencyBucket, UserPanel
from monitoring.stats import AccuracyReport


def _snapshot(**overrides) -> Snapshot:
    base = {
        "window_days": None,
        "total": 959,
        "seeded": 926,
        "statuses": {},
        "thresholds_digest": "deadbeef1234",
        "latency": [],
        "accuracy": AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[]),
        "panel": UserPanel(0, 0, None, None, None),
    }
    base.update(overrides)
    return Snapshot(**base)


# --------------------------------------------------------------------------- the window


def test_the_default_window_is_all_history(monkeypatch):
    """The regression this file exists for. A default that expires is not a default."""
    monkeypatch.delenv("DASHBOARD_WINDOW_DAYS", raising=False)
    assert window_days() is None


@pytest.mark.parametrize("raw", ["all", "ALL", "0", "", "  "])
def test_the_four_things_an_operator_types_for_all_time_all_mean_all_time(monkeypatch, raw):
    monkeypatch.setenv("DASHBOARD_WINDOW_DAYS", raw)
    assert window_days() is None


def test_an_operator_can_still_narrow_the_window(monkeypatch):
    """All-time is the default, not a ceiling. 'The last 7 days' stays available."""
    monkeypatch.setenv("DASHBOARD_WINDOW_DAYS", "7")
    assert window_days() == 7


def test_drift_is_always_bounded_even_when_everything_else_is_not(monkeypatch):
    """PSI against an all-time production distribution cannot say drift *started*: the old
    data dilutes the new for as long as the project runs. This is the one window that is
    load-bearing, so it has its own variable and its own default."""
    monkeypatch.delenv("DRIFT_WINDOW_DAYS", raising=False)
    assert drift_window_days() == DEFAULT_DRIFT_WINDOW_DAYS
    assert isinstance(drift_window_days(), int)


def test_the_all_time_sentinel_predates_anything_the_system_can_hold():
    assert BEGINNING_OF_TIME < dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    assert BEGINNING_OF_TIME.tzinfo is not None, "a naive sentinel breaks the ts >= comparison"


def test_the_caption_says_all_history_rather_than_naming_a_number(monkeypatch):
    caption = window_caption(_snapshot(window_days=None))
    assert "all history" in caption.lower()
    assert "last none days" not in caption.lower()


def test_the_caption_still_names_the_number_when_the_window_is_narrowed():
    caption = window_caption(_snapshot(window_days=7))
    assert "last 7 days" in caption


def test_the_caption_discloses_the_drift_window_separately():
    """Two windows on one page is exactly the kind of thing a reader assumes is one window."""
    caption = window_caption(_snapshot(window_days=None, drift_window_days=14))
    assert "drift" in caption.lower() and "14" in caption


# ------------------------------------------------------------------- the latency chart


def _buckets(counts: list[int]) -> list[LatencyBucket]:
    day = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
    return [
        LatencyBucket(bucket=day + dt.timedelta(days=i), n=n, p50=17.0, p95=21.0)
        for i, n in enumerate(counts)
    ]


def test_the_caption_reports_requests_per_day_not_only_bucket_count():
    """The bucket count answers 'is there enough history'. It cannot answer 'is any of it
    worth believing', and that is the question the 114 ms point needed asked of it."""
    caption = latency_caption(8, [69, 210, 226, 180, 106, 59, 108, 1])
    assert "min 1" in caption and "max 226" in caption


def test_a_single_sample_day_is_called_out_as_thin():
    """The exact shape that produced the false regression: seven honest days and one day
    holding a single cold-start request."""
    caption = latency_caption(8, [69, 210, 226, 180, 106, 59, 108, 1])
    assert "1 day(s) carry fewer than" in caption
    assert str(MIN_SAMPLES_PER_BUCKET) in caption
    assert "should not be read as a level" in caption


def test_seven_buckets_of_one_request_each_no_longer_passes_as_a_trend():
    """The old guard counted buckets, so this input passed it. Seven points, seven requests."""
    caption = latency_caption(7, [1, 1, 1, 1, 1, 1, 1])
    assert "7 day(s) carry fewer than" in caption


def test_a_well_populated_chart_carries_no_thinness_warning():
    caption = latency_caption(7, [69, 210, 226, 180, 106, 59, 108])
    assert "carry fewer than" not in caption
    assert "min 59" in caption and "max 226" in caption


def test_the_caption_says_the_points_are_not_joined():
    """Because the whole failure was a straight line drawn across a week of no traffic."""
    caption = latency_caption(8, [69, 210, 226, 180, 106, 59, 108, 1])
    assert "not joined" in caption


def test_the_not_enough_history_guard_still_fires_first():
    assert "not enough" in latency_caption(3, [500, 500, 500]).lower()


def test_the_caption_survives_being_given_no_counts():
    """render() passes counts, but the signature keeps them optional and the older callers
    in the guard suite pass a bucket count alone."""
    caption = latency_caption(8)
    assert "8 daily buckets" in caption
    assert "min" not in caption


# --------------------------------------------------------------------- the drift caption


def test_an_empty_drift_window_is_reported_as_no_traffic_not_as_no_drift():
    """"No label exceeds the PSI alert threshold" over zero predictions is a finding of
    stability drawn from silence. The two states have to read differently, because one of
    them means the reader should go and look at why traffic stopped."""
    caption = drift_caption([], alert_psi=0.2, n=0)
    assert "No predictions in the drift window" in caption
    assert "No label exceeds" not in caption


def test_a_handful_of_predictions_cannot_carry_a_drift_alert():
    """Even handed the alerting labels, the caption refuses the sentence. The query layer
    already withholds the flag; this is the second half of the same rule, so a hand-built
    row that claims an alert cannot put "investigate the model" on the screenshot over
    three comments."""
    caption = drift_caption(["toxic", "obscene", "insult"], alert_psi=0.2, n=3)
    assert "Only 3 prediction(s) in the drift window" in caption
    assert str(MIN_DRIFT_SAMPLES) in caption
    assert "PSI >=" not in caption
    assert "Investigate" not in caption


def test_the_drift_caption_reports_its_denominator_the_way_the_latency_caption_does():
    """The latency caption names requests per day. Until this one named n, the highest-
    weighted panel on the page was the only one whose reader could not tell how much
    traffic it stood on."""
    assert "1200 predictions" in drift_caption([], alert_psi=0.2, n=1200)
    assert "1200 predictions" in drift_caption(["toxic"], alert_psi=0.2, n=1200)


def test_the_drift_caption_still_names_the_labels_when_the_window_is_populated():
    """Mirror of the small-sample tests: the alert has to survive having enough evidence."""
    caption = drift_caption(["toxic", "insult"], alert_psi=0.2, n=1200)
    assert "PSI >= 0.2 on: toxic, insult" in caption
    assert "Investigate before trusting the model" in caption


def test_the_drift_caption_survives_being_given_no_count():
    """Same contract as `latency_caption`'s optional counts: `None` is "not recorded", and
    the small-sample branches are skipped rather than guessed at."""
    assert drift_caption([], alert_psi=0.2) == "No label exceeds the PSI alert threshold of 0.2."
    assert "PSI >= 0.2 on: toxic." in drift_caption(["toxic"], alert_psi=0.2)
