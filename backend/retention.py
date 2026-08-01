"""Retention purge with a BOUNDED pending-review exemption.

Three rules, applied in this order, and the order is the design:

1. Expire pending reviews older than PENDING_REVIEW_TTL_DAYS. Delivery spec section 6.4
   exempts pending rows from the purge so it cannot destroy a reviewer's evidence
   mid-workflow. Unbounded, that exemption is attacker-controlled retention: anything that
   lands in the queue and is never reviewed is kept forever, which defeats the 30-day policy
   for exactly the content an attacker chose to submit (premortem remediation 3.13).
   Expiring first is what makes rule 2's exemption finite.
2. Null `predictions.input_text` older than INPUT_TEXT_RETENTION_DAYS, except where a review
   is still pending or rescored.
3. Null `review_queue.input_text_snapshot` older than SNAPSHOT_RETENTION_DAYS regardless of
   status, so no path retains user text past the stated policy.

Every other column survives: probabilities, decision, flags, timestamps, and latency are what
the monitoring dashboard reads, and they are not personal data.
"""

import argparse
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select, update

from backend.db import Prediction, ReviewQueue

# The two statuses that mean "a human still owes this row a decision". Only these exempt a
# prediction from the input-text purge, and only until the hard TTL expires them.
OPEN_REVIEW_STATUSES = ("pending", "rescored")


@dataclass(frozen=True)
class PurgeReport:
    expired_reviews: int
    purged_input_text: int
    purged_snapshots: int


def purge(
    session,
    now: dt.datetime,
    *,
    input_text_retention_days: int,
    pending_review_ttl_days: int,
    snapshot_retention_days: int,
) -> PurgeReport:
    expired = session.execute(
        update(ReviewQueue)
        .where(
            ReviewQueue.status.in_(OPEN_REVIEW_STATUSES),
            ReviewQueue.enqueued_ts < now - dt.timedelta(days=pending_review_ttl_days),
        )
        .values(status="expired")
    ).rowcount

    still_open = select(ReviewQueue.request_id).where(
        ReviewQueue.status.in_(OPEN_REVIEW_STATUSES)
    )
    purged_text = session.execute(
        update(Prediction)
        .where(
            Prediction.ts < now - dt.timedelta(days=input_text_retention_days),
            Prediction.input_text.is_not(None),
            Prediction.request_id.not_in(still_open),
        )
        .values(input_text=None)
    ).rowcount

    purged_snapshots = session.execute(
        update(ReviewQueue)
        .where(
            ReviewQueue.enqueued_ts < now - dt.timedelta(days=snapshot_retention_days),
            ReviewQueue.input_text_snapshot.is_not(None),
        )
        .values(input_text_snapshot=None)
    ).rowcount

    session.commit()
    return PurgeReport(expired, purged_text, purged_snapshots)


def main() -> None:
    from sqlalchemy.orm import sessionmaker

    from backend.config import load_settings
    from backend.db import make_engine

    parser = argparse.ArgumentParser(description="Run the input-text retention purge")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    engine = make_engine(settings)
    try:
        if args.dry_run:
            print("dry run: no rows modified")
            return
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            report = purge(
                session,
                dt.datetime.now(dt.UTC),
                input_text_retention_days=settings.input_text_retention_days,
                pending_review_ttl_days=settings.pending_review_ttl_days,
                snapshot_retention_days=settings.snapshot_retention_days,
            )
        print(
            f"expired_reviews={report.expired_reviews} "
            f"purged_input_text={report.purged_input_text} "
            f"purged_snapshots={report.purged_snapshots}"
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
