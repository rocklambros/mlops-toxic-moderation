"""The dashboard's three graded aggregations, asserted against a real Postgres.

Every helper here inserts a row that satisfies every NOT NULL column Phase 2 declares.
Omitting `input_chars`, `status` or `persist_status` aborts the transaction, and the
assertions that follow then measure an empty table rather than the query under test.
"""

import datetime as dt

import pytest
from sqlalchemy import text

from model.labels import LABELS
from monitoring.queries import latency_over_time

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.UTC)


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
