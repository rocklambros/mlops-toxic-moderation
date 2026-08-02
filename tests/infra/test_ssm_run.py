"""H5. The single most dangerous property of the deploy path: green while doing nothing.

`aws ssm send-command` is fire-and-forget. A `--targets` expression that matches ZERO
instances still returns a CommandId and exits 0, so a deploy job built on `send-command`
alone reports success while nothing was deployed -- and the demo URL keeps serving whatever
it was serving last week.

Every test here runs the real script against a fake `aws` whose behaviour the test chooses,
so each failure mode is exercised rather than described.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infra/aws/ssm_run.sh"
REDACTOR = REPO / "scripts/redact.py"

AWS_STUB = r'''#!/usr/bin/env python3
"""A fake `aws` whose behaviour is driven entirely by STUB_* environment variables."""
import os
import sys

argv = sys.argv[1:]


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


count = int(os.environ.get("STUB_INVOCATION_COUNT", "0"))

if "send-command" in argv:
    if os.environ.get("STUB_SEND_FAILS") == "1":
        print("An error occurred (AccessDeniedException)", file=sys.stderr)
        sys.exit(255)
    if os.environ.get("STUB_SEND_RETURNS_NOTHING") == "1":
        print("None")
        sys.exit(0)
    with open(os.environ["STUB_SEND_LOG"], "w", encoding="utf-8") as handle:
        handle.write("\n".join(argv))
    print("cmd-0001")
    sys.exit(0)

if "list-command-invocations" in argv:
    query = opt("--query", "")
    if query.startswith("length("):
        print(count)
    else:
        print("\t".join(f"i-{n:03d}" for n in range(count)))
    sys.exit(0)

if "get-command-invocation" in argv:
    query = opt("--query")
    instance = opt("--instance-id", "")
    key = instance.replace("-", "_")
    if query == "Status":
        print(os.environ.get(f"STUB_STATUS_{key}", "Success"))
    elif query == "StandardErrorContent":
        print(os.environ.get("STUB_STDERR", ""))
    else:
        print(os.environ.get("STUB_STDOUT", ""))
    sys.exit(0)

print(f"unexpected aws call: {argv}", file=sys.stderr)
sys.exit(9)
'''

FAST = {"SSM_REGISTER_TIMEOUT": "1", "SSM_RUN_TIMEOUT": "2", "SSM_POLL_SECONDS": "0"}
PAYLOAD = "bash /opt/toxic/bootstrap.sh abc backend"


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    target = tmp_path / "bin"
    make_stub(target, "aws", AWS_STUB)
    return target


def _run(bin_dir: Path, args: list[str], **env: str):
    return run(SCRIPT, args, bin_dir,
               env={**FAST, "STUB_SEND_LOG": str(bin_dir.parent / "send.log"), **env})


def test_zero_matching_instances_fails_the_deploy(bin_dir):
    """The exact H5 failure: a CommandId, exit 0, and nothing deployed."""
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="0")
    assert result.returncode != 0
    assert "saw 0" in result.stderr
    assert "nothing was deployed" in result.stderr


def test_a_partial_fleet_match_fails_the_deploy(bin_dir):
    result = _run(bin_dir, ["backend", "3", PAYLOAD], STUB_INVOCATION_COUNT="2")
    assert result.returncode != 0
    assert "expected 3" in result.stderr


def test_more_instances_than_expected_also_fails(bin_dir):
    """A stray instance carrying the tag means the fleet does not match the plan."""
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="2")
    assert result.returncode != 0
    assert "does not match the plan" in result.stderr


def test_failed_invocation_prints_standard_error_and_fails(bin_dir):
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="1",
                  STUB_STATUS_i_000="Failed", STUB_STDERR="denied: ecr pull permission")
    assert result.returncode != 0
    assert "denied: ecr pull permission" in result.stderr
    assert "Failed" in result.stdout + result.stderr


def test_a_timed_out_invocation_fails(bin_dir):
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="1",
                  STUB_STATUS_i_000="TimedOut")
    assert result.returncode != 0


def test_an_invocation_stuck_in_progress_fails_rather_than_hanging(bin_dir):
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="1",
                  STUB_STATUS_i_000="InProgress")
    assert result.returncode != 0
    assert "PollTimeout" in result.stdout + result.stderr


def test_one_failure_among_several_still_fails_the_whole_roll(bin_dir):
    result = _run(bin_dir, ["backend", "3", PAYLOAD], STUB_INVOCATION_COUNT="3",
                  STUB_STATUS_i_001="Failed")
    assert result.returncode != 0


def test_all_success_exits_zero(bin_dir):
    result = _run(bin_dir, ["backend", "3", PAYLOAD], STUB_INVOCATION_COUNT="3")
    assert result.returncode == 0, result.stderr
    assert "matched 3/3" in result.stdout


def test_every_instance_is_polled_not_just_the_first(bin_dir):
    """"All succeeded" from a loop that only ever looked at one instance is the same lie in
    a smaller costume."""
    result = _run(bin_dir, ["backend", "3", PAYLOAD], STUB_INVOCATION_COUNT="3")
    assert result.returncode == 0, result.stderr
    for number in range(3):
        assert f"i-{number:03d}" in result.stdout, result.stdout


def test_a_send_command_failure_is_not_swallowed(bin_dir):
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_SEND_FAILS="1")
    assert result.returncode != 0


def test_a_send_command_that_returns_no_id_is_not_treated_as_a_deploy(bin_dir):
    """`--query Command.CommandId --output text` prints the string `None` rather than
    failing when the field is absent, and `None` is a perfectly good shell string."""
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_SEND_RETURNS_NOTHING="1")
    assert result.returncode != 0
    assert "CommandId" in result.stderr


def test_missing_arguments_are_a_usage_error_not_a_deploy(bin_dir):
    result = _run(bin_dir, ["backend"])
    assert result.returncode == 2
    assert "usage" in result.stderr
    assert not (bin_dir.parent / "send.log").exists(), "a usage error still sent a command"


def test_a_non_numeric_expected_count_is_a_usage_error(bin_dir):
    """`[ "$observed" -ge "all" ]` is a shell error, and a shell error inside the loop that
    proves the fleet matched is a proof that never ran."""
    result = _run(bin_dir, ["backend", "all", PAYLOAD])
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_a_zero_expected_count_is_refused(bin_dir):
    """Expecting nothing is trivially satisfiable, and it is what someone writes when they
    do not know how many instances there are."""
    result = _run(bin_dir, ["backend", "0", PAYLOAD])
    assert result.returncode == 2


def test_a_payload_that_would_break_the_parameters_json_is_refused(bin_dir):
    """--parameters takes a JSON document assembled by string interpolation. A double quote
    in the command silently changes the document's shape; SSM then runs a different command
    from the one the caller wrote, or the call fails in a way that reads like an IAM problem.
    """
    result = _run(bin_dir, ["backend", "1", 'bash -c "echo hi"'])
    assert result.returncode == 2
    assert "quote" in result.stderr.lower() or "unsupported" in result.stderr.lower()


def test_the_target_expression_names_the_component_tag(bin_dir):
    """Targeting by the wrong tag key matches zero instances -- caught by the count check,
    but only after a deploy that looked like it was going to work."""
    _run(bin_dir, ["backend", "3", PAYLOAD], STUB_INVOCATION_COUNT="3")
    sent = (bin_dir.parent / "send.log").read_text(encoding="utf-8")
    assert "Key=tag:Component,Values=backend" in sent, sent
    assert "AWS-RunShellScript" in sent
    assert PAYLOAD in sent


def test_the_invocation_output_is_redacted_before_it_reaches_the_log(bin_dir):
    """StandardErrorContent is printed into a GitHub Actions log on a PUBLIC repository. The
    instance is not supposed to print anything sensitive, and this is the second line of
    defence for the day it does."""
    leaky = "arn:aws:iam::123456789012:role/toxic-mod-backend AKIAIOSFODNN7EXAMPLE"
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="1",
                  STUB_STATUS_i_000="Failed", STUB_STDERR=leaky,
                  SSM_REDACTOR=str(REDACTOR))
    assert result.returncode != 0
    assert "123456789012" not in result.stderr, result.stderr
    assert "AKIAIOSFODNN7EXAMPLE" not in result.stderr
    assert "<account-id>" in result.stderr and "<aws-access-key-id>" in result.stderr


def test_the_redaction_is_not_silently_skipped_when_the_redactor_is_missing(bin_dir):
    """Falling back to printing raw would make the control above disappear on exactly the
    runner where nobody is looking at it."""
    result = _run(bin_dir, ["backend", "1", PAYLOAD], STUB_INVOCATION_COUNT="1",
                  STUB_STATUS_i_000="Failed", STUB_STDERR="arn:aws:iam::123456789012:role/x",
                  SSM_REDACTOR=str(bin_dir / "no-such-redactor.py"))
    assert result.returncode != 0
    assert "123456789012" not in result.stderr, result.stderr
    assert "redactor" in result.stderr.lower()


def test_the_script_does_not_claim_the_application_works():
    """ssm_run.sh proves a shell exited 0 on N boxes. verify_deploy.sh is the deploy gate,
    and conflating the two is how a green deploy job ships a container that never started."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "verify_deploy" in body, "the script does not say what it is NOT proving"
    assert not re.search(r"(?i)deploy (succeeded|successful|complete)", body)
