"""Read-only aggregations for the monitoring dashboard.

Every statement here is a SELECT. The dashboard connects with a read-only database role
(premortem H16), and no column carrying a raw comment -- neither the prediction's own copy
nor the review queue's snapshot of it -- is ever named, because the dashboard screenshot is
a public deliverable (delivery spec section 6.4). `tests/unit/test_dashboard_guards.py`
scans this package for both properties rather than trusting this paragraph.

Flags are recomputed as `prob_<label> >= thresholds[<label>]` rather than read from a
stored column, so the production series and the Phase 1 baseline always share one decision
rule. That is what makes the PSI comparison in `drift_report` mean anything.

Day buckets are truncated in UTC explicitly. `date_trunc('day', ts)` would bucket by the
database session's TimeZone, so the same rows would produce a different chart -- and a
different bucket count against the seven-bucket floor -- depending on a server setting
nobody thought was part of the metric.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text

# Bucket boundary for every time series on the dashboard. See the module docstring.
_DAY_BUCKET = "date_trunc('day', ts, 'UTC')"


@dataclass(frozen=True)
class LatencyBucket:
    bucket: dt.datetime
    n: int
    p50: float
    p95: float


def latency_over_time(conn, since: dt.datetime) -> list[LatencyBucket]:
    """Per-day n, median and 95th percentile latency, oldest bucket first.

    Percentiles rather than a mean: rubric 3.2 asks for latency over time, and the thing
    that matters about a latency series is its tail (premortem H28). A mean over a bucket
    holding one 5-second outlier reports neither the typical request nor the outlier.
    """
    rows = conn.execute(
        text(
            f"SELECT {_DAY_BUCKET} AS bucket, count(*) AS n, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
            "FROM predictions WHERE ts >= :since GROUP BY 1 ORDER BY 1"
        ),
        {"since": since},
    ).all()
    return [
        LatencyBucket(bucket=row.bucket, n=int(row.n), p50=float(row.p50), p95=float(row.p95))
        for row in rows
    ]
