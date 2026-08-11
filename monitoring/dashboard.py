"""Monitoring dashboard. Streamlit, port 8502, EC2 #3 (rubric 3.1, 3.2).

Rubric 3.2 asks for "a separate frontend app on a different EC2 server (data exchanged via
the database, not JSON files)", showing three things. This renders exactly those three, plus
the honesty captions that keep a thin dataset from looking like a rich one:

  1. prediction latency over time (p50 and p95 per day),
  2. distribution of predicted classes as target drift, plotted against the Phase 1
     baseline with a per-label PSI and a stated alert threshold,
  3. live accuracy from human feedback, design-weighted, with per-stratum n and a Wilson
     interval -- never a bare point estimate.

Panels 1 and 2 both have a small-sample floor and they spend it differently. Latency plots
thin days and names them in the caption, because a percentile over one request is still a
measurement of that request. Drift's output is a sentence telling a reader to go and
investigate the model, so below `MIN_DRIFT_SAMPLES` predictions the bars are drawn and the
alert is not; a drift window holding no predictions at all withholds the bars too, because
a production rate of 0.0 beside a 9.6% baseline reads as "the model stopped flagging"
rather than "nobody asked it anything".

**Every observation on this page comes from the database.** Predictions, latencies, flag
rates, queue depth and feedback are read from RDS through `monitoring/queries.py` under a
read-only role, on EC2 #3, which is a different host from the user UI on EC2 #2. No process
hands this app a file of metrics, and this app writes none.

The two JSON files this module opens are neither predictions nor feedback. They are model
artifacts, fetched with the model and digest-verified alongside it:

  THRESHOLDS_PATH  the pinned per-label decision boundary the backend also serves with
  BASELINE_PATH    the training-time reference flag rates drift is measured against

Reading those from the database would mean the dashboard's notion of "the decision
boundary" could drift from the backend's, which is the failure that pinning exists to
prevent. `tests/unit/test_dashboard.py` pins the distinction so that nobody later
"optimises" a metrics cache into a file and quietly falsifies the rubric clause.

Two structural rules, both enforced by `tests/unit/test_dashboard_guards.py`:

* The connection string is MONITORING_DB_DSN, a read-only role, and every statement this
  package issues is a SELECT (premortem H16).
* No panel names a column holding a raw comment. This screenshot is a public deliverable
  (delivery spec section 6.4).

Streamlit is imported inside the functions that draw, not at module scope. That is not
style: it keeps every function below importable, and therefore testable, in a job with no
Streamlit installed -- which is the job where the repository-wide rendering-sink scan runs.
`collect` and `render` are separate for the same reason: the degenerate cases that C5 is
about (no rows, one row, one stratum) are exercised against `render` directly.
"""

import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from model.labels import LABELS
from monitoring.baseline import (
    Baseline,
    BaselineContractError,
    BaselineMissingError,
    load_baseline,
    load_thresholds,
)
from monitoring.queries import (
    DEFAULT_ALERT_PSI,
    MIN_DRIFT_SAMPLES,
    DriftRow,
    LatencyBucket,
    UserPanel,
    drift_report,
    flag_rate_series,
    latency_over_time,
    live_accuracy,
    review_counts,
    seeded_share,
    user_feedback_panel,
)
from monitoring.stats import AccuracyReport

# Seven daily buckets is the floor below which "latency over time" is a scatter plot of one
# afternoon. `scripts/seed_demo.py` enforces the same number from the other side.
MIN_BUCKETS = 7

# Below this many requests in a day, `percentile_cont(0.5)` is reporting one or two
# measurements rather than a level. The chart still plots the point -- hiding real data is
# worse -- but the caption says which days are thin.
MIN_SAMPLES_PER_BUCKET = 20

# The drift panel's floor is `MIN_DRIFT_SAMPLES`, imported from `monitoring.queries` because
# that is where the alert flag is computed. See the module docstring for why the two panels
# treat a thin window differently.

# The dashboard shows ALL history by default, and bounds only the drift comparison.
#
# It used to bound everything with one 14-day `since` threaded into all seven queries. Only
# one of them needs a bound. Drift is a comparison of a *recent* production distribution
# against a fixed baseline, so without a window old data dilutes the signal forever and
# "drift started" becomes undetectable. The other six are either time series whose x-axis
# already carries recency, or totals that a reader expects to be project-wide.
#
# Bounding them anyway had a failure mode that is not theoretical: every prediction in this
# project was made between 2026-07-27 and 2026-08-02, so on 2026-08-17 a 14-day window
# contained zero rows and every panel rendered empty, with no error and nothing to say the
# data still existed. A dashboard whose contents depend on the hour someone opens it is
# reporting the window, not the system.
#
# `DASHBOARD_WINDOW_DAYS` still narrows everything when an operator wants "the last N days";
# unset, empty, `all`, or `0` mean all of it.
DEFAULT_WINDOW_DAYS: int | None = None
DEFAULT_DRIFT_WINDOW_DAYS = 14

# Older than any row this system can hold, so `ts >= :since` is a no-op rather than a
# separate un-filtered query. Keeping one SQL string per question is worth more than saving
# a comparison the planner discards anyway.
BEGINNING_OF_TIME = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
DEFAULT_BASELINE_PATH = Path("artifacts/baseline_flag_rates.json")
DEFAULT_THRESHOLDS_PATH = Path("artifacts/thresholds.json")

# The reference loaders raise exactly these two, and only their class names are ever
# rendered. An exception's message carries a filesystem path and, on a contract failure, a
# value read out of the artifact -- neither belongs on a public screenshot, and neither
# belongs in a widget that parses markdown.
REFERENCE_ERRORS = ("BaselineMissingError", "BaselineContractError")
REFERENCE_UNAVAILABLE = (
    "Drift reference unavailable, so the drift panel is not shown. Phase 1 must publish "
    "baseline_flag_rates.json and thresholds.json alongside the promoted model."
)


def window_days() -> int | None:
    """Days of history for every panel except drift. `None` means all of it.

    Unset, empty, `all` and `0` all mean all-time, because those are the four things an
    operator types when they mean "stop hiding data from me".
    """
    raw = os.environ.get("DASHBOARD_WINDOW_DAYS")
    if raw is None:
        return DEFAULT_WINDOW_DAYS
    raw = raw.strip().lower()
    if raw in {"", "all", "0"}:
        return None
    return int(raw)


def drift_window_days() -> int:
    """Days of production traffic compared against the fixed baseline.

    This one is load-bearing and is never unbounded: PSI against an all-time production
    distribution cannot tell you that drift *started*.
    """
    return int(os.environ.get("DRIFT_WINDOW_DAYS", DEFAULT_DRIFT_WINDOW_DAYS))


def configured_alert_psi() -> float:
    return float(os.environ.get("DRIFT_PSI_ALERT", DEFAULT_ALERT_PSI))


def baseline_path() -> Path:
    if "BASELINE_PATH" in os.environ:
        return Path(os.environ["BASELINE_PATH"])
    return DEFAULT_BASELINE_PATH


def thresholds_path() -> Path:
    if "THRESHOLDS_PATH" in os.environ:
        return Path(os.environ["THRESHOLDS_PATH"])
    return DEFAULT_THRESHOLDS_PATH


@dataclass(frozen=True)
class Snapshot:
    """Everything on the page, read once, from the database.

    `drift` and `flags` are None when the pinned reference is missing: a drift chart with no
    reference cannot answer whether anything changed, so it is withheld rather than drawn
    against an implicit zero.
    """

    window_days: int | None
    total: int
    seeded: int
    statuses: dict[str, int]
    thresholds_digest: str
    latency: list[LatencyBucket]
    accuracy: AccuracyReport
    panel: UserPanel
    drift_window_days: int = DEFAULT_DRIFT_WINDOW_DAYS
    drift: list[DriftRow] | None = None
    flags: object | None = None
    reference_error: str | None = None


@dataclass(frozen=True)
class Reference:
    thresholds: dict[str, float] | None = None
    baseline: Baseline | None = None
    error: str | None = None
    digest: str = "missing"


# --------------------------------------------------------------------------------------
# Formatters. Each returns a fixed sentence built from numbers and closed vocabularies, so
# a markdown-capable widget never receives a value this module did not construct.
# --------------------------------------------------------------------------------------


def accuracy_caption(report: AccuracyReport) -> str:
    if report.point is None:
        return (
            "Not enough reviewed items to estimate live accuracy. Run `make seed-demo` or "
            "review items in the reviewer console."
        )
    strata = ", ".join(
        f"{s.stratum} n={s.n} (pi={s.sample_rate:g}, {s.accuracy:.1%})" for s in report.strata
    )
    interval = f"{report.point:.1%} (95% CI {report.lo:.1%}-{report.hi:.1%})"
    if not any(s.stratum == "random-audit" for s in report.strata):
        return (
            f"{interval}; {strata}. WARNING: the random-audit stratum is empty, so this "
            "measures only the model's own flagged set and is blind to confidently-allowed "
            "false negatives."
        )
    return (
        f"{interval}, Horvitz-Thompson weighted by the inclusion probability recorded at "
        f"enqueue time; {strata}; effective n={report.effective_n:.1f}."
    )


def accuracy_metric(point: float) -> str:
    """The one number a grader reads off the screenshot. `float()` refuses a string, so
    nothing but a number can reach the widget."""
    return f"{float(point):.1%}"


def latency_caption(n_buckets: int, sample_counts: list[int] | None = None) -> str:
    """Say how many requests each point stands for, not just how many points there are.

    The old caption counted buckets and stopped. Seven buckets of one request each passed
    that check and rendered as a trend line, which is how a single cold-start request after
    eight idle days read as a 5x latency regression. The bucket count answers "is there
    enough history"; the sample counts answer "is any of it worth believing", and only the
    second one catches that failure.
    """
    if int(n_buckets) < MIN_BUCKETS:
        return (
            f"Not enough history: {int(n_buckets)} daily bucket(s), {MIN_BUCKETS} required "
            "before this chart shows a trend. Run `make seed-demo`."
        )
    head = (
        f"{int(n_buckets)} daily buckets. Each point is one day's p50 or p95; points are "
        "not joined, because days with no traffic are gaps rather than a straight line "
        "between the days either side of them."
    )
    if not sample_counts:
        return head
    lo, hi = min(sample_counts), max(sample_counts)
    thin = sum(1 for n in sample_counts if n < MIN_SAMPLES_PER_BUCKET)
    detail = f" Requests per day: min {int(lo)}, max {int(hi)}."
    if thin:
        detail += (
            f" {thin} day(s) carry fewer than {MIN_SAMPLES_PER_BUCKET} requests, so their "
            "percentiles are close to single measurements and should not be read as a level."
        )
    return head + detail


def drift_caption(
    alerting: list[str],
    alert_psi: float | None = None,
    n: int | None = None,
    live_n: int | None = None,
) -> str:
    """Say what the comparison rests on, not only what it concluded.

    `n` is the number of predictions in the drift window. Two of its values are not drift
    findings at all and must not read like one: zero means the window holds no traffic, and
    a handful means the flag rates moved by a third of a comment each. Both were previously
    reported as "PSI >= 0.2 on: toxic, obscene, insult. Investigate before trusting the
    model." on the highest-weighted panel of a graded page.

    `None` means the caller recorded no count -- the same contract `latency_caption` gives
    its optional sample counts. `collect` always records one, so the small-sample branches
    are skipped only for hand-built snapshots rather than guessed at.
    """
    threshold = configured_alert_psi() if alert_psi is None else float(alert_psi)
    named = [label for label in LABELS if label in set(alerting)]
    if n is not None and int(n) <= 0:
        return (
            "No predictions in the drift window, so there is nothing to compare against the "
            "baseline. That is no recent traffic, not a flag rate of zero, and the bars are "
            "withheld rather than drawn against a production series of zeroes."
        )
    if n is not None and int(n) < MIN_DRIFT_SAMPLES:
        return (
            f"Only {int(n)} prediction(s) in the drift window, fewer than the "
            f"{MIN_DRIFT_SAMPLES} this panel requires before it reports drift. One flagged "
            f"comment moves a flag rate by {1.0 / int(n):.0%} at this size, so the bars "
            "below are recent traffic and no PSI alert is claimed from them."
        )
    # A window dominated by replayed rows cannot carry a drift finding in either direction.
    # `make seed-demo` replays the locked held-out split, and baseline_flag_rates.json was
    # computed over that same split, so PSI across those rows compares a distribution with
    # itself. Reporting "no label exceeds the threshold" over them states a conclusion the
    # comparison is structurally incapable of reaching.
    if live_n is not None and n is not None and live_n < int(n):
        if int(live_n) < MIN_DRIFT_SAMPLES:
            return (
                f"{int(n)} predictions in the drift window, but {int(n) - int(live_n)} of "
                f"them are replayed held-out rows that the baseline was itself computed "
                f"over -- comparing those against it is a wiring check, not a measurement. "
                f"Only {int(live_n)} are live traffic, fewer than the {MIN_DRIFT_SAMPLES} "
                f"this panel requires, so no drift finding is claimed. The bars below are "
                f"the full window."
            )
    stated = "." if n is None else f", over {int(n)} predictions in the drift window."
    if not named:
        return f"No label exceeds the PSI alert threshold of {threshold:g}{stated}"
    return (
        f"PSI >= {threshold:g} on: {', '.join(named)}{stated} Investigate before trusting "
        "the model."
    )


def user_caption(panel: UserPanel) -> str:
    if panel.rate is None:
        return "Not enough user feedback yet."
    return (
        f"{panel.rate:.1%} agreement (95% CI {panel.lo:.1%}-{panel.hi:.1%}), n={int(panel.n)}. "
        "This is self-selected and is NOT an unbiased accuracy estimate, so it is reported "
        "separately from live accuracy. A disagreement sends the comment to a human reviewer."
    )


def window_caption(data: "Snapshot") -> str:
    """The provenance line. Every value is an integer or a hex digest computed here."""
    scope = (
        "Window: all history"
        if data.window_days is None
        else f"Window: last {int(data.window_days)} days"
    )
    return (
        f"{scope}; drift is compared over the last {int(data.drift_window_days)} days. "
        f"{int(data.total)} predictions, of which "
        f"{int(data.seeded)} are replayed held-out Jigsaw comments from `make seed-demo`. "
        f"Queue: {int(data.statuses.get('pending', 0))} pending, "
        f"{int(data.statuses.get('rescored', 0))} rescored, "
        f"{int(data.statuses.get('reviewed', 0))} reviewed. "
        f"thresholds.json sha256:{_hex(data.thresholds_digest)}."
    )


def reference_error_caption(name: str | None) -> str:
    """A fixed sentence, plus the failure's class name only when it is one this module
    raises. Anything else is dropped rather than rendered."""
    if name in REFERENCE_ERRORS:
        return f"{REFERENCE_UNAVAILABLE} ({name})"
    return REFERENCE_UNAVAILABLE


def _hex(value: str) -> str:
    """A digest, or the fixed word `missing`. Never an arbitrary string."""
    candidate = str(value)
    if candidate and all(character in "0123456789abcdef" for character in candidate):
        return candidate
    return "missing"


def _digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# --------------------------------------------------------------------------------------
# Reading. Two functions: one loads the pinned model artifacts, one reads the database.
# --------------------------------------------------------------------------------------


def load_reference() -> Reference:
    """Fail closed and say so, rather than plotting drift against an implicit zero."""
    try:
        return Reference(
            thresholds=load_thresholds(thresholds_path()),
            baseline=load_baseline(baseline_path()),
            digest=_digest(thresholds_path()),
        )
    except (BaselineMissingError, BaselineContractError) as exc:
        return Reference(error=type(exc).__name__, digest=_digest(thresholds_path()))


def collect(
    conn,
    reference: Reference,
    now: dt.datetime,
    days: int | None,
    psi_alert: float,
    drift_days: int = DEFAULT_DRIFT_WINDOW_DAYS,
) -> Snapshot:
    """Two windows, because the panels ask two different questions.

    `days` bounds description -- what has this system done. `None` means all of it.
    `drift_days` bounds comparison -- is recent traffic unlike the baseline. That one is
    always bounded, whatever `days` says, or PSI silently answers a different question.
    """
    since = BEGINNING_OF_TIME if days is None else now - dt.timedelta(days=days)
    drift_since = now - dt.timedelta(days=drift_days)
    total, seeded = seeded_share(conn, since)
    drift = flags = None
    if reference.thresholds is not None and reference.baseline is not None:
        drift = drift_report(
            conn, drift_since, reference.thresholds, reference.baseline, alert_psi=psi_alert
        )
        flags = flag_rate_series(conn, since, reference.thresholds)
    return Snapshot(
        window_days=days,
        drift_window_days=drift_days,
        total=total,
        seeded=seeded,
        statuses=review_counts(conn, since),
        thresholds_digest=reference.digest,
        latency=latency_over_time(conn, since),
        accuracy=live_accuracy(conn, since),
        panel=user_feedback_panel(conn, since),
        drift=drift,
        flags=flags,
        reference_error=reference.error,
    )


# --------------------------------------------------------------------------------------
# Drawing. Every branch below has to survive an empty database: nothing renders NaN, and
# nothing divides by anything (premortem C5).
# --------------------------------------------------------------------------------------


def render(data: Snapshot) -> None:
    import pandas as pd
    import streamlit as st

    st.title("Toxic comment moderation: production monitoring")
    st.caption(window_caption(data))
    if data.reference_error is not None:
        st.error(reference_error_caption(data.reference_error))

    st.header("1. Prediction latency over time")
    if data.latency:
        frame = pd.DataFrame(
            [
                {
                    "day": bucket.bucket,
                    "p50 (ms)": bucket.p50,
                    "p95 (ms)": bucket.p95,
                    "requests": int(bucket.n),
                }
                for bucket in data.latency
            ]
        )
        # Points, not a line. A line interpolates across days that have no traffic, which
        # invents a trend out of two distant measurements; sizing by request count then says
        # which points are worth reading.
        st.scatter_chart(frame, x="day", y=["p50 (ms)", "p95 (ms)"], size="requests")
    st.caption(latency_caption(len(data.latency), [int(b.n) for b in data.latency]))

    st.header("2. Predicted class distribution (target drift)")
    if data.drift:
        _render_drift(data)
    st.caption(
        drift_caption(
            alerting_labels(data),
            n=drift_sample_size(data),
            live_n=drift_live_sample_size(data),
        )
    )

    st.header("3. Live accuracy from human feedback")
    if data.accuracy.point is not None:
        st.metric("Live accuracy (design-weighted)", accuracy_metric(data.accuracy.point))
    st.caption(accuracy_caption(data.accuracy))
    if data.accuracy.strata:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "stratum": stratum.stratum,
                        "n": stratum.n,
                        "correct": stratum.correct,
                        "inclusion probability": stratum.sample_rate,
                        "accuracy": stratum.accuracy,
                        "95% CI low": stratum.lo,
                        "95% CI high": stratum.hi,
                    }
                    for stratum in data.accuracy.strata
                ],
                columns=[
                    "stratum", "n", "correct", "inclusion probability", "accuracy",
                    "95% CI low", "95% CI high",
                ],
            ),
            hide_index=True,
            width="stretch",
        )

    st.subheader("User feedback")
    st.caption(user_caption(data.panel))


def alerting_labels(data: Snapshot) -> list[str]:
    return [row.label for row in (data.drift or []) if row.alert]


def drift_sample_size(data: Snapshot) -> int | None:
    """How many predictions the drift panel's production rates were computed from.

    One number per window, carried on every row because it is the denominator of that row's
    rate. `None` means no row recorded one, which `collect` never produces.
    """
    rows = data.drift or []
    return rows[0].n if rows else None


def drift_live_sample_size(data: "Snapshot") -> int | None:
    """The live half of the drift denominator, or None if the rows did not record one."""
    return next((row.live_n for row in (data.drift or [])), None)


def _render_drift(data: Snapshot) -> None:
    import altair as alt
    import pandas as pd
    import streamlit as st

    rows = data.drift or []
    if drift_sample_size(data) == 0:
        # Nothing to compare. The flag-rate series below is computed over the *description*
        # window, which is all history, so an empty fortnight says nothing about it and it
        # stays on the page; the caption carries the reason the bars are gone.
        _render_flag_rates(data)
        return
    long = pd.DataFrame(
        [{"label": row.label, "series": "baseline", "rate": row.baseline_rate} for row in rows]
        + [{"label": row.label, "series": "production", "rate": row.production_rate}
           for row in rows],
        columns=["label", "series", "rate"],
    )
    chart = (
        alt.Chart(long)
        .mark_bar()
        .encode(
            x=alt.X("label:N", sort=list(LABELS)),
            xOffset="series:N",
            y=alt.Y("rate:Q", title="flag rate"),
            color=alt.Color("series:N", title=""),
        )
    )
    st.altair_chart(chart, width="stretch")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "label": row.label,
                    "baseline": row.baseline_rate,
                    "production": row.production_rate,
                    # The denominator, beside the rate it divides. A production column with
                    # nothing to divide by is the defect this guard exists for.
                    "n": row.n,
                    "PSI": row.psi,
                    "JS": row.js,
                    "alert": row.alert,
                }
                for row in rows
            ],
            columns=["label", "baseline", "production", "n", "PSI", "JS", "alert"],
        ),
        hide_index=True,
        width="stretch",
    )
    _render_flag_rates(data)


def _render_flag_rates(data: Snapshot) -> None:
    """Flag rate per label per day, over the description window rather than the drift one.

    One bucket is a point rather than a series, and joining two distant points invents a
    trend, so the chart needs at least two.
    """
    import streamlit as st

    if data.flags is not None and len(data.flags) > 1:
        st.line_chart(data.flags, x="bucket", y=list(LABELS))


def main() -> None:
    import streamlit as st
    from sqlalchemy import create_engine

    st.set_page_config(page_title="Toxic moderation monitoring", layout="wide")
    reference = load_reference()
    days = window_days()
    engine = create_engine(os.environ["MONITORING_DB_DSN"], future=True, pool_pre_ping=True)
    with engine.connect() as conn:
        data = collect(
            conn,
            reference,
            dt.datetime.now(dt.UTC),
            days,
            configured_alert_psi(),
            drift_window_days(),
        )
    render(data)


if __name__ == "__main__":
    main()
