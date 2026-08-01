"""The port exposure contract, and the Terraform it holds to account.

H12 in one sentence: opening 8501 for a grader must not also open the console that writes
the graded metric. That is a Terraform property, but Phase A2 owns the only Terraform root
module -- a second declaration of one security group is a `terraform validate` failure and
`terraform validate` is a required CI check. So this file asserts the Python contract
unconditionally, and asserts A2's `infra/terraform/` against it whenever that module is
present in the tree.

`TERRAFORM_PORT_LOCALS` is written out longhand on purpose. Comparing `PORTS` to itself
proves nothing; comparing it to a second, independently maintained statement of the same
fact is what catches a renumber.
"""

import re
from pathlib import Path

import pytest

from infra.exposure import DEMO_EXPOSED_PORTS, OPERATOR_ONLY_PORTS, PORTS

TF_ROOT = Path("infra/terraform")
RETIRED_TF = Path("infra/terraform/app_ingress.tf")

# Phase A2 network.tf `locals.ports`, restated. A2 and Phase 3 use different names for the
# same two tiers, which is exactly why the mapping is written down rather than assumed.
TERRAFORM_PORT_LOCALS = {
    "backend": 8000,
    "frontend": 8501,
    "monitoring": 8502,
    "reviewer_ui": 8503,
}


def test_reviewer_ui_is_operator_only():
    assert PORTS["reviewer_ui"].number == 8503
    assert PORTS["reviewer_ui"].demo_exposed is False
    assert 8503 in OPERATOR_ONLY_PORTS
    assert 8503 not in DEMO_EXPOSED_PORTS


def test_the_demo_toggle_covers_exactly_the_graded_surface():
    assert DEMO_EXPOSED_PORTS == {8000, 8501, 8502}


def test_every_port_is_unique():
    numbers = [port.number for port in PORTS.values()]
    assert len(numbers) == len(set(numbers))


def test_every_port_names_a_distinct_terraform_local():
    locals_names = [port.tf_local for port in PORTS.values()]
    assert len(locals_names) == len(set(locals_names))


def test_the_python_contract_states_the_terraform_port_map():
    """Unconditional half of the cross-check: a renumber on the Python side goes red here
    even on a branch where Phase A2's root module is not checked out."""
    assert {port.tf_local: port.number for port in PORTS.values()} == TERRAFORM_PORT_LOCALS


def test_the_exposure_contract_declares_no_terraform_of_its_own():
    assert not RETIRED_TF.exists(), (
        "security groups are declared once, in Phase A2's network.tf; two declarations of "
        "one address is a `terraform validate` failure and CI is a required check"
    )


def _tf_sources() -> list[Path]:
    return sorted(TF_ROOT.rglob("*.tf")) if TF_ROOT.is_dir() else []


def _require_terraform() -> None:
    if not TF_ROOT.is_dir():
        pytest.skip(
            "infra/terraform/ is Phase A2's root module and is not on this branch; this "
            "assertion goes live the moment A2 merges, and TERRAFORM_PORT_LOCALS above "
            "carries the same fact in the meantime"
        )


def test_the_terraform_locals_match_the_python_contract():
    _require_terraform()
    source = (TF_ROOT / "network.tf").read_text(encoding="utf-8")
    block = re.search(r"locals\s*\{(.*?)\n\}", source, re.S)
    assert block, "network.tf must declare locals.ports so the Python contract can be compared"
    pairs = re.findall(r"(\w+)\s*=\s*(\d+)", block.group(1))
    declared = {name: int(value) for name, value in pairs}
    assert declared == {port.tf_local: port.number for port in PORTS.values()}


def test_no_terraform_rule_of_any_kind_reaches_an_operator_only_port():
    """H12, stated over every .tf file rather than over one named resource: the reviewer
    port has no ingress anywhere, which is the whole justification docs/tls-decision.md
    gives for accepting cleartext HTTP."""
    _require_terraform()
    by_local = {port.tf_local: port.number for port in PORTS.values()}
    offenders: list[str] = []
    for path in _tf_sources():
        source = path.read_text(encoding="utf-8")
        blocks = re.findall(r"ingress\s*\{(.*?)\n\s*\}", source, re.S)
        blocks += re.findall(
            r'resource\s+"aws_vpc_security_group_ingress_rule"\s+"[^"]+"\s*\{(.*?)\n\}',
            source,
            re.S,
        )
        for block in blocks:
            ports = {int(value) for value in re.findall(r"(?:from|to)_port\s*=\s*(\d+)", block)}
            ports |= {
                by_local[name]
                for name in re.findall(r"local\.ports\.(\w+)", block)
                if name in by_local
            }
            reached = ports & OPERATOR_ONLY_PORTS
            if reached:
                offenders.append(f"{path}: {sorted(reached)}")
    assert not offenders, offenders
