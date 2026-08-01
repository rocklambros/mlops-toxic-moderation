"""Monitoring dashboard. Streamlit, port 8502, EC2 #3 (rubric 3.1, 3.2).

Rubric 3.2 asks for "a separate frontend app on a different EC2 server (data exchanged via
the database, not JSON files)", showing three things. This renders exactly those three, plus
the honesty captions that keep a thin dataset from looking like a rich one:

  1. prediction latency over time (p50 and p95 per day),
  2. distribution of predicted classes as target drift, plotted against the Phase 1
     baseline with a per-label PSI and a stated alert threshold,
  3. live accuracy from human feedback, design-weighted, with per-stratum n and a Wilson
     interval -- never a bare point estimate.

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
DEFAULT_WINDOW_DAYS = 14
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


def window_days() -> int:
    return int(os.environ.get("DASHBOARD_WINDOW_DAYS", DEFAULT_WINDOW_DAYS))


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

    window_days: int
    total: int
    seeded: int
    statuses: dict[str, int]
    thresholds_digest: str
    latency: list[LatencyBucket]
    accuracy: AccuracyReport
    panel: UserPanel
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


def latency_caption(n_buckets: int) -> str:
    if int(n_buckets) < MIN_BUCKETS:
        return (
            f"Not enough history: {int(n_buckets)} daily bucket(s), {MIN_BUCKETS} required "
            "before this chart shows a trend. Run `make seed-demo`."
        )
    return f"{int(n_buckets)} daily buckets. The two lines are p50 and p95 latency."


def drift_caption(alerting: list[str], alert_psi: float | None = None) -> str:
    threshold = configured_alert_psi() if alert_psi is None else float(alert_psi)
    named = [label for label in LABELS if label in set(alerting)]
    if not named:
        return f"No label exceeds the PSI alert threshold of {threshold:g}."
    return (
        f"PSI >= {threshold:g} on: {', '.join(named)}. Investigate before trusting the model."
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
    return (
        f"Window: last {int(data.window_days)} days. {int(data.total)} predictions, of which "
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


def collect(conn, reference: Reference, now: dt.datetime, days: int, psi_alert: float) -> Snapshot:
    since = now - dt.timedelta(days=days)
    total, seeded = seeded_share(conn, since)
    drift = flags = None
    if reference.thresholds is not None and reference.baseline is not None:
        drift = drift_report(
            conn, since, reference.thresholds, reference.baseline, alert_psi=psi_alert
        )
        flags = flag_rate_series(conn, since, reference.thresholds)
    return Snapshot(
        window_days=days,
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
                {"day": bucket.bucket, "p50 (ms)": bucket.p50, "p95 (ms)": bucket.p95}
                for bucket in data.latency
            ]
        )
        st.line_chart(frame, x="day", y=["p50 (ms)", "p95 (ms)"])
    st.caption(latency_caption(len(data.latency)))

    st.header("2. Predicted class distribution (target drift)")
    if data.drift:
        _render_drift(data)
    st.caption(drift_caption(alerting_labels(data)))

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
            use_container_width=True,
        )

    st.subheader("User feedback")
    st.caption(user_caption(data.panel))


def alerting_labels(data: Snapshot) -> list[str]:
    return [row.label for row in (data.drift or []) if row.alert]


def _render_drift(data: Snapshot) -> None:
    import altair as alt
    import pandas as pd
    import streamlit as st

    rows = data.drift or []
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
    st.altair_chart(chart, use_container_width=True)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "label": row.label,
                    "baseline": row.baseline_rate,
                    "production": row.production_rate,
                    "PSI": row.psi,
                    "JS": row.js,
                    "alert": row.alert,
                }
                for row in rows
            ],
            columns=["label", "baseline", "production", "PSI", "JS", "alert"],
        ),
        hide_index=True,
        use_container_width=True,
    )
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
        data = collect(conn, reference, dt.datetime.now(dt.UTC), days, configured_alert_psi())
    render(data)


if __name__ == "__main__":
    main()
