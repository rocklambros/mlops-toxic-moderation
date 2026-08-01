"""The `terraform` job cannot report success without having validated Terraform.

`terraform fmt -check -recursive infra/` over a tree with no `.tf` files exits 0.
`terraform init -backend=false` in an empty directory exits 0, and so does `terraform
validate` after it. So the whole job is green on a branch where `infra/terraform` does not
exist -- which is this branch, because Phase A2 is unmerged. That is the same shape as the
vacuous `.github/workflows` scan `tests/unit/test_workflow_hygiene.py` was written to prevent:
a control that reports success over an empty set.

Two assertions, deliberately different in kind:

* the guard step is asserted *statically*, from the committed workflow, and is therefore
  provable today. It is what makes the job fail loudly instead of passing vacuously.
* the commands themselves are only run when there is something to run them against. When
  `infra/terraform` is absent this file SKIPS with the reason named, rather than passing --
  a green result here would say "Terraform is valid" about a tree containing no Terraform.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
TF_ROOT = REPO / "infra" / "terraform"


def terraform_job() -> dict:
    document = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = (document.get("jobs") or {}).get("terraform")
    assert job is not None, "ci.yml declares no `terraform` job"
    return job


def run_bodies() -> list[str]:
    return [str(step.get("run") or "") for step in terraform_job().get("steps") or []]


def test_the_terraform_job_guards_against_an_absent_directory_before_it_validates():
    """Without this step the job is a green check over nothing at all."""
    bodies = run_bodies()
    validating = [
        index
        for index, body in enumerate(bodies)
        if "terraform fmt" in body or "terraform validate" in body
    ]
    assert validating, "the terraform job runs neither `fmt` nor `validate`"
    guards = [
        index
        for index, body in enumerate(bodies)
        if "infra/terraform" in body and "exit 1" in body
    ]
    assert guards, (
        "no step fails the job when infra/terraform is absent, so `fmt -check -recursive` over "
        "an empty tree and `validate` after an empty `init` both exit 0 and the job certifies "
        "that nothing is valid"
    )
    assert min(guards) < min(validating), (
        "the guard runs after the commands it is supposed to gate, so the job has already "
        "reported success on an empty tree by the time it fires"
    )


def test_the_guard_also_refuses_a_directory_with_no_terraform_in_it():
    """`mkdir infra/terraform` would satisfy a directory-existence check on its own, and
    `init`/`validate` in an empty directory exit 0. The file-count check is the half of the
    guard that survives a half-finished merge."""
    guard = "\n".join(body for body in run_bodies() if "infra/terraform" in body)
    assert "*.tf" in guard, (
        "the guard checks only that the directory exists; an empty infra/terraform still "
        "passes fmt, init and validate"
    )


def test_the_pinned_terraform_version_is_a_version_the_job_can_install():
    """The job reads `.terraform-version` and hands it to setup-terraform. A missing or
    unparseable file makes every future run install whatever the action defaults to."""
    pinned = (REPO / ".terraform-version").read_text(encoding="utf-8").strip()
    assert pinned.count(".") == 2 and all(part.isdigit() for part in pinned.split(".")), (
        f".terraform-version holds {pinned!r}, which is not an X.Y.Z release"
    )
    assert ".terraform-version" in "\n".join(run_bodies()), (
        "nothing in the terraform job reads .terraform-version, so the pin is decorative"
    )


def test_terraform_fmt_and_validate_pass_on_the_tree_this_branch_carries():
    """The commands the runner will run, run here. Skipped -- loudly, with the reason named --
    rather than passed, when there is nothing to run them against."""
    if not TF_ROOT.is_dir() or not any(TF_ROOT.rglob("*.tf")):
        pytest.skip(
            "NOT VERIFIED: infra/terraform is absent from this branch (Phase A2 is unmerged), "
            "so `terraform fmt`/`init`/`validate` have nothing to check. The ci.yml guard "
            "asserted above is what makes the job FAIL rather than pass in this state; this "
            "case turns real the moment Phase A2 lands"
        )
    binary = shutil.which("terraform")
    if binary is None:
        pytest.skip("NOT VERIFIED: no `terraform` binary on PATH to run fmt/init/validate with")

    fmt = subprocess.run(
        [binary, "fmt", "-check", "-recursive", "infra/"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert fmt.returncode == 0, f"terraform fmt -check reported drift:\n{fmt.stdout}{fmt.stderr}"

    init = subprocess.run(
        [binary, "init", "-backend=false", "-input=false"],
        cwd=TF_ROOT, capture_output=True, text=True,
    )
    assert init.returncode == 0, f"terraform init failed:\n{init.stdout}{init.stderr}"
    validate = subprocess.run(
        [binary, "validate"], cwd=TF_ROOT, capture_output=True, text=True
    )
    assert validate.returncode == 0, (
        f"terraform validate failed:\n{validate.stdout}{validate.stderr}"
    )
