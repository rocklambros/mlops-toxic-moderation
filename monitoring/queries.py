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

import pandas as pd
from sqlalchemy import text

from model.labels import LABELS
from monitoring.baseline import Baseline
from monitoring.stats import (
    AccuracyReport,
    horvitz_thompson_accuracy,
    js_divergence,
    psi,
    wilson_interval,
)

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


# Standard PSI reading: < 0.1 no meaningful shift, 0.1-0.2 moderate, >= 0.2 major.
DEFAULT_ALERT_PSI = 0.2


@dataclass(frozen=True)
class DriftRow:
    label: str
    baseline_rate: float
    production_rate: float
    psi: float
    js: float
    alert: bool


def _flag_sum_sql() -> str:
    """One conditional sum per label, each against its OWN threshold.

    Comparing every label to one number would be a different decision rule from the one
    Phase 1 used to compute the reference rates, and two series computed under two rules
    cannot be compared -- the PSI would measure the rule change, not the data.
    """
    return ", ".join(
        f"sum(CASE WHEN prob_{label} >= :thr_{label} THEN 1 ELSE 0 END) AS flag_{label}"
        for label in LABELS
    )


def _threshold_binds(thresholds: dict[str, float]) -> dict[str, float]:
    return {f"thr_{label}": float(thresholds[label]) for label in LABELS}


def production_flag_rates(
    conn, since: dt.datetime, thresholds: dict[str, float]
) -> tuple[int, dict[str, float]]:
    row = conn.execute(
        text(f"SELECT count(*) AS n, {_flag_sum_sql()} FROM predictions WHERE ts >= :since"),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().one()
    n = int(row["n"])
    if n == 0:
        return 0, {label: 0.0 for label in LABELS}
    return n, {label: float(row[f"flag_{label}"] or 0) / n for label in LABELS}


def drift_report(
    conn,
    since: dt.datetime,
    thresholds: dict[str, float],
    baseline: Baseline,
    alert_psi: float = DEFAULT_ALERT_PSI,
) -> list[DriftRow]:
    """One row per label: the Phase 1 reference rate, the production rate, and the distance.

    An empty window is a zero rate, not a division by zero -- this panel has to render on a
    database nobody has sent traffic to yet (premortem C5).
    """
    _, production = production_flag_rates(conn, since, thresholds)
    rows = []
    for label in LABELS:
        reference = baseline.flag_rates[label]
        observed = production[label]
        score = psi(reference, observed)
        rows.append(
            DriftRow(
                label=label,
                baseline_rate=reference,
                production_rate=observed,
                psi=score,
                js=js_divergence(reference, observed),
                alert=score >= alert_psi,
            )
        )
    return rows


def flag_rate_series(conn, since: dt.datetime, thresholds: dict[str, float]) -> pd.DataFrame:
    rows = conn.execute(
        text(
            f"SELECT {_DAY_BUCKET} AS bucket, count(*) AS n, {_flag_sum_sql()} "
            "FROM predictions WHERE ts >= :since GROUP BY 1 ORDER BY 1"
        ),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().all()
    records = []
    for row in rows:
        n = int(row["n"]) or 1
        record = {"bucket": row["bucket"]}
        for label in LABELS:
            record[label] = float(row[f"flag_{label}"] or 0) / n
        records.append(record)
    return pd.DataFrame(records, columns=["bucket", *LABELS])


@dataclass(frozen=True)
class UserPanel:
    n: int
    agree: int
    rate: float | None
    lo: float | None
    hi: float | None


def live_accuracy(conn, since: dt.datetime) -> AccuracyReport:
    """Design-weighted accuracy over the two probability-sampled strata.

    `sample_rate IS NOT NULL` is the filter that keeps self-selected rows out. A
    `user-report` row has no known inclusion probability, and a `source='user'` feedback row
    is not a label at all -- both would bias the estimate and both would make the graded
    number writable by an anonymous visitor.

    The weighting itself lives in `horvitz_thompson_accuracy`. Reporting correct/total over
    the union instead is the H8 defect: it is the same query, one arithmetic step shorter,
    and it reads as a plausible accuracy while being biased toward whichever stratum
    happens to be larger.
    """
    rows = conn.execute(
        text(
            "SELECT q.source AS stratum, q.sample_rate, f.exact_match "
            "FROM feedback f "
            "JOIN review_queue q ON q.request_id = f.request_id "
            "JOIN predictions p ON p.request_id = f.request_id "
            "WHERE f.source = 'reviewer' AND q.sample_rate IS NOT NULL AND p.ts >= :since"
        ),
        {"since": since},
    ).all()
    return horvitz_thompson_accuracy(
        (row.stratum, float(row.sample_rate), bool(row.exact_match)) for row in rows
    )


def review_counts(conn, since: dt.datetime) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT q.status, count(*) AS n FROM review_queue q "
            "JOIN predictions p ON p.request_id = q.request_id "
            "WHERE p.ts >= :since GROUP BY 1"
        ),
        {"since": since},
    ).all()
    return {row.status: int(row.n) for row in rows}


def seeded_share(conn, since: dt.datetime) -> tuple[int, int]:
    """Total predictions in the window, and how many of them `make seed-demo` replayed.

    The dashboard states this out loud. A screenshot of replayed held-out comments that
    does not say so is a screenshot of production traffic that never happened.
    """
    row = conn.execute(
        text(
            "SELECT count(*) AS total, "
            "sum(CASE WHEN is_seed THEN 1 ELSE 0 END) AS seeded "
            "FROM predictions WHERE ts >= :since"
        ),
        {"since": since},
    ).one()
    return int(row.total), int(row.seeded or 0)


def user_feedback_panel(conn, since: dt.datetime) -> UserPanel:
    """The self-selected panel, reported on its own and never pooled into live accuracy."""
    row = conn.execute(
        text(
            "SELECT count(*) AS n, sum(CASE WHEN f.exact_match THEN 1 ELSE 0 END) AS agree "
            "FROM feedback f JOIN predictions p ON p.request_id = f.request_id "
            "WHERE f.source = 'user' AND p.ts >= :since"
        ),
        {"since": since},
    ).one()
    n = int(row.n)
    if n == 0:
        return UserPanel(n=0, agree=0, rate=None, lo=None, hi=None)
    agree = int(row.agree or 0)
    lo, hi = wilson_interval(agree, n)
    return UserPanel(n=n, agree=agree, rate=agree / n, lo=lo, hi=hi)
