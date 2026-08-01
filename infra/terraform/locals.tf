# ---------------------------------------------------------------------------
# Locals shared across the root module.
#
# OWNERSHIP MATTERS HERE. Terraform aborts the whole module with
# "Duplicate local value definition" if one name is declared in two files, and
# `terraform validate` is a required check, so a collision takes main red for
# everybody rather than failing only the file that caused it. The locals owned
# by other files at the time of writing:
#
#   network.tf   ports, public_cidrs, private_cidrs
#   ecr.tf       components
#   compute.tf   ecr_registry, backend_internal_url
#   budget.tf    budget_notifications
#
# Nothing in this file may re-declare any of those, and nothing added to those
# files may re-declare account_id or either allowlist below.
# ---------------------------------------------------------------------------

locals {
  # Resolved once from the data source in versions.tf. The CloudTrail bucket
  # name, the ECR registry host baked into user data, the gha-deploy
  # ssm:SendCommand resource ARN and the EventBridge Scheduler trust policy's
  # aws:SourceAccount condition all need it; reading it from here rather than
  # repeating data.aws_caller_identity.current.account_id keeps the number of
  # places that mention the account id small, which matters because this
  # repository is public.
  account_id = data.aws_caller_identity.current.account_id

  # The Sandbox OU service control policy, restated as data (foundation spec
  # §5.1). These two lists are the reason the instance-type and region variables
  # in variables.tf carry validation blocks: a value outside either list is not
  # a Terraform error, it is an AccessDenied several minutes into an apply with
  # no explanation attached. Kept here so that a future resize or region move
  # has one place to look for what the policy actually permits, and so the
  # enumeration survives even if the variable validations are refactored.
  #
  # Changing either list here changes nothing in AWS. The policy itself is
  # attached to ou-uxh0-bfrm5nib and is editable only from the management
  # account, under a different profile.
  scp_instance_type_allowlist = ["t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"]
  scp_region_allowlist        = ["us-west-2", "us-east-1"]
}
