import datetime as dt
from dataclasses import replace

import pytest
from sqlalchemy import select
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


def test_review_row_records_its_inclusion_probability(session):
    """H8. Stratified collection without stratified estimation is still biased. The weight
    has to be stored at enqueue time; it cannot be reconstructed later, because the audit
    rate is a deploy-time setting that may change between rows."""
    insert_prediction(session, make_row())
    enqueue_review(
        session,
        ReviewIntent(
            request_id="r1",
            source="random-audit",
            inclusion_probability=0.05,
            input_text_snapshot="you are an idiot",
        ),
    )
    session.commit()
    stored = session.get(ReviewQueue, "r1")
    assert stored.source == "random-audit"
    assert stored.inclusion_probability == pytest.approx(0.05)
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
                inclusion_probability=1.0,
                input_text_snapshot="x",
            ),
        )
        session.commit()


def test_feedback_source_is_constrained_to_reviewer_or_user(session):
    insert_prediction(session, make_row())
    session.add(Feedback(request_id="r1", source="reviewer", actor_id="rock", agree=True))
    session.commit()
    session.add(Feedback(request_id="r1", source="bot", actor_id="x"))
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
                inclusion_probability=1.0,
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
                inclusion_probability=1.0,
                input_text_snapshot="x",
                enqueued_ts=enqueued,
            ),
        )
    session.commit()
    assert [row.request_id for row in fetch_pending_reviews(session, limit=10)] == ["r2", "r1"]
