"""The Sandbox OU service control policy, tested offline as the pure data artifact it is.

Three of these assertions exist because the guardrail they cover was wrong in an earlier
draft and the mistake was invisible in review:

* H3 -- `ec2:InstanceType` is a *resource-level* condition key on the `instance` resource.
  A statement scoped to `Resource: "*"` also covers the volume and the network interface
  that the same `RunInstances` call creates, neither of which carries the key, and a
  `StringNotEquals` test against an absent key matches. The deny then fires on every
  launch, including the four classes this project needs.
* H18 -- `Bool` and `BoolIfExists` differ only when the key is absent, and the correct
  operator differs per action. `_deny_fires` below models that rule so both directions
  are tested rather than asserted, and `test_the_two_operators_actually_differ_on_absence`
  keeps the model honest.
* H19 -- `ec2:RunInstances` is one launch path of four, and `ec2:ModifyInstanceAttribute`
  reaches any instance type on an existing instance.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCP_PATH = REPO_ROOT / "infra" / "aws" / "scp-sandbox-guardrails.json"

# The three-instance topology (backend, frontend, monitoring) plus the sanctioned
# upsize target. Written down here because H3 is that it was never written down.
ALLOWED_INSTANCE_TYPES = {"t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"}

LAUNCH_ACTIONS = {
    "ec2:RunInstances",
    "ec2:CreateFleet",
    "ec2:RequestSpotInstances",
    "ec2:RequestSpotFleet",
}

# AWS caps an SCP document at 5120 characters, whitespace excluded.
SCP_SIZE_QUOTA = 5120


@pytest.fixture(scope="module")
def policy():
    return json.loads(SCP_PATH.read_text())


def statement(policy, sid):
    matches = [s for s in policy["Statement"] if s.get("Sid") == sid]
    assert matches, f"no statement with Sid {sid}; the guardrail it carries is absent"
    return matches[0]


def _policy_bool(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


def _deny_fires(stmt, context):
    """Does this statement's Condition block match the given request context?

    Only the operators this document uses are modelled, with AWS's documented
    key-absence rules -- which is the entire point of the RDS cases below:

        Bool             key absent -> no match -> the Deny does NOT fire (fails open)
        BoolIfExists     key absent -> match    -> the Deny DOES fire (fails closed)
        StringNotEquals  key absent -> match    -> negated operators match on absence
    """
    condition = stmt.get("Condition")
    if condition is None:
        return True
    for operator, tests in condition.items():
        for key, want in tests.items():
            wanted = want if isinstance(want, list) else [want]
            present = key in context
            if operator == "Bool":
                if not present or _policy_bool(context[key]) not in wanted:
                    return False
            elif operator == "BoolIfExists":
                if present and _policy_bool(context[key]) not in wanted:
                    return False
            elif operator == "StringNotEquals":
                if present and context[key] in wanted:
                    return False
            else:
                raise AssertionError(f"unmodelled condition operator: {operator}")
    return True


# --- document shape ------------------------------------------------------------------


def test_the_document_is_valid_policy_json():
    body = json.loads(SCP_PATH.read_text())
    assert body["Version"] == "2012-10-17"
    assert isinstance(body["Statement"], list)


def test_every_statement_is_a_deny(policy):
    # An SCP that Allows anything is a filter that grants nothing and confuses review.
    assert [s["Sid"] for s in policy["Statement"] if s["Effect"] != "Deny"] == []


def test_every_statement_has_a_unique_sid(policy):
    sids = [s["Sid"] for s in policy["Statement"]]
    assert len(set(sids)) == len(sids)


def test_the_region_lock_is_the_first_statement(policy):
    assert policy["Statement"][0]["Sid"] == "DenyOutsideHomeRegion"


def test_the_policy_fits_the_scp_size_quota(policy):
    compact = json.dumps(policy, separators=(",", ":"))
    assert len(compact) < SCP_SIZE_QUOTA, f"compacted document is {len(compact)} characters"


# --- H3: the instance-type allowlist --------------------------------------------------


def test_the_instance_type_allowlist_is_exactly_the_four_required_classes(policy):
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    listed = stmt["Condition"]["StringNotEquals"]["ec2:InstanceType"]
    assert set(listed) == ALLOWED_INSTANCE_TYPES
    assert len(listed) == len(ALLOWED_INSTANCE_TYPES), "no duplicates"


def test_the_allowlist_uses_stringnotequals_not_a_wildcard_operator(policy):
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    # StringNotLike would readmit t4g.2xlarge and every other size in the family.
    assert list(stmt["Condition"]) == ["StringNotEquals"]


def test_the_allowlist_denies_a_class_outside_it_and_permits_every_class_inside_it(policy):
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    for allowed in sorted(ALLOWED_INSTANCE_TYPES):
        assert not _deny_fires(stmt, {"ec2:InstanceType": allowed}), allowed
    for denied in ("t4g.xlarge", "t4g.2xlarge", "g5.xlarge", "m5.large"):
        assert _deny_fires(stmt, {"ec2:InstanceType": denied}), denied


def test_the_instance_type_statement_is_scoped_to_the_instance_resource(policy):
    # H3's resource-scoping trap: see the module docstring.
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    assert stmt["Resource"] == "arn:aws:ec2:*:*:instance/*"


def test_an_absent_instance_type_key_fires_the_deny_which_is_why_the_resource_is_scoped(
    policy,
):
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    # A volume or network interface created by the same RunInstances call carries no
    # ec2:InstanceType. Scoped to "*" this statement would deny every launch.
    assert _deny_fires(stmt, {}) is True


# --- H19: every launch path, and instance-type mutation -------------------------------


def test_all_four_launch_paths_carry_the_allowlist(policy):
    stmt = statement(policy, "DenyNonAllowlistedInstanceLaunch")
    assert set(stmt["Action"]) == LAUNCH_ACTIONS


def test_modify_instance_attribute_is_denied_without_a_condition(policy):
    stmt = statement(policy, "DenyInstanceAttributeMutation")
    assert stmt["Action"] == ["ec2:ModifyInstanceAttribute"]
    assert stmt["Resource"] == "arn:aws:ec2:*:*:instance/*"
    # ec2:InstanceType on this action resolves to the instance's CURRENT type, so a
    # condition would read t4g.medium while the request moves it to a GPU class and the
    # deny would never fire. Only an unconditional deny is effective.
    assert "Condition" not in stmt
    assert _deny_fires(stmt, {"ec2:InstanceType": "t4g.medium"}) is True


# --- H18: Bool versus BoolIfExists, both directions -----------------------------------


def test_the_two_operators_actually_differ_on_key_absence():
    # Guards the model the three RDS cases below rely on. If this ever passes
    # vacuously, those cases prove nothing.
    bool_stmt = {"Condition": {"Bool": {"k": "true"}}}
    exists_stmt = {"Condition": {"BoolIfExists": {"k": "true"}}}
    assert _deny_fires(bool_stmt, {}) is False
    assert _deny_fires(exists_stmt, {}) is True
    assert _deny_fires(bool_stmt, {"k": True}) is True
    assert _deny_fires(exists_stmt, {"k": True}) is True
    assert _deny_fires(bool_stmt, {"k": False}) is False
    assert _deny_fires(exists_stmt, {"k": False}) is False


def test_rds_create_without_a_managed_master_password_fails_closed_on_key_absence(policy):
    stmt = statement(policy, "DenyRdsCreateWithoutManagedMasterPassword")
    assert stmt["Action"] == ["rds:CreateDBInstance"]
    assert stmt["Condition"] == {"BoolIfExists": {"rds:ManageMasterUserPassword": "false"}}

    # present true -> Terraform's manage_master_user_password = true. Permitted.
    assert not _deny_fires(stmt, {"rds:ManageMasterUserPassword": True})
    # present false -> an explicit opt-out. Denied.
    assert _deny_fires(stmt, {"rds:ManageMasterUserPassword": False})
    # absent -> exactly what a random_password in Terraform state produces. Denied.
    # Bool here would fail open and let that through, which is the outcome the
    # guardrail exists to prevent.
    assert _deny_fires(stmt, {})


def test_rds_create_publicly_accessible_fails_closed_on_key_absence(policy):
    stmt = statement(policy, "DenyRdsCreatePubliclyAccessible")
    assert set(stmt["Action"]) == {"rds:CreateDBInstance", "rds:RestoreDBInstanceFromDBSnapshot"}
    assert stmt["Condition"] == {"BoolIfExists": {"rds:PubliclyAccessible": "true"}}

    assert not _deny_fires(stmt, {"rds:PubliclyAccessible": False})
    assert _deny_fires(stmt, {"rds:PubliclyAccessible": True})
    # RDS's own default when the key is absent is not universally false, and Terraform
    # always sends the key on create, so absence fails closed at no cost.
    assert _deny_fires(stmt, {})


def test_rds_modify_uses_bool_because_absence_means_leave_this_attribute_alone(policy):
    stmt = statement(policy, "DenyRdsModifyToPubliclyAccessible")
    assert stmt["Action"] == ["rds:ModifyDBInstance"]
    assert stmt["Condition"] == {"Bool": {"rds:PubliclyAccessible": "true"}}

    assert _deny_fires(stmt, {"rds:PubliclyAccessible": True})
    assert not _deny_fires(stmt, {"rds:PubliclyAccessible": False})
    # Terraform sends only changed attributes. BoolIfExists here would deny every
    # unrelated ModifyDBInstance -- bumping backup_retention_period, for one.
    assert not _deny_fires(stmt, {})


def test_no_statement_conditions_on_the_unsupported_rds_databaseclass_key(policy):
    # rds:DatabaseClass is not supported on CreateDBInstance. The key would be absent
    # on every call, so a StringNotEquals cap on it would deny all RDS creation.
    assert "rds:DatabaseClass" not in json.dumps(policy)


def test_aurora_clusters_are_denied_outright(policy):
    stmt = statement(policy, "DenyAuroraClusters")
    assert set(stmt["Action"]) == {
        "rds:CreateDBCluster",
        "rds:RestoreDBClusterFromSnapshot",
        "rds:RestoreDBClusterToPointInTime",
    }
    assert "Condition" not in stmt


# --- H17: detective controls and the evidence store -----------------------------------


def test_detective_control_denies_cover_disable_and_redirect_not_only_delete(policy):
    actions = set(statement(policy, "DenyDetectiveControlTampering")["Action"])
    required = {
        # Deleting the trail is the obvious evasion; redirecting it and narrowing it
        # to nothing are the quiet ones.
        "cloudtrail:StopLogging",
        "cloudtrail:DeleteTrail",
        "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors",
        "cloudtrail:PutInsightSelectors",
        # UpdateDetector with enable=false disables GuardDuty without deleting it.
        "guardduty:DeleteDetector",
        "guardduty:UpdateDetector",
        "guardduty:DeleteMembers",
        "guardduty:DisassociateFromAdministratorAccount",
        "guardduty:DisassociateMembers",
        "guardduty:DeletePublishingDestination",
        "guardduty:UpdatePublishingDestination",
        "guardduty:StopMonitoringMembers",
    }
    assert required <= actions, f"undenied detective-control actions: {sorted(required - actions)}"


def test_the_trail_bucket_evidence_deletion_is_denied(policy):
    stmt = statement(policy, "DenyTrailEvidenceDestruction")
    required = {
        "s3:DeleteBucket",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:DeleteBucketPolicy",
        "s3:PutLifecycleConfiguration",
    }
    assert required <= set(stmt["Action"])
    # Both the bucket and its objects, or half the evidence is unprotected.
    assert sorted(stmt["Resource"]) == [
        "arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*",
        "arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*/*",
    ]


def test_the_trail_bucket_creation_verbs_stay_permitted(policy):
    actions = set(statement(policy, "DenyTrailEvidenceDestruction")["Action"])
    # Phase A2's Terraform must still be able to build the bucket under this SCP.
    for verb in ("s3:CreateBucket", "s3:PutBucketVersioning", "s3:PutBucketPolicy"):
        assert verb not in actions


# --- H19 and C11: region lock and static credentials ----------------------------------


def test_the_region_lock_permits_only_us_west_2(policy):
    stmt = statement(policy, "DenyOutsideHomeRegion")
    assert stmt["Condition"]["StringNotEquals"]["aws:RequestedRegion"] == "us-west-2"
    # An Action list here would leave every unlisted service unconstrained; the
    # documented construction is NotAction.
    assert "Action" not in stmt
    assert "us-east-1" not in json.dumps(stmt), "us-east-1 is not a permitted region"


def test_the_region_lock_exempts_the_global_services_terraform_needs(policy):
    exempt = set(statement(policy, "DenyOutsideHomeRegion")["NotAction"])
    required = {
        "iam:*",
        "sts:*",
        "organizations:*",
        "account:*",
        "budgets:*",
        "ce:*",
        "support:*",
        "health:*",
        "cloudfront:*",
        "route53:*",
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
    }
    # Denying any of these breaks terraform apply on iam.tf or on the budget.
    assert required <= exempt, f"missing exemptions: {sorted(required - exempt)}"


def test_the_region_lock_does_not_exempt_a_regional_workload_service(policy):
    exempt = " ".join(statement(policy, "DenyOutsideHomeRegion")["NotAction"])
    for prefix in ("ec2:", "rds:", "ecr:", "ssm:", "secretsmanager:", "logs:",
                   "cloudtrail:", "guardduty:"):
        assert prefix not in exempt, f"exempting {prefix} reopens region sprawl"


def test_the_region_lock_fires_outside_the_home_region_and_not_inside_it(policy):
    stmt = statement(policy, "DenyOutsideHomeRegion")
    assert _deny_fires(stmt, {"aws:RequestedRegion": "us-east-1"})
    assert _deny_fires(stmt, {"aws:RequestedRegion": "eu-west-1"})
    assert not _deny_fires(stmt, {"aws:RequestedRegion": "us-west-2"})


def test_static_credential_creation_is_denied(policy):
    actions = set(statement(policy, "DenyStaticCredentialCreation")["Action"])
    required = {
        "iam:CreateUser",
        "iam:CreateAccessKey",
        "iam:UpdateAccessKey",
        "iam:CreateLoginProfile",
        "iam:UpdateLoginProfile",
        "iam:CreateServiceSpecificCredential",
    }
    assert required <= actions, f"the account could still mint: {sorted(required - actions)}"


def test_organization_escape_is_denied(policy):
    stmt = statement(policy, "DenyOrganizationEscape")
    assert set(stmt["Action"]) == {"organizations:LeaveOrganization", "account:CloseAccount"}
