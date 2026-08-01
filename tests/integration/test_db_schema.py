import datetime as dt
import pathlib
from dataclasses import replace

import pytest
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError

from backend.db import (
    Feedback,
    PendingWrite,
    Prediction,
    PredictionRow,
    ReviewIntent,
    ReviewQueue,
    enqueue_review,
    fetch_pending_reviews,
    insert_prediction,
    write_pending,
)
from model.labels import LABELS

pytestmark = pytest.mark.integration


def make_row(request_id="r1", **overrides) -> PredictionRow:
    row = PredictionRow(
        request_id=request_id,
        input_text="you are an idiot",
        input_chars=16,
        model_version="toxic-clf:v3@sha256:" + "a" * 64,
        probs={label: 0.1 for label in LABELS},
        decision="review",
        max_prob=0.1,
        latency_ms=12,
        status="ok",
        persist_status="direct",
    )
    return replace(row, **overrides) if overrides else row


def test_prediction_round_trip_preserves_every_probability(session):
    probs = {label: round(0.1 * index, 3) for index, label in enumerate(LABELS, start=1)}
    insert_prediction(session, make_row(probs=probs))
    session.commit()
    stored = session.get(Prediction, "r1")
    assert stored.prob_toxic == pytest.approx(0.1)
    assert stored.prob_identity_hate == pytest.approx(0.6)
    assert stored.ts is not None


def test_insert_is_idempotent_on_request_id(session):
    """The spool drain is at-least-once by design: rows are committed before the spool is
    truncated, so a crash mid-drain must duplicate nothing."""
    insert_prediction(session, make_row())
    insert_prediction(session, make_row())
    session.commit()
    assert len(session.scalars(select(Prediction)).all()) == 1


def test_explicit_timestamp_is_honoured(session):
    backdated = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.UTC)
    insert_prediction(session, make_row(ts=backdated))
    session.commit()
    assert session.get(Prediction, "r1").ts == backdated


def test_review_row_records_its_sample_rate(session):
    """H8. Stratified collection without stratified estimation is still biased. The weight
    has to be stored at enqueue time; it cannot be reconstructed later, because the audit
    rate is a deploy-time setting that may change between rows."""
    insert_prediction(session, make_row())
    enqueue_review(
        session,
        ReviewIntent(
            request_id="r1",
            source="random-audit",
            sample_rate=0.05,
            input_text_snapshot="you are an idiot",
        ),
    )
    session.commit()
    stored = session.get(ReviewQueue, "r1")
    assert stored.source == "random-audit"
    assert stored.sample_rate == pytest.approx(0.05)
    assert stored.input_text_snapshot == "you are an idiot"
    assert stored.status == "pending"


def test_review_source_is_constrained_to_the_three_documented_values(session):
    insert_prediction(session, make_row())
    session.commit()
    with pytest.raises(IntegrityError, match="ck_review_source"):
        enqueue_review(
            session,
            ReviewIntent(
                request_id="r1",
                source="whatever",
                sample_rate=1.0,
                input_text_snapshot="x",
            ),
        )
        session.commit()


def test_feedback_source_is_constrained_to_reviewer_or_user(session):
    insert_prediction(session, make_row())
    session.add(
        Feedback(
            request_id="r1",
            source="reviewer",
            reviewer_id="rock",
            agreement={"toxic": True},
            exact_match=True,
        )
    )
    session.commit()
    session.add(Feedback(request_id="r1", source="bot", reviewer_id="x"))
    with pytest.raises(IntegrityError, match="ck_feedback_source"):
        session.commit()


def test_write_pending_stamps_latency_after_the_insert(session):
    stamped = write_pending(
        session,
        PendingWrite(
            prediction=make_row(latency_ms=0),
            review=ReviewIntent(
                request_id="r1",
                source="flagged",
                sample_rate=1.0,
                input_text_snapshot="you are an idiot",
            ),
        ),
        stamp=lambda: 77,
    )
    session.commit()
    assert stamped == 77
    assert session.get(Prediction, "r1").latency_ms == 77
    assert session.get(ReviewQueue, "r1") is not None


def test_fetch_pending_reviews_returns_oldest_first(session):
    older = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    newer = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)
    for request_id, enqueued in (("r1", newer), ("r2", older)):
        insert_prediction(session, make_row(request_id=request_id))
        enqueue_review(
            session,
            ReviewIntent(
                request_id=request_id,
                source="flagged",
                sample_rate=1.0,
                input_text_snapshot="x",
                enqueued_ts=enqueued,
            ),
        )
    session.commit()
    assert [row.request_id for row in fetch_pending_reviews(session, limit=10)] == ["r2", "r1"]


# --- Task 10a: one schema, not two (gap IFACE-DB-SCHEMA, H8, H9) ---------------------


def test_the_review_queue_sampling_column_has_exactly_one_name(session):
    """Phase 3's admit_review and seed_demo write sample_rate. A surviving NOT NULL column
    under the retired name makes every enqueue a NotNullViolation on the real database."""
    cols = {c.name for c in ReviewQueue.__table__.columns}
    assert "sample_rate" in cols
    assert "inclusion" + "_probability" not in cols


def test_a_user_report_row_may_carry_a_null_sample_rate(session):
    """H9's referral path. Horvitz-Thompson must ignore rows of unknown inclusion."""
    session.add(
        Prediction(
            request_id="r1",
            input_text="x",
            input_chars=1,
            model_version="m",
            decision="allow",
            max_prob=0.1,
            latency_ms=5,
            status="ok",
            persist_status="direct",
            **{f"prob_{label}": 0.1 for label in LABELS},
        )
    )
    session.add(
        ReviewQueue(
            request_id="r1", source="user-report", sample_rate=None, input_text_snapshot="x"
        )
    )
    session.commit()
    assert session.get(ReviewQueue, "r1").sample_rate is None


def test_a_design_stratum_row_cannot_omit_its_sample_rate(session):
    insert_prediction(session, make_row(request_id="r2"))
    session.commit()
    with pytest.raises(IntegrityError, match="review_queue_sample_rate_ck"):
        session.add(ReviewQueue(request_id="r2", source="flagged", sample_rate=None))
        session.commit()
    session.rollback()


def test_the_review_source_vocabulary_admits_user_report(session):
    checks = [
        c
        for c in ReviewQueue.__table__.constraints
        if isinstance(c, CheckConstraint) and "source" in str(c.sqltext)
    ]
    assert any("user-report" in str(c.sqltext) for c in checks), (
        "the H9 user-referral path writes source='user-report' and this constraint rejects it"
    )


def test_the_feedback_table_matches_the_phase3_contract(session):
    cols = {c.name for c in Feedback.__table__.columns}
    assert {"request_id", "ts", "source", "reviewer_id", "agreement", "exact_match"} <= cols
    assert not ({"actor_id", "agree", "true_labels", "model_labels"} & cols), (
        "two column sets for one table is how the dashboard reads a column nothing writes"
    )


def test_the_schema_entry_point_has_the_name_phase_3_imports():
    from backend import db

    assert hasattr(db, "init_db"), "Phase 3's conftest does `from backend.db import init_db`"


def test_no_module_in_the_repo_still_uses_the_retired_sampling_column_name():
    """A rename that misses one call site is a NotNullViolation on day 13, not a lint nit.

    The needle is assembled at runtime so that this module, which git greps like any other,
    is not itself the only offender the scan can ever find.
    """
    needle = "inclusion" + "_probability"
    repo = pathlib.Path(__file__).resolve().parents[2]
    offenders = [
        str(path.relative_to(repo))
        for path in repo.rglob("*.py")
        if ".venv" not in str(path)
        and "__pycache__" not in path.parts
        and needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
