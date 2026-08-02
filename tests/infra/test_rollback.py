"""C8. Rollback exists for the day everything else is on fire. It has to be boring.

Everything here RUNS `rollback.sh`. The properties that matter -- that it checks the whole
target before it touches the first instance, that a red gate does not become a recorded
success, that a second run does not walk back into the version it just escaped -- are all
control flow, and control flow is not visible to a grep.
"""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run, shell_code

SCRIPT = Path("infra/aws/rollback.sh").resolve()
RECORD = Path("infra/aws/record_deploy.sh").resolve()

# The repository names the applied account actually has. `<project>-<component>`, and the
# project is `toxic-mod`: the plan for this phase assumed `toxic-<component>`. This stub is
# strict about them on purpose -- a hardcoded name in rollback.sh fails here the same way it
# would fail against ECR, rather than passing against a stub that answers to anything.
REPOSITORIES = {
    "/toxic/images/backend": "toxic-mod-backend",
    "/toxic/images/frontend": "toxic-mod-frontend",
    "/toxic/images/monitoring": "toxic-mod-monitoring",
}

AWS_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


REPOSITORIES = json.loads(os.environ["STUB_REPOSITORIES"])

if "get-parameter" in argv:
    values = dict(REPOSITORIES)
    values["/toxic/deploy/previous-sha"] = os.environ.get("STUB_PREVIOUS", "")
    values["/toxic/deploy/current-sha"] = os.environ.get("STUB_CURRENT", "")
    value = values.get(opt("--name"), "")
    if not value:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(value)
    sys.exit(0)

if "describe-images" in argv:
    repository = opt("--repository-name")
    if repository not in set(REPOSITORIES.values()):
        print(f"RepositoryNotFoundException: {repository}", file=sys.stderr)
        sys.exit(254)
    if repository == os.environ.get("STUB_MISSING_REPO"):
        print("ImageNotFoundException", file=sys.stderr)
        sys.exit(254)
    missing_tag = os.environ.get("STUB_MISSING_TAG", "")
    if missing_tag and opt("--image-ids") == f"imageTag={missing_tag}":
        print("ImageNotFoundException", file=sys.stderr)
        sys.exit(254)
    # `--output text` prints the literal `None` for an absent field AND EXITS 0. An
    # exit-code check calls that a present image.
    if os.environ.get("STUB_NONE_DIGEST") == "1":
        print("None")
        sys.exit(0)
    print("sha256:" + "b" * 64)
    sys.exit(0)

print(f"unexpected: {argv}", file=sys.stderr)
sys.exit(9)
'''

FAIL = '#!/bin/bash\necho "$(basename "$0") $*" >&2\nexit 1\n'


@pytest.fixture()
def harness(tmp_path: Path):
    """Returns (run_rollback, trace_path, journal_path).

    Every helper `rollback.sh` shells out to writes one line to `trace`, so the ORDER of the
    roll, the gate and the record is an observable fact rather than a claim about the source.
    """
    import json

    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    trace = tmp_path / "trace"
    journal = tmp_path / "journal"

    def go(args=(), *, failing=(), **env):
        for name in ("ssm_run.sh", "verify_live.sh", "record_deploy.sh"):
            body = (
                FAIL
                if name in failing
                else f'#!/bin/bash\necho "{name} $*" >> "{trace}"\nexit 0\n'
            )
            make_stub(fake, name, body)
        base = {
            "STUB_JOURNAL": str(journal),
            "STUB_REPOSITORIES": json.dumps(REPOSITORIES),
            "ROLLBACK_SCRIPT_DIR": str(fake),
            "STUB_PREVIOUS": "aaa1111",
            "STUB_CURRENT": "bbb2222",
        }
        base.update(env)
        return run(SCRIPT, list(args), bin_dir, env=base)

    def traced() -> list[str]:
        return trace.read_text().splitlines() if trace.exists() else []

    return go, traced


def test_rollback_never_invokes_terraform():
    """C8. On the day this runs, an apply is a bigger risk than the outage: it can
    force-replace instances and destroy the baked artifacts."""
    code = shell_code(SCRIPT)
    assert "terraform" not in code.lower()


def test_rollback_reads_the_previous_sha_when_none_is_given(harness):
    go, _traced = harness
    result = go()
    assert result.returncode == 0, result.stderr
    assert "bbb2222 -> aaa1111" in result.stdout


def test_rollback_refuses_when_no_previous_sha_exists(harness):
    go, traced = harness
    result = go(STUB_PREVIOUS="")
    assert result.returncode != 0
    assert "no rollback target" in result.stderr
    assert traced() == [], "a rollback with no target still touched the fleet"


def test_rollback_verifies_every_image_still_exists_before_it_touches_anything(harness):
    """Discovering a missing image halfway through a roll leaves the fleet split across two
    versions, which is worse than either. The check is BEFORE the first SendCommand."""
    go, traced = harness
    result = go(["aaa1111"], STUB_MISSING_REPO="toxic-mod-monitoring")
    assert result.returncode != 0
    assert "toxic-mod-monitoring" in result.stderr
    assert traced() == [], "the roll started before the target was known to be complete"


def test_rollback_checks_the_reviewer_image_as_well(harness):
    """The reviewer console is the frontend repository at `<sha>-reviewer`. It is a separate
    build and it can be absent on its own -- and compose on EC2 #2 hard-fails on a missing
    image reference, taking the graded user interface down with it."""
    go, traced = harness
    result = go(["aaa1111"], STUB_MISSING_TAG="aaa1111-reviewer")
    assert result.returncode != 0
    assert "reviewer" in result.stderr
    assert traced() == []


def test_rollback_resolves_repository_names_from_parameter_store(harness):
    """`<project>-<component>`, and `project` is a Terraform variable. A literal `toxic-*` here
    would ask ECR about repositories that do not exist, and every rollback would refuse."""
    go, _traced = harness
    result = go(["aaa1111"])
    assert result.returncode == 0, result.stderr
    code = shell_code(SCRIPT)
    assert "/images/" in code, "no repository name is read from Parameter Store at all"
    # Not just "no literal name": `toxic-mod-$1` composes the right names today and is the
    # same bug, because `toxic-mod` is the value of a Terraform variable, not a constant.
    for literal in ("toxic-backend", "toxic-frontend", "toxic-monitoring",
                    "toxic-mod-backend", "toxic-mod-frontend", "toxic-mod-monitoring",
                    "toxic-mod-$", "toxic-$"):
        assert literal not in code, f"hardcoded ECR repository name: {literal}"


def test_a_repository_that_answers_None_is_not_a_present_image(harness):
    """`aws ... --output text` prints the string `None` for an absent field and EXITS 0, so a
    check that reads only the exit code calls that a present image and rolls into a fleet that
    cannot pull anything. roll.sh has the same guard for the same reason."""
    go, traced = harness
    result = go(["aaa1111"], STUB_NONE_DIGEST="1")
    assert result.returncode != 0
    assert traced() == [], "a rollback started against images that are not there"


def test_rollback_rolls_all_three_components_then_verifies_then_records(harness):
    go, traced = harness
    result = go(["aaa1111"])
    assert result.returncode == 0, result.stderr
    lines = traced()
    assert [line.split()[0] for line in lines] == [
        "ssm_run.sh", "ssm_run.sh", "ssm_run.sh", "verify_live.sh", "record_deploy.sh"
    ]
    rolls = [line for line in lines if line.startswith("ssm_run.sh")]
    assert all("aaa1111" in line for line in rolls)
    for component in ("backend", "frontend", "monitoring"):
        assert any(
            line.startswith(f"ssm_run.sh {component} 1 bash /opt/toxic/bootstrap.sh aaa1111 "
                            f"{component}")
            for line in rolls
        ), f"{component} was not rolled with its component argument: {rolls}"


def test_a_failed_verification_does_not_record_the_rollback_as_current(harness):
    go, traced = harness
    result = go(["aaa1111"], failing=("verify_live.sh",))
    assert result.returncode != 0
    assert not any(line.startswith("record_deploy.sh") for line in traced())


def test_a_failed_roll_stops_before_the_remaining_components(harness):
    """A rollback that keeps going after the backend refused leaves two of three tiers moved."""
    go, traced = harness
    result = go(["aaa1111"], failing=("ssm_run.sh",))
    assert result.returncode != 0
    assert traced() == []


def test_rollback_does_not_make_the_sha_it_escaped_the_next_rollback_target(harness):
    """Run `make rollback` twice and the second run must not walk back into the broken
    version. record_deploy.sh moves current to previous, which is right for a DEPLOY and
    exactly wrong here: it would leave previous-sha naming the SHA this command just
    escaped."""
    go, traced = harness
    result = go(["aaa1111"])
    assert result.returncode == 0, result.stderr
    record = next(line for line in traced() if line.startswith("record_deploy.sh"))
    assert "--keep-previous" in record, record


def test_record_deploy_keeps_the_previous_pointer_when_asked(tmp_path):
    """The other half of the property above, on the script that owns the pointer."""
    import json as _json  # noqa: F401  (kept local; the stub takes its state from files)

    bin_dir = tmp_path / "bin"
    journal = tmp_path / "journal"
    store = tmp_path / "store"
    store.write_text("/toxic/deploy/current-sha=bbb2222\n/toxic/deploy/previous-sha=aaa1111\n")
    make_stub(bin_dir, "aws", _RECORD_AWS_STUB)
    result = run(
        RECORD,
        ["--keep-previous", "aaa1111"],
        bin_dir,
        env={"STUB_JOURNAL": str(journal), "STUB_STORE": str(store)},
    )
    assert result.returncode == 0, result.stderr
    written = journal.read_text().splitlines()
    assert written == ["/toxic/deploy/current-sha=aaa1111"], written
    assert "/toxic/deploy/previous-sha=aaa1111" in store.read_text()


_RECORD_AWS_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
journal = Path(os.environ["STUB_JOURNAL"])
store = Path(os.environ["STUB_STORE"])


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


def read():
    if not store.exists():
        return {}
    return dict(line.split("=", 1) for line in store.read_text().splitlines() if line)


if "get-parameter" in argv:
    values = read()
    name = opt("--name")
    if name not in values:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(values[name])
    sys.exit(0)

if "put-parameter" in argv:
    name, value = opt("--name"), opt("--value")
    with journal.open("a") as handle:
        handle.write(f"{name}={value}\n")
    values = read()
    values[name] = value
    store.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    sys.exit(0)

print(f"unexpected: {argv}", file=sys.stderr)
sys.exit(9)
'''
