"""What the monitoring dashboard is allowed to do, and what it must survive.

Three separate properties, none implying the others:

* it holds a read-only role and its code matches the grant (premortem H16),
* it never names a column carrying a raw comment, because the screenshot is a public
  deliverable (delivery spec section 6.4),
* it renders on a database with nothing in it (premortem C5). A dashboard that raises on an
  empty table fails the demo at exactly the moment it is being graded.

The rendering tests drive `render` with a recording stand-in for Streamlit. That proves this
module's own branching -- the empty guards, the captions, the divisions it does not do --
and makes no claim about Streamlit's API, which is not installed in the unit job by design
(`requirements/ui.in`: the UI surfaces resolve into their own lock).
"""

import re
import sys
import types
from pathlib import Path

import pytest

from model.labels import LABELS
from monitoring.dashboard import (
    MIN_BUCKETS,
    MIN_REVIEWED_FOR_ESTIMATE,
    REFERENCE_UNAVAILABLE,
    Snapshot,
    accuracy_caption,
    accuracy_floor_notice,
    accuracy_metric,
    alerting_labels,
    drift_caption,
    latency_caption,
    reference_error_caption,
    render,
    user_caption,
    window_caption,
)
from monitoring.queries import DriftRow, LatencyBucket, UserPanel
from monitoring.stats import AccuracyReport, StratumStat
from tests.unit.sink_scan import interpolated_sink_calls

DML = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+TABLE"
    r"|CREATE\s+TABLE|TRUNCATE|GRANT)\b",
    re.IGNORECASE,
)
RAW_TEXT = re.compile(r"\binput_text(_snapshot)?\b")
SOURCE = Path("monitoring/dashboard.py").read_text(encoding="utf-8")
XSS = "<img src=x onerror=alert(1)>"

# Markdown-capable widgets may be handed a literal, or the output of one of these. Each is
# tested below to prove it cannot carry a value this module did not construct.
VETTED_FORMATTERS = frozenset(
    {
        "accuracy_caption",
        "accuracy_floor_notice",
        "accuracy_metric",
        "drift_caption",
        "latency_caption",
        "reference_error_caption",
        "user_caption",
        "window_caption",
    }
)


# --------------------------------------------------------------------------------------
# Access posture
# --------------------------------------------------------------------------------------


def test_the_scan_covers_more_than_zero_files():
    """A scan over an empty directory passes vacuously, which is how this control dies."""
    assert len(list(Path("monitoring").rglob("*.py"))) >= 4


def test_monitoring_issues_no_write_statements():
    """H16: the dashboard holds a read-only role, and the code must match the grant."""
    for path in sorted(Path("monitoring").rglob("*.py")):
        assert not DML.search(path.read_text(encoding="utf-8")), f"{path} writes"


def test_the_write_statement_scanner_reports_before_it_is_trusted():
    assert DML.search("conn.execute(text('INSERT INTO predictions VALUES (1)'))")
    assert DML.search("UPDATE review_queue SET status = 'reviewed'")
    assert not DML.search("SELECT count(*) FROM predictions")


def test_monitoring_never_selects_raw_user_text():
    """Delivery spec section 6.4: the dashboard screenshot is a public deliverable."""
    for path in sorted(Path("monitoring").rglob("*.py")):
        assert not RAW_TEXT.search(path.read_text(encoding="utf-8")), f"{path} reads comments"


def test_the_raw_text_scanner_reports_before_it_is_trusted():
    assert RAW_TEXT.search("SELECT input_text FROM predictions")
    assert RAW_TEXT.search("q.input_text_snapshot")
    assert not RAW_TEXT.search("SELECT count(*) FROM predictions")


def test_dashboard_uses_a_dedicated_read_only_dsn():
    assert "MONITORING_DB_DSN" in SOURCE
    assert "DATABASE_URL" not in SOURCE


def test_no_markdown_sink_in_the_dashboard_renders_a_value_it_did_not_choose():
    """The dashboard shows counts and label names, never a comment -- but the sink rule is
    the control and it is enforced by the scanner, not by that argument."""
    assert interpolated_sink_calls(SOURCE, VETTED_FORMATTERS) == []


def test_the_sink_scanner_flags_an_interpolated_caption():
    """Non-vacuity for the line above."""
    assert interpolated_sink_calls('st.caption(f"{total} predictions")')
    assert interpolated_sink_calls("st.metric('Live accuracy', report.point)")
    assert interpolated_sink_calls("st.caption(window_caption(data))", VETTED_FORMATTERS) == []


# --------------------------------------------------------------------------------------
# Formatters
# --------------------------------------------------------------------------------------


def _report(strata: list[StratumStat], point: float | None = 0.8333) -> AccuracyReport:
    return AccuracyReport(
        n=sum(stratum.n for stratum in strata),
        point=point,
        lo=0.6975,
        hi=0.9156,
        effective_n=43.9,
        strata=strata,
    )


FLAGGED = StratumStat("flagged", 200, 120, 1.0, 0.60, 0.5308, 0.6654)
AUDIT = StratumStat("random-audit", 20, 19, 0.05, 0.95, 0.7639, 0.9911)


def test_accuracy_caption_on_empty_data_says_so_instead_of_rendering_nan():
    caption = accuracy_caption(
        AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[])
    )
    assert "not enough" in caption.lower()
    assert "nan" not in caption.lower()


def test_accuracy_caption_reports_the_point_the_interval_and_every_stratum_n():
    caption = accuracy_caption(_report([FLAGGED, AUDIT]))
    assert "83.3%" in caption
    # 0.6975 formatted to one decimal place is 69.8%, not 69.7%: the interval is rounded,
    # not truncated, so the printed bound never claims to be tighter than it is.
    assert "69.8%" in caption and "91.6%" in caption
    assert "flagged n=200" in caption
    assert "random-audit n=20" in caption
    assert "pi=0.05" in caption
    assert "effective n=43.9" in caption


def test_accuracy_caption_warns_when_the_audit_stratum_is_empty():
    only_flagged = _report([StratumStat("flagged", 10, 10, 1.0, 1.0, 0.7, 1.0)], point=1.0)
    assert "audit stratum is empty" in accuracy_caption(only_flagged).lower()


def test_accuracy_caption_does_not_warn_when_the_audit_stratum_is_present():
    """Mirror of the test above. A caption that always warned would pass it and say nothing."""
    assert "audit stratum is empty" not in accuracy_caption(_report([FLAGGED, AUDIT])).lower()


def test_accuracy_metric_refuses_anything_that_is_not_a_number():
    assert accuracy_metric(0.8333) == "83.3%"
    with pytest.raises(ValueError):
        accuracy_metric(XSS)


def test_accuracy_floor_notice_refuses_anything_that_is_not_a_number():
    """Mirror of the guard above: `int(report.n)` fails closed on adversarial input, which
    is what lets this formatter sit in VETTED_FORMATTERS beside accuracy_metric."""
    bad = AccuracyReport(n=XSS, point=1.0, lo=0.2, hi=1.0, effective_n=1.0, strata=[])
    with pytest.raises(ValueError):
        accuracy_floor_notice(bad)


def test_latency_caption_requires_seven_buckets_before_claiming_a_trend():
    assert "not enough" in latency_caption(3).lower()
    assert str(MIN_BUCKETS) in latency_caption(3)
    assert "not enough" not in latency_caption(14).lower()
    assert "not enough" in latency_caption(MIN_BUCKETS - 1).lower()
    assert "not enough" not in latency_caption(MIN_BUCKETS).lower()


def test_drift_caption_names_the_threshold_and_the_alerting_labels():
    caption = drift_caption(["toxic", "threat"], alert_psi=0.2)
    assert "0.2" in caption
    assert "toxic" in caption and "threat" in caption
    assert "no label" in drift_caption([], alert_psi=0.2).lower()


def test_drift_caption_names_the_threshold_it_was_given_not_a_constant():
    assert "0.35" in drift_caption(["toxic"], alert_psi=0.35)


def test_drift_caption_refuses_a_label_this_model_does_not_score():
    """The only strings that reach this sentence are label names, and the label set is
    closed. An unknown one is dropped rather than interpolated."""
    caption = drift_caption(["toxic", XSS], alert_psi=0.2)
    assert "toxic" in caption
    assert XSS not in caption


def test_user_caption_says_it_is_self_selected_and_not_the_graded_estimate():
    caption = user_caption(UserPanel(n=40, agree=30, rate=0.75, lo=0.60, hi=0.86))
    assert "self-selected" in caption.lower()
    assert "n=40" in caption
    assert "not" in caption.lower()
    assert "not enough" in user_caption(UserPanel(0, 0, None, None, None)).lower()


def test_window_caption_states_the_seeded_share_and_the_pinned_threshold_digest():
    caption = window_caption(_snapshot(total=2000, seeded=2000, statuses={"pending": 4}))
    assert "2000 predictions" in caption
    assert "2000 are replayed" in caption
    assert "4 pending" in caption
    assert "0 reviewed" in caption
    assert "sha256:deadbeef1234" in caption


def test_window_caption_refuses_a_digest_that_is_not_a_digest():
    caption = window_caption(_snapshot(digest=XSS))
    assert XSS not in caption
    assert "missing" in caption


def test_the_reference_failure_names_the_error_class_and_never_its_message():
    """A BaselineMissingError's message carries a filesystem path; a contract failure's
    carries a value read out of the artifact. Neither belongs on a public screenshot."""
    assert "BaselineMissingError" in reference_error_caption("BaselineMissingError")
    assert reference_error_caption(f"Whoops {XSS}") == REFERENCE_UNAVAILABLE
    assert XSS not in reference_error_caption(XSS)
    assert reference_error_caption(None) == REFERENCE_UNAVAILABLE


# --------------------------------------------------------------------------------------
# Rendering, including every degenerate shape a demo database can be in
# --------------------------------------------------------------------------------------


class _Recorder:
    """Records what was drawn. Every attribute is callable and returns another recorder, so
    a chained charting API cannot make this fake the reason a test passes."""

    def __init__(self, log: list[tuple[str, tuple, dict]], name: str = "") -> None:
        self._log = log
        self._name = name

    def __getattr__(self, attribute: str) -> "_Recorder":
        return _Recorder(self._log, f"{self._name}.{attribute}".lstrip("."))

    def __call__(self, *args, **kwargs) -> "_Recorder":
        self._log.append((self._name, args, kwargs))
        return _Recorder(self._log, self._name)


@pytest.fixture()
def drawn(monkeypatch):
    log: list[tuple[str, tuple, dict]] = []
    for name in ("streamlit", "altair"):
        module = types.ModuleType(name)
        module.__getattr__ = lambda attribute, _log=log: _Recorder(_log, attribute)  # type: ignore[method-assign]
        monkeypatch.setitem(sys.modules, name, module)
    yield log


def _calls(log, name: str) -> list[tuple]:
    return [entry for entry in log if entry[0] == name]


def _captions(log) -> str:
    return " ".join(str(entry[1][0]) for entry in log if entry[0] == "caption" and entry[1])


def _snapshot(**overrides) -> Snapshot:
    base = {
        "window_days": 14,
        "total": 0,
        "seeded": 0,
        "statuses": {},
        "thresholds_digest": "deadbeef1234",
        "latency": [],
        "accuracy": AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[]),
        "panel": UserPanel(0, 0, None, None, None),
    }
    if "digest" in overrides:
        base["thresholds_digest"] = overrides.pop("digest")
    base.update(overrides)
    return Snapshot(**base)


def _bucket(day: int) -> LatencyBucket:
    import datetime as dt

    return LatencyBucket(
        bucket=dt.datetime(2026, 8, 1, tzinfo=dt.UTC) + dt.timedelta(days=day),
        n=10,
        p50=20.0,
        p95=40.0,
    )


def _drift(
    rate: float = 0.0,
    alert: bool = False,
    n: int | None = None,
    live_n: int | None = None,
) -> list[DriftRow]:
    return [
        DriftRow(label=label, baseline_rate=0.1, production_rate=rate, psi=0.0, js=0.0,
                 alert=alert, n=n, live_n=live_n)
        for label in LABELS
    ]


def test_the_recorder_records(drawn):
    """Non-vacuity: a fake that swallowed every call would make every assertion below
    vacuously true."""
    import streamlit as st

    st.caption("hello")
    assert _calls(drawn, "caption") == [("caption", ("hello",), {})]


def test_an_empty_database_renders_every_panel_instead_of_raising(drawn):
    """C5 in one test. Nothing has ever been predicted, reviewed, or fed back."""
    render(_snapshot())

    assert len(_calls(drawn, "header")) == 3, "a graded panel is missing from an empty page"
    assert _calls(drawn, "line_chart") == []
    assert _calls(drawn, "metric") == []
    assert _calls(drawn, "dataframe") == []
    captions = _captions(drawn).lower()
    assert "not enough history" in captions
    assert "not enough reviewed items" in captions
    assert "not enough user feedback" in captions
    assert "nan" not in captions


def test_one_row_renders_without_claiming_a_trend(drawn):
    render(
        _snapshot(
            total=1,
            latency=[_bucket(0)],
            accuracy=AccuracyReport(
                n=1, point=1.0, lo=0.2065, hi=1.0, effective_n=1.0,
                strata=[StratumStat("flagged", 1, 1, 1.0, 1.0, 0.2065, 1.0)],
            ),
            panel=UserPanel(1, 1, 1.0, 0.2065, 1.0),
        )
    )
    captions = _captions(drawn)
    assert "1 daily bucket(s)" in captions
    assert "audit stratum is empty" in captions.lower()
    assert "nan" not in captions.lower()
    assert "inf" not in captions.lower()


def test_an_all_one_class_accuracy_renders_a_bounded_interval(drawn):
    """Every reviewed item agreed. The interval must still be inside [0, 1] and the metric
    must still be a percentage rather than a NaN."""
    render(
        _snapshot(
            accuracy=AccuracyReport(
                n=50, point=1.0, lo=0.9285, hi=1.0, effective_n=50.0,
                strata=[
                    StratumStat("flagged", 40, 40, 1.0, 1.0, 0.9118, 1.0),
                    StratumStat("random-audit", 10, 10, 0.05, 1.0, 0.7225, 1.0),
                ],
            )
        )
    )
    assert _calls(drawn, "metric")[0][1][1] == "100.0%"
    assert "100.0% (95% CI 92.8%-100.0%)" in _captions(drawn)


def test_below_the_accuracy_floor_renders_the_notice_and_withholds_the_metric(drawn):
    """A point estimate exists but n is below MIN_REVIEWED_FOR_ESTIMATE: the notice must
    render and the headline metric must not."""
    render(
        _snapshot(
            accuracy=AccuracyReport(
                n=MIN_REVIEWED_FOR_ESTIMATE - 1, point=1.0, lo=0.6, hi=1.0,
                effective_n=float(MIN_REVIEWED_FOR_ESTIMATE - 1),
                strata=[
                    StratumStat(
                        "flagged", MIN_REVIEWED_FOR_ESTIMATE - 1, MIN_REVIEWED_FOR_ESTIMATE - 1,
                        1.0, 1.0, 0.6, 1.0,
                    )
                ],
            )
        )
    )
    assert _calls(drawn, "metric") == []
    assert len(_calls(drawn, "info")) == 1
    assert str(MIN_REVIEWED_FOR_ESTIMATE - 1) in str(_calls(drawn, "info")[0][1][0])


def test_at_the_accuracy_floor_renders_the_metric_and_withholds_the_notice(drawn):
    """n at MIN_REVIEWED_FOR_ESTIMATE: the headline metric must render and the notice must
    not. This is the case that catches two independent `if` statements standing in for
    `if`/`elif` in `render`: with two ifs, `point is not None` holds for any n >= 1, so both
    the metric and the notice would draw here."""
    render(
        _snapshot(
            accuracy=AccuracyReport(
                n=MIN_REVIEWED_FOR_ESTIMATE, point=1.0, lo=0.9, hi=1.0,
                effective_n=float(MIN_REVIEWED_FOR_ESTIMATE),
                strata=[
                    StratumStat(
                        "flagged", MIN_REVIEWED_FOR_ESTIMATE, MIN_REVIEWED_FOR_ESTIMATE,
                        1.0, 1.0, 0.9, 1.0,
                    )
                ],
            )
        )
    )
    assert _calls(drawn, "metric")[0][1][1] == "100.0%"
    assert _calls(drawn, "info") == []


def test_a_stratum_with_no_samples_is_simply_absent_and_the_page_says_so(drawn):
    """The random-audit stratum is empty because RANDOM_AUDIT_RATE was left at zero. The
    estimate is then blind to confidently-allowed false negatives, and must say so rather
    than divide by a zero-sized stratum."""
    render(
        _snapshot(
            accuracy=AccuracyReport(
                n=10, point=0.5, lo=0.24, hi=0.76, effective_n=10.0,
                strata=[StratumStat("flagged", 10, 5, 1.0, 0.5, 0.24, 0.76)],
            )
        )
    )
    assert "WARNING: the random-audit stratum is empty" in _captions(drawn)
    assert len(_calls(drawn, "dataframe")) == 1


def test_a_drift_window_with_no_predictions_renders_zero_rates(drawn):
    """Every production rate is 0.0 because nothing was predicted. The panel draws; it does
    not divide by the window's row count."""
    render(_snapshot(drift=_drift(rate=0.0), flags=None))
    assert len(_calls(drawn, "altair_chart")) == 1
    assert "No label exceeds the PSI alert threshold" in _captions(drawn)


def test_a_missing_drift_reference_withholds_the_panel_and_says_why(drawn):
    render(_snapshot(reference_error="BaselineMissingError"))
    assert _calls(drawn, "altair_chart") == []
    assert len(_calls(drawn, "error")) == 1
    assert "Drift reference unavailable" in str(_calls(drawn, "error")[0][1][0])


def test_a_single_flag_rate_bucket_does_not_get_a_trend_line(drawn):
    """One bucket is a point, not a series; `len(flags) > 1` is what keeps it off the page."""
    import pandas as pd

    one = pd.DataFrame([{"bucket": 1, **dict.fromkeys(LABELS, 0.0)}])
    render(_snapshot(drift=_drift(), flags=one))
    assert _calls(drawn, "line_chart") == []

    two = pd.DataFrame([{"bucket": i, **dict.fromkeys(LABELS, 0.0)} for i in (1, 2)])
    render(_snapshot(drift=_drift(), flags=two))
    assert len(_calls(drawn, "line_chart")) == 1


def test_the_alerting_labels_reach_the_caption(drawn):
    render(_snapshot(drift=_drift(rate=0.9, alert=True)))
    caption = _captions(drawn)
    assert "PSI >=" in caption
    for label in LABELS:
        assert label in caption


def test_alerting_labels_is_empty_when_there_is_no_drift_panel():
    assert alerting_labels(_snapshot()) == []
    assert alerting_labels(_snapshot(drift=_drift(alert=False))) == []
    assert alerting_labels(_snapshot(drift=_drift(alert=True))) == list(LABELS)


def test_render_wires_the_live_sample_size_into_the_drift_caption(drawn):
    """`render` has to pass `live_n=` through to `drift_caption`, not just `n=`. A snapshot
    whose rows carry `n=2048, live_n=5` -- 2043 replayed rows, 5 live -- can only produce the
    seed-dominated "wiring check" wording if that keyword argument survives the call in
    `render`. Deleting it silently falls back to `drift_caption`'s `live_n=None` default,
    which reports this exact seed-dominated window as an ordinary 2048-prediction
    measurement with no caveat at all -- the regression `test_drift_seeded_separation.py`
    cannot catch, because it calls `drift_caption` directly and never exercises `render`."""
    render(_snapshot(drift=_drift(n=2048, live_n=5)))
    caption = _captions(drawn)
    assert "wiring check" in caption
    assert "2048" in caption
    assert "Only 5 are live traffic" in caption
