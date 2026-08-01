"""The Phase 3 migration, asserted against a real Postgres.

Phase 2 already ships `review_queue.source`, `review_queue.sample_rate` and the `feedback`
table, so most of what this migration does on a Phase-2 database is prove itself a no-op.
What it genuinely adds is `predictions.is_seed`, `predictions.submitter_fp`, the composite
index the per-source quota queries, and `feedback_one_user_row` -- and, on a database that
somehow lacks them, the CHECK constraints the Horvitz-Thompson estimator depends on.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.schema_phase3 import apply_phase3_schema
from model.labels import LABELS

pytestmark = pytest.mark.integration

EXPECTED_PREDICTION_COLUMNS = {
    "request_id", "ts", "input_text", "model_version", "decision", "max_prob", "latency_ms",
    *(f"prob_{label}" for label in LABELS),
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    ).scalars()
    return set(rows)


def _constraints(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :t"
        ),
        {"t": table},
    ).scalars()
    return set(rows)


def test_phase2_predictions_contract_is_intact(conn):
    assert EXPECTED_PREDICTION_COLUMNS <= _columns(conn, "predictions")


def test_phase3_columns_exist(conn):
    assert {"is_seed", "submitter_fp"} <= _columns(conn, "predictions")
    assert {"source", "sample_rate", "input_text_snapshot"} <= _columns(conn, "review_queue")
    assert {"source", "reviewer_id", "agreement", "exact_match", "ts"} <= _columns(conn, "feedback")


def test_migration_is_idempotent(engine, conn):
    apply_phase3_schema(engine)
    apply_phase3_schema(engine)
    assert {"is_seed", "submitter_fp"} <= _columns(conn, "predictions")


def test_the_migration_adds_no_second_copy_of_a_phase2_constraint(conn):
    """A DO-block guarded by `duplicate_object` only fires when the name already matches.
    Adding the same predicate under a fresh name would leave two constraints enforcing one
    rule, which is how a later `DROP CONSTRAINT` silently changes nothing."""
    review = _constraints(conn, "review_queue")
    feedback = _constraints(conn, "feedback")
    assert "ck_review_source" in review
    assert "review_queue_sample_rate_ck" in review
    assert "ck_feedback_source" in feedback
    assert "feedback_reviewer_agreement_ck" in feedback
    assert "review_queue_source_ck" not in review, "duplicate of ck_review_source"
    assert "feedback_source_ck" not in feedback, "duplicate of ck_feedback_source"


def test_the_per_source_quota_index_exists(conn):
    """`admit_review` counts a fingerprint's rows inside a time window on every enqueue.
    Without this index that count is a sequential scan of the whole prediction table."""
    indexes = conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'predictions'")
    ).scalars()
    assert "predictions_fp_ts_idx" in set(indexes)


def _insert_prediction(conn, request_id: str) -> None:
    """Insert a minimal but VALID prediction row.

    Every NOT NULL column Phase 2 declares is supplied. Omitting `input_chars`, `status` or
    `persist_status` would abort the transaction here, and the review_queue INSERT that
    follows would then raise InFailedSqlTransaction -- a green-looking `pytest.raises` that
    never reached the CHECK constraint under test.
    """
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.1" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, input_chars, model_version, "
            f"{cols}, decision, max_prob, latency_ms, status, persist_status) "
            f"VALUES (:rid, now(), 'x', 1, 'm', {vals}, 'allow', 0.1, 5, 'ok', 'direct')"
        ),
        {"rid": request_id},
    )
    assert conn.execute(
        text("SELECT count(*) FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar_one() == 1, "the fixture row did not land, so the constraint below is untested"


def test_design_stratum_without_sample_rate_is_rejected(conn):
    """H8: an unweighted pool is only possible if a row can exist without its inclusion
    probability. The database refuses."""
    _insert_prediction(conn, "r1")
    with pytest.raises(IntegrityError, match="review_queue_sample_rate_ck"):
        conn.execute(
            text(
                "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
                "VALUES ('r1', now(), 'pending', 'flagged', NULL)"
            )
        )


def test_user_report_stratum_must_have_null_sample_rate(conn):
    _insert_prediction(conn, "r2")
    with pytest.raises(IntegrityError, match="review_queue_sample_rate_ck"):
        conn.execute(
            text(
                "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
                "VALUES ('r2', now(), 'pending', 'user-report', 1.0)"
            )
        )


def test_a_design_stratum_row_with_its_rate_is_accepted(conn):
    """The mirror of the two rejections above. Without it, a CHECK that rejected every row
    would pass this file."""
    _insert_prediction(conn, "r2b")
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
            "VALUES ('r2b', now(), 'pending', 'random-audit', 0.05)"
        )
    )
    assert conn.execute(
        text("SELECT sample_rate FROM review_queue WHERE request_id = 'r2b'")
    ).scalar_one() == pytest.approx(0.05)


def test_one_user_feedback_row_per_request(conn):
    _insert_prediction(conn, "r3")
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
            "VALUES ('r3', now(), 'user', '{}'::jsonb, true)"
        )
    )
    with pytest.raises(IntegrityError, match="feedback_one_user_row"):
        conn.execute(
            text(
                "INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                "VALUES ('r3', now(), 'user', '{}'::jsonb, false)"
            )
        )


def test_two_reviewer_feedback_rows_for_one_request_are_still_allowed(conn):
    """The unique index is partial. It must cap the anonymous path without capping a second
    reviewer's opinion, which is a legitimate row."""
    _insert_prediction(conn, "r4")
    for exact in ("true", "false"):
        conn.execute(
            text(
                "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, "
                f"exact_match) VALUES ('r4', now(), 'reviewer', 'rock', "
                f"'{{\"toxic\": true}}'::jsonb, {exact})"
            )
        )
    assert conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'r4'")
    ).scalar_one() == 2
