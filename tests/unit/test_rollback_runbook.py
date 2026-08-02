"""C8 and H26. The runbook is off the cut list, and the rehearsal is what makes it real.

A runbook nobody has run is a hypothesis. This file cannot make anyone rehearse, but it can
make the difference between "rehearsed" and "written down" impossible to blur: the evidence
record carries a machine-readable Status, and the assertions below are different in each
state. A PENDING record must not carry an outcome, and a REHEARSED one must carry a date, two
SHAs, a wall-clock time and a transcript.

The other half of the file is anti-rot. Every command the runbook gives is checked against the
tree it is a runbook for -- the scripts exist, the Make targets exist, the SSM parameters are
ones something actually publishes, and the AWS identifiers are the ones Terraform declares.
The plan this was written from named `toxicmod-final`, `toxicmod-private` and
`toxicmod-restored`; the account has `toxic-mod-*`, and every one of those commands would have
failed in an incident, which is the only time anyone would have found out.
"""

import re
from pathlib import Path

import pytest

RUNBOOK = Path("infra/ROLLBACK.md")
REHEARSAL = Path("docs/evidence/p5-rollback-rehearsal.md")
MAKEFILE = Path("Makefile")
TERRAFORM = Path("infra/terraform")

STATUS_RE = re.compile(r"^Status:\s*(PENDING|REHEARSED)\b", re.MULTILINE)


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _rehearsal() -> str:
    return REHEARSAL.read_text(encoding="utf-8")


def _project() -> str:
    """`var.project`'s default, which is the prefix on every AWS name in the account."""
    source = (TERRAFORM / "variables.tf").read_text(encoding="utf-8")
    match = re.search(r'variable "project" \{.*?default\s*=\s*"([^"]+)"', source, re.DOTALL)
    assert match, "variables.tf declares no default for var.project"
    return match.group(1)


def _table_field(body: str, field: str) -> str:
    match = re.search(rf"^\|\s*{re.escape(field)}\s*\|(.*?)\|", body, re.MULTILINE)
    assert match, f"the rehearsal record has no `{field}` row"
    return match.group(1).strip()


# --------------------------------------------------------------------------------------
# the runbook
# --------------------------------------------------------------------------------------


def test_runbook_covers_the_four_recovery_scenarios():
    body = _runbook().lower()
    for scenario in (
        "bad deploy",            # re-roll the previous SHA
        "instance replaced",     # the box is new and empty
        "database",              # restore the graded dataset
        "total teardown",        # terraform destroy happened
    ):
        assert scenario in body, f"no procedure for: {scenario}"


def test_runbook_gives_exact_commands_not_descriptions():
    body = _runbook()
    for command in ("make rollback", "make db-restore", "infra/aws/rollback.sh",
                    "aws ssm get-parameter", "aws rds restore-db-instance-from-db-snapshot"):
        assert command in body, f"missing exact command: {command}"


def test_runbook_states_the_no_terraform_rule():
    body = _runbook()
    assert "without touching Terraform" in body or "does not touch Terraform" in body


def test_runbook_states_the_time_budget_for_each_scenario():
    body = _runbook()
    assert len(re.findall(r"\b\d+\s*(?:minutes|min)\b", body)) >= 4


def test_the_runbook_is_referenced_where_an_operator_would_look():
    assert "ROLLBACK.md" in Path("README.md").read_text(encoding="utf-8")
    assert "ROLLBACK.md" in Path("infra/deploy/toxic-stack.service").read_text(encoding="utf-8")


def test_every_repository_script_the_runbook_names_exists():
    """A runbook that names a script nobody wrote fails the first time anyone reaches for it,
    which is during an incident."""
    named = sorted(set(re.findall(r"infra/aws/[\w.-]+\.sh", _runbook())))
    assert named, "the runbook gives no runnable command from this repository"
    for script in named:
        path = Path(script)
        assert path.exists(), f"the runbook names {script}, which does not exist"
        assert path.stat().st_mode & 0o111, f"{script} is not executable"


def test_every_make_target_the_runbook_names_exists():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    for target in sorted(set(re.findall(r"\bmake ([a-z][\w-]*)", _runbook()))):
        assert re.search(rf"^{re.escape(target)}\s*:", makefile, re.MULTILINE), (
            f"the runbook says `make {target}` and the Makefile has no such target"
        )


def test_every_ssm_parameter_the_runbook_reads_is_one_something_writes():
    """A parameter nobody publishes answers ParameterNotFound, and the runbook reader concludes
    the stack is broken rather than that the runbook is."""
    published = (TERRAFORM / "deploy.tf").read_text(encoding="utf-8")
    # The three sources that put a value at a /toxic/ name: Terraform's parameter map above,
    # the operator and instance scripts, and user data -- which writes the boot marker as its
    # very last line and is a template, not a script.
    written = "\n".join(
        path.read_text(encoding="utf-8")
        for group in (
            Path("infra/aws").glob("*.sh"),
            Path("infra/deploy/instance").glob("*.sh"),
            (TERRAFORM / "templates").glob("*.tftpl"),
        )
        for path in sorted(group)
    )
    source = published + "\n" + written
    for name in sorted(set(re.findall(r"/toxic/[\w./-]+", _runbook()))):
        # Every script builds its names from `${PARAM_PREFIX}`, which defaults to /toxic, so
        # the tail is what appears literally.
        tail = name[len("/toxic"):]
        if name in source or tail in source:
            continue
        # user_data writes `/toxic/boot/${component}`, so the per-component leaf never appears
        # literally anywhere. Accept the templated form, and only for a leaf that is one of
        # the three components -- a typo'd leaf is still a failure.
        namespace, _, leaf = tail.rpartition("/")
        assert leaf in ("backend", "frontend", "monitoring") and f"{namespace}/$" in source, (
            f"the runbook reads {name}, which neither Terraform publishes nor any script writes"
        )


@pytest.mark.parametrize(
    "wrong",
    ["toxicmod-final", "toxicmod-private", "toxicmod-restored", "toxicmod-pg", "toxicmod-db"],
)
def test_the_runbook_does_not_use_identifiers_the_account_does_not_have(wrong):
    """Every AWS name in this account is `${var.project}-...` and the project is `toxic-mod`.
    The plan this runbook was written from used `toxicmod-*` throughout."""
    assert wrong not in _runbook(), f"{wrong} is not a name anything in this account has"


def test_the_runbook_names_the_resources_terraform_actually_declares():
    project = _project()
    body = _runbook()
    for name, why in (
        (f"{project}-pg", "the RDS instance identifier stop/start and restore all take"),
        (f"{project}-db", "the DB subnet group a restored instance has to be placed in"),
        (f"{project}-final", "the prefix on the final snapshot terraform destroy leaves"),
    ):
        assert name in body, f"the runbook never names {name}: {why}"


def test_the_runbook_does_not_promise_a_lifecycle_rule_that_matches_nothing():
    """ECR rule 2 selects `tagPrefixList: ["sha-"]`, and this pipeline tags with the BARE git
    SHA. No rule matches those images, so a rollback target does not silently age out -- and a
    runbook that says it does sends an operator rebuilding when the real cause is that the
    build for that SHA never ran."""
    ecr = (TERRAFORM / "ecr.tf").read_text(encoding="utf-8")
    assert 'tagPrefixList = ["sha-"]' in ecr, (
        "the tag scheme changed; re-check what the runbook says about images ageing out"
    )
    assert "keep-last-10" not in _runbook()


# --------------------------------------------------------------------------------------
# the rehearsal record
# --------------------------------------------------------------------------------------


def test_the_rehearsal_record_declares_a_machine_readable_status():
    status = STATUS_RE.search(_rehearsal())
    assert status, (
        "the rehearsal record must open with `Status: PENDING` or `Status: REHEARSED`. Without "
        "it, 'we wrote the runbook' and 'we ran the runbook' are the same document"
    )


def test_a_pending_record_claims_nothing():
    """The failure mode this file exists to prevent is a template with plausible-looking values
    in it, which reads exactly like evidence six weeks later."""
    body = _rehearsal()
    if STATUS_RE.search(body).group(1) != "PENDING":
        pytest.skip("the rehearsal has been run; the REHEARSED assertions apply instead")
    for field in ("Date", "Rolled from", "Rolled to", "Wall-clock", "Outcome"):
        assert _table_field(body, field) == "(pending)", (
            f"`{field}` carries a value while the record says the rehearsal has not run"
        )
    assert "Success" not in body, "a PENDING record must not report an outcome"
    assert not re.search(r"\b[0-9a-f]{7,40}\b", body), (
        "a PENDING record must not name a SHA it did not roll"
    )


def test_a_pending_record_gives_the_exact_commands_that_would_complete_it():
    body = _rehearsal()
    if STATUS_RE.search(body).group(1) != "PENDING":
        pytest.skip("the rehearsal has been run")
    for command in ("make deploy-verify", "make rollback", "gh workflow run deploy.yml"):
        assert command in body, f"a pending record must name what to run: {command}"


def test_a_rehearsed_record_is_dated_and_complete():
    """H26. Every step in the runbook was first exercised while the system was known-good."""
    body = _rehearsal()
    if STATUS_RE.search(body).group(1) != "REHEARSED":
        pytest.skip("the rehearsal has not been run yet; the PENDING assertions apply instead")
    assert re.search(r"\b20\d\d-\d\d-\d\d\b", _table_field(body, "Date")), "no rehearsal date"
    for field in ("Rolled from", "Rolled to"):
        assert re.search(r"[0-9a-f]{7,40}", _table_field(body, field)), f"{field} names no SHA"
    assert _table_field(body, "Rolled from") != _table_field(body, "Rolled to"), (
        "a rollback from a SHA to itself rehearsed nothing"
    )
    assert re.search(r"\b\d+\s*(?:m|min|minutes|s|seconds)\b", _table_field(body, "Wall-clock")), (
        "Wall-clock records no real elapsed time"
    )
    assert "verify_deploy.sh" in body or "verify_live.sh" in body, "no gate is recorded"
    assert _table_field(body, "Outcome"), "no outcome"
    transcript = re.search(r"```\n(.*?)```", body, re.DOTALL)
    assert transcript and transcript.group(1).strip(), (
        "a REHEARSED record with an empty transcript is a claim, not evidence"
    )
    assert "paste" not in transcript.group(1).lower(), "the transcript is still the placeholder"
    assert "not yet rehearsed" not in body.lower()
