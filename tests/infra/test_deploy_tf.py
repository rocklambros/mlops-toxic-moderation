"""Phase 5's Terraform surface, and the A2 properties the deploy depends on.

Everything the deploy scripts read has to exist before they read it. The failure this file
is written against is the quiet one: `roll.sh` calls `aws ssm get-parameter --name
/toxic/db/endpoint`, Terraform never declared that parameter, and the first anyone learns of
it is a `ParameterNotFound` inside an SSM invocation on a box with no SSH.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.infra import tfparse

REPO = Path(__file__).resolve().parents[2]
TERRAFORM = REPO / "infra" / "terraform"
NETWORK = TERRAFORM / "network.tf"
DEPLOY = TERRAFORM / "deploy.tf"
IAM = TERRAFORM / "iam.tf"
OIDC = TERRAFORM / "oidc.tf"

# The parameter namespace is a contract between Terraform and the instance scripts. Declared
# here so that adding a parameter is a visible diff in two places rather than one.
EXPECTED_PARAMETERS = {
    "/toxic/deploy/bucket",
    "/toxic/deploy/registry",
    "/toxic/logs/backend",
    "/toxic/logs/frontend",
    "/toxic/logs/monitoring",
    "/toxic/logs/rescorer",
    "/toxic/endpoints/backend",
    "/toxic/endpoints/frontend",
    "/toxic/endpoints/monitoring",
    "/toxic/endpoints/backend-internal",
    "/toxic/db/endpoint",
    "/toxic/db/name",
    "/toxic/db/master-secret-arn",
    "/toxic/db/readonly-secret-arn",
    "/toxic/secrets/wandb-api-key",
    "/toxic/secrets/reviewer-shared-secret",
    "/toxic/secrets/demo-api-key",
    "/toxic/secrets/submitter-fp-key",
    "/toxic/model/wandb-artifact",
    "/toxic/reviewer/id",
}


def declared_parameters() -> set[str]:
    """Every `/toxic/...` name deploy.tf publishes, read from the map it publishes them from."""
    source = tfparse.source_of("deploy.tf")
    return set(re.findall(r'"(/toxic/[A-Za-z0-9/_-]+)"\s*=', source))


def test_public_subnets_auto_assign_a_public_ip():
    """H26. If they do not, user data runs before the EIP attaches and has no route."""
    source = NETWORK.read_text(encoding="utf-8")
    subnets = tfparse.resources_of_kind("aws_subnet")
    public = {name: body for name, body in subnets.items() if "public" in name}
    assert public, "no public subnet is declared"
    for name, body in public.items():
        assert body.get("map_public_ip_on_launch") is True, (
            f"{name} does not auto-assign a public IP; user data will boot with no route"
        )
    assert "map_public_ip_on_launch" in source


def test_instance_roles_may_write_only_the_boot_marker_parameters():
    source = IAM.read_text(encoding="utf-8")
    assert "ssm:PutParameter" in source
    assert "parameter/toxic/boot/*" in source
    assert not re.search(r'"ssm:PutParameter"[^}]*parameter/toxic/\*', source, re.S), (
        "do not grant write over the whole namespace: an instance able to rewrite "
        "/toxic/deploy/current-sha can lie about what it is running"
    )


def test_deploy_bucket_exists_and_blocks_public_access():
    assert "deploy" in tfparse.resource_names("aws_s3_bucket")
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_public_access_block")
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_server_side_encryption_configuration")
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_versioning")
    block = tfparse.resources_of_kind("aws_s3_bucket_public_access_block")["deploy"]
    for setting in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                    "restrict_public_buckets"):
        assert block.get(setting) is True, f"{setting} is not set on the deploy bucket"


def test_the_deploy_bucket_is_emptied_on_destroy():
    """terraform destroy is cost control #2; a non-empty bucket blocks it."""
    assert tfparse.resources_of_kind("aws_s3_bucket")["deploy"].get("force_destroy") is True


def test_the_lifecycle_rule_never_expires_the_database_dumps():
    """The dumps ARE the graded dashboard dataset (H6). Expiring them on a schedule would
    delete the evidence the demo is graded on, silently, thirty days in."""
    source = tfparse.source_of("deploy.tf")
    lifecycle = source.split('resource "aws_s3_bucket_lifecycle_configuration" "deploy"')[1]
    db_rule = lifecycle.split('prefix = "db/"')[1].split("rule {")[0]
    assert "expiration" not in db_rule, "a dump expiry rule would delete the graded dataset"


def test_every_parameter_the_deploy_scripts_read_is_declared():
    """The namespace is the contract. A script reading a name Terraform never published
    fails with ParameterNotFound inside an SSM invocation, on a host with no SSH."""
    declared = declared_parameters()
    assert declared == EXPECTED_PARAMETERS, (
        f"missing: {sorted(EXPECTED_PARAMETERS - declared)}; "
        f"undeclared extras: {sorted(declared - EXPECTED_PARAMETERS)}"
    )


def test_the_frontend_is_pointed_at_the_backends_private_address():
    """sg-frontend permits egress to 8000 only inside the public subnet CIDRs, so a UI
    container configured with the backend's Elastic IP sends its traffic out through the
    internet gateway, misses that rule, and is dropped. The graded UI would render and every
    prediction would time out."""
    source = tfparse.source_of("deploy.tf")
    internal = re.search(r'"/toxic/endpoints/backend-internal"\s*=\s*([^\n]+)', source)
    assert internal, "no private-address endpoint is published for the frontend"
    assert "backend_internal_url" in internal.group(1), internal.group(1)
    public = re.search(r'"/toxic/endpoints/backend"\s*=\s*([^\n]+)', source)
    assert public and "aws_eip.backend" in public.group(1), "the public endpoint is not the EIP"


def test_instance_roles_can_read_the_deploy_payload_and_the_artifact_mirror():
    source = IAM.read_text(encoding="utf-8")
    assert "s3:GetObject" in source, "no instance role can fetch the deploy payload"
    assert "/deploy/*" in source and "/artifacts/*" in source, (
        "the S3 grant is not scoped to the deploy payload and the artifact mirror"
    )
    assert '"${aws_s3_bucket.deploy.arn}/db/*"' not in source, (
        "an instance can read the database dumps"
    )
    for role in ("backend_deploy_payload", "frontend_deploy_payload", "monitoring_deploy_payload"):
        assert role in tfparse.resource_names("aws_iam_role_policy"), f"{role} is not attached"


def test_the_tier_that_verifies_the_reviewer_secret_is_the_tier_that_can_read_it():
    """backend/review_api.py is what HMACs the reviewer session token; frontend/reviewer.py
    reads BACKEND_URL and DEMO_API_KEY and nothing else. Granting the secret to the
    internet-facing Streamlit tier instead of the verifier is premortem H16's harm sentence
    with the wrong tier's name in it."""
    source = IAM.read_text(encoding="utf-8")
    backend = source.split('data "aws_iam_policy_document" "backend"')[1].split("\n}\n")[0]
    frontend = source.split('data "aws_iam_policy_document" "frontend"')[1].split("\n}\n")[0]
    assert "reviewer_shared_secret" in backend, "the verifier cannot read the secret it verifies"
    assert "reviewer_shared_secret" not in frontend, (
        "the internet-facing UI tier still holds a credential it never reads"
    )
    assert "master_user_secret" not in frontend and "db_readonly" not in frontend


def test_the_deploy_role_can_write_the_payload_it_is_supposed_to_upload():
    """gha-deploy carries an explicit `Deny s3:*`, and an explicit Deny cannot be overridden
    by any later Allow. Left as written, every deploy fails at the upload with AccessDenied
    and no Allow anyone adds will ever take effect."""
    source = OIDC.read_text(encoding="utf-8")
    denies = re.search(r'sid\s*=\s*"DenyPrivilegeEscalationAndInfrastructureChange"(.*?)\n  \}',
                       source, re.S)
    assert denies, "the escalation Deny is gone"
    assert '"s3:*"' not in denies.group(1), (
        "the blanket S3 Deny is back; it makes the deploy payload upload impossible"
    )
    assert "not_resources" in source, "the S3 Deny is not scoped away from the deploy bucket"
    assert "s3:PutObject" in source, "gha-deploy cannot upload a deploy payload"


def test_the_deploy_bucket_name_is_not_committed_anywhere():
    """The bucket name embeds the account id and this repository is public. Terraform builds
    it from a data source; nothing may hardcode the rendered value."""
    source = tfparse.source_of("deploy.tf")
    assert not re.search(r"(?<![0-9A-Fa-f])[0-9]{12}(?![0-9A-Fa-f])", source), (
        "deploy.tf carries a literal twelve-digit account id"
    )
