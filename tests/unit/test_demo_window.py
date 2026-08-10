"""The public demo window expires, and while it is open it must agree with the code.

`infra/terraform/demo.auto.tfvars` opens the three graded listeners to the internet so a
grader can reach them. That is a deliberate exception to the exposure posture premortem H15
rests on: the original acceptance of cleartext HTTP in `docs/tls-decision.md` was argued on
an exposure window "measured in hours", with `demo_cidrs` opened only "while a grader is
looking". This window is open-ended, which is why that decision was re-opened and
re-accepted on 2026-08-10 rather than quietly stretched.

An exception to an exposure control is fine. An exception to an exposure control that nobody
remembers making is how a demo window becomes a permanently internet-facing cleartext
service. So these tests do two things a comment cannot: they fail once the backstop has
passed, and they fail if the file stops saying what it claims to say.

Deleting the file is the fix, not editing the date. `rm infra/terraform/demo.auto.tfvars &&
terraform apply` closes the listeners back to the operator allowlist.
"""

import datetime as dt
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TFVARS = REPO / "infra/terraform/demo.auto.tfvars"
TLS_DECISION = REPO / "docs/tls-decision.md"


def _restore_after(text: str) -> dt.date:
    match = re.search(r"^#\s*RESTORE_AFTER:\s*(\d{4}-\d{2}-\d{2})\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(
            "demo.auto.tfvars carries no `# RESTORE_AFTER: YYYY-MM-DD` line. Opening the "
            "listeners to the internet with no expiry is a permanent change wearing a "
            "temporary label"
        )
    return dt.date.fromisoformat(match.group(1))


@pytest.mark.skipif(not TFVARS.is_file(), reason="demo window already closed; file removed")
def test_the_demo_window_has_not_expired():
    """The one that goes red on its own."""
    deadline = _restore_after(TFVARS.read_text(encoding="utf-8"))
    today = dt.date.today()
    assert today <= deadline, (
        f"the demo window backstop passed on {deadline} and today is {today}. The three "
        f"graded listeners have been serving cleartext HTTP to the whole internet since "
        f"then. Close them:\n"
        f"    rm infra/terraform/demo.auto.tfvars && terraform apply\n"
        f"Only move RESTORE_AFTER if the demo genuinely needs to stay open, and say why."
    )


@pytest.mark.skipif(not TFVARS.is_file(), reason="demo window already closed; file removed")
def test_the_override_actually_opens_the_listeners():
    """Guards against the file surviving as a comment-only husk. Then every apply would
    re-close the listeners while this file still implied they were open, and the README's
    live URLs would time out with nothing anywhere reporting an error."""
    text = TFVARS.read_text(encoding="utf-8")
    assert re.search(r'^\s*demo_cidrs\s*=\s*\[\s*"0\.0\.0\.0/0"\s*\]\s*$', text, re.MULTILINE), (
        "demo.auto.tfvars no longer sets `demo_cidrs = [\"0.0.0.0/0\"]`, so it no longer "
        "opens anything, but its presence still says the demo window is open"
    )


@pytest.mark.skipif(not TFVARS.is_file(), reason="demo window already closed; file removed")
def test_the_variable_it_overrides_still_exists_and_still_defaults_to_closed():
    """The override is only meaningful against a default of []. If the default ever becomes
    a public CIDR, the listeners are open for every apply, not just for the demo window, and
    this file is no longer an exception -- it is cover for a silent change of policy."""
    variables = (REPO / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    block = re.search(r'variable\s+"demo_cidrs"\s*\{.*?\n\}', variables, re.DOTALL)
    assert block, "variable `demo_cidrs` is gone; demo.auto.tfvars sets nothing"
    assert re.search(r"^\s*default\s*=\s*\[\s*\]\s*$", block.group(0), re.MULTILINE), (
        "`demo_cidrs` no longer defaults to []. The listeners are now open by default for "
        "every apply, not just for the demo window (premortem H15)"
    )


@pytest.mark.skipif(not TFVARS.is_file(), reason="demo window already closed; file removed")
def test_the_reviewer_port_is_not_opened_by_the_demo_window():
    """H12/H15. The entire acceptance of cleartext rests on 8503 never being reachable. A
    demo window that opened it would hand the reviewer shared secret and raw comment text to
    anyone on the path, which is the specific harm the decision claimed to close
    structurally rather than accept."""
    text = TFVARS.read_text(encoding="utf-8")
    assert "8503" in text, (
        "demo.auto.tfvars does not say what it declines to open. Say it explicitly, so that "
        "widening this file later requires deleting the sentence that forbids it"
    )
    network = (REPO / "infra/terraform/network.tf").read_text(encoding="utf-8")
    for match in re.finditer(r"from_port\s*=\s*(\d+)", network):
        port = match.group(1)
        assert port != "8503", "network.tf now carries an ingress rule for the reviewer port"


@pytest.mark.skipif(not TFVARS.is_file(), reason="demo window already closed; file removed")
def test_the_decision_record_was_reopened_for_this_window():
    """The original acceptance was argued on an exposure window measured in hours. An
    open-ended window is a different risk, and the record has to say so in its own voice --
    otherwise the tfvars file is silently relying on reasoning that no longer covers it."""
    assert TLS_DECISION.is_file(), "demo.auto.tfvars cites docs/tls-decision.md, which is absent"
    body = TLS_DECISION.read_text(encoding="utf-8")
    assert "open-ended" in body.lower(), (
        "docs/tls-decision.md does not acknowledge that the demo window is open-ended. Its "
        "residual-risk argument still assumes a few supervised hours"
    )
