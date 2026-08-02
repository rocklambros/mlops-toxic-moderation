"""C8. The rollback target has to survive the failure that makes rollback necessary.

Two parameters carry it, and the order they are written in is the whole trick: previous-sha
first, because a crash between the two writes must lose the NEW pointer rather than the old
one. Losing the new pointer costs a re-run; losing the old one removes recovery at exactly
the moment recovery is needed.
"""

from pathlib import Path

import pytest

from tests.infra import tfparse
from tests.infra.shellstub import make_stub, run, shell_code

SCRIPT = Path("infra/aws/record_deploy.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
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
    name = opt("--name")
    values = read()
    if name not in values:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(values[name])
    sys.exit(0)

if "put-parameter" in argv:
    name, value = opt("--name"), opt("--value")
    if os.environ.get("STUB_FAIL_ON") == name:
        print("simulated failure", file=sys.stderr)
        sys.exit(255)
    with journal.open("a") as handle:
        handle.write(f"{name}={value}\n")
    values = read()
    values[name] = value
    store.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    sys.exit(0)

print(f"unexpected: {argv}", file=sys.stderr)
sys.exit(9)
'''


@pytest.fixture()
def env(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "journal"
    store = tmp_path / "store"
    return bin_dir, journal, store, {"STUB_JOURNAL": str(journal), "STUB_STORE": str(store)}


def _journal(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_first_deploy_records_current_and_leaves_previous_unset(env):
    bin_dir, journal, _store, stub_env = env
    result = run(SCRIPT, ["aaa1111"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert _journal(journal) == ["/toxic/deploy/current-sha=aaa1111"]


def test_previous_sha_is_recorded_before_current_sha(env):
    """A crash between the two writes must lose the NEW pointer, not the old one."""
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n")
    result = run(SCRIPT, ["bbb2222"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert _journal(journal) == [
        "/toxic/deploy/previous-sha=aaa1111",
        "/toxic/deploy/current-sha=bbb2222",
    ]


def test_redeploying_the_same_sha_does_not_destroy_the_rollback_target(env):
    """Re-running a deploy must not set previous == current and strand the rollback."""
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n/toxic/deploy/previous-sha=zzz0000\n")
    result = run(SCRIPT, ["aaa1111"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert "/toxic/deploy/previous-sha=aaa1111" not in _journal(journal)
    assert "/toxic/deploy/previous-sha=zzz0000" in store.read_text(), (
        "the rollback target was overwritten by a redeploy of what is already current"
    )


def test_a_failed_previous_write_aborts_before_current_is_moved(env):
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n")
    result = run(
        SCRIPT,
        ["bbb2222"],
        bin_dir,
        env={**stub_env, "STUB_FAIL_ON": "/toxic/deploy/previous-sha"},
    )
    assert result.returncode != 0
    assert "/toxic/deploy/current-sha=bbb2222" not in _journal(journal)
    assert "/toxic/deploy/current-sha=aaa1111" in store.read_text(), (
        "current-sha must still name the SHA that is actually serving"
    )


def test_a_missing_sha_argument_is_a_usage_error(env):
    bin_dir, _journal_path, _store, stub_env = env
    result = run(SCRIPT, [], bin_dir, env=stub_env)
    assert result.returncode != 0
    assert _journal(_journal_path) == [], "a usage error wrote to Parameter Store"


def test_the_recorded_sha_is_never_empty(env):
    """`record_deploy.sh ""` from a workflow whose IMAGE_TAG did not resolve would blank the
    pointer that rollback reads, and blank it AFTER a successful health gate -- the one moment
    everything looks fine."""
    bin_dir, journal, _store, stub_env = env
    result = run(SCRIPT, [""], bin_dir, env=stub_env)
    assert result.returncode != 0
    assert _journal(journal) == []


def test_nothing_is_recorded_under_a_prefix_an_instance_can_write(env):
    """iam.tf grants each instance ssm:PutParameter on /toxic/boot/* and nowhere else, so an
    instance cannot lie about which SHA is serving. Recording under that prefix would hand it
    back."""
    assert "/toxic/boot/" not in shell_code(SCRIPT)


def test_the_deploy_role_is_allowed_to_write_the_two_parameters_it_records():
    """The script is not the control; the grant is. `gha-deploy` is scoped to ssm:GetParameter
    on /toxic/*, and a role that can read but not write turns the last step of a successful
    deploy into AccessDenied -- after the images are pushed, after the roll, after the health
    gate went green, which is the most expensive possible place to discover a missing verb.
    """
    policy = tfparse.source_of("oidc.tf")
    assert "ssm:PutParameter" in policy, (
        "gha-deploy cannot write /toxic/deploy/current-sha; record_deploy.sh will fail with "
        "AccessDenied at the end of every otherwise-successful deploy"
    )
    for name in ("current-sha", "previous-sha"):
        assert f"parameter/toxic/deploy/{name}" in policy, f"no write grant for {name}"
    # Scoped to those two names and not to the namespace. /toxic/endpoints/* is what the
    # health gate probes and /toxic/images/* is where every push is aimed; a wildcard here
    # would let the deploy role point the gate at something it controls.
    assert "parameter/toxic/deploy/*" not in policy, "the write grant is a namespace wildcard"
    assert "parameter/toxic/*" not in policy.split("SsmRecordWhichShaIsServing")[1]
