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
    observation_is_improbable,
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
        # `_DAY_BUCKET` is a module-level constant and `since` is a bound parameter.
        # `percentile_cont ... WITHIN GROUP` is an ordered-set aggregate with no ORM
        # expression form, which is why this query is written in SQL at all.
        # nosemgrep: avoid-sqlalchemy-text
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

# The denominator an alert has to rest on, and the drift panel's counterpart to the latency
# panel's `MIN_SAMPLES_PER_BUCKET`.
#
# PSI reads a proportion and knows nothing about the count behind it. One flagged comment
# out of three is a 33% flag rate against a 9.6% baseline: PSI 0.35, a "major shift" alert.
# Under that same baseline, seeing at least one flag in three predictions happens 27% of the
# time, so the alert is a coin flip on a quiet day rather than a finding. At 30 predictions
# it takes 9 flags to cross 0.2, which the baseline produces 0.2% of the time. Thirty is
# roughly where the alert stops being a property of the sample size.
#
# This is not solved by widening the drift window. The window is bounded so that PSI can say
# drift *started*; a wider one dilutes new traffic with old for as long as the project runs.
MIN_DRIFT_SAMPLES = 30


@dataclass(frozen=True)
class DriftRow:
    label: str
    baseline_rate: float
    production_rate: float
    psi: float
    js: float
    alert: bool
    # How many predictions `production_rate` is a proportion OF. A rate of 0.0 over n=0 is
    # "nobody asked the model anything"; over n=1200 it is "the model flagged nothing".
    # Those are different findings and the rate alone cannot tell them apart. `None` means
    # the count was not recorded, which `drift_report` never does.
    n: int | None = None
    # How many of those `n` predictions were live traffic rather than rows `make seed-demo`
    # replayed. The seeded rows ARE the baseline's own sample, so a PSI computed over them
    # is a wiring check. `live_n` is what a drift claim has to be measured over.
    live_n: int | None = None


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
    conn, since: dt.datetime, thresholds: dict[str, float], seeded: bool | None = None
) -> tuple[int, dict[str, float]]:
    """Flag rates over the window. `seeded` selects which rows count.

    `None` is every row and is what the panel's headline series uses. `False` is live traffic
    only, and it exists because `make seed-demo` replays the locked held-out split -- the same
    split `baseline_flag_rates.json` was computed over. PSI between the seeded rows and that
    baseline is a comparison of a distribution with itself: zero by construction, and not a
    statement about production. The live subset is the only part of the window that can carry
    a drift finding.
    """
    predicate = {None: "", False: " AND NOT is_seed", True: " AND is_seed"}[seeded]
    row = conn.execute(
        # `_flag_sum_sql()` emits one `sum(...)` per label from LABELS and compares each
        # probability against a BOUND threshold placeholder; the caller's thresholds reach
        # the database through `_threshold_binds`, never through the string. `predicate` is
        # selected from a literal dict keyed on a bool, so it is not caller text either.
        # nosemgrep: avoid-sqlalchemy-text
        text(
            f"SELECT count(*) AS n, {_flag_sum_sql()} FROM predictions "
            f"WHERE ts >= :since{predicate}"
        ),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().one()
    n = int(row["n"])
    if n == 0:
        # Placeholders, not measurements. The count is returned first precisely so a caller
        # cannot read these zeros as a flag rate: against a 9.6% baseline a genuine 0.0
        # scores PSI 1.11, which is a major-shift alert raised by an untouched database.
        return 0, {label: 0.0 for label in LABELS}
    return n, {label: float(row[f"flag_{label}"] or 0) / n for label in LABELS}


def drift_report(
    conn,
    since: dt.datetime,
    thresholds: dict[str, float],
    baseline: Baseline,
    alert_psi: float = DEFAULT_ALERT_PSI,
    min_n: int = MIN_DRIFT_SAMPLES,
) -> list[DriftRow]:
    """One row per label: the Phase 1 reference rate, the production rate, its denominator,
    and the distance.

    An empty window is a zero rate, not a division by zero -- this panel has to render on a
    database nobody has sent traffic to yet (premortem C5). It is also not a drift alert. An
    alert needs a distance and a denominator, and PSI supplies only the first: `min_n` is
    how many predictions that distance has to be measured over before it says something
    about the traffic rather than about the sample size. `n` travels on every row so the
    panel can name which of the two is missing rather than printing "investigate the model"
    over three comments.
    """
    n, production = production_flag_rates(conn, since, thresholds)
    live_n, _ = production_flag_rates(conn, since, thresholds, seeded=False)
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
                alert=(
                    score >= alert_psi
                    and n >= min_n
                    and observation_is_improbable(reference, observed, n)
                ),
                n=n,
                live_n=live_n,
            )
        )
    return rows


def flag_rate_series(conn, since: dt.datetime, thresholds: dict[str, float]) -> pd.DataFrame:
    rows = conn.execute(
        # Both interpolations are the module constants above; `since` and every threshold are
        # bound parameters.
        # nosemgrep: avoid-sqlalchemy-text
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
