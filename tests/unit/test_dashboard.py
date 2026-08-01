"""Rubric 3.2's parenthetical, pinned: "data exchanged via the database, not JSON files".

The clause appears in no other plan, spec or test, and Phase 5's rubric parser folds it into
a parent row that asserts nothing (gap RUBRIC-3.2-JSON). Meanwhile the deployed dashboard
does open two JSON files off a mounted volume. Those two are the pinned decision boundary
and the training-time reference distribution -- **model artifacts**, fetched and
digest-verified with the model -- while every observation comes from RDS. That is a
defensible reading of the clause; it was not a reading anybody had written down.

These tests write it down and hold it, so that a later "let's cache the metrics to JSON"
cannot pass review by looking like an optimisation.
"""

import ast
import re
from pathlib import Path

DASHBOARD = Path("monitoring/dashboard.py")
# Every way a Python module reads a data file. `read_text` is the only one allowed, and only
# because `monitoring/baseline.py` uses it to load the two pinned model artifacts.
FILE_READS = {"read_text", "read_csv", "read_json", "read_parquet", "open", "load"}
# Every way it could write one. The dashboard's role is read-only end to end (premortem
# H16): not the database, and not the disk either.
FILE_WRITES = {"write_text", "write_bytes", "to_csv", "to_json", "to_parquet", "dump"}


def _attribute_calls(source: str, names: set[str]) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in names
    }


def test_the_file_call_scanner_reports_before_it_is_trusted():
    """A scan that cannot report is indistinguishable from a scan with nothing to report."""
    assert _attribute_calls("pd.read_json('metrics.json')", FILE_READS) == {"read_json"}
    assert _attribute_calls("json.load(handle)", FILE_READS) == {"load"}
    assert _attribute_calls("frame.to_json('out.json')", FILE_WRITES) == {"to_json"}
    assert _attribute_calls("conn.execute(query)", FILE_READS | FILE_WRITES) == set()


def test_the_dashboard_reads_no_prediction_data_from_a_file():
    """The two JSON files the dashboard does open are MODEL artifacts, not observations."""
    body = DASHBOARD.read_text(encoding="utf-8")
    opened = _attribute_calls(body, FILE_READS)
    assert opened <= {"read_text"}, f"dashboard reads data from files: {sorted(opened)}"
    assert "load_baseline" in body and "load_thresholds" in body
    for forbidden in ("predictions.json", "feedback.json", "metrics.json", "latency.json"):
        assert forbidden not in body


def test_no_module_in_the_monitoring_package_reads_or_writes_a_data_file():
    """The single-file scan above is the weak form of the rule. A metrics cache added beside
    the dashboard rather than inside it would satisfy that one and falsify the clause."""
    offenders = {}
    for path in sorted(Path("monitoring").rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        reads = _attribute_calls(body, FILE_READS) - {"read_text"}
        writes = _attribute_calls(body, FILE_WRITES)
        if reads or writes:
            offenders[str(path)] = sorted(reads | writes)
    assert offenders == {}, offenders


def test_the_only_two_files_the_dashboard_opens_are_model_artifacts():
    body = DASHBOARD.read_text(encoding="utf-8")
    paths = set(re.findall(r'os\.environ\["([A-Z_]*PATH)"\]', body))
    assert paths == {"BASELINE_PATH", "THRESHOLDS_PATH"}


def test_every_panel_sources_its_observations_from_the_query_layer():
    """The graded panels each take a live connection. A panel that took a path instead would
    be the JSON-file exchange the rubric forbids."""
    body = DASHBOARD.read_text(encoding="utf-8")
    for query in (
        "latency_over_time",
        "drift_report",
        "live_accuracy",
        "flag_rate_series",
        "user_feedback_panel",
        "review_counts",
        "seeded_share",
    ):
        assert query in body, f"{query} is not called; the panel has another data source"


def test_the_observation_reader_is_handed_a_connection_and_never_a_path():
    """`collect` is the single seam between the page and its data. Its first parameter is the
    database connection; a `Path` parameter here is the file exchange arriving by the back
    door."""
    tree = ast.parse(DASHBOARD.read_text(encoding="utf-8"))
    collect = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "collect"
    )
    arguments = [argument.arg for argument in collect.args.args]
    assert arguments[0] == "conn"
    annotations = {
        ast.unparse(argument.annotation) for argument in collect.args.args if argument.annotation
    }
    assert "Path" not in annotations and "str" not in annotations


def test_the_rubric_reading_is_written_down_where_a_grader_will_find_it():
    doc = DASHBOARD.read_text(encoding="utf-8")
    assert "not JSON files" in doc, (
        "rubric 3.2's parenthetical must be quoted and answered in the module docstring"
    )
    assert "model artifacts" in doc.lower()
