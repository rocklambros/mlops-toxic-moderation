# ---------------------------------------------------------------------------
# One role and one instance profile per tier (premortem H16). A single shared
# ec2-app-role would mean a Streamlit RCE on the internet-facing box yields the
# W&B key, the reviewer secret, and master read/write on every table.
#
# Each policy names exactly the repository, log group and secret its own tier
# needs. Two omissions are load-bearing rather than incidental:
#
#   - the monitoring tier cannot read the RDS master secret, which is what makes
#     the SELECT-only `monitor_ro` Postgres role a control rather than a
#     convention;
#   - the frontend tier cannot read it either. That tier is the internet-facing
#     Streamlit box named in H16's harm sentence, and Phase 3 binding principle 1
#     is "No UI container holds a database write credential." The frontend reaches
#     Postgres through the backend API; that is what the backend tier is for.
#
# Deliberately absent from every tier: `iam:*` and `sts:*`. No instance may pass
# a role or mint another identity, so an instance compromise cannot become an
# account compromise.
#
# The nightly-stop scheduler role is NOT declared here. It lives with the
# schedules it serves, in budget.tf, because a resource declared in two files of
# one root module fails `terraform validate` outright.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ---- backend -------------------------------------------------------------

resource "aws_iam_role" "backend" {
  name               = "${var.project}-backend"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# The only way onto any instance: there is no SSH, no bastion, and no port 22.
resource "aws_iam_role_policy_attachment" "backend_ssm" {
  role       = aws_iam_role.backend.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "backend" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullBackendOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app["backend"].arn]
  }

  statement {
    sid       = "LogsBackendGroupOnly"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app["backend"].arn}:*"]
  }

  # The one tier that writes to Postgres, and therefore the one tier that holds
  # the RDS-managed master credential.
  #
  # db_readonly is here for one reason and it is not that the backend uses it.
  # The database is private, there is no bastion, and the ONLY path that can run
  # the monitor_ro bootstrap SQL is the SSM document in data.tf, which targets
  # THIS instance because it is the only tier with both a 5432 path to RDS and
  # the master credential. That document does
  #   aws secretsmanager get-secret-value --secret-id <ReadonlySecretArn>
  # to read the password it must set on the role, so without this ARN the
  # bootstrap fails with AccessDenied on its second API call and the read-only
  # role -- the whole of premortem H16's control -- is never created.
  #
  # This grants the backend no privilege it does not already hold: monitor_ro is
  # strictly weaker than the master credential on the line above (CONNECT, USAGE
  # and SELECT only). The monitoring tier still cannot read the master secret,
  # which is the direction of the H16 control that matters.
  # The reviewer shared secret is HERE and not on the frontend tier, and that placement is
  # the control rather than a detail. `backend/review_api.py::_reviewer` is what HMACs a
  # session token against it; `frontend/reviewer.py` reads BACKEND_URL and DEMO_API_KEY and
  # nothing else, which is exactly what infra/deploy/compose.frontend.yml says in its
  # comment. Phase A2 granted it to the frontend instead -- a credential the internet-facing
  # Streamlit tier never reads, held by the one tier H16's harm sentence is written about.
  statement {
    sid     = "SecretsBackendOnly"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.wandb_api_key.arn,
      aws_secretsmanager_secret.db_readonly.arn,
      aws_secretsmanager_secret.reviewer_shared_secret.arn,
      aws_secretsmanager_secret.demo_api_key.arn,
      aws_secretsmanager_secret.submitter_fp_key.arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "backend" {
  name   = "${var.project}-backend"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.backend.json
}

resource "aws_iam_instance_profile" "backend" {
  name = "${var.project}-backend"
  role = aws_iam_role.backend.name
}

# ---- frontend ------------------------------------------------------------

resource "aws_iam_role" "frontend" {
  name               = "${var.project}-frontend"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "frontend_ssm" {
  role       = aws_iam_role.frontend.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "frontend" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPullFrontendOnly"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app["frontend"].arn]
  }

  statement {
    sid       = "LogsFrontendGroupOnly"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app["frontend"].arn}:*"]
  }

  statement {
    sid     = "SecretsFrontendOnly"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      # The demo API key and NOTHING ELSE. Both Streamlit entry points on this host send it
      # as X-API-Key on every backend call (frontend/ui.py, frontend/reviewer.py), and
      # neither reads anything else that is secret.
      #
      # The RDS master credential was here in an earlier draft; combined with this tier's
      # internet-facing exposure that was premortem H16's harm sentence verbatim -- "a
      # Streamlit RCE on the internet-facing box yields ... master-user read/write on all
      # three tables" -- inside the file that claims to close it. The W&B key is absent for
      # the same reason: this tier neither trains nor loads artifacts from W&B. The reviewer
      # shared secret moved to the backend tier in Phase 5: the tier that VERIFIES a
      # credential is the tier that must hold it, and no code on this host reads it.
      aws_secretsmanager_secret.demo_api_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "frontend" {
  name   = "${var.project}-frontend"
  role   = aws_iam_role.frontend.id
  policy = data.aws_iam_policy_document.frontend.json
}

resource "aws_iam_instance_profile" "frontend" {
  name = "${var.project}-frontend"
  role = aws_iam_role.frontend.name
}

# ---- monitoring ----------------------------------------------------------

resource "aws_iam_role" "monitoring" {
  name               = "${var.project}-monitoring"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "monitoring_ssm" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "monitoring" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # This tier also hosts the DistilBERT re-scorer if it survives the cut-line,
  # which is why it pulls two repositories and writes two log groups.
  statement {
    sid    = "EcrPullMonitoringAndRescorer"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.app["monitoring"].arn,
      aws_ecr_repository.app["rescorer"].arn,
    ]
  }

  statement {
    sid     = "LogsMonitoringAndRescorerGroupsOnly"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.app["monitoring"].arn}:*",
      "${aws_cloudwatch_log_group.app["rescorer"].arn}:*",
    ]
  }

  # Read-only database credentials ONLY. The RDS master secret is deliberately
  # absent: the dashboard must not be able to write the graded feedback table,
  # and the W&B key and reviewer secret are absent because this tier neither
  # trains models nor renders the review queue.
  statement {
    sid       = "SecretsReadOnlyDatabaseRoleOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_readonly.arn]
  }
}

resource "aws_iam_role_policy" "monitoring" {
  name   = "${var.project}-monitoring"
  role   = aws_iam_role.monitoring.id
  policy = data.aws_iam_policy_document.monitoring.json
}

resource "aws_iam_instance_profile" "monitoring" {
  name = "${var.project}-monitoring"
  role = aws_iam_role.monitoring.name
}

# ---- boot marker ---------------------------------------------------------
#
# The last line of user data writes /toxic/boot/<component>, which is the only
# way to ask "did this instance's bootstrap reach the end?" from outside a host
# that has no SSH. Because it is the last line of a `set -e` script, an
# ungranted PutParameter turns every otherwise-successful boot into a FAILED
# marker on the console -- so the grant and the script have to land together.
#
# Scoped to the /toxic/boot/ prefix and not to /toxic/*. The deploy pipeline's
# record of which SHA is serving lives at /toxic/deploy/*, and an instance able
# to rewrite that could lie about what it is running; /toxic/endpoints/* is what
# the health gate reads. One statement, shared by the three tiers, because each
# tier writes only its own component's key and a per-tier document would be
# three copies of one sentence.

data "aws_iam_policy_document" "boot_marker" {
  statement {
    sid       = "WriteOwnBootMarker"
    effect    = "Allow"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter/toxic/boot/*"]
  }
}

resource "aws_iam_role_policy" "backend_boot_marker" {
  name   = "${var.project}-backend-boot-marker"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.boot_marker.json
}

resource "aws_iam_role_policy" "frontend_boot_marker" {
  name   = "${var.project}-frontend-boot-marker"
  role   = aws_iam_role.frontend.id
  policy = data.aws_iam_policy_document.boot_marker.json
}

resource "aws_iam_role_policy" "monitoring_boot_marker" {
  name   = "${var.project}-monitoring-boot-marker"
  role   = aws_iam_role.monitoring.id
  policy = data.aws_iam_policy_document.boot_marker.json
}

# ---- the deploy payload and the artifact mirror --------------------------
#
# What SendCommand actually runs is `bash /opt/toxic/bootstrap.sh <sha> <component>`, and
# the first thing that script does is pull s3://<deploy bucket>/deploy/<sha>/ onto the box.
# Without this grant the whole deploy path is one AccessDenied, discovered inside an SSM
# invocation on a host with no SSH.
#
# Scoped by PREFIX, not to the bucket. The same bucket holds db/, which is the graded
# dashboard dataset dumped by `make aws-down`: an instance has no reason to read a database
# dump and every reason not to be able to. The ListBucket condition is what makes that real
# -- without it, `aws s3 ls s3://<bucket>/db/` succeeds and enumerates the dumps by name even
# though GetObject on them is denied.
#
# One document, three attachments, because the three tiers need exactly the same two
# prefixes: the payload they are told to install, and the digest-keyed mirror the model and
# its two sidecar artifacts are fetched from.

data "aws_iam_policy_document" "deploy_payload" {
  statement {
    sid    = "ReadDeployPayloadAndArtifactMirror"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.deploy.arn}/deploy/*",
      "${aws_s3_bucket.deploy.arn}/artifacts/*",
    ]
  }

  statement {
    sid       = "ListOnlyTheTwoPrefixesTheDeployReads"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.deploy.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["deploy/*", "artifacts/*"]
    }
  }
}

resource "aws_iam_role_policy" "backend_deploy_payload" {
  name   = "${var.project}-backend-deploy-payload"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.deploy_payload.json
}

# ---- the database dump prefix, backend only ------------------------------
#
# premortem H6 and H29. `make aws-down` has `db-dump` as a hard prerequisite, so
# every teardown path produces a restorable dump first -- and RDS is private with
# no bastion, so the dump can only be taken from inside the VPC. The backend tier
# is the only one with a 5432 route AND the master credential, so it is the only
# tier that can take it, which makes this the grant the whole H6 remediation
# rests on. Without it `pg_dump | aws s3 cp - s3://<bucket>/db/...` is an
# AccessDenied inside an SSM invocation, `make aws-down` refuses to stop
# anything, and the cost control is dead.
#
# The read is granted alongside the write for two reasons: `make db-restore`
# streams the object back from this same host, and `db_dump.sh` reads the object
# it just uploaded and makes pg_restore parse it, because `aws s3 cp -` uploads
# whatever it received before a broken pipe and a truncated archive is not a
# backup.
#
# ONE tier, and the omission is the control. The frontend is the internet-facing
# Streamlit box H16's harm sentence is written about, and the monitoring tier
# connects as the SELECT-only monitor_ro role; a full dump of the database in
# either place would undo both. This grants the backend nothing it does not
# already hold -- it has the master credential and can read every row directly --
# which is exactly why it is the tier that gets it.
#
# No ListBucket: `aws s3 cp` to and from a known key does not need it, and
# `aws s3 ls s3://<bucket>/db/` from an instance would enumerate every session's
# dataset by name.

data "aws_iam_policy_document" "database_dump" {
  statement {
    sid    = "ReadAndWriteTheDatabaseDumpPrefix"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.deploy.arn}/db/*"]
  }
}

resource "aws_iam_role_policy" "backend_database_dump" {
  name   = "${var.project}-backend-database-dump"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.database_dump.json
}

resource "aws_iam_role_policy" "frontend_deploy_payload" {
  name   = "${var.project}-frontend-deploy-payload"
  role   = aws_iam_role.frontend.id
  policy = data.aws_iam_policy_document.deploy_payload.json
}

resource "aws_iam_role_policy" "monitoring_deploy_payload" {
  name   = "${var.project}-monitoring-deploy-payload"
  role   = aws_iam_role.monitoring.id
  policy = data.aws_iam_policy_document.deploy_payload.json
}
