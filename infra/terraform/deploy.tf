# ---------------------------------------------------------------------------
# Phase 5's Terraform surface.
#
# Two things live here and nothing else: the bucket that carries a deploy, and the parameter
# namespace the instance scripts read. Both exist so that no part of the deploy pipeline has
# to run `terraform output` inside a world-readable Actions log, and so a rollback can find
# what it needs without reading Terraform state.
#
# THE PARAMETER NAMESPACE IS A CONTRACT. infra/deploy/instance/{bootstrap,roll}.sh read every
# name declared in local.deploy_parameters and read nothing else;
# tests/infra/test_roll_secrets.py compares the two sets in both directions. A parameter a
# script reads and Terraform never published fails with ParameterNotFound inside an SSM
# invocation, on a host with no SSH -- which is the most expensive place in this system to
# discover a typo.
#
# SECRET NAMES ARE PUBLISHED, SECRET VALUES ARE NOT. /toxic/secrets/* carries the Secrets
# Manager *identifier* of each credential, so roll.sh never hardcodes a name that can drift
# from the resource that owns it. The plan for this phase hardcoded `toxic/wandb-api-key`
# while the applied account has `toxic-mod/wandb-api-key`, and every read would have failed.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "deploy" {
  bucket = "${var.project}-deploy-${local.account_id}"

  # terraform destroy is cost control #2 and must not be blocked by objects. Every object in
  # here is reproducible: a deploy payload is a git SHA's worth of files, the artifact mirror
  # is keyed by a digest recorded in MODEL_CARD.md, and the database dumps are re-taken by
  # `make aws-down` before anything is stopped.
  force_destroy = true

  tags = { Name = "${var.project}-deploy" }
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A deploy payload is overwritten in place when a SHA is re-rolled, and the artifact mirror
# is written once per digest. Versioning is what makes "the previous bytes" recoverable after
# either, and it is the precondition for the noncurrent-version expiry below.
resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id

  # Ordering matters for correctness, not style: this depends on versioning being ON, because
  # a noncurrent-version rule on an unversioned bucket silently applies to nothing.
  depends_on = [aws_s3_bucket_versioning.deploy]

  rule {
    id     = "expire-superseded-deploy-payloads"
    status = "Enabled"
    filter {
      prefix = "deploy/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # The artifact mirror. Keyed by digest, so an object here is never superseded -- a new
  # model is a new key. Nothing expires: the mirror is what makes a bring-up survive a
  # Weights & Biases outage, and an expired mirror object turns that outage back into an
  # outage of ours.
  rule {
    id     = "keep-the-artifact-mirror"
    status = "Enabled"
    filter {
      prefix = "artifacts/"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Database dumps ARE the graded dashboard dataset (premortem H6). They are deliberately
  # NOT expired on a schedule: a 30-day rule here would delete the evidence the demo is
  # graded on, silently, at exactly the point in the term when nobody is looking.
  rule {
    id     = "keep-database-dumps"
    status = "Enabled"
    filter {
      prefix = "db/"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------
# Two credentials Phase 5 needs and Phase A2 did not create.
#
# Containers, not values. Terraform never writes a secret value: the operator seeds each one
# once with `aws secretsmanager put-secret-value`, so no credential passes through state.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "demo_api_key" {
  name                    = "${var.project}/demo-api-key"
  description             = "X-API-Key for POST /predict. Read by the backend (to check it) and the two UI tiers (to send it)."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "submitter_fp_key" {
  name                    = "${var.project}/submitter-fp-key"
  description             = "HMAC key for the submitter fingerprint. Backend only; absent, the per-source quota loses stability across restarts."
  recovery_window_in_days = 7
}

# ---------------------------------------------------------------------------
# The parameter namespace.
# ---------------------------------------------------------------------------

locals {
  deploy_parameters = {
    # Where a deploy payload and the artifact mirror live, and where images come from.
    "/toxic/deploy/bucket"   = aws_s3_bucket.deploy.bucket
    "/toxic/deploy/registry" = local.ecr_registry

    # awslogs driver targets, one per component. Read into /etc/toxic/stack.env by roll.sh.
    "/toxic/logs/backend"    = aws_cloudwatch_log_group.app["backend"].name
    "/toxic/logs/frontend"   = aws_cloudwatch_log_group.app["frontend"].name
    "/toxic/logs/monitoring" = aws_cloudwatch_log_group.app["monitoring"].name
    "/toxic/logs/rescorer"   = aws_cloudwatch_log_group.app["rescorer"].name

    # What verify_deploy.sh probes: the three public listeners on their stable Elastic IPs.
    "/toxic/endpoints/backend"    = "http://${aws_eip.backend.public_ip}:8000"
    "/toxic/endpoints/frontend"   = "http://${aws_eip.frontend.public_ip}:8501"
    "/toxic/endpoints/monitoring" = "http://${aws_eip.monitoring.public_ip}:8502"

    # What the UI containers must actually call, and it is NOT the line above.
    # aws_security_group.frontend permits egress to 8000 only inside the public subnet
    # CIDRs, so a UI configured with the backend's Elastic IP sends its traffic out through
    # the internet gateway, misses that rule, and is dropped: the page renders and every
    # prediction times out. Published as its own name so the difference is impossible to
    # collapse by accident.
    "/toxic/endpoints/backend-internal" = local.backend_internal_url

    # The database. The endpoint carries the port; the DSNs roll.sh writes need both.
    "/toxic/db/endpoint"            = aws_db_instance.main.endpoint
    "/toxic/db/name"                = aws_db_instance.main.db_name
    "/toxic/db/master-secret-arn"   = aws_db_instance.main.master_user_secret[0].secret_arn
    "/toxic/db/readonly-secret-arn" = aws_secretsmanager_secret.db_readonly.arn

    # Secret IDENTIFIERS. See the header: the value never passes through here, and roll.sh
    # resolves every `--secret-id` from this map rather than from a literal in a shell
    # script that no `terraform plan` would ever contradict.
    "/toxic/secrets/wandb-api-key"          = aws_secretsmanager_secret.wandb_api_key.arn
    "/toxic/secrets/reviewer-shared-secret" = aws_secretsmanager_secret.reviewer_shared_secret.arn
    "/toxic/secrets/demo-api-key"           = aws_secretsmanager_secret.demo_api_key.arn
    "/toxic/secrets/submitter-fp-key"       = aws_secretsmanager_secret.submitter_fp_key.arn

    # The registered artifact fetch_artifacts.sh asks the registry for, and the reviewer
    # identity backend/reviewer_auth.py compares a token's claim against. Neither is secret:
    # the first is a public registry path, the second is an identity, not an authenticator.
    "/toxic/model/wandb-artifact" = var.wandb_artifact
    "/toxic/reviewer/id"          = var.reviewer_id
  }
}

resource "aws_ssm_parameter" "deploy" {
  for_each = local.deploy_parameters

  name  = each.key
  type  = "String"
  value = each.value

  tags = { Name = each.key }
}

output "deploy_bucket" {
  description = "S3 bucket holding per-SHA deploy payloads, the digest-keyed artifact mirror, and the database dumps. Marked sensitive because the bucket name ends in the account id."
  value       = aws_s3_bucket.deploy.bucket
  sensitive   = true
}
