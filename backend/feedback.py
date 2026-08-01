"""Feedback records, and the line between the two kinds of them.

`source='reviewer'` rows carry a per-label agreement vector from a human who saw the
comment. `source='user'` rows carry one bit from an anonymous visitor. They are stored in
the same table and used for different things: the reviewer rows feed the design-weighted
live-accuracy estimate, the user rows feed their own panel and a referral into the review
queue. Pooling them would make a graded metric writable by anyone with a browser, and would
also be unsound -- a self-selected verdict has no known inclusion probability, so
Horvitz-Thompson has nothing to weight it by.

The user verdict is a closed two-value vocabulary, which is the size cap: there is no
free-text field on the internet-facing feedback path to cap.

`derive_feedback` refuses to guess. A missing reviewer label, a missing model flag, a label
neither side scores, a non-binary value, or an unattributable reviewer are all errors rather
than defaults, because each of the corresponding defaults manufactures agreement -- and
agreement is the numerator of the graded metric.
"""

import datetime as dt
import json
from dataclasses import dataclass

from sqlalchemy import text

from model.labels import LABELS

USER_VERDICTS: frozenset[str] = frozenset({"agree", "disagree"})


@dataclass(frozen=True)
class FeedbackRecord:
    request_id: str
    source: str
    reviewer_id: str | None
    agreement: dict[str, bool]
    exact_match: bool


def derive_feedback(
    request_id: str,
    reviewer_labels: dict[str, int],
    model_flags: dict[str, bool],
    reviewer_id: str,
) -> FeedbackRecord:
    if not reviewer_id or not str(reviewer_id).strip():
        raise ValueError("reviewer_id must be a non-empty server-derived identity")
    unknown = sorted(set(reviewer_labels) - set(LABELS))
    if unknown:
        raise ValueError(f"reviewer_labels carries labels this model does not score: {unknown}")
    agreement: dict[str, bool] = {}
    for label in LABELS:
        if label not in reviewer_labels:
            raise ValueError(f"reviewer_labels is missing {label!r}")
        if label not in model_flags:
            raise ValueError(f"model_flags is missing {label!r}")
        value = reviewer_labels[label]
        if value not in (0, 1, True, False):
            raise ValueError(f"reviewer_labels[{label!r}]={value!r} is outside {{0, 1}}")
        agreement[label] = bool(value) == bool(model_flags[label])
    return FeedbackRecord(
        request_id=request_id,
        source="reviewer",
        reviewer_id=reviewer_id,
        agreement=agreement,
        exact_match=all(agreement.values()),
    )


def user_feedback(request_id: str, verdict: str) -> FeedbackRecord:
    if verdict not in USER_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(USER_VERDICTS)}")
    return FeedbackRecord(
        request_id=request_id,
        source="user",
        reviewer_id=None,
        agreement={},
        exact_match=verdict == "agree",
    )


def insert_feedback(conn, record: FeedbackRecord, ts: dt.datetime | None = None) -> None:
    """Write one record. `ts` is explicit only so the seeder can back-date its rows.

    The NULL is cast rather than left untyped: psycopg sends a bare `None` with no type, and
    `COALESCE($1, now())` on an untyped parameter is a resolution Postgres is entitled to
    refuse.
    """
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES (:rid, COALESCE(CAST(:ts AS timestamptz), now()), :src, :who, "
            "CAST(:agree AS jsonb), :exact)"
        ),
        {
            "rid": record.request_id,
            "ts": ts,
            "src": record.source,
            "who": record.reviewer_id,
            "agree": json.dumps(record.agreement),
            "exact": record.exact_match,
        },
    )
