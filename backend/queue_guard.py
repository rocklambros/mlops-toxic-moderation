"""Admission control for the review queue and the user-feedback path.

Two properties the delivery spec (section 6.4) makes normative and that nothing else
enforces: the queue is depth-capped, and it is rate-limited per source. A flood of toxic
submissions would otherwise bury real items and poison the graded live-accuracy metric.

Three caps, because one is not enough. `max_pending` bounds the queue globally so no single
fingerprint's traffic can wedge it. `max_pending_per_source` stops one fingerprint filling
that global allowance. `max_enqueues_per_source_per_window` is the only one of the three
that is a *rate* limit -- without it a flooder whose items are reviewed promptly faces no
limit at all, because pending counts fall as fast as they rise.

This is also the single place where a row's sampling stratum and inclusion probability are
recorded. `sample_rate` is written here, at enqueue time, because it cannot be recovered
afterwards: RANDOM_AUDIT_RATE is configuration and configuration changes.

Concurrency, stated because it is real: the counts are read and then the row is inserted,
so two simultaneous requests can both observe a queue one below the cap and both be
admitted. The caps are a flood defence, not an invariant, and overshooting one by the
number of concurrent workers is acceptable; the invariant that must not bend --
`review_queue_sample_rate_ck` -- is enforced by the database rather than here.
"""

import datetime as dt
import os
from dataclasses import dataclass

from sqlalchemy import text

RANDOM_AUDIT_RATE: float = float(os.environ.get("RANDOM_AUDIT_RATE", "0.05"))


@dataclass(frozen=True)
class AdmissionConfig:
    max_pending: int = 500
    max_pending_per_source: int = 20
    window_seconds: int = 3600
    max_enqueues_per_source_per_window: int = 30
    max_user_feedback_per_source_per_window: int = 20
    user_feedback_window_seconds: int = 86400
    random_audit_rate: float = RANDOM_AUDIT_RATE


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str


def _sample_rate(source: str, config: AdmissionConfig) -> float | None:
    if source == "flagged":
        return 1.0  # every flagged item is reviewed: inclusion probability 1
    if source == "random-audit":
        rate = config.random_audit_rate
        if not 0.0 < rate <= 1.0:
            # pi = 0 divides by zero in the estimator, and the CHECK constraint rejects it.
            # A caller enqueueing an audit row with the sampler switched off is a bug worth
            # naming, not an IntegrityError three frames away.
            raise ValueError(
                f"random_audit_rate must be in (0, 1] to enqueue a random-audit row; got {rate!r}"
            )
        return rate
    if source == "user-report":
        return None  # self-selected: inclusion probability unknown, stays NULL
    raise ValueError(f"unknown review_queue source {source!r}")


def admit_review(
    conn,
    *,
    request_id: str,
    source: str,
    submitter_fp: str | None,
    now: dt.datetime,
    config: AdmissionConfig,
) -> Admission:
    rate = _sample_rate(source, config)

    existing = conn.execute(
        text("SELECT 1 FROM review_queue WHERE request_id = :rid"), {"rid": request_id}
    ).first()
    if existing is not None:
        return Admission(False, "duplicate")

    snapshot = conn.execute(
        text("SELECT input_text FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar()
    if snapshot is None:
        return Admission(False, "unknown_request")

    pending = conn.execute(
        text("SELECT count(*) FROM review_queue WHERE status = 'pending'")
    ).scalar_one()
    if pending >= config.max_pending:
        return Admission(False, "queue_full")

    if submitter_fp is not None:
        per_source_pending = conn.execute(
            text(
                "SELECT count(*) FROM review_queue q JOIN predictions p "
                "ON p.request_id = q.request_id "
                "WHERE q.status = 'pending' AND p.submitter_fp = :fp"
            ),
            {"fp": submitter_fp},
        ).scalar_one()
        if per_source_pending >= config.max_pending_per_source:
            return Admission(False, "source_quota")

        window_start = now - dt.timedelta(seconds=config.window_seconds)
        recent = conn.execute(
            text(
                "SELECT count(*) FROM review_queue q JOIN predictions p "
                "ON p.request_id = q.request_id "
                "WHERE q.enqueued_ts >= :start AND p.submitter_fp = :fp"
            ),
            {"start": window_start, "fp": submitter_fp},
        ).scalar_one()
        if recent >= config.max_enqueues_per_source_per_window:
            return Admission(False, "source_quota")

    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', :src, :rate, :snap)"
        ),
        {"rid": request_id, "ts": now, "src": source, "rate": rate, "snap": snapshot},
    )
    conn.commit()
    return Admission(True, "ok")


def admit_user_feedback(
    conn,
    *,
    request_id: str,
    submitter_fp: str | None,
    now: dt.datetime,
    config: AdmissionConfig,
) -> Admission:
    ts = conn.execute(
        text("SELECT ts FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar()
    if ts is None:
        return Admission(False, "unknown_request")
    if (now - ts).total_seconds() > config.user_feedback_window_seconds:
        return Admission(False, "expired")

    already = conn.execute(
        text("SELECT 1 FROM feedback WHERE request_id = :rid AND source = 'user'"),
        {"rid": request_id},
    ).first()
    if already is not None:
        return Admission(False, "duplicate")

    if submitter_fp is not None:
        window_start = now - dt.timedelta(seconds=config.user_feedback_window_seconds)
        recent = conn.execute(
            text(
                "SELECT count(*) FROM feedback f JOIN predictions p "
                "ON p.request_id = f.request_id "
                "WHERE f.source = 'user' AND f.ts >= :start AND p.submitter_fp = :fp"
            ),
            {"start": window_start, "fp": submitter_fp},
        ).scalar_one()
        if recent >= config.max_user_feedback_per_source_per_window:
            return Admission(False, "source_quota")

    return Admission(True, "ok")
