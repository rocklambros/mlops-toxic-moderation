# ---------------------------------------------------------------------------
# Root-module wiring: version floor, pinned providers, provider configuration.
#
# The S3 backend lives in backend.tf. Terraform merges multiple `terraform`
# blocks belonging to one root module, so splitting them by concern is legal and
# keeps state configuration readable next to provider configuration.
#
# required_version is load-bearing rather than housekeeping. S3 native state
# locking -- the `use_lockfile` argument in backend.tf -- became generally
# available in Terraform 1.11, and the DynamoDB lock arguments were deprecated in
# the same release. On 1.10 or older the backend block would be accepted and the
# state would be written with no lock at all, because this project deliberately
# has no DynamoDB lock table to fall back to. The floor is what makes the absence
# of that table safe.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.11"

  required_providers {
    # Pinned to an exact major and minor. The patch level floats within 6.57.x,
    # and the committed .terraform.lock.hcl pins the exact build plus its
    # checksums, which is what makes a run reproducible. Bumping the minor is a
    # deliberate edit here followed by `terraform init -upgrade`, never an
    # incidental consequence of running init on a new machine.
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.57"
    }

    # Supplies time_static, which captures the moment the database was created
    # and then holds still in state. It is what gives the RDS final snapshot a
    # per-lifecycle unique identifier without the permanent diff that
    # timestamp() would produce (premortem H6).
    time = {
      source  = "hashicorp/time"
      version = "~> 0.14"
    }
  }
}

provider "aws" {
  region = var.region

  # Applied to every taggable resource in the module. Two of these keys are
  # load-bearing rather than cosmetic: the gha-deploy role's ssm:SendCommand
  # statement is conditioned on ssm:resourceTag/Project, and the teardown and
  # cost queries in the runbooks filter on Name=tag:Project. A resource that
  # escapes this block is a resource SendCommand cannot reach and a cost query
  # cannot see.
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Phase       = "A2"
    }
  }
}

provider "time" {}

# The account id is needed by the CloudTrail bucket name, the ECR registry host
# in user data, the gha-deploy SendCommand resource ARN, and the EventBridge
# Scheduler trust policy's aws:SourceAccount condition. Declared once here and
# read through local.account_id in locals.tf so no other file repeats it, and so
# a second declaration elsewhere fails fast rather than drifting.
data "aws_caller_identity" "current" {}
