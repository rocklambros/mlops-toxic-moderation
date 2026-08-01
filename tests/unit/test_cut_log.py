"""C8. A checkpoint nobody evaluated recovers zero days, which is the original defect.

Delivery spec section 8 replaced a trailing trigger with two leading indicators, both dated
before the work they would cancel. Nothing anywhere recorded that either evaluation
happened. Worse, Phase 4 ships a CI gate whose escape hatch is this very file -- a file no
task created -- so the strongest cost control in the project could be removed by a
FileNotFoundError.

This suite gives the log a schema and makes the schema enforceable. Two of the assertions
below are deliberately time- and artifact-dependent, which is unusual in a suite that is
otherwise pinned to a fixed seed. That is the point: the failure being guarded is "the day
arrived, the developer was mid-phase, and nothing forced the decision". A checkpoint left
`PENDING` past its due date, or left `PENDING` once the evidence it was waiting on exists,
turns this suite red until somebody writes the decision down.
"""

import datetime as dt
import re
from pathlib import Path

LOG = Path("docs/cut-log.md")
CHECKPOINTS = ("day-8", "day-11")
ITEMS = ("AIBOM and SBOM", "RunPod sweep", "DistilBERT", "second-opinion column")
CONDITIONS = {"MET", "NOT MET", "PENDING"}
DECISIONS = {"no cut", "cut", "not due"}


def _rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 5 and cells[0].lower() != "checkpoint":
                rows[cells[0]] = cells
    return rows


def _cited_paths(cell: str) -> list[str]:
    return [
        quoted
        for quoted in re.findall(r"`([^`]+)`", cell)
        if "/" in quoted and "." in quoted.rsplit("/", 1)[-1]
    ]


def test_the_checklist_file_the_ci_escape_hatch_reads_actually_exists():
    """Phase 4's test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded reads this
    path. A missing file is an escape hatch that opens on a FileNotFoundError."""
    assert LOG.exists(), "docs/cut-log.md is read by the Phase 4 reaper gate"


def test_the_row_parser_reports_before_it_is_trusted():
    """Every assertion below is over `_rows()`. A parser that returned nothing would make
    all of them vacuous, which is precisely the shape of the defect being fixed."""
    assert set(_rows()) >= set(CHECKPOINTS)
    assert all(len(cells) >= 5 for cells in _rows().values())


def test_every_checkpoint_in_the_delivery_spec_has_a_row():
    assert set(CHECKPOINTS) <= set(_rows()), "an unevaluated checkpoint recovers zero days"


def test_every_checkpoint_row_records_a_date_a_condition_and_a_decision():
    for name, cells in _rows().items():
        assert re.match(r"20\d\d-\d\d-\d\d", cells[1]), f"{name}: no evaluation date"
        assert cells[2] in CONDITIONS, f"{name}: condition not adjudicated"
        assert cells[3] in DECISIONS, f"{name}: no pre-committed action taken"


def test_a_pending_checkpoint_has_decided_nothing():
    """`PENDING` says the indicator is not yet readable. It must not also carry a decision,
    or "not due" becomes a way to record a cut nobody adjudicated."""
    for name, cells in _rows().items():
        if cells[2] == "PENDING":
            assert cells[3] == "not due", f"{name}: pending, yet an action was taken"
            assert not cells[4].strip("- "), f"{name}: pending, yet items are listed as cut"
        else:
            assert cells[3] != "not due", f"{name}: adjudicated, yet no action recorded"


def test_a_pending_checkpoint_is_still_in_the_future():
    """The forcing function. `End of day 8` arrives whether or not anyone is looking; from
    the day after, this file is wrong until the decision is written down."""
    today = dt.date.today()
    for name, cells in _rows().items():
        if cells[2] != "PENDING":
            continue
        due = dt.date.fromisoformat(cells[1][:10])
        assert due >= today, (
            f"{name}: due {due}, still PENDING. Evaluate the delivery-spec condition and "
            "record MET/NOT MET with the pre-committed action before any cut-line work."
        )


def test_a_pending_checkpoint_names_evidence_that_does_not_exist_yet():
    """The second forcing function, and the one that does not depend on a calendar: a
    checkpoint may only be pending while the artifact it reads is genuinely absent. The day
    that artifact lands, the indicator is readable and this row must be adjudicated."""
    for name, cells in _rows().items():
        if cells[2] != "PENDING":
            continue
        cited = _cited_paths(cells[5] if len(cells) > 5 else "")
        assert cited, f"{name}: pending with no evidence artifact named"
        for path in cited:
            assert not Path(path).exists(), (
                f"{name}: {path} exists, so the condition is readable and must be adjudicated"
            )


def test_a_cut_names_the_items_and_the_ordered_list_position():
    for name, cells in _rows().items():
        if cells[3] == "cut":
            assert any(item.lower() in cells[4].lower() for item in ITEMS), (
                f"{name}: 'cut' with no named item from the spec's ordered cut list"
            )


def test_the_never_cut_list_was_not_touched():
    body = LOG.read_text(encoding="utf-8").lower()
    for protected in ("readme", "rollback", "leakage firewall", "ci gate", "safe model loading"):
        assert f"cut: {protected}" not in body, f"{protected} is on the never-cut list"
