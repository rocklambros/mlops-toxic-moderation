"""REG-5. Delivery spec section 12: the live URL is reachable AFTER a stop/start cycle.

Starting three instances and returning is how a bookmarked URL turns out to be dead five
minutes before a demo. Nothing in the previous design started containers on a stop/start cycle
at all, and `restart: unless-stopped` does not cover a host whose stack was brought down.
"""

import re
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run, shell_code

SCRIPT = Path("infra/aws/aws_up.sh").resolve()
MAKEFILE = Path("Makefile")

AWS_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    name = opt("--name")
    if name.startswith("/toxic/boot/") and os.environ.get("STUB_NO_BOOT_MARKER") == "1":
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    if name.startswith("/toxic/endpoints/"):
        print("http://198.51.100.7:8000")
    else:
        print("ok")
elif "describe-db-instances" in argv:
    print(os.environ.get("STUB_DB_STATUS", "available"))
sys.exit(0)
'''

SUCCEED = '#!/bin/bash\necho "$(basename "$0") $*"\nexit 0\n'
FAIL = '#!/bin/bash\necho "$(basename "$0") $*" >&2\nexit 1\n'


def _harness(tmp_path: Path, verify: str = SUCCEED, **env):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    make_stub(fake, "ssm_run.sh", SUCCEED)
    make_stub(fake, "verify_live.sh", verify)
    base = {
        "STUB_JOURNAL": str(tmp_path / "j"),
        "AWS_UP_SCRIPT_DIR": str(fake),
        "INSTANCE_IDS": "i-1 i-2 i-3",
        "DB_INSTANCE_ID": "toxic-mod-pg",
        "AWS_UP_POLL_SECONDS": "0",
        "AWS_UP_TIMEOUT": "2",
    }
    base.update(env)
    return bin_dir, base, tmp_path / "j"


def _journal(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_aws_up_gates_on_health_not_on_instance_state(tmp_path):
    bin_dir, env, _journal_path = _harness(tmp_path, verify=FAIL)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0, "aws-up returned green while the application was down"


def test_aws_up_returns_zero_only_when_all_three_endpoints_answer(tmp_path):
    bin_dir, env, _journal_path = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode == 0, result.stderr
    assert "verify_live.sh" in result.stdout


def test_aws_up_starts_the_database_before_the_instances(tmp_path):
    """The backend's lifespan fails closed on an unreachable database, so a host that comes up
    first simply restart-loops until Postgres answers -- and TimeoutStartSec is 900s."""
    bin_dir, env, journal_path = _harness(tmp_path)
    run(SCRIPT, [], bin_dir, env=env)
    lines = _journal(journal_path)
    rds = next(i for i, line in enumerate(lines) if "start-db-instance" in line)
    ec2 = next(i for i, line in enumerate(lines) if "start-instances" in line)
    assert rds < ec2


def test_aws_up_waits_for_rds_to_be_available_not_merely_started(tmp_path):
    """`start-db-instance` returns while the status is still `starting`."""
    bin_dir, env, _journal_path = _harness(tmp_path, STUB_DB_STATUS="starting")
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0
    assert "available" in result.stderr, result.stderr


def test_aws_up_waits_for_the_boot_marker_before_it_rolls(tmp_path):
    """H26. Rolling into a host whose user data has not finished fails for the wrong reason and
    wastes the first ten minutes of every debugging session.

    With no marker AND no answering endpoint, there is no evidence the host is ready, so this
    must still be fatal."""
    bin_dir, env, _journal_path = _harness(tmp_path, verify=FAIL, STUB_NO_BOOT_MARKER="1")
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0
    assert "boot marker" in result.stderr


def test_a_missing_boot_marker_is_not_fatal_when_the_component_is_already_serving(tmp_path):
    """Measured against the live fleet on 2026-08-10: `make aws-up` could not succeed at all.

    The marker is a PROXY for "did this host's bootstrap reach the end". The script's own
    comment assumes it "is already present from the first boot", and on this fleet it is not:
    `compute.tf` sets `user_data_replace_on_change = false` and puts `user_data` under
    `ignore_changes`, deliberately, because the SCP denies `ec2:ModifyInstanceAttribute`. So
    when the marker was added to the template, Terraform correctly changed nothing on the
    running instances, and `/toxic/boot/*` has never existed. The gate could never pass, and
    it is the gate in the documented recovery command.

    When the endpoint answers, the question the marker asks is already answered, and answered
    more directly: a host that is serving HTTP finished booting. So this falls back to the
    stronger evidence rather than blocking on the weaker proxy -- and says so, loudly, because
    a silent fallback is how the marker quietly stops meaning anything.
    """
    bin_dir, env, _journal_path = _harness(tmp_path, STUB_NO_BOOT_MARKER="1")
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "boot marker" in combined, "the fallback must not be silent"
    assert "already serving" in combined, combined


def test_the_fallback_still_rolls_the_stack_rather_than_skipping_ahead(tmp_path):
    """Falling back on readiness must not also skip the work that follows it."""
    bin_dir, env, _journal_path = _harness(tmp_path, STUB_NO_BOOT_MARKER="1")
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode == 0, result.stderr
    assert "verify_live.sh" in result.stdout, "the health gate was skipped"


def test_aws_up_explicitly_starts_the_stack_unit_on_every_component(tmp_path):
    """`restart: unless-stopped` covers a daemon restart. It does not cover a replaced instance
    or a rollback that ran `compose down`, and `systemctl start` is idempotent."""
    bin_dir, env, _journal_path = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    for component in ("backend", "frontend", "monitoring"):
        assert f"ssm_run.sh {component} 1" in result.stdout


def test_aws_up_prints_the_three_urls_for_the_operator(tmp_path):
    bin_dir, env, _journal_path = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.stdout.count("http://") >= 3


def test_aws_up_changes_no_infrastructure(tmp_path):
    """The two EventBridge cost schedules are DISABLED for the grading window. A bring-up that
    ran an apply -- or that re-enabled them as a convenience -- would put the graded stack back
    on a nightly timer, and the failure would appear the following morning."""
    code = shell_code(SCRIPT)
    assert "terraform apply" not in code
    assert "scheduler" not in code
    assert "nightly" not in code


def test_aws_up_reads_only_terraform_outputs_that_exist():
    from tests.infra import tfparse

    code = shell_code(SCRIPT)
    outputs = tfparse.source_of("outputs.tf")
    names = re.findall(r"terraform output[^\n]*?(?:-raw|-json)\s+(\w+)", code)
    assert names, "aws_up.sh discovers neither the instances nor the database"
    for name in names:
        assert f'output "{name}"' in outputs, f"aws_up.sh reads an undeclared output: {name}"


def test_the_gate_is_the_same_one_the_deploy_uses(tmp_path):
    """A second implementation of "is it up?" is a second thing that can disagree with the
    deploy, and the one that disagrees quietly is the one an operator believes."""
    code = shell_code(SCRIPT)
    assert "verify_live.sh" in code
    assert "curl" not in code


@pytest.mark.parametrize(
    "target", ["aws-up", "aws-down", "db-dump", "db-restore", "rollback", "deploy-verify"]
)
def test_every_lifecycle_target_runs_a_script_that_exists(target):
    """A Makefile target naming a script nobody wrote fails at the worst moment -- the first
    time anyone reaches for it, which is during an incident."""
    body = MAKEFILE.read_text(encoding="utf-8")
    recipe = re.search(rf"^{re.escape(target)}\s*:[^\n]*\n((?:\t[^\n]*\n)+)", body, re.MULTILINE)
    assert recipe, f"no recipe for {target}"
    for script in re.findall(r"\$\(AWS\)/([\w.-]+\.sh)", recipe.group(1)):
        path = Path("infra/aws") / script
        assert path.exists(), f"`make {target}` runs {path}, which does not exist"
        assert path.stat().st_mode & 0o111, f"{path} is not executable"
