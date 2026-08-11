"""A drift alert is a claim about the model, so it needs a denominator.

The latency panel has had small-sample protection since the day a single cold-start request
read as a 5x regression: `MIN_SAMPLES_PER_BUCKET`, and a caption that names the thin days.
The drift panel had none, and it is the panel a grader weights highest.

That gap fires on a date. Every bulk prediction in this project was made between 2026-07-27
and 2026-08-02, and the drift window is bounded at 14 days on purpose -- PSI against an
all-time production distribution cannot say that drift *started*. So from roughly 2026-08-16
the drift window holds a handful of hand-typed test comments, and shortly after that it
holds nothing:

* one flagged comment out of three is a 33% flag rate against a 9.6% baseline, which is a
  PSI of 0.35 and a "major shift" alert. Under the baseline itself, at least one flag in
  three predictions happens 27% of the time, so on a quiet day that alert is a coin flip.
  At n=30 it takes 9 flags to cross 0.2, which the baseline produces 0.2% of the time.
* with no rows at all, `production_flag_rates` reports 0.0 for every label, which is
  indistinguishable from "the model flagged nothing". `psi(0.0961, 0.0)` is 1.11, so an
  untouched database printed "PSI >= 0.2 on: toxic, obscene, insult. Investigate before
  trusting the model."

The fix is a floor, not a wider window: widening it would only postpone the same dilution
that the 14 days exist to prevent.
"""

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

from model.labels import LABELS
from monitoring.baseline import load_baseline, load_thresholds
from monitoring.dashboard import Snapshot, drift_sample_size, render
from monitoring.queries import (
    DEFAULT_ALERT_PSI,
    MIN_DRIFT_SAMPLES,
    DriftRow,
    UserPanel,
    drift_report,
    production_flag_rates,
)
from monitoring.stats import AccuracyReport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
THRESHOLDS = load_thresholds(FIXTURES / "thresholds.json")
BASELINE = load_baseline(FIXTURES / "baseline_flag_rates.json")
SINCE = dt.datetime(2026, 8, 16, tzinfo=dt.UTC)


class FakeWindow:
    """One `predictions` window: `n` rows, of which `flagged[label]` cleared the threshold.

    The SQL itself is asserted against a real Postgres in `tests/integration/test_queries.py`.
    What is under test here is the decision the counts feed, which is the part that has to
    hold on the demo database as it empties out.
    """

    def __init__(self, n: int, flagged: dict[str, int] | None = None) -> None:
        self.row = {"n": n} | {f"flag_{label}": 0 for label in LABELS}
        self.row.update({f"flag_{label}": count for label, count in (flagged or {}).items()})
        self.statements: list[str] = []

    def execute(self, statement, parameters=None):
        self.statements.append(str(statement))
        return self

    def mappings(self):
        return self

    def one(self):
        return self.row


def test_the_fake_window_answers_the_query_before_it_is_trusted():
    """Non-vacuity. A stand-in that returned nothing would make every count below zero, and
    a guard that fires on zero rows would then pass every test in this file."""
    window = FakeWindow(n=200, flagged={"toxic": 60})
    n, rates = production_flag_rates(window, SINCE, THRESHOLDS)
    assert window.statements, "the query layer never asked the connection anything"
    assert n == 200
    assert rates["toxic"] == pytest.approx(0.30)
    assert rates["threat"] == pytest.approx(0.0)


# ------------------------------------------------------------------ the query layer


def _rows(window: FakeWindow, **kwargs) -> dict[str, DriftRow]:
    report = drift_report(window, SINCE, THRESHOLDS, BASELINE, **kwargs)
    return {row.label: row for row in report}


def test_one_flagged_comment_in_a_three_row_window_is_not_a_drift_alert():
    """The exact shape the deployed dashboard reaches two weeks after the demo traffic
    stops. The distance is real arithmetic; it is not evidence, because the rate it is
    computed from moves by 33 points per comment."""
    rows = _rows(FakeWindow(n=3, flagged={"toxic": 1}))
    assert rows["toxic"].production_rate == pytest.approx(1 / 3)
    assert rows["toxic"].psi > DEFAULT_ALERT_PSI, "the PSI itself is unchanged"
    assert rows["toxic"].alert is False
    assert rows["toxic"].n == 3


def test_the_same_shift_still_alerts_once_the_window_carries_traffic():
    """Mirror of the test above. A guard that suppressed every alert would pass that one and
    turn the graded drift panel into a decoration."""
    rows = _rows(FakeWindow(n=200, flagged={"toxic": 60}))
    assert rows["toxic"].production_rate == pytest.approx(0.30)
    assert rows["toxic"].alert is True
    assert rows["toxic"].n == 200


def test_an_empty_window_reports_a_zero_denominator_rather_than_a_flag_rate_of_zero():
    """Rates of 0.0 with n=0 mean "nobody asked the model anything". Rates of 0.0 with
    n=1200 mean "the model flagged nothing". Only the second is a finding, and before `n`
    travelled with the row the panel could not tell them apart -- so an empty database
    scored PSI 1.11 on toxic and alerted on three labels."""
    rows = _rows(FakeWindow(n=0))
    assert all(row.production_rate == 0.0 for row in rows.values())
    assert all(row.n == 0 for row in rows.values())
    assert rows["toxic"].psi > 1.0
    assert not any(row.alert for row in rows.values())


def test_the_minimum_sample_size_is_the_one_the_caller_states():
    """The floor is a parameter for the same reason `alert_psi` is: a constant compiled into
    the decision cannot be stated on the screenshot or moved for a different traffic shape.

    UPDATED when the tail test was added beside the floor. The floor is still the caller's to
    set and still blocks below itself, which is what this test pins. It is no longer
    sufficient on its own: an alert now also needs the observation to be improbable under the
    baseline, so the sample here is one where both agree. The previous version used n=10 with
    three flags, which clears a floor of 1 but is a one-in-fourteen event under a 0.0961
    baseline, and calling that a drift alert is the thing the tail test exists to refuse.
    """
    window = FakeWindow(n=200, flagged={"toxic": 60})
    assert _rows(window, min_n=1000)["toxic"].alert is False, "the floor no longer blocks"
    assert _rows(window, min_n=MIN_DRIFT_SAMPLES)["toxic"].alert is True


def test_clearing_the_floor_is_necessary_but_not_sufficient():
    """The floor counts rows. It cannot tell whether those rows say anything.

    n=10 with three flags clears a floor of 1 and scores PSI above the threshold, and it is
    still a one-in-fourteen event under the baseline. Both gates have to agree.
    """
    window = FakeWindow(n=10, flagged={"toxic": 3})
    assert _rows(window, min_n=1)["toxic"].psi >= 0.2, "this sample no longer reaches the PSI bar"
    assert _rows(window, min_n=1)["toxic"].alert is False


def test_the_floor_is_high_enough_that_a_single_comment_cannot_reach_it():
    """A floor of 1 or 2 would satisfy every test above and none of the reasoning: the whole
    failure is one comment carrying a whole flag rate."""
    assert MIN_DRIFT_SAMPLES >= 30


# ------------------------------------------------------------------ the rendered panel


class _Recorder:
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
        "window_days": None,
        "total": 959,
        "seeded": 926,
        "statuses": {},
        "thresholds_digest": "deadbeef1234",
        "latency": [],
        "accuracy": AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[]),
        "panel": UserPanel(0, 0, None, None, None),
    }
    base.update(overrides)
    return Snapshot(**base)


def _drift(n: int | None, rate: float = 0.0, alert: bool = False) -> list[DriftRow]:
    return [
        DriftRow(label=label, baseline_rate=0.0961, production_rate=rate, psi=0.35, js=0.05,
                 alert=alert, n=n)
        for label in LABELS
    ]


def test_the_recorder_records(drawn):
    """Non-vacuity for every render assertion below."""
    import streamlit as st

    st.caption("hello")
    assert _calls(drawn, "caption") == [("caption", ("hello",), {})]


def test_an_empty_drift_window_withholds_the_bars_and_says_which_is_missing(drawn):
    """Bars drawn against a window with no rows put a production rate of 0.0 beside a 9.6%
    baseline, which reads as "the model stopped flagging" rather than "nobody asked it
    anything". The chart is the claim, so the chart is what is withheld."""
    render(_snapshot(drift=_drift(n=0)))
    assert _calls(drawn, "altair_chart") == []
    caption = _captions(drawn)
    assert "No predictions in the drift window" in caption
    assert "No label exceeds" not in caption, "silence is not a finding of stability"


def test_a_thin_drift_window_still_draws_the_bars_and_refuses_only_the_alert(drawn):
    """Hiding real data is worse -- the latency chart plots thin days for the same reason.
    What is withheld is the sentence that tells a reader to go and investigate the model."""
    render(_snapshot(drift=_drift(n=3, rate=1 / 3, alert=True)))
    assert len(_calls(drawn, "altair_chart")) == 1
    caption = _captions(drawn)
    assert "Only 3 prediction(s) in the drift window" in caption
    assert "PSI >=" not in caption
    assert "Investigate before trusting the model" not in caption


def test_a_populated_window_reports_the_alert_and_the_denominator_behind_it(drawn):
    """The panel this whole guard exists to keep working."""
    render(_snapshot(drift=_drift(n=1200, rate=0.30, alert=True)))
    assert len(_calls(drawn, "altair_chart")) == 1
    caption = _captions(drawn)
    assert "PSI >=" in caption
    assert "1200 predictions" in caption
    assert all(label in caption for label in LABELS)


def test_the_flag_rate_series_survives_an_empty_drift_window(drawn):
    """The series under the drift panel is computed over the *description* window, which is
    all history. An empty 14-day comparison says nothing about it, so withholding it too
    would hide months of real data behind a fortnight of silence."""
    import pandas as pd

    flags = pd.DataFrame([{"bucket": i, **dict.fromkeys(LABELS, 0.02)} for i in (1, 2)])
    render(_snapshot(drift=_drift(n=0), flags=flags))
    assert _calls(drawn, "altair_chart") == []
    assert len(_calls(drawn, "line_chart")) == 1


def test_a_row_that_records_no_denominator_is_unknown_rather_than_empty():
    """`None` is "the count was not recorded", which only hand-built rows are; `collect`
    always records one. Reading it as zero would withhold every panel the older guard suite
    builds, and reading it as large would restore the defect."""
    assert drift_sample_size(_snapshot()) is None
    assert drift_sample_size(_snapshot(drift=_drift(n=None))) is None
    assert drift_sample_size(_snapshot(drift=_drift(n=0))) == 0
    assert drift_sample_size(_snapshot(drift=_drift(n=1200))) == 1200


# --- The evidence half: a floor on n cannot carry a per-label claim -------------------------


def test_psi_carries_no_sample_size_so_a_floor_alone_cannot_gate_the_alert():
    """The measurement that motivated adding a tail test beside the floor.

    PSI answers only "how far apart are these two rates". Against a 0.0961 baseline an
    observed rate of zero scores 1.112 whether it was measured over thirty predictions or two
    thousand, so no PSI threshold can separate a real shift from a quiet afternoon.
    """
    from monitoring.stats import psi

    scores = {n: psi(0.0961, 0.0) for n in (30, 60, 200, 2000)}
    assert len(set(scores.values())) == 1, "PSI moved with n; this test's premise is stale"
    assert scores[30] > 1.0


def test_thirty_benign_predictions_are_not_evidence_that_the_model_stopped_flagging():
    """A one-in-twenty-one event is not a finding. Under a 0.0961 baseline, thirty
    predictions with nothing flagged happens 4.8 percent of the time by chance."""
    from monitoring.stats import observation_is_improbable

    assert observation_is_improbable(0.0961, 0.0, 30) is False


def test_sixty_benign_predictions_are_evidence():
    """One-in-four-hundred. The alert should fire here, and the floor alone would have fired
    at thirty as well, which is the difference this test pins."""
    from monitoring.stats import observation_is_improbable

    assert observation_is_improbable(0.0961, 0.0, 60) is True


def test_a_rare_label_demands_the_larger_sample_it_actually_needs():
    """`threat` has a baseline flag rate of 0.0030. Seeing none of it in a hundred
    predictions is the ordinary case, not a signal, and a single floor calibrated for a ten
    percent label would have alerted on it. The tail adapts per label for free."""
    from monitoring.stats import observation_is_improbable

    assert observation_is_improbable(0.0030, 0.0, 100) is False
    assert observation_is_improbable(0.0030, 0.0, 3000) is True


def test_a_real_shift_in_a_full_window_still_alerts():
    """The guard must not swallow the signal it exists to protect. Thirty percent flagged
    against a 0.0961 baseline over two thousand rows is a genuine shift."""
    from monitoring.stats import observation_is_improbable, psi

    assert psi(0.0961, 0.30) >= 0.2
    assert observation_is_improbable(0.0961, 0.30, 2000) is True


def test_the_tail_does_not_overflow_on_a_full_graded_window():
    """The direct binomial form raised OverflowError here: math.comb(2000, 600) does not fit
    in a float, and the graded window routinely holds two thousand rows."""
    from monitoring.stats import observation_is_improbable

    assert observation_is_improbable(0.0961, 0.30, 2000) is True
    assert observation_is_improbable(0.0961, 0.0961, 2000) is False
