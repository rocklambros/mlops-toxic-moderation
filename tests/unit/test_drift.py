"""The drift reference, and every way it is allowed to refuse to load.

A drift panel with no reference plots a production-only series that cannot answer whether
anything changed, and looks exactly like a working chart. So every malformed input below
must raise, not degrade.
"""

import json
from pathlib import Path

import pytest

from model.labels import LABELS
from monitoring.baseline import (
    Baseline,
    BaselineContractError,
    BaselineMissingError,
    load_baseline,
    load_thresholds,
)

# Anchored to this file, not to the working directory: a relative "tests/fixtures" turns
# `pytest tests/unit/test_drift.py` run from anywhere but the repo root into a
# BaselineMissingError, i.e. into the fail-closed path these tests are meant to isolate.
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _mutated_baseline(tmp_path: Path, mutate) -> Path:
    payload = json.loads((FIXTURES / "baseline_flag_rates.json").read_text())
    mutate(payload)
    bad = tmp_path / "baseline_flag_rates.json"
    bad.write_text(json.dumps(payload))
    return bad


def test_load_baseline_returns_all_six_rates():
    baseline = load_baseline(FIXTURES / "baseline_flag_rates.json")
    assert isinstance(baseline, Baseline)
    assert tuple(baseline.flag_rates) == LABELS
    assert baseline.flag_rates["threat"] == pytest.approx(0.0030)
    assert baseline.n == 23851
    assert baseline.model_version == "toxic-clf:v3"


def test_the_baseline_carries_the_provenance_the_footer_displays():
    """Rubric 3.2's drift number is only interpretable next to the data and model it was
    measured on. A reference with no provenance is a number with no denominator."""
    baseline = load_baseline(FIXTURES / "baseline_flag_rates.json")
    assert baseline.schema_version == 1
    assert len(baseline.data_version) == 64
    assert baseline.n > 0


def test_missing_baseline_fails_closed(tmp_path):
    """Without this, the drift panel plots a production-only series that cannot answer
    whether anything changed -- and looks identical to a working chart."""
    with pytest.raises(BaselineMissingError, match="baseline_flag_rates.json"):
        load_baseline(tmp_path / "baseline_flag_rates.json")


def test_a_directory_where_the_baseline_should_be_fails_closed(tmp_path):
    (tmp_path / "baseline_flag_rates.json").mkdir()
    with pytest.raises(BaselineMissingError, match="baseline_flag_rates.json"):
        load_baseline(tmp_path / "baseline_flag_rates.json")


def test_a_truncated_baseline_is_rejected_rather_than_read_as_empty(tmp_path):
    bad = tmp_path / "baseline_flag_rates.json"
    bad.write_text('{"schema_version": 1, "flag_rates": {"toxic": 0.1')
    with pytest.raises(BaselineContractError, match="not valid JSON"):
        load_baseline(bad)


def test_baseline_missing_a_label_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].pop("threat"))
    with pytest.raises(BaselineContractError, match="threat"):
        load_baseline(bad)


def test_baseline_rate_outside_unit_interval_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].__setitem__("toxic", 1.4))
    with pytest.raises(BaselineContractError, match="toxic"):
        load_baseline(bad)


def test_a_negative_baseline_rate_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].__setitem__("obscene", -0.01))
    with pytest.raises(BaselineContractError, match="obscene"):
        load_baseline(bad)


def test_a_non_numeric_baseline_rate_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].__setitem__("insult", "0.05"))
    with pytest.raises(BaselineContractError, match="insult"):
        load_baseline(bad)


def test_a_boolean_baseline_rate_is_rejected(tmp_path):
    """JSON `true` is an `int` to `isinstance`, so an unguarded numeric check reads it as a
    100% flag rate and reports a fabricated drift alert against it."""
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].__setitem__("toxic", True))
    with pytest.raises(BaselineContractError, match="toxic"):
        load_baseline(bad)


def test_a_baseline_for_a_different_label_set_is_rejected(tmp_path):
    """An extra label means the reference was produced by a model this dashboard is not
    monitoring. Silently ignoring it compares two different decision rules."""
    bad = _mutated_baseline(tmp_path, lambda p: p["flag_rates"].__setitem__("spam", 0.01))
    with pytest.raises(BaselineContractError, match="spam"):
        load_baseline(bad)


def test_a_baseline_with_no_flag_rates_object_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p.__setitem__("flag_rates", None))
    with pytest.raises(BaselineContractError, match="flag_rates"):
        load_baseline(bad)


def test_unknown_schema_version_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p.__setitem__("schema_version", 99))
    with pytest.raises(BaselineContractError, match="schema_version"):
        load_baseline(bad)


def test_an_absent_schema_version_is_rejected(tmp_path):
    bad = _mutated_baseline(tmp_path, lambda p: p.pop("schema_version"))
    with pytest.raises(BaselineContractError, match="schema_version"):
        load_baseline(bad)


def test_load_thresholds_returns_one_float_per_label():
    thresholds = load_thresholds(FIXTURES / "thresholds.json")
    assert tuple(thresholds) == LABELS
    assert all(0.0 < value < 1.0 for value in thresholds.values())
    assert thresholds["threat"] == pytest.approx(0.18)


def test_missing_thresholds_file_fails_closed(tmp_path):
    with pytest.raises(BaselineMissingError, match="thresholds.json"):
        load_thresholds(tmp_path / "thresholds.json")


def test_missing_threshold_label_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "thresholds.json").read_text())
    payload.pop("identity_hate")
    bad = tmp_path / "thresholds.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="identity_hate"):
        load_thresholds(bad)


def test_a_threshold_outside_the_unit_interval_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "thresholds.json").read_text())
    payload["toxic"] = 1.4
    bad = tmp_path / "thresholds.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="toxic"):
        load_thresholds(bad)


def test_the_committed_fixtures_share_one_label_set():
    """The reference series and the production series must be produced by the same decision
    rule, which they can only be if both files cover exactly the same labels."""
    baseline = load_baseline(FIXTURES / "baseline_flag_rates.json")
    thresholds = load_thresholds(FIXTURES / "thresholds.json")
    assert tuple(baseline.flag_rates) == tuple(thresholds) == LABELS
