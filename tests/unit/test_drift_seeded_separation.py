"""The drift panel compared the seed against a baseline computed over the same rows.

`make seed-demo` replays the locked held-out split through /predict, and
`baseline_flag_rates.json` is computed over that same split. So PSI was measured between a
distribution and itself: zero by construction, unable to move, and reported in the same
voice as a measurement -- "No label exceeds the PSI alert threshold of 0.2, over 2000
predictions in the drift window."

The fix is not to filter the seed out and stop. That takes the window from 2048 rows to 48
and mutes a graded panel. It is to carry both series and let the caption say which one is a
wiring check and which one is evidence.
"""

import datetime as dt
from pathlib import Path

from model.labels import LABELS
from monitoring.baseline import load_baseline, load_thresholds
from monitoring.dashboard import drift_caption
from monitoring.queries import MIN_DRIFT_SAMPLES, drift_report, production_flag_rates

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
THRESHOLDS = load_thresholds(FIXTURES / "thresholds.json")
BASELINE = load_baseline(FIXTURES / "baseline_flag_rates.json")
SINCE = dt.datetime(2026, 8, 16, tzinfo=dt.UTC)


class SplitWindow:
    """A `predictions` window that answers differently for the whole set and the live subset.

    It decides which to return by looking for the `is_seed` predicate in the SQL, so a change
    that stops emitting the predicate cannot pass by accident -- the fake would answer with
    the full-window row and the live counts would be wrong.
    """

    def __init__(self, total: int, live: int, flagged: dict[str, int] | None = None) -> None:
        self.total, self.live = total, live
        self.flagged = flagged or {}
        self.statements: list[str] = []
        self._last = ""

    def execute(self, statement, parameters=None):
        self._last = str(statement)
        self.statements.append(self._last)
        return self

    def mappings(self):
        return self

    def one(self):
        n = self.live if "NOT is_seed" in self._last else self.total
        row = {"n": n} | {f"flag_{label}": 0 for label in LABELS}
        for label, count in self.flagged.items():
            row[f"flag_{label}"] = min(count, n)
        return row


def test_the_live_filter_reaches_the_sql_and_changes_the_count():
    window = SplitWindow(total=2000, live=48)
    all_n, _ = production_flag_rates(window, SINCE, THRESHOLDS)
    live_n, _ = production_flag_rates(window, SINCE, THRESHOLDS, seeded=False)
    assert all_n == 2000
    assert live_n == 48
    assert any("NOT is_seed" in s for s in window.statements)


def test_the_unfiltered_call_emits_no_seed_predicate():
    """Backward compatibility: the default must be the query that already shipped."""
    window = SplitWindow(total=2000, live=48)
    production_flag_rates(window, SINCE, THRESHOLDS)
    assert not any("is_seed" in s for s in window.statements)


def test_drift_rows_carry_the_live_denominator_beside_the_full_one():
    window = SplitWindow(total=2000, live=48, flagged={"toxic": 200})
    rows = {row.label: row for row in drift_report(window, SINCE, THRESHOLDS, BASELINE)}
    assert rows["toxic"].n == 2000
    assert rows["toxic"].live_n == 48


def test_a_seed_dominated_window_is_not_reported_as_a_drift_finding():
    """The exact live shape on 2026-08-11: 2048 rows, 2000 of them replayed reference data."""
    caption = drift_caption([], alert_psi=0.2, n=2048, live_n=20)
    assert "replayed" in caption
    assert "20" in caption
    assert "No label exceeds" not in caption, (
        "a window whose rows ARE the reference distribution cannot support that claim"
    )


def test_a_window_with_enough_live_traffic_still_reports_normally():
    caption = drift_caption([], alert_psi=0.2, n=2048, live_n=MIN_DRIFT_SAMPLES + 1)
    assert "No label exceeds" in caption


def test_an_unseeded_window_is_unaffected():
    """live_n == n means nothing was replayed; the caption must not gain a caveat."""
    caption = drift_caption([], alert_psi=0.2, n=500, live_n=500)
    assert "No label exceeds" in caption
    assert "replayed" not in caption


def test_the_caption_still_handles_a_caller_that_records_no_live_count():
    caption = drift_caption([], alert_psi=0.2, n=500)
    assert "No label exceeds" in caption
