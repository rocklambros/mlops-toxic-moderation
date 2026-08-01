# ---------------------------------------------------------------------------
# Four container repositories, one per component the system ships.
#
# Tags are IMMUTABLE. That is what makes "the deployed container traces back to
# an exact commit" a property rather than an aspiration: a `sha-<gitsha>` tag can
# never be repointed at a different digest, so the tag in the SSM roll and the
# tag in the model card mean the same image forever.
#
# Immutability has one consequence that shapes the lifecycle policy below: a
# moving `stable` or `latest` pointer is impossible here, because repointing a
# tag is exactly what immutability forbids. Promotion therefore has to mint a
# NEW, unique tag on an EXISTING digest:
#
#   MANIFEST=$(aws ecr batch-get-image --repository-name toxic-mod-backend \
#                --image-ids imageTag=sha-<gitsha> \
#                --query 'images[0].imageManifest' --output text)
#   aws ecr put-image --repository-name toxic-mod-backend \
#     --image-tag release-$(date -u +%Y%m%d)-<gitsha> --image-manifest "$MANIFEST"
#
# That costs no push and no pull -- it adds a tag to a manifest already in the
# repository -- and it is the operator action that says "this digest is the
# last known good rollback target."
# ---------------------------------------------------------------------------

locals {
  # The four containers this system ships. Declared once, HERE, and consumed by
  # observability.tf, iam.tf and outputs.tf. It is deliberately not repeated in
  # a second file: two `locals` blocks in one root module defining the same key
  # fail `terraform validate` outright.
  components = ["backend", "frontend", "monitoring", "rescorer"]
}

resource "aws_ecr_repository" "app" {
  for_each = toset(local.components)

  name                 = "${var.project}-${each.value}"
  image_tag_mutability = "IMMUTABLE"

  # Required for `terraform destroy` to complete: a repository holding images
  # cannot be deleted, and a half-destroyed stack keeps billing. Teardown is
  # cost control #2, so nothing may block it.
  force_delete = true

  # Basic scan-on-push. It reads the OS package manifest, so it sees the base
  # image and NOT the Python dependency tree (premortem H35). It is kept because
  # it is free and catches base-image CVEs; the Python side is covered by the
  # dependency scanning in CI, not here.
  image_scanning_configuration {
    scan_on_push = true
  }

  # SSE-S3 with the ECR service key. Named explicitly rather than left implicit
  # so the absence of a customer-managed key is a visible decision (a CMK costs
  # a monthly charge per key and buys key-policy control this account does not
  # need for public-dataset model images).
  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = "${var.project}-${each.value}", Component = each.value }
}

# ---------------------------------------------------------------------------
# Lifecycle policy.
#
# ECR evaluates rules in ascending rulePriority, and the rule that matters here
# is the ordering guarantee AWS documents: an image that matches the tagging
# requirements of a rule cannot be expired by a rule of LOWER priority. Rule 1
# therefore does not "keep" promoted images by expiring nothing -- it claims
# them, which is what makes rule 2 unable to touch them.
#
# Without rule 1 the retention window is the whole rollback plan: four images
# per push to main means the last-known-good target ages out of the window on
# its own schedule, and the day it is needed is exactly the day nobody has been
# counting pushes.
#
# The count on rule 2 is 30, not the AWS foundation spec section 7.2 figure of
# 10. The premortem kept the objection "ECR keep-last-10 erodes rollback
# targets" (dropped ledger, folded into the H6 rollback remediation): four
# images per push means ten tags is under two days of commits on a 19-day
# schedule. Rule 1 is what GUARANTEES a rollback target; rule 2's number only
# decides how much unpromoted history stays reachable without a promotion.
#
# Every prefix and count below is a literal on purpose. Routing them through
# locals would render the policy unreadable in `terraform plan` output and in
# the offline assertion suite, which reads this expression as text.
# ---------------------------------------------------------------------------

resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Never expire an explicitly promoted digest (release-* tag). Claiming these here is what stops rule 2 from expiring them."
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["release-"]
          countType     = "imageCountMoreThan"
          countNumber   = 9999
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last 30 SHA-tagged images so an unpromoted rollback target still exists"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 3
        description  = "Expire untagged layers after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}
