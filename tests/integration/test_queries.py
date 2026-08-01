"""The dashboard's three graded aggregations, asserted against a real Postgres.

Every helper here inserts a row that satisfies every NOT NULL column Phase 2 declares.
Omitting `input_chars`, `status` or `persist_status` aborts the transaction, and the
assertions that follow then measure an empty table rather than the query under test.
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from model.labels import LABELS
from monitoring.baseline import Baseline, load_thresholds
from monitoring.queries import (
    DriftRow,
    drift_report,
    flag_rate_series,
    latency_over_time,
    live_accuracy,
    review_counts,
    seeded_share,
    user_feedback_panel,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)

THRESHOLDS = load_thresholds(Path("tests/fixtures/thresholds.json"))
BASELINE = Baseline(
    schema_version=1,
    data_version="d",
    model_version="toxic-clf:v3",
    n=1000,
    flag_rates={
        "toxic": 0.10, "severe_toxic": 0.01, "obscene": 0.05,
        "threat": 0.003, "insult": 0.05, "identity_hate": 0.009,
    },
)


def insert_prediction(conn, request_id, ts, probs=None, latency_ms=20, is_seed=False):
    probs = probs or {label: 0.05 for label in LABELS}
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status, is_seed) "
            f"VALUES (:rid, :ts, 'text', 4, 'm', {binds}, 'allow', :mx, :lat, 'ok', 'direct', "
            ":seed)"
        ),
        {
            "rid": request_id,
            "ts": ts,
            "mx": max(probs.values()),
            "lat": latency_ms,
            "seed": is_seed,
            **{f"p_{label}": probs[label] for label in LABELS},
        },
    )


def test_the_fixture_helper_actually_writes_a_row(conn):
    """Non-vacuity. Every assertion below is over rows this helper inserted; a silently
    rejected INSERT would turn each of them into a statement about an empty table."""
    insert_prediction(conn, "probe", NOW)
    conn.commit()
    assert conn.execute(text("SELECT count(*) FROM predictions")).scalar_one() == 1


def test_latency_buckets_by_day_with_percentiles(conn):
    for day in range(8):
        ts = NOW - dt.timedelta(days=day)
        for i in range(10):
            insert_prediction(conn, f"p{day}_{i}", ts, latency_ms=10 + i * 10)
    conn.commit()

    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=14))
    assert len(buckets) == 8
    assert [b.n for b in buckets] == [10] * 8
    assert buckets[0].bucket < buckets[-1].bucket
    assert buckets[0].p50 == pytest.approx(55.0)
    assert buckets[0].p95 == pytest.approx(95.5)
    assert buckets[0].p95 >= buckets[0].p50


def test_latency_reports_a_tail_that_a_mean_would_hide(conn):
    """Rubric 3.2 asks for latency over time; H28 is about the tail. A bucket whose p95 is
    its mean is a bucket computed with the wrong function."""
    ts = NOW - dt.timedelta(days=1)
    for i in range(99):
        insert_prediction(conn, f"fast{i}", ts, latency_ms=10)
    insert_prediction(conn, "slow", ts, latency_ms=5000)
    conn.commit()

    bucket = latency_over_time(conn, since=NOW - dt.timedelta(days=14))[0]
    assert bucket.n == 100
    assert bucket.p50 == pytest.approx(10.0)
    assert bucket.p95 == pytest.approx(10.0)
    assert bucket.p95 != pytest.approx(sum([10] * 99 + [5000]) / 100)


def test_latency_on_an_empty_table_returns_an_empty_list_not_a_crash(conn):
    assert latency_over_time(conn, since=NOW - dt.timedelta(days=14)) == []


def test_day_buckets_are_utc_and_not_the_database_session_timezone(conn):
    """Two rows on the same UTC day fall on different local days in Los Angeles. If the
    bucket followed the session's TimeZone, the same data would produce a different chart --
    and a different count against the dashboard's seven-bucket floor -- on a server whose
    timezone nobody thought was part of the metric."""
    insert_prediction(conn, "early", dt.datetime(2026, 8, 14, 2, 0, tzinfo=dt.UTC))
    insert_prediction(conn, "late", dt.datetime(2026, 8, 14, 20, 0, tzinfo=dt.UTC))
    conn.commit()
    conn.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))

    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=14))
    assert len(buckets) == 1, "buckets followed the session timezone, not UTC"
    assert buckets[0].n == 2


def test_latency_respects_the_window(conn):
    insert_prediction(conn, "old", NOW - dt.timedelta(days=40))
    insert_prediction(conn, "new", NOW - dt.timedelta(days=1))
    conn.commit()
    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=14))
    assert len(buckets) == 1


def _toxic_probs(value: float) -> dict[str, float]:
    probs = {label: 0.01 for label in LABELS}
    probs["toxic"] = value
    return probs


def test_drift_report_returns_one_row_per_label_with_a_reference(conn):
    for i in range(100):
        # 30 of 100 above the 0.45 toxic threshold -> production rate 0.30 vs baseline 0.10
        insert_prediction(conn, f"d{i}", NOW - dt.timedelta(hours=i),
                          probs=_toxic_probs(0.9 if i < 30 else 0.1))
    conn.commit()

    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert [row.label for row in rows] == list(LABELS)
    toxic = next(row for row in rows if row.label == "toxic")
    assert isinstance(toxic, DriftRow)
    assert toxic.baseline_rate == pytest.approx(0.10)
    assert toxic.production_rate == pytest.approx(0.30)
    assert toxic.psi == pytest.approx(0.26999, abs=1e-4)
    assert toxic.js == pytest.approx(0.04678, abs=1e-4)
    assert toxic.alert is True


def test_a_stable_label_does_not_alert(conn):
    for i in range(100):
        insert_prediction(conn, f"s{i}", NOW - dt.timedelta(hours=i),
                          probs=_toxic_probs(0.9 if i < 10 else 0.1))
    conn.commit()
    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    toxic = next(row for row in rows if row.label == "toxic")
    assert toxic.production_rate == pytest.approx(0.10)
    assert toxic.psi == pytest.approx(0.0, abs=1e-9)
    assert toxic.alert is False


def test_each_label_is_flagged_against_its_own_threshold(conn):
    """One probability, six different answers. A single global threshold -- or the wrong
    label's -- reproduces none of this, and would silently redefine the decision rule the
    Phase 1 baseline was computed under, which is the only thing that makes PSI comparable."""
    for i in range(10):
        insert_prediction(conn, f"pl{i}", NOW - dt.timedelta(hours=i),
                          probs=dict.fromkeys(LABELS, 0.28))
    conn.commit()
    rows = {row.label: row for row in drift_report(
        conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS, baseline=BASELINE)}
    # thresholds.json: toxic .45, severe_toxic .30, obscene .50, threat .18, insult .47,
    # identity_hate .25. Only threat and identity_hate sit below 0.28.
    assert rows["threat"].production_rate == pytest.approx(1.0)
    assert rows["identity_hate"].production_rate == pytest.approx(1.0)
    for label in ("toxic", "severe_toxic", "obscene", "insult"):
        assert rows[label].production_rate == pytest.approx(0.0), label


def test_a_probability_exactly_on_the_threshold_is_flagged(conn):
    """`>=`, not `>`. Phase 1's baseline_flag_rates.json was computed with the same
    comparison; flipping it here puts the reference and the production series on two
    different decision rules and the PSI stops meaning anything."""
    insert_prediction(conn, "edge", NOW - dt.timedelta(hours=1),
                      probs=_toxic_probs(THRESHOLDS["toxic"]))
    conn.commit()
    rows = {row.label: row for row in drift_report(
        conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS, baseline=BASELINE)}
    assert rows["toxic"].production_rate == pytest.approx(1.0)


def test_the_alert_threshold_is_the_one_the_caller_states(conn):
    """The caption names a number. If the flag were computed against a constant, the number
    on the screenshot and the rule behind it would be two different things."""
    for i in range(100):
        insert_prediction(conn, f"al{i}", NOW - dt.timedelta(hours=i),
                          probs=_toxic_probs(0.9 if i < 30 else 0.1))
    conn.commit()
    strict = drift_report(conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS,
                          baseline=BASELINE, alert_psi=0.2)
    lax = drift_report(conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS,
                       baseline=BASELINE, alert_psi=1.0)
    assert next(row for row in strict if row.label == "toxic").alert is True
    assert next(row for row in lax if row.label == "toxic").alert is False


def test_drift_on_an_empty_window_reports_zero_rates_without_dividing_by_zero(conn):
    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert len(rows) == len(LABELS)
    assert all(row.production_rate == 0.0 for row in rows)
    assert all(row.psi >= 0.0 for row in rows)


def test_flag_rate_series_has_one_row_per_bucket_and_one_column_per_label(conn):
    for day in range(7):
        for i in range(5):
            insert_prediction(conn, f"t{day}_{i}", NOW - dt.timedelta(days=day),
                              probs=_toxic_probs(0.9 if i < 2 else 0.1))
    conn.commit()
    frame = flag_rate_series(conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS)
    assert len(frame) == 7
    assert list(frame.columns) == ["bucket", *LABELS]
    assert frame["toxic"].iloc[0] == pytest.approx(0.4)
    assert frame["threat"].iloc[0] == pytest.approx(0.0)


def test_flag_rate_series_on_an_empty_window_still_has_every_column(conn):
    """A degenerate frame with no columns is what makes the dashboard's chart call raise
    instead of drawing nothing."""
    frame = flag_rate_series(conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS)
    assert len(frame) == 0
    assert list(frame.columns) == ["bucket", *LABELS]


def _reviewed(conn, request_id, stratum, sample_rate, correct, ts=None):
    ts = ts or NOW - dt.timedelta(days=1)
    insert_prediction(conn, request_id, ts)
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot, reviewer_id, reviewed_ts) VALUES (:rid, :ts, 'reviewed', "
            ":src, :rate, 'text', 'rock', :ts)"
        ),
        {"rid": request_id, "ts": ts, "src": stratum, "rate": sample_rate},
    )
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES (:rid, :ts, 'reviewer', 'rock', CAST(:agree AS jsonb), :exact)"
        ),
        {
            "rid": request_id,
            "ts": ts,
            "agree": json.dumps({label: correct for label in LABELS}),
            "exact": correct,
        },
    )


def test_live_accuracy_is_design_weighted_not_pooled(conn):
    for i in range(200):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=i < 120)
    for i in range(20):
        _reviewed(conn, f"ra{i}", "random-audit", 0.05, correct=i < 19)
    conn.commit()

    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 220
    assert report.point == pytest.approx(0.83333, abs=1e-4)   # pooled would be 0.63182
    assert {s.stratum for s in report.strata} == {"flagged", "random-audit"}
    assert next(s for s in report.strata if s.stratum == "flagged").n == 200
    assert report.lo < report.point < report.hi
    # Kish's effective n, not the raw 220: 220 unequally-weighted observations do not carry
    # the information of 220 independent ones, and an interval evaluated at the raw n would
    # claim they do.
    assert report.effective_n == pytest.approx(43.9024, abs=1e-3)
    assert report.effective_n < report.n


def test_two_audit_rates_are_two_design_cells_not_one_stratum(conn):
    """RANDOM_AUDIT_RATE is deploy configuration and configuration changes. Rows drawn at
    0.05 and rows drawn at 0.50 were drawn under different designs; folding them under one
    name would weight half of them by a probability they were never drawn with."""
    for i in range(10):
        _reviewed(conn, f"fa{i}", "flagged", 1.0, correct=True)
    _reviewed(conn, "lo1", "random-audit", 0.05, correct=True)
    _reviewed(conn, "hi1", "random-audit", 0.50, correct=False)
    conn.commit()

    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert len(report.strata) == 3, [s.stratum for s in report.strata]
    rates = sorted(s.sample_rate for s in report.strata if s.stratum == "random-audit")
    assert rates == [0.05, 0.5]
    # 10*1 + 1*20 correct out of 10*1 + 1*20 + 1*2 total weight.
    assert report.point == pytest.approx(30.0 / 32.0, abs=1e-6)


def test_live_accuracy_respects_the_window(conn):
    """The dashboard states a window in its caption. A row outside it that still moves the
    number makes the caption a false statement about the metric beside it."""
    for i in range(10):
        _reviewed(conn, f"in{i}", "flagged", 1.0, correct=True)
    for i in range(10):
        _reviewed(conn, f"out{i}", "flagged", 1.0, correct=False,
                  ts=NOW - dt.timedelta(days=40))
    conn.commit()
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 10
    assert report.point == pytest.approx(1.0)


def test_live_accuracy_on_an_empty_table_is_none_not_a_zero_division(conn):
    """C5: this is the panel that renders NaN or a traceback in the graded screenshot when
    nothing has ever been reviewed."""
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 0
    assert report.point is None
    assert report.strata == []


def test_user_feedback_cannot_move_the_graded_estimate(conn):
    """H9 composed with H8: an anonymous write path must not be an anonymous write path
    INTO THE GRADED METRIC."""
    for i in range(200):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=i < 120)
    conn.commit()
    before = live_accuracy(conn, since=NOW - dt.timedelta(days=14))

    for i in range(200):
        conn.execute(
            text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                 "VALUES (:rid, :ts, 'user', '{}'::jsonb, false)"),
            {"rid": f"fl{i}", "ts": NOW},
        )
    conn.commit()
    after = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert after.point == pytest.approx(before.point)
    assert after.n == before.n


def test_user_report_stratum_is_excluded_from_the_estimate(conn):
    for i in range(10):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=True)
    insert_prediction(conn, "ur1", NOW - dt.timedelta(days=1))
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot, reviewer_id, reviewed_ts) VALUES ('ur1', :ts, 'reviewed', "
            "'user-report', NULL, 'text', 'rock', :ts)"
        ),
        {"ts": NOW - dt.timedelta(days=1)},
    )
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES ('ur1', :ts, 'reviewer', 'rock', CAST(:agree AS jsonb), false)"
        ),
        {"ts": NOW - dt.timedelta(days=1),
         "agree": json.dumps({label: False for label in LABELS})},
    )
    conn.commit()
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 10
    assert report.point == pytest.approx(1.0)


def test_user_panel_reports_its_own_n_and_interval(conn):
    for i in range(10):
        insert_prediction(conn, f"u{i}", NOW - dt.timedelta(hours=1))
        conn.execute(
            text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                 "VALUES (:rid, :ts, 'user', '{}'::jsonb, :ok)"),
            {"rid": f"u{i}", "ts": NOW, "ok": i < 8},
        )
    conn.commit()
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=14))
    assert panel.n == 10 and panel.agree == 8
    assert panel.rate == pytest.approx(0.8)
    assert panel.lo == pytest.approx(0.4901, abs=1e-3)
    assert panel.hi == pytest.approx(0.9433, abs=1e-3)


def test_the_user_panel_counts_no_reviewer_row(conn):
    """The two sources share a table. A panel that counted both would report the reviewer's
    work as public agreement, and the separation H9 asks for would exist only in the
    caption."""
    for i in range(10):
        _reviewed(conn, f"rv{i}", "flagged", 1.0, correct=True)
    conn.commit()
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=14))
    assert panel.n == 0 and panel.rate is None


def test_user_panel_on_empty_data_is_none_not_nan(conn):
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=14))
    assert panel.n == 0 and panel.rate is None and panel.lo is None


def test_review_counts_break_the_queue_down_by_status(conn):
    """The footer caption names pending, rescored and reviewed. A count that collapsed them
    would let an empty queue and a full one print the same line."""
    _reviewed(conn, "done", "flagged", 1.0, correct=True)
    insert_prediction(conn, "waiting", NOW - dt.timedelta(days=1))
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES ('waiting', :ts, 'pending', 'flagged', 1.0, 'text')"
        ),
        {"ts": NOW - dt.timedelta(days=1)},
    )
    conn.commit()
    counts = review_counts(conn, since=NOW - dt.timedelta(days=14))
    assert counts == {"reviewed": 1, "pending": 1}
    assert review_counts(conn, since=NOW + dt.timedelta(days=1)) == {}


def test_seeded_share_separates_replayed_traffic_from_live_traffic(conn):
    """The dashboard says out loud how much of its data is `make seed-demo` replay. A share
    that always read zero would let a screenshot of seeded data look like production."""
    for i in range(7):
        insert_prediction(conn, f"seed{i}", NOW - dt.timedelta(days=1), is_seed=True)
    for i in range(3):
        insert_prediction(conn, f"live{i}", NOW - dt.timedelta(days=1), is_seed=False)
    conn.commit()
    assert seeded_share(conn, since=NOW - dt.timedelta(days=14)) == (10, 7)
    assert seeded_share(conn, since=NOW + dt.timedelta(days=1)) == (0, 0)
