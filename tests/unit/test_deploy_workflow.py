"""H35, C7, DELIV-3. The deploy workflow is the highest-privilege code in the repository.

Four defects close here. Unpinned third-party Actions can mint the `gha-deploy` OIDC token.
The repository's default workflow permission is *write* (confirmed against the live
repository: `default_workflow_permissions: "write"`), and nothing narrows it unless the
workflow does. An unattended `terraform apply` on every push to `main` would mean a README
typo can replace three instances. And the account id lands in world-readable Actions logs the
moment an ECR URI is printed.

`tests/unit/test_workflow_hygiene.py` already applies the repository-wide scans -- pinning,
version comments, per-job permissions, pinned runners, no `continue-on-error`,
no `pull_request_target` -- to every file under `.github/workflows`, including this one. What
is here is what is true of the DEPLOY workflow specifically and of no other.
"""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/deploy.yml")
CREDENTIALS_ACTION = "aws-actions/configure-aws-credentials"


def _doc() -> dict:
    # PyYAML parses the bare key `on:` as boolean True. Normalise it.
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _body() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps() -> list[dict]:
    steps: list[dict] = []
    for job in _doc()["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _executed() -> str:
    """Everything this workflow actually runs: every `run:` body and every `uses:` reference.

    Deliberately not the file. This file is more comment than YAML, and the comments name the
    very things the checks below forbid -- the paragraph explaining WHY `terraform apply` is
    an operator action contains the string `terraform apply`. A scan that reads its own
    rationale and fails is a scan somebody deletes.
    """
    parts: list[str] = []
    for step in _steps():
        parts.append(str(step.get("run", "")))
        parts.append(str(step.get("uses", "")))
    return "\n".join(parts)


def test_every_action_is_pinned_to_a_full_commit_sha():
    """H35. A floating tag on any of these can mint the gha-deploy OIDC token."""
    for step in _steps():
        uses = step.get("uses")
        if not uses:
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), f"unpinned action: {uses}"


def test_every_pinned_action_records_the_version_it_pins():
    for line in _body().splitlines():
        if "uses:" in line and "@" in line:
            assert re.search(r"#\s*v?\d+\.\d+", line), f"no version comment: {line.strip()}"


def test_top_level_permissions_are_empty():
    """H35. The repository default is write; an empty top level makes every job opt in."""
    assert _doc()["permissions"] == {}


def test_each_job_requests_only_what_it_needs():
    for name, job in _doc()["jobs"].items():
        permissions = job.get("permissions")
        assert permissions is not None, f"job {name} inherits permissions"
        assert set(permissions) <= {"id-token", "contents", "packages"}, name
        assert permissions.get("contents", "read") == "read", f"job {name} may write the repo"


def test_deploy_workflow_never_runs_terraform_apply():
    """C7. A README typo must not be able to replace all three instances."""
    executed = _executed()
    assert "terraform apply" not in executed
    assert "terraform destroy" not in executed
    assert "terraform" not in executed, "the deploy path runs no Terraform at all"


def test_docs_only_pushes_cannot_trigger_deploy():
    paths_ignore = _doc()["on"]["push"]["paths-ignore"]
    for pattern in ("docs/**", "**.md"):
        assert pattern in paths_ignore, f"{pattern} is not ignored"


def test_the_model_card_trap_that_paths_ignore_creates_is_recorded_where_it_is_created():
    """`**.md` also ignores MODEL_CARD.md, and backend/Dockerfile BAKES that file.

    So a card-only commit -- a new promoted digest, a new registry version -- reaches `main`
    and does not reach production, and the next unrelated code push carries the model swap
    with it. That is a real consequence of a filter written for README churn, and the place a
    future editor will look is the filter itself. `gh workflow run deploy.yml --ref main` is
    the deliberate action; this asserts the workflow says so where the trap lives, not in a
    document nobody opens while editing YAML.
    """
    body = _body()
    filter_block = body[body.index("paths-ignore:") : body.index("workflow_dispatch:")]
    context = body[: body.index("paths-ignore:")][-1200:] + filter_block
    assert "MODEL_CARD.md" in context, (
        "paths-ignore hides MODEL_CARD.md from the push trigger and nothing next to it says so"
    )
    assert "workflow_dispatch" in body


def test_the_production_environment_gates_the_deploy():
    jobs = _doc()["jobs"]
    assert any(job.get("environment") == "production" for job in jobs.values())


def test_every_job_that_assumes_the_deploy_role_declares_the_production_environment():
    """The IAM trust policy is single-valued, so this is a hard requirement, not hygiene.

    `infra/terraform/oidc.tf` conditions `sts:AssumeRoleWithWebIdentity` on
    `token.actions.githubusercontent.com:sub == repo:<repo>:environment:production` -- with
    `StringEquals` and one value, deliberately, because a two-element array is evaluated by
    IAM as an OR and that is premortem H4. A job without `environment: production` mints a
    token whose `sub` is `repo:<repo>:ref:refs/heads/main`, which matches nothing, and the
    step fails with `Not authorized to perform sts:AssumeRoleWithWebIdentity`. A build job
    that pushes five images and cannot get credentials is the whole deploy.
    """
    for name, job in _doc()["jobs"].items():
        assumes = any(
            str(step.get("uses", "")).startswith(CREDENTIALS_ACTION)
            for step in job.get("steps", [])
        )
        if not assumes:
            continue
        assert job.get("environment") == "production", (
            f"job {name} assumes the deploy role without declaring `environment: production`; "
            "the OIDC trust policy requires that claim in `sub` and will refuse the token"
        )


def test_no_static_aws_credential_reaches_the_workflow():
    """OIDC or nothing. A long-lived key in a repository secret is the thing OIDC replaces."""
    body = _body()
    for marker in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "aws-access-key-id"):
        assert marker not in body, f"{marker} appears in the deploy workflow"


def test_the_workflow_checks_out_the_tree_it_is_deploying():
    """`-f sha=<older>` must build that SHA's code, not `main` labelled with its tag.

    ECR tags are immutable, so the image push would fail loudly -- but the S3 deploy payload
    is not immutable, and `deploy/<sha>/` would be silently overwritten with roll.sh,
    compose files and MODEL_CARD.md from a different commit. The instance then installs one
    commit's scripts alongside another commit's containers.
    """
    doc = _doc()
    tag = doc["env"]["IMAGE_TAG"]
    checkouts = [
        step for step in _steps() if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts, "the workflow never checks out the repository"
    accepted = {tag.replace(" ", ""), "${{env.IMAGE_TAG}}"}
    for step in checkouts:
        ref = (step.get("with") or {}).get("ref")
        assert ref is not None, "checkout takes the triggering ref, not the SHA being deployed"
        assert ref.replace(" ", "") in accepted, (
            f"checkout ref {ref!r} is not the SHA the images are tagged with ({tag!r})"
        )


def test_checkout_does_not_leave_a_token_in_the_git_config():
    """`persist-credentials: true` is the default and writes the token into .git/config, where
    every later step -- including a docker build context -- can read it."""
    for step in _steps():
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert (step.get("with") or {}).get("persist-credentials") is False, step


def test_account_id_is_masked_before_the_first_ecr_step():
    """DELIV-3. Actions logs on a public repository are world-readable."""
    steps = _steps()
    mask_index = next(i for i, s in enumerate(steps) if "add-mask" in str(s.get("run", "")))
    ecr_index = next(
        i
        for i, s in enumerate(steps)
        if "ecr" in str(s.get("uses", "")).lower() or "ecr" in str(s.get("run", "")).lower()
    )
    assert mask_index < ecr_index, "the account id is printed before it is masked"


def test_every_job_that_touches_ecr_masks_the_account_id_first():
    """One masked job and one unmasked job is the same leak. `::add-mask::` is per-job."""
    for name, job in _doc()["jobs"].items():
        steps = job.get("steps", [])
        touches = [
            i
            for i, s in enumerate(steps)
            if "ecr" in str(s.get("uses", "")).lower() or "ecr" in str(s.get("run", "")).lower()
        ]
        if not touches:
            continue
        masks = [i for i, s in enumerate(steps) if "add-mask" in str(s.get("run", ""))]
        assert masks and min(masks) < min(touches), f"job {name} reaches ECR before masking"


def test_the_roll_is_asserted_and_then_verified_over_http():
    """H5. The order matters: assert the roll, THEN prove the endpoints answer."""
    body = _body()
    roll = body.index("ssm_run.sh")
    verify = body.index("verify_live.sh")
    record = body.index("record_deploy.sh")
    assert roll < verify < record, "a failed deploy must not be recorded as the current SHA"


def test_the_roll_expects_exactly_one_instance_per_component():
    body = _body()
    for component in ("backend", "frontend", "monitoring"):
        assert re.search(rf"ssm_run\.sh {component} 1 ", body), component


def test_the_roll_names_the_component_as_well_as_the_sha():
    """`roll.sh` refuses to run without one. Nothing on the running instances writes
    /etc/toxic/component -- the applied user data predates it -- and reading the Component tag
    would need an ec2:DescribeTags grant no role has. A payload of `bootstrap.sh <sha>` alone
    dies with `unknown component ''` on every instance, after SSM has already reported the
    send succeeded."""
    body = _body()
    for component in ("backend", "frontend", "monitoring"):
        assert re.search(
            rf"ssm_run\.sh {component} 1 bash /opt/toxic/bootstrap\.sh \S+ {component}\b", body
        ), f"the {component} roll does not pass its component to bootstrap.sh"


def test_no_ecr_repository_name_is_hardcoded():
    """Repository names are `<project>-<component>`, and `project` is a Terraform variable.

    `infra/terraform/deploy.tf` publishes each one at /toxic/images/<component> for exactly
    this reason: the plan for this phase assumed `toxic-<component>` while the applied account
    has `toxic-mod-<component>`, and every push would have gone to a repository that does not
    exist. A literal here is the same bug with a longer feedback loop -- ECR creates nothing
    on push, so it fails, but only after the whole image has been built.
    """
    body = _body()
    assert "/toxic/images/" in body, "the workflow does not read the repository names from SSM"
    for literal in ("toxic-backend", "toxic-frontend", "toxic-monitoring", "toxic-rescorer",
                    "toxic-mod-backend", "toxic-mod-frontend", "toxic-mod-monitoring"):
        assert literal not in body, f"hardcoded ECR repository name: {literal}"


def test_the_build_matrix_covers_five_images_across_four_repositories():
    body = _body()
    for image in ("backend", "frontend", "reviewer", "monitoring", "rescorer"):
        assert image in body, image
    assert "-reviewer" in body, "the reviewer image shares the frontend repository by tag"


def test_no_step_writes_a_secret_to_the_log_or_to_ssm():
    for line in _body().splitlines():
        if "ssm" in line and "${{ secrets." in line:
            raise AssertionError(f"a GitHub secret reaches SSM: {line.strip()}")


def test_the_workflow_can_be_dispatched_manually_for_a_rollback():
    assert "workflow_dispatch" in _doc()["on"]


def test_the_deploy_is_serialised_and_never_cancelled_mid_roll():
    """Two concurrent rolls put two SHAs on three instances. Cancelling one mid-roll leaves
    the fleet split, which is worse than either SHA."""
    concurrency = _doc()["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert concurrency["group"]


def test_every_script_the_workflow_runs_exists_in_the_repository():
    """Derived from the workflow rather than listed here, so a script added to the roll cannot
    be one that only exists on the author's machine. A missing one is discovered inside a
    production job that has already pushed five images."""
    named = sorted(set(re.findall(r"infra/aws/[\w.-]+\.sh", _executed())))
    assert named, "the workflow runs none of the repository's deploy scripts"
    for script in named:
        assert Path(script).exists(), f"the workflow runs {script}, which does not exist"


def test_the_three_scripts_that_make_the_deploy_safe_are_all_in_the_roll():
    """Named explicitly as well, because the check above passes on a workflow that runs one
    harmless script and skips the count assertion, the health gate and the record."""
    executed = _executed()
    for script, why in (
        ("infra/aws/ssm_run.sh", "the roll would be fire-and-forget"),
        ("infra/aws/verify_live.sh", "SSM Success would be mistaken for a working deploy"),
        ("infra/aws/record_deploy.sh", "rollback would have no target"),
    ):
        assert script in executed, f"{script} is not run: {why}"
