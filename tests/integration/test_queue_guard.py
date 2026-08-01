"""Admission control, against a real Postgres.

Three separate controls live in `admit_review`, and each needs a test that can only pass
because that control is present: the global depth cap, the per-fingerprint pending cap, and
the per-fingerprint sliding-window enqueue rate. The plan's own test set pinned the third
one to 99 in every case, which leaves the sliding window entirely unexercised -- so it is
exercised explicitly here, including its lower boundary.
"""

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.queue_guard import AdmissionConfig, admit_review, admit_user_feedback
from model.labels import LABELS

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.UTC)


def _predict_row(conn, request_id: str, fp: str | None = "aaaabbbbccccdddd", ts=NOW) -> None:
    """Insert a VALID prediction row.

    `input_chars`, `status` and `persist_status` are NOT NULL in the Phase 2 schema.
    Omitting them aborts the transaction here, and every assertion that follows then
    measures InFailedSqlTransaction instead of the control it names.
    """
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.1" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status, submitter_fp) "
            f"VALUES (:rid, :ts, 'hello', 5, 'm', {vals}, 'allow', 0.1, 5, 'ok', 'direct', :fp)"
        ),
        {"rid": request_id, "ts": ts, "fp": fp},
    )


def _pending(conn) -> int:
    return conn.execute(text("SELECT count(*) FROM review_queue")).scalar_one()


def test_admits_a_flagged_row_and_records_its_inclusion_prob(conn):
    _predict_row(conn, "r1")
    result = admit_review(
        conn, request_id="r1", source="flagged", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(),
    )
    assert result.admitted and result.reason == "ok"
    row = conn.execute(
        text("SELECT source, sample_rate, status, input_text_snapshot, enqueued_ts "
             "FROM review_queue WHERE request_id = 'r1'")
    ).one()
    assert row.source == "flagged"
    assert row.sample_rate == pytest.approx(1.0)
    assert row.status == "pending"
    assert row.input_text_snapshot == "hello"
    assert row.enqueued_ts == NOW


def test_the_snapshot_survives_the_retention_purge_of_the_original(conn):
    """`input_text_snapshot` is copied at enqueue time precisely because the 30-day purge
    nulls `predictions.input_text`. A reviewer must not be handed an empty comment."""
    _predict_row(conn, "snap")
    admit_review(conn, request_id="snap", source="flagged", submitter_fp="aaaabbbbccccdddd",
                 now=NOW, config=AdmissionConfig())
    conn.execute(text("UPDATE predictions SET input_text = NULL WHERE request_id = 'snap'"))
    assert conn.execute(
        text("SELECT input_text_snapshot FROM review_queue WHERE request_id = 'snap'")
    ).scalar_one() == "hello"


def test_random_audit_records_the_configured_rate(conn):
    _predict_row(conn, "r2")
    admit_review(
        conn, request_id="r2", source="random-audit", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(random_audit_rate=0.05),
    )
    rate = conn.execute(
        text("SELECT sample_rate FROM review_queue WHERE request_id = 'r2'")
    ).scalar_one()
    assert rate == pytest.approx(0.05)


def test_the_recorded_rate_is_the_one_in_force_at_enqueue_time(conn):
    """RANDOM_AUDIT_RATE is deploy configuration. Two rows enqueued either side of a change
    must carry the two different weights, because that is the whole reason the column is
    written here rather than reconstructed by the estimator."""
    for request_id, rate in (("a", 0.05), ("b", 0.20)):
        _predict_row(conn, request_id)
        admit_review(conn, request_id=request_id, source="random-audit",
                     submitter_fp="aaaabbbbccccdddd", now=NOW,
                     config=AdmissionConfig(random_audit_rate=rate,
                                            max_enqueues_per_source_per_window=99))
    rates = dict(conn.execute(
        text("SELECT request_id, sample_rate FROM review_queue ORDER BY request_id")
    ).all())
    assert rates == pytest.approx({"a": 0.05, "b": 0.20})


def test_an_audit_rate_of_zero_is_refused_rather_than_stored(conn):
    """pi = 0 is not a valid inclusion probability; a row carrying it would divide by zero
    in the estimator. The CHECK constraint would also reject it, but a caller with the audit
    sampler switched off deserves the reason, not an IntegrityError."""
    _predict_row(conn, "z0")
    with pytest.raises(ValueError, match="random_audit_rate"):
        admit_review(conn, request_id="z0", source="random-audit",
                     submitter_fp="aaaabbbbccccdddd", now=NOW,
                     config=AdmissionConfig(random_audit_rate=0.0))
    assert _pending(conn) == 0


def test_an_unknown_source_is_refused(conn):
    _predict_row(conn, "z1")
    with pytest.raises(ValueError, match="source"):
        admit_review(conn, request_id="z1", source="whatever",
                     submitter_fp="aaaabbbbccccdddd", now=NOW, config=AdmissionConfig())
    assert _pending(conn) == 0


def test_user_report_records_a_null_rate(conn):
    _predict_row(conn, "r3")
    admit_review(
        conn, request_id="r3", source="user-report", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(),
    )
    rate = conn.execute(
        text("SELECT sample_rate FROM review_queue WHERE request_id = 'r3'")
    ).scalar_one()
    assert rate is None


def test_an_unknown_request_is_not_enqueued(conn):
    result = admit_review(conn, request_id="ghost", source="flagged",
                          submitter_fp="aaaabbbbccccdddd", now=NOW, config=AdmissionConfig())
    assert not result.admitted and result.reason == "unknown_request"
    assert _pending(conn) == 0


def test_depth_cap_rejects_once_the_queue_is_full(conn):
    config = AdmissionConfig(max_pending=3, max_pending_per_source=99,
                             max_enqueues_per_source_per_window=99)
    for i in range(3):
        _predict_row(conn, f"d{i}")
        assert admit_review(conn, request_id=f"d{i}", source="flagged",
                            submitter_fp="aaaabbbbccccdddd", now=NOW, config=config).admitted
    _predict_row(conn, "d3")
    result = admit_review(conn, request_id="d3", source="flagged",
                          submitter_fp="aaaabbbbccccdddd", now=NOW, config=config)
    assert not result.admitted and result.reason == "queue_full"
    assert _pending(conn) == 3


def test_the_depth_cap_counts_only_pending_rows(conn):
    """A reviewed queue must reopen. Counting every historical row would wedge the queue
    permanently at the cap after the first `max_pending` reviews."""
    config = AdmissionConfig(max_pending=2, max_pending_per_source=99,
                             max_enqueues_per_source_per_window=99)
    for i in range(2):
        _predict_row(conn, f"p{i}")
        assert admit_review(conn, request_id=f"p{i}", source="flagged",
                            submitter_fp="aaaabbbbccccdddd", now=NOW, config=config).admitted
    conn.execute(text("UPDATE review_queue SET status = 'reviewed' WHERE request_id = 'p0'"))
    _predict_row(conn, "p2")
    assert admit_review(conn, request_id="p2", source="flagged",
                        submitter_fp="aaaabbbbccccdddd", now=NOW, config=config).admitted


def test_per_source_quota_rejects_a_flood_from_one_fingerprint(conn):
    config = AdmissionConfig(max_pending=99, max_pending_per_source=2,
                             max_enqueues_per_source_per_window=99)
    for i in range(2):
        _predict_row(conn, f"f{i}", fp="1111111111111111")
        assert admit_review(conn, request_id=f"f{i}", source="flagged",
                            submitter_fp="1111111111111111", now=NOW, config=config).admitted
    _predict_row(conn, "f2", fp="1111111111111111")
    result = admit_review(conn, request_id="f2", source="flagged",
                          submitter_fp="1111111111111111", now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"


def test_a_second_fingerprint_is_not_starved_by_the_first(conn):
    config = AdmissionConfig(max_pending=99, max_pending_per_source=2,
                             max_enqueues_per_source_per_window=99)
    for i in range(2):
        _predict_row(conn, f"g{i}", fp="1111111111111111")
        admit_review(conn, request_id=f"g{i}", source="flagged",
                     submitter_fp="1111111111111111", now=NOW, config=config)
    _predict_row(conn, "h0", fp="2222222222222222")
    assert admit_review(conn, request_id="h0", source="flagged",
                        submitter_fp="2222222222222222", now=NOW, config=config).admitted


def test_the_sliding_window_caps_enqueues_even_when_nothing_is_pending(conn):
    """The pending cap alone is not a rate limit: a flooder whose items are reviewed
    promptly would face no limit at all. This is the control that stops that, and it is the
    one the plan's own test set disabled in every case."""
    config = AdmissionConfig(max_pending=99, max_pending_per_source=99,
                             window_seconds=3600, max_enqueues_per_source_per_window=2)
    for i in range(2):
        _predict_row(conn, f"w{i}", fp="4444444444444444")
        assert admit_review(conn, request_id=f"w{i}", source="flagged",
                            submitter_fp="4444444444444444", now=NOW, config=config).admitted
    conn.execute(text("UPDATE review_queue SET status = 'reviewed'"))
    _predict_row(conn, "w2", fp="4444444444444444")
    result = admit_review(conn, request_id="w2", source="flagged",
                          submitter_fp="4444444444444444", now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"


def test_enqueues_older_than_the_window_stop_counting(conn):
    """The window has to slide. A cap that counts every enqueue ever made is a permanent
    ban after the first burst."""
    config = AdmissionConfig(max_pending=99, max_pending_per_source=99,
                             window_seconds=3600, max_enqueues_per_source_per_window=2)
    stale = NOW - dt.timedelta(hours=2)
    for i in range(2):
        _predict_row(conn, f"s{i}", fp="5555555555555555", ts=stale)
        assert admit_review(conn, request_id=f"s{i}", source="flagged",
                            submitter_fp="5555555555555555", now=stale, config=config).admitted
    _predict_row(conn, "s2", fp="5555555555555555")
    assert admit_review(conn, request_id="s2", source="flagged",
                        submitter_fp="5555555555555555", now=NOW, config=config).admitted


def test_the_window_boundary_is_inclusive_of_a_row_exactly_at_its_edge(conn):
    """Pinned so a later `>` / `>=` flip is a red test rather than a silent one-row leak."""
    config = AdmissionConfig(max_pending=99, max_pending_per_source=99,
                             window_seconds=3600, max_enqueues_per_source_per_window=1)
    edge = NOW - dt.timedelta(seconds=3600)
    _predict_row(conn, "e0", fp="6666666666666666", ts=edge)
    assert admit_review(conn, request_id="e0", source="flagged",
                        submitter_fp="6666666666666666", now=edge, config=config).admitted
    _predict_row(conn, "e1", fp="6666666666666666")
    result = admit_review(conn, request_id="e1", source="flagged",
                          submitter_fp="6666666666666666", now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"


def test_a_quota_is_keyed_on_the_fingerprint_not_on_the_source_stratum(conn):
    """One flooder must not be able to buy extra allowance by alternating strata."""
    config = AdmissionConfig(max_pending=99, max_pending_per_source=99,
                             window_seconds=3600, max_enqueues_per_source_per_window=2)
    for i, source in enumerate(("flagged", "user-report")):
        _predict_row(conn, f"m{i}", fp="7777777777777777")
        assert admit_review(conn, request_id=f"m{i}", source=source,
                            submitter_fp="7777777777777777", now=NOW, config=config).admitted
    _predict_row(conn, "m2", fp="7777777777777777")
    result = admit_review(conn, request_id="m2", source="random-audit",
                          submitter_fp="7777777777777777", now=NOW,
                          config=AdmissionConfig(max_pending=99, max_pending_per_source=99,
                                                 window_seconds=3600,
                                                 max_enqueues_per_source_per_window=2,
                                                 random_audit_rate=0.05))
    assert not result.admitted and result.reason == "source_quota"


def test_enqueueing_the_same_request_twice_is_a_no_op(conn):
    _predict_row(conn, "dup")
    assert admit_review(conn, request_id="dup", source="flagged",
                        submitter_fp="aaaabbbbccccdddd", now=NOW,
                        config=AdmissionConfig()).admitted
    second = admit_review(conn, request_id="dup", source="user-report",
                          submitter_fp="aaaabbbbccccdddd", now=NOW, config=AdmissionConfig())
    assert not second.admitted and second.reason == "duplicate"
    row = conn.execute(
        text("SELECT source, sample_rate FROM review_queue WHERE request_id = 'dup'")
    ).one()
    assert row.source == "flagged" and row.sample_rate == pytest.approx(1.0)
    assert _pending(conn) == 1


def test_a_rejected_enqueue_writes_no_row(conn):
    """A guard that rejects and inserts anyway is worse than no guard: the caller believes
    the queue is capped while it grows."""
    config = AdmissionConfig(max_pending=1, max_pending_per_source=99,
                             max_enqueues_per_source_per_window=99)
    _predict_row(conn, "k0")
    admit_review(conn, request_id="k0", source="flagged", submitter_fp="aaaabbbbccccdddd",
                 now=NOW, config=config)
    _predict_row(conn, "k1")
    admit_review(conn, request_id="k1", source="flagged", submitter_fp="aaaabbbbccccdddd",
                 now=NOW, config=config)
    assert _pending(conn) == 1
    assert conn.execute(
        text("SELECT count(*) FROM review_queue WHERE request_id = 'k1'")
    ).scalar_one() == 0


def _insert_user_feedback(conn, request_id: str, ts=NOW) -> None:
    conn.execute(
        text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
             "VALUES (:rid, :ts, 'user', '{}'::jsonb, true)"),
        {"rid": request_id, "ts": ts},
    )


def test_user_feedback_is_refused_for_an_unknown_request(conn):
    result = admit_user_feedback(conn, request_id="nope", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW, config=AdmissionConfig())
    assert not result.admitted and result.reason == "unknown_request"


def test_user_feedback_is_refused_outside_the_window(conn):
    stale = NOW - dt.timedelta(days=3)
    _predict_row(conn, "old", ts=stale)
    result = admit_user_feedback(conn, request_id="old", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW,
                                 config=AdmissionConfig(user_feedback_window_seconds=86400))
    assert not result.admitted and result.reason == "expired"


def test_user_feedback_inside_the_window_is_admitted(conn):
    """The mirror of the expiry test. Without it a guard that refused everything would pass
    every other case in this file."""
    recent = NOW - dt.timedelta(hours=1)
    _predict_row(conn, "fresh", ts=recent)
    result = admit_user_feedback(conn, request_id="fresh", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW,
                                 config=AdmissionConfig(user_feedback_window_seconds=86400))
    assert result.admitted and result.reason == "ok"


def test_a_second_user_verdict_on_one_request_is_refused(conn):
    """The partial unique index would reject it too. Refusing here means the caller gets a
    reason instead of a 500."""
    _predict_row(conn, "twice")
    _insert_user_feedback(conn, "twice")
    result = admit_user_feedback(conn, request_id="twice", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW, config=AdmissionConfig())
    assert not result.admitted and result.reason == "duplicate"


def test_user_feedback_quota_is_enforced_per_fingerprint(conn):
    config = AdmissionConfig(max_user_feedback_per_source_per_window=2)
    for i in range(3):
        _predict_row(conn, f"u{i}", fp="3333333333333333")
    for i in range(2):
        assert admit_user_feedback(conn, request_id=f"u{i}", submitter_fp="3333333333333333",
                                   now=NOW, config=config).admitted
        _insert_user_feedback(conn, f"u{i}")
    result = admit_user_feedback(conn, request_id="u2", submitter_fp="3333333333333333",
                                 now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"


def test_a_second_fingerprint_keeps_its_own_user_feedback_allowance(conn):
    config = AdmissionConfig(max_user_feedback_per_source_per_window=1)
    _predict_row(conn, "v0", fp="8888888888888888")
    _predict_row(conn, "v1", fp="9999999999999999")
    assert admit_user_feedback(conn, request_id="v0", submitter_fp="8888888888888888",
                               now=NOW, config=config).admitted
    _insert_user_feedback(conn, "v0")
    assert admit_user_feedback(conn, request_id="v1", submitter_fp="9999999999999999",
                               now=NOW, config=config).admitted


def test_user_feedback_older_than_the_window_stops_counting_against_the_quota(conn):
    config = AdmissionConfig(max_user_feedback_per_source_per_window=1,
                             user_feedback_window_seconds=86400)
    _predict_row(conn, "y0", fp="0000000000000000", ts=NOW - dt.timedelta(days=3))
    _insert_user_feedback(conn, "y0", ts=NOW - dt.timedelta(days=3))
    _predict_row(conn, "y1", fp="0000000000000000")
    assert admit_user_feedback(conn, request_id="y1", submitter_fp="0000000000000000",
                               now=NOW, config=config).admitted


def test_a_reviewer_feedback_row_does_not_spend_a_visitor_quota(conn):
    """The two sources are counted separately on purpose: reviewer throughput must not
    throttle the public path, and a visitor must not throttle the reviewers."""
    config = AdmissionConfig(max_user_feedback_per_source_per_window=1)
    _predict_row(conn, "x0", fp="1212121212121212")
    _predict_row(conn, "x1", fp="1212121212121212")
    conn.execute(
        text("INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
             "VALUES ('x0', :ts, 'reviewer', 'rock', '{\"toxic\": true}'::jsonb, true)"),
        {"ts": NOW},
    )
    assert admit_user_feedback(conn, request_id="x1", submitter_fp="1212121212121212",
                               now=NOW, config=config).admitted


def test_the_check_constraint_still_backstops_a_caller_that_bypasses_the_guard(conn):
    """Admission control is the polite path; the database is the one that cannot be talked
    around. Both must hold."""
    _predict_row(conn, "raw")
    with pytest.raises(IntegrityError, match="review_queue_sample_rate_ck"):
        conn.execute(
            text("INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
                 "VALUES ('raw', now(), 'pending', 'random-audit', NULL)")
        )
