"""The one place that knows the three endpoint parameter names, exercised rather than read.

`verify_deploy.sh` is the gate and it takes three URLs in the environment. Four callers need
those URLs -- the deploy workflow, `rollback.sh`, `aws_up.sh` and `make deploy-verify` -- and
each hand-written copy of the resolution is a chance to misname one parameter and probe two
live endpoints and one typo. That is a green gate over a broken deploy, so the resolution is
one script and this file runs it.
"""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run, shell_code

SCRIPT = Path("infra/aws/verify_live.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    values = json.loads(os.environ["STUB_PARAMS"])
    name = opt("--name")
    if name not in values:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(values[name])
    sys.exit(0)

print(f"unexpected: {argv}", file=sys.stderr)
sys.exit(9)
'''

ENDPOINTS = {
    "/toxic/endpoints/backend": "http://198.51.100.7:8000",
    "/toxic/endpoints/frontend": "http://198.51.100.8:8501",
    "/toxic/endpoints/monitoring": "http://198.51.100.9:8502",
}

# Prints what verify_deploy.sh was handed, so the test can assert on the environment rather
# than on the source of the script that builds it.
GATE = """#!/bin/bash
echo "gate BACKEND_URL=${BACKEND_URL-unset}"
echo "gate FRONTEND_URL=${FRONTEND_URL-unset}"
echo "gate MONITORING_URL=${MONITORING_URL-unset}"
exit 0
"""
RED_GATE = '#!/bin/bash\necho "gate ran" >&2\nexit 1\n'


@pytest.fixture()
def harness(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    journal = tmp_path / "journal"

    def go(params: dict[str, str], gate: str = GATE):
        make_stub(fake, "verify_deploy.sh", gate)
        return run(
            SCRIPT,
            [],
            bin_dir,
            env={
                "STUB_JOURNAL": str(journal),
                "STUB_PARAMS": __import__("json").dumps(params),
                "VERIFY_LIVE_SCRIPT_DIR": str(fake),
            },
        )

    return go


def test_the_three_endpoints_reach_the_gate_in_its_environment(harness):
    result = harness(ENDPOINTS)
    assert result.returncode == 0, result.stderr
    for url in ENDPOINTS.values():
        assert url in result.stdout, result.stdout


def test_a_red_gate_is_a_red_verify(harness):
    """This script resolves parameters. It must not be able to turn a failing gate green."""
    result = harness(ENDPOINTS, gate=RED_GATE)
    assert result.returncode != 0


@pytest.mark.parametrize("missing", sorted(ENDPOINTS))
def test_a_missing_endpoint_parameter_refuses_before_the_gate_runs(harness, missing):
    """An unresolved URL reaches curl as no argument at all, and curl's usage error reads
    nothing like "that endpoint was never published"."""
    result = harness({k: v for k, v in ENDPOINTS.items() if k != missing})
    assert result.returncode != 0
    assert missing in result.stderr, result.stderr
    assert "gate" not in result.stdout, "the gate ran with an unresolved endpoint"


@pytest.mark.parametrize("empty_value", ["", "None"])
def test_an_empty_endpoint_parameter_is_refused_too(harness, empty_value):
    """`aws ssm get-parameter --output text` prints the literal `None` for an absent field
    and exits 0, and `None` is a perfectly good shell string."""
    params = dict(ENDPOINTS)
    params["/toxic/endpoints/frontend"] = empty_value
    result = harness(params)
    assert result.returncode != 0
    assert "gate" not in result.stdout


def test_no_endpoint_has_a_default():
    """A default of localhost passes on the operator's laptop while the fleet is dark."""
    code = shell_code(SCRIPT)
    for marker in ("localhost", "127.0.0.1", ":-http"):
        assert marker not in code, f"verify_live.sh carries a default endpoint: {marker}"


def test_the_gate_is_verify_deploy_and_not_a_reimplementation_of_it():
    code = shell_code(SCRIPT)
    assert "verify_deploy.sh" in code
    assert "curl" not in code, "the probing belongs to the gate, not to the parameter resolver"
