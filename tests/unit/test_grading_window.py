"""The grading-window exception expires, and while it is open it must agree with the code.

`infra/terraform/grading.auto.tfvars` disarms the nightly stop so the graded stack is up
whenever the instructor looks at it. That is a deliberate exception to premortem H7, whose
whole point is that the SCP caps the hourly *rate* and says nothing about *duration*: three
allowlisted instances plus RDS left running reach the $100 ceiling inside a month without a
single policy violation. An exception to a cost control is fine. An exception to a cost
control that nobody remembers making is how the ceiling gets hit.

So these tests do two things a comment cannot: they fail once the window has closed, and
they fail if the file stops saying what it claims to say.
"""

import datetime as dt
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TFVARS = REPO / "infra/terraform/grading.auto.tfvars"


def _restore_after(text: str) -> dt.date:
    match = re.search(r"^#\s*RESTORE_AFTER:\s*(\d{4}-\d{2}-\d{2})\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(
            "grading.auto.tfvars carries no `# RESTORE_AFTER: YYYY-MM-DD` line. An override "
            "of a cost control with no expiry is a permanent change wearing a temporary label"
        )
    return dt.date.fromisoformat(match.group(1))


@pytest.mark.skipif(not TFVARS.is_file(), reason="grading window already closed; file removed")
def test_the_grading_window_has_not_expired():
    """The one that goes red on its own. Deleting the file is the fix, not editing the date:
    the date should only move if the grading window genuinely moved."""
    deadline = _restore_after(TFVARS.read_text(encoding="utf-8"))
    today = dt.date.today()
    assert today <= deadline, (
        f"the grading window closed on {deadline} and today is {today}. The nightly stop is "
        f"still disarmed, so the three instances and RDS have been running continuously "
        f"since then. Restore the control:\n"
        f"    rm infra/terraform/grading.auto.tfvars && terraform apply\n"
        f"Only move RESTORE_AFTER if grading itself moved."
    )


@pytest.mark.skipif(not TFVARS.is_file(), reason="grading window already closed; file removed")
def test_the_override_actually_disarms_the_nightly_stop():
    """Guards against the file surviving as a comment-only husk after someone deletes the
    assignment. Then every apply would re-arm the stop while this file still implied it had
    not, which is worse than no file at all."""
    text = TFVARS.read_text(encoding="utf-8")
    assert re.search(r"^\s*nightly_stop_enabled\s*=\s*false\s*$", text, re.MULTILINE), (
        "grading.auto.tfvars no longer sets `nightly_stop_enabled = false`, so it no longer "
        "disarms anything, but its presence still says the window is open"
    )


@pytest.mark.skipif(not TFVARS.is_file(), reason="grading window already closed; file removed")
def test_the_variable_it_overrides_still_exists_and_still_defaults_to_on():
    """The override is only meaningful against a default of true. If the default is ever
    flipped to false, the cost control is off for everyone and this file is no longer an
    exception -- it is redundant cover for a silent change of policy."""
    variables = (REPO / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    block = re.search(
        r'variable\s+"nightly_stop_enabled"\s*\{.*?\n\}', variables, re.DOTALL
    )
    assert block, "variable `nightly_stop_enabled` is gone; grading.auto.tfvars sets nothing"
    assert re.search(r"^\s*default\s*=\s*true\s*$", block.group(0), re.MULTILINE), (
        "`nightly_stop_enabled` no longer defaults to true. The nightly stop is now off by "
        "default for every apply, not just for the grading window (premortem H7)"
    )
