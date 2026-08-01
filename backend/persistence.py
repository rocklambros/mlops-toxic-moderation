"""The /predict persistence path. One module decides direct, spooled, or fail closed.

Ordering matters and is deliberate (premortem H30):

  direct   insert with a bounded checkout and one retry            -> HTTP 200
  spooled  fsync'd local row, replayed when Postgres recovers      -> HTTP 200
  full     the spool reached its bound                             -> HTTP 503

Only the third path returns 503, and reaching it costs an attacker SPOOL_MAX_ROWS successful
requests through the rate limiter.
"""

import time
from dataclasses import dataclass, replace

from sqlalchemy.exc import SQLAlchemyError

from backend.db import PendingWrite, with_persist_status, write_pending
from backend.spool import Spool


@dataclass(frozen=True)
class PersistResult:
    persist_status: str
    latency_ms: int
    error: str | None = None
    commit_ms: float = 0.0


def persist_prediction(
    session_factory,
    spool: Spool,
    pending: PendingWrite,
    t0: float,
    retries: int = 1,
) -> PersistResult:
    """Persist one prediction. Raises SpoolFull only when the degraded path is exhausted."""
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            with session_factory() as session:
                latency_ms = write_pending(
                    session,
                    pending,
                    stamp=lambda: int(round((time.perf_counter() - t0) * 1000)),
                )
                commit_started = time.perf_counter()
                session.commit()
                commit_ms = (time.perf_counter() - commit_started) * 1000
            return PersistResult(
                persist_status="direct", latency_ms=latency_ms, commit_ms=commit_ms
            )
        except SQLAlchemyError as exc:
            last = exc

    latency_ms = int(round((time.perf_counter() - t0) * 1000))
    degraded = with_persist_status(pending, "spooled")
    spool.append(
        PendingWrite(
            prediction=replace(degraded.prediction, latency_ms=latency_ms),
            review=degraded.review,
        )
    )
    return PersistResult(
        persist_status="spooled",
        latency_ms=latency_ms,
        error=type(last).__name__ if last else None,
    )


def drain_spool(session_factory, spool: Spool) -> int:
    """Replay spooled rows into Postgres. At-least-once by construction.

    Rows are committed BEFORE the spool is truncated, so a crash between the two duplicates
    rather than loses - and `insert_prediction` is idempotent on `request_id`, so a duplicate
    is a no-op. The stored `latency_ms` is the value measured at request time, never the
    drain time, or the graded latency series would be corrupted by an unrelated outage.
    """
    entries = spool.read_all()
    if not entries:
        return 0
    with session_factory() as session:
        for entry in entries:
            write_pending(session, entry, stamp=entry.prediction.latency_ms)
        session.commit()
    spool.truncate()
    return len(entries)
