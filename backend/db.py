"""SQLAlchemy models and write paths for the three RDS tables.

Rubric 2.2 requires every prediction request, its output, and a timestamp to be logged, and
rubric 3.2's dashboard is built on these tables, so the schema carries the columns the
monitoring queries need rather than the columns the request happens to have. Three of them
exist because of specific premortem findings: `review_queue.source` and
`review_queue.sample_rate` (H8), `review_queue.input_text_snapshot` (the retention purge
nulls `predictions.input_text` and review must not depend on it), and
`predictions.status` / `persist_status` (H28 and H30 - failed and degraded requests write
rows so the latency tail is present in the series).

This is the one definition of the three tables. Phase 3 consumes exactly these names -
`sample_rate`, `source='user-report'`, `feedback(reviewer_id, agreement, exact_match)`,
`init_db` - so a divergence here is a runtime failure on the deployed database rather than
a style disagreement (gap IFACE-DB-SCHEMA).
"""

import datetime as dt
from dataclasses import dataclass, replace

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from model.labels import LABELS

# `review_queue.source` and `feedback.source` are different vocabularies and both survive:
# a review row arrives from a design stratum or from a user's referral, whereas a feedback
# row is authored by a reviewer or by a user.
REVIEW_SOURCES = ("flagged", "random-audit", "user-report")
REVIEW_STATUSES = ("pending", "rescored", "reviewed", "expired")
FEEDBACK_SOURCES = ("reviewer", "user")


class Base(DeclarativeBase):
    pass


class Prediction(Base):
    __tablename__ = "predictions"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    input_text: Mapped[str | None] = mapped_column(Text)
    input_chars: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(200))
    prob_toxic: Mapped[float | None] = mapped_column(Float)
    prob_severe_toxic: Mapped[float | None] = mapped_column(Float)
    prob_obscene: Mapped[float | None] = mapped_column(Float)
    prob_threat: Mapped[float | None] = mapped_column(Float)
    prob_insult: Mapped[float | None] = mapped_column(Float)
    prob_identity_hate: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str | None] = mapped_column(String(10))
    max_prob: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))
    persist_status: Mapped[str] = mapped_column(String(10))
    error_kind: Mapped[str | None] = mapped_column(String(60))
    client_fp: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint("status in ('ok','error')", name="ck_predictions_status"),
        CheckConstraint(
            "persist_status in ('direct','spooled')", name="ck_predictions_persist_status"
        ),
        CheckConstraint("latency_ms >= 0", name="ck_predictions_latency_nonneg"),
        CheckConstraint(
            "decision is null or decision in ('allow','review','block')",
            name="ck_predictions_decision",
        ),
    )


class ReviewQueue(Base):
    __tablename__ = "review_queue"

    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("predictions.request_id"), primary_key=True
    )
    enqueued_ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source: Mapped[str] = mapped_column(String(16))
    sample_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_text_snapshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    distilbert_probs: Mapped[dict | None] = mapped_column(JSONB)
    reviewer_labels: Mapped[dict | None] = mapped_column(JSONB)
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    reviewed_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source in ('flagged','random-audit','user-report')", name="ck_review_source"
        ),
        CheckConstraint(
            "status in ('pending','rescored','reviewed','expired')", name="ck_review_status"
        ),
        # A design stratum must carry the pi it was drawn with; a user report has no known
        # inclusion probability and must carry NULL, so the estimator skips it until a human
        # reviews it under a known design (premortem H8, H9).
        CheckConstraint(
            "(source in ('flagged','random-audit')"
            " AND sample_rate IS NOT NULL AND sample_rate > 0 AND sample_rate <= 1)"
            " OR (source = 'user-report' AND sample_rate IS NULL)",
            name="review_queue_sample_rate_ck",
        ),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("predictions.request_id"), index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source: Mapped[str] = mapped_column(String(16))
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    agreement: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    exact_match: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        CheckConstraint("source in ('reviewer','user')", name="ck_feedback_source"),
        # A reviewer row exists to record per-label agreement. An empty object is the shape a
        # dashboard silently reads as "no disagreement", so the write is refused instead.
        CheckConstraint(
            "source <> 'reviewer' OR agreement <> '{}'::jsonb",
            name="feedback_reviewer_agreement_ck",
        ),
    )


@dataclass(frozen=True)
class PredictionRow:
    request_id: str
    input_text: str | None
    input_chars: int
    model_version: str
    probs: dict[str, float] | None
    decision: str | None
    max_prob: float | None
    latency_ms: int
    status: str
    persist_status: str
    error_kind: str | None = None
    client_fp: str | None = None
    ts: dt.datetime | None = None


@dataclass(frozen=True)
class ReviewIntent:
    request_id: str
    source: str
    sample_rate: float | None
    input_text_snapshot: str | None
    enqueued_ts: dt.datetime | None = None


@dataclass(frozen=True)
class PendingWrite:
    prediction: PredictionRow
    review: ReviewIntent | None = None


def make_engine(settings) -> Engine:
    """Bounded pool with a short checkout timeout.

    Under database pressure the endpoint must fail over to the spool in a couple of seconds
    rather than pile up connections until the instance runs out of workers. That is the
    difference between degraded and down (premortem H30).
    """
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(1, int(settings.db_timeout_seconds))},
        future=True,
    )


def init_db(engine: Engine) -> None:
    """Idempotent create of the three tables. Named for the seam Phase 3 imports."""
    Base.metadata.create_all(engine)


init_schema = init_db  # historical name used inside Phase 2; both are the same function


def insert_prediction(session, row: PredictionRow) -> None:
    values = {
        "request_id": row.request_id,
        "input_text": row.input_text,
        "input_chars": row.input_chars,
        "model_version": row.model_version,
        "decision": row.decision,
        "max_prob": row.max_prob,
        "latency_ms": row.latency_ms,
        "status": row.status,
        "persist_status": row.persist_status,
        "error_kind": row.error_kind,
        "client_fp": row.client_fp,
    }
    if row.ts is not None:
        values["ts"] = row.ts
    probs = row.probs or {}
    for label in LABELS:
        values[f"prob_{label}"] = probs.get(label)
    session.execute(
        pg_insert(Prediction).values(**values).on_conflict_do_nothing(index_elements=["request_id"])
    )


def enqueue_review(session, intent: ReviewIntent) -> None:
    values = {
        "request_id": intent.request_id,
        "source": intent.source,
        "sample_rate": intent.sample_rate,
        "input_text_snapshot": intent.input_text_snapshot,
        "status": "pending",
    }
    if intent.enqueued_ts is not None:
        values["enqueued_ts"] = intent.enqueued_ts
    session.execute(
        pg_insert(ReviewQueue).values(**values).on_conflict_do_nothing(index_elements=["request_id"])
    )


def fetch_pending_reviews(session, limit: int) -> list[ReviewQueue]:
    return list(
        session.scalars(
            select(ReviewQueue)
            .where(ReviewQueue.status == "pending")
            .order_by(ReviewQueue.enqueued_ts)
            .limit(limit)
        )
    )


def write_pending(session, pending: PendingWrite, stamp) -> int:
    """Insert the prediction row and any review row, then stamp `latency_ms`.

    `stamp` is either an int (replay of an already-measured row) or a zero-argument callable
    evaluated AFTER the insert statements (the live path). Premortem H28: stamping before
    persistence omits the slowest component from the graded latency chart. What remains
    outside the measurement is the COMMIT round trip, which the caller measures separately
    and emits as `commit_ms` on the request log line.
    """
    insert_prediction(session, pending.prediction)
    if pending.review is not None:
        enqueue_review(session, pending.review)
    latency_ms = int(stamp()) if callable(stamp) else int(stamp)
    session.execute(
        update(Prediction)
        .where(Prediction.request_id == pending.prediction.request_id)
        .values(latency_ms=latency_ms)
    )
    return latency_ms


def with_persist_status(pending: PendingWrite, persist_status: str) -> PendingWrite:
    return PendingWrite(
        prediction=replace(pending.prediction, persist_status=persist_status),
        review=pending.review,
    )
