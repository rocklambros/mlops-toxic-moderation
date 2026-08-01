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
  # the RDS-managed master credential. The reviewer shared secret is not here:
  # the reviewer UI runs on the frontend instance, not this one.
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
  statement {
    sid     = "SecretsBackendOnly"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.wandb_api_key.arn,
      aws_secretsmanager_secret.db_readonly.arn,
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
      # The reviewer shared secret and NOTHING ELSE. The RDS master credential was
      # here in an earlier draft; combined with this tier's internet-facing exposure
      # that was premortem H16's harm sentence verbatim -- "a Streamlit RCE on the
      # internet-facing box yields ... master-user read/write on all three tables" --
      # inside the file that claims to close it. The W&B key is absent for the same
      # reason: this tier neither trains nor loads artifacts from W&B.
      aws_secretsmanager_secret.reviewer_shared_secret.arn,
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
