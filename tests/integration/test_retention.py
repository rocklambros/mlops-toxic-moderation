import datetime as dt

import pytest

from backend.db import Prediction, ReviewIntent, ReviewQueue, enqueue_review, insert_prediction
from backend.retention import purge
from tests.integration.test_db_schema import make_row

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)
POLICY = {
    "input_text_retention_days": 30,
    "pending_review_ttl_days": 7,
    "snapshot_retention_days": 30,
}


def seed(session, request_id, *, predicted_days_ago, enqueued_days_ago=None, status="pending"):
    insert_prediction(
        session,
        make_row(request_id=request_id, ts=NOW - dt.timedelta(days=predicted_days_ago)),
    )
    if enqueued_days_ago is not None:
        enqueue_review(
            session,
            ReviewIntent(
                request_id=request_id,
                source="flagged",
                sample_rate=1.0,
                input_text_snapshot="you are an idiot",
                enqueued_ts=NOW - dt.timedelta(days=enqueued_days_ago),
            ),
        )
        session.execute(
            ReviewQueue.__table__.update()
            .where(ReviewQueue.request_id == request_id)
            .values(status=status)
        )
    session.commit()


def test_recent_predictions_are_untouched(session):
    seed(session, "r1", predicted_days_ago=10)
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_old_prediction_without_a_pending_review_is_purged(session):
    seed(session, "r1", predicted_days_ago=31)
    report = purge(session, NOW, **POLICY)
    assert report.purged_input_text == 1
    assert session.get(Prediction, "r1").input_text is None
    assert session.get(Prediction, "r1").decision is not None  # the row survives


def test_a_live_pending_review_exempts_its_prediction(session):
    """Delivery spec section 6.4: the purge must not destroy the reviewer's evidence
    mid-workflow."""
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=2)
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_pending_exemption_expires_at_the_hard_ttl(session):
    """Remediation 3.13, and the reason this task exists. An unbounded pending exemption is
    attacker-controlled retention: anything that lands in the queue and is never reviewed is
    kept forever, which defeats the 30-day policy for exactly the content an attacker chose
    to submit. The exemption is capped, so the queue cannot be used as a storage primitive."""
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=8)
    report = purge(session, NOW, **POLICY)
    assert report.expired_reviews == 1
    assert session.get(ReviewQueue, "r1").status == "expired"
    assert session.get(Prediction, "r1").input_text is None


def test_a_rescored_review_still_exempts_within_the_ttl(session):
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=3, status="rescored")
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_snapshots_are_nulled_at_their_own_ttl_regardless_of_status(session):
    seed(session, "r1", predicted_days_ago=45, enqueued_days_ago=31, status="reviewed")
    report = purge(session, NOW, **POLICY)
    assert report.purged_snapshots == 1
    assert session.get(ReviewQueue, "r1").input_text_snapshot is None
    assert session.get(ReviewQueue, "r1").status == "reviewed"  # the row survives


def test_purge_is_idempotent(session):
    seed(session, "r1", predicted_days_ago=31)
    purge(session, NOW, **POLICY)
    second = purge(session, NOW, **POLICY)
    assert second.purged_input_text == 0
    assert second.expired_reviews == 0
