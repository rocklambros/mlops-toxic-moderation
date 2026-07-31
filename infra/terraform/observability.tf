# ---------------------------------------------------------------------------
# Observability: log destinations, one alerting channel, the detective controls
# from AWS foundation spec section 7.5, and the alarms that make premortem H27
# false.
#
# H27 said: "No system observability. No container logs leave the box (no log
# driver configured anywhere), and the only alarm in the entire design is for
# root sign-in. Nothing pages when /predict is down -- which section 10 makes a
# DESIGNED behaviour whenever RDS is unreachable."
#
# Three things close it, and all three have to be present or the finding is only
# cosmetically remediated:
#
#   1. A destination for container logs. The log groups below are it; compute.tf
#      points the Docker `awslogs` driver at them and iam.tf grants each tier
#      logs:CreateLogStream + logs:PutLogEvents on its own group and no other.
#   2. Something that notices. Four alarms below: the instance is gone
#      (StatusCheckFailed), /predict is answering 503 (log metric filter),
#      /health itself stopped answering (scheduled probe), and the probe that
#      answers the last question stopped running (FailedInvocations).
#   3. Something that PAGES. An alarm publishing to a topic with no confirmed
#      subscriber is H27 in its operative form. See the subscription note below;
#      confirmation is a human step that `terraform apply` cannot perform and
#      does not report on.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Log destinations.
#
# 14-day retention because the CloudWatch default is "never expire" and log
# storage is a silent recurring cost against a $100 ceiling.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  for_each = toset(local.components)

  name              = "/${var.project}/${each.value}"
  retention_in_days = var.log_retention_days

  tags = { Name = "/${var.project}/${each.value}", Component = each.value }
}

# ---------------------------------------------------------------------------
# One SNS topic carries every operational signal in this account: budget
# thresholds, the health alarms, and root-user activity.
#
# NOT encrypted with `alias/aws/sns`, deliberately. AWS-managed KMS keys have a
# key policy that cannot be edited, and it does not grant the service principals
# that publish here -- cloudwatch.amazonaws.com, budgets.amazonaws.com,
# events.amazonaws.com -- kms:GenerateDataKey*. Encrypting this topic with the
# AWS-managed key therefore makes alarm publication fail with KMS AccessDenied,
# silently, on the exact code path H27 exists to protect. AWS documents the
# supported alternative (SNS Developer Guide, "compatibility between event
# sources from AWS services and encrypted topics"): a CUSTOMER-managed key whose
# key policy grants those principals. That costs ~$1/month per key plus request
# charges and protects notification bodies that contain no secrets and no
# personal data. Declined on purpose. If a checkov run flags CKV_AWS_26, the
# suppression rationale is this paragraph -- do not "fix" it by adding
# kms_master_key_id, which would break paging.
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

# An AWS email subscription is created in PendingConfirmation state and stays
# there until the recipient clicks the link. Terraform reports success either
# way, so a green apply proves nothing about whether anyone is reachable.
# Confirming it is a required post-apply step, and it does NOT survive
# `terraform destroy`: every re-apply needs the link clicked again.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "AllowBudgetsToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowCloudWatchAlarmsToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }

  statement {
    sid       = "AllowEventBridgeToPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# ---------------------------------------------------------------------------
# CloudTrail, to a dedicated bucket, with log file validation.
#
# `enable_log_file_validation` is what makes tampering DETECTABLE: CloudTrail
# writes a signed digest file every hour covering the log files delivered in
# that hour, so a deleted or edited object can be proven rather than suspected
# (premortem H17, foundation spec section 5.1 trap 4).
#
# Delete restriction on the bucket is the second half of H17, and it is a
# bucket policy here rather than a reliance on the Phase A1 SCP. Read this
# before renaming the bucket:
#
#   The SCP statement DenyTrailEvidenceDestruction is scoped to
#   `arn:aws:s3:::rockcyber-mlops-toxic-cloudtrail-*`, which this bucket's name
#   (`${var.project}-cloudtrail-<account>`) does NOT match. That is deliberate,
#   and it is a trade, not an oversight. The SCP denies s3:DeleteBucket,
#   s3:DeleteObject, s3:DeleteObjectVersion, s3:DeleteBucketPolicy and
#   s3:PutLifecycleConfiguration with no principal exception, and SCPs bind
#   every principal in a member account including the one running Terraform. A
#   bucket named into that pattern therefore cannot be emptied or deleted by
#   anyone, so `terraform destroy` -- cost control #2, and a hard constraint of
#   this phase -- would fail permanently on this resource and leave a
#   half-destroyed billing stack. The bucket policy below buys the same
#   protection against every principal that is not an account administrator,
#   while leaving teardown possible. If tamper-proofing against the operator is
#   later judged more valuable than automated teardown, the correct fix is to
#   add an `aws:PrincipalArn` exception to the SCP in the MANAGEMENT account and
#   then rename this bucket -- not to drop force_destroy here.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "trail" {
  bucket = "${var.project}-cloudtrail-v2-${local.account_id}"

  # Teardown is cost control #2. A non-empty bucket blocks it.
  force_destroy = true

  tags = { Name = "${var.project}-cloudtrail" }
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id

  rule {
    id     = "expire-trail-objects"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.trail]
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "AWSCloudTrailAclCheck"
    effect    = "Allow"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.trail.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }

  statement {
    sid       = "AWSCloudTrailWrite"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.trail.arn}/AWSLogs/${local.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  # Evidence destruction, denied to everything that is not an administrator of
  # this account. The workload principals -- three instance roles and the
  # GitHub deploy role -- hold no S3 permission at all, so this is defence in
  # depth against a future grant rather than a fix for a present hole. It also
  # covers the two non-delete verbs that destroy evidence just as effectively:
  # rewriting the lifecycle configuration to expire objects tomorrow, and
  # suspending versioning so an overwrite is not recoverable.
  #
  # StringNotLike on aws:PrincipalArn is an ALLOW-LIST inversion: a principal
  # not matching one of these patterns is denied. Both real administrative
  # paths are listed -- the Identity Center permission set (`AWSReservedSSO_*`)
  # and `OrganizationAccountAccessRole`, which is how the management account
  # reaches this account today -- plus the account root break-glass. Note the
  # failure mode if this list is ever wrong: `terraform destroy` fails with
  # AccessDenied on DeleteObject while emptying the bucket. The fix is to add
  # the applying principal here and `terraform apply` before retrying.
  statement {
    sid    = "DenyTrailEvidenceDestructionByWorkloads"
    effect = "Deny"

    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:PutBucketPolicy",
      "s3:PutLifecycleConfiguration",
      "s3:PutBucketVersioning",
    ]

    resources = [
      aws_s3_bucket.trail.arn,
      "${aws_s3_bucket.trail.arn}/*",
    ]

    principals {
      type = "AWS"
      identifiers = [
        aws_iam_role.backend.arn,
        aws_iam_role.frontend.arn,
        aws_iam_role.monitoring.arn,
        aws_iam_role.gha_deploy.arn,
      ]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json

  # The public access block must exist first, or S3 can reject a policy on a
  # bucket it considers publicly writable.
  depends_on = [aws_s3_bucket_public_access_block.trail]
}

# ---------------------------------------------------------------------------
# THE TWO RESOURCES `terraform destroy` CANNOT DELETE. Read this before running
# a teardown and finding out.
#
# The Phase A1 service control policy statement DenyDetectiveControlTampering
# (infra/aws/scp-sandbox-guardrails.json) denies, with NO condition and NO
# principal exception:
#
#   cloudtrail:StopLogging, cloudtrail:DeleteTrail, cloudtrail:UpdateTrail,
#   cloudtrail:PutEventSelectors, cloudtrail:PutInsightSelectors,
#   guardduty:DeleteDetector, guardduty:UpdateDetector, ...
#
# A service control policy binds EVERY principal in this member account,
# including the Identity Center administrator and OrganizationAccountAccessRole,
# which are the two identities that run Terraform here. So:
#
#   * CREATE succeeds. CreateTrail, StartLogging and CreateDetector are not in
#     the deny list, and nothing here configures event or insight selectors.
#   * DESTROY FAILS on exactly these two resources, with AccessDenied, and
#     leaves the rest of the stack destroyed around them.
#   * An in-place CHANGE to the trail also fails, because UpdateTrail is denied.
#     Editing any argument below therefore means destroy-and-recreate, which the
#     first bullet says is impossible. Treat the trail's arguments as immutable
#     for the life of this account.
#
# That collides head-on with this phase's constraint that `terraform destroy`
# must succeed cleanly, because teardown is cost control #2. It is stated here
# rather than discovered at 23:00 on the last day.
#
# TEARDOWN PROCEDURE, until the policy is amended:
#
#   terraform -chdir=infra/terraform state rm \
#     aws_cloudtrail.main aws_guardduty_detector.main
#   terraform -chdir=infra/terraform destroy
#
# `state rm` forgets the two resources without calling AWS, so the destroy of
# everything else completes. What survives, and what it costs:
#
#   - The trail keeps running and keeps trying to deliver to a bucket that
#     destroy removed. Management events are free for the first copy; the
#     failed deliveries cost nothing. It is noise, not spend.
#   - GUARDDUTY KEEPS BILLING, roughly $4/month at this event volume, and no
#     principal in this account can turn it off. This is a real, recurring,
#     unavoidable-from-here cost after teardown.
#
# THE ACTUAL FIX, which is not in this file and not in this account: add a
# principal exception to DenyDetectiveControlTampering in the MANAGEMENT
# account -- a StringNotLike on aws:PrincipalArn admitting
# AWSReservedSSO_*/OrganizationAccountAccessRole, exactly as the trail bucket
# policy above already does -- and then plain `terraform destroy` works with no
# state surgery. Until that edit lands, the procedure above is the teardown.
# Do NOT "fix" this by deleting these two resources from the module: CloudTrail
# and GuardDuty are Phase A2 deliverables in the delivery spec, and dropping
# them trades a documented teardown step for a missing detective control.
# ---------------------------------------------------------------------------

resource "aws_cloudtrail" "main" {
  name                          = "${var.project}-trail"
  s3_bucket_name                = aws_s3_bucket.trail.id
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true

  depends_on = [aws_s3_bucket_policy.trail]
}

resource "aws_guardduty_detector" "main" {
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"
}

# ---------------------------------------------------------------------------
# Root usage. Any activity by the account root user is either the operator
# deliberately opening the break-glass, or an incident. This is the detective
# control that makes "keep root as break-glass" (foundation spec 5.2) a
# defensible decision rather than a standing unwatched credential.
#
# KNOWN COVERAGE GAP, stated rather than papered over: this rule lives on the
# default event bus in us-west-2, and AWS records ROOT console sign-in events
# in us-east-1 regardless of where the browser is. Global-service API calls by
# root land there too. So this rule catches root API activity in us-west-2 and
# will NOT catch a root console sign-in. The multi-region CloudTrail above does
# record those events, so they are auditable after the fact; what is missing is
# the page. Closing it properly needs a second rule on the us-east-1 default bus
# under a provider alias, forwarded to a topic in that region (EventBridge SNS
# targets are same-region), and that is deliberately out of this phase's scope.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "root_usage" {
  name        = "${var.project}-root-usage"
  description = "Any activity by the account root user"

  event_pattern = jsonencode({
    "detail-type" = [
      "AWS API Call via CloudTrail",
      "AWS Console Sign In via CloudTrail",
    ]
    detail = {
      userIdentity = {
        type = ["Root"]
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "root_usage" {
  rule      = aws_cloudwatch_event_rule.root_usage.name
  target_id = "alerts"
  arn       = aws_sns_topic.alerts.arn
}

# ---------------------------------------------------------------------------
# Health alarms (premortem H27, second clause). All of them notify the topic
# above, and all of them set treat_missing_data = "notBreaching" because the
# nightly stop schedule in budget.tf takes the instances down on purpose every
# night. An alarm that pages nightly is an alarm the operator learns to ignore,
# which is the same outcome as having no alarm with extra steps.
#
# They cover genuinely different failures:
#
#   backend_status_check       the instance is gone or wedged
#   backend_predict_unavailable  the API is up and /predict is returning 503,
#                                which the design MAKES the behaviour whenever
#                                RDS is unreachable
#   backend_health_probe       the instance is fine, the API is not answering
#                              at all -- container crashed, OOM-killed, port not
#                              bound, image rolled to something broken. Nothing
#                              else here sees that: a dead container writes no
#                              503 line and fails no EC2 status check.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "backend_status_check" {
  alarm_name          = "${var.project}-backend-status-check"
  alarm_description   = "EC2 #1 failed its instance or system status check."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  dimensions          = { InstanceId = aws_instance.backend.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# /predict returns 503 when a prediction cannot be persisted, which the design
# makes a deliberate behaviour whenever RDS is unreachable. That is the outage
# nothing previously noticed.
resource "aws_cloudwatch_log_metric_filter" "backend_503" {
  name           = "${var.project}-backend-503"
  log_group_name = aws_cloudwatch_log_group.app["backend"].name
  pattern        = "\"503 Service Unavailable\""

  metric_transformation {
    name      = "PredictUnavailable"
    namespace = "${var.project}/backend"
    value     = "1"
    # default_value 0 is what makes the metric report during healthy periods.
    # Without it the metric only has data points when the failure is already
    # happening, so the alarm sits in INSUFFICIENT_DATA and never transitions
    # cleanly back to OK.
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "backend_predict_unavailable" {
  alarm_name          = "${var.project}-predict-unavailable"
  alarm_description   = "/predict is returning 503, which means persistence is failing."
  namespace           = "${var.project}/backend"
  metric_name         = "PredictUnavailable"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.backend_503]
}

# ---------------------------------------------------------------------------
# The /health probe (premortem H27: "Nothing pages when /predict is down").
#
# Every five minutes, EventBridge sends one SSM Run Command to the instance
# tagged Component=backend. The command curls /health on LOOPBACK and writes a
# single line into the backend's own log group; a metric filter counts the
# failures and the alarm below pages after two consecutive failed probes
# (~10 minutes).
#
# Why this shape rather than the two obvious alternatives:
#
#   - A Route 53 health check probes from ~30 public checker ranges. The backend
#     security group admits var.operator_cidrs only, so every check would fail
#     until ingress is opened to those ranges -- an alarm that requires widening
#     the attack surface to work is a bad trade, and it also cannot see 8000 at
#     all during the closed-demo default.
#   - EventBridge API destinations / a Lambda prober would come from outside the
#     VPC (there is no NAT gateway) and hit the same ingress wall.
#
# Probing over the loopback interface sidesteps both, and it needs NO new IAM on
# the instance: iam.tf already grants the backend role logs:CreateLogStream and
# logs:PutLogEvents on exactly this log group and no other, which is the whole
# permission surface this probe uses. The trade is stated plainly: a loopback
# probe cannot see a security-group or route-table break between the instance
# and the internet. aws_cloudwatch_metric_alarm.backend_status_check covers a
# dead instance; a reachability regression is caught by the deploy-time
# `curl /health` against the Elastic IP, not by this.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "health_probe_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    # Confused-deputy guard, same reasoning as the scheduler role in budget.tf:
    # without it, events.amazonaws.com is trusted as a bare service principal
    # and a rule in ANY account could name this role. Scoped by account rather
    # than by aws:SourceArn deliberately -- an ArnLike on the rule ARN would be
    # tighter, and its failure mode is a probe that silently never runs, which
    # is the exact class of failure H27 is about. The role can do one thing:
    # send AWS-RunShellScript to instances in this account tagged
    # Project=<project> and Component=backend.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "health_probe" {
  name               = "${var.project}-health-probe"
  description        = "EventBridge role that sends the scheduled /health Run Command"
  assume_role_policy = data.aws_iam_policy_document.health_probe_assume.json
}

data "aws_iam_policy_document" "health_probe" {
  # SendCommand needs BOTH the document and the instances as resources, and the
  # tag condition can only be applied to the instance statement: the document
  # carries no Component tag, so a single statement with the condition would
  # deny the call outright.
  statement {
    sid       = "RunShellScriptDocument"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  statement {
    sid       = "SendOnlyToTheBackendInstance"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${var.region}:${local.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Project"
      values   = [var.project]
    }

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Component"
      values   = ["backend"]
    }
  }
}

resource "aws_iam_role_policy" "health_probe" {
  name   = "${var.project}-health-probe"
  role   = aws_iam_role.health_probe.id
  policy = data.aws_iam_policy_document.health_probe.json
}

resource "aws_cloudwatch_event_rule" "backend_health_probe" {
  name                = "${var.project}-backend-health-probe"
  description         = "Every 5 minutes, curl /health on EC2 #1 and record the result"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "backend_health_probe" {
  rule      = aws_cloudwatch_event_rule.backend_health_probe.name
  target_id = "backend-health-probe"
  arn       = "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"
  role_arn  = aws_iam_role.health_probe.arn

  run_command_targets {
    key    = "tag:Component"
    values = ["backend"]
  }

  # HEALTHPROBE_OK / HEALTHPROBE_FAIL are single tokens on purpose: they survive
  # the AWS CLI shorthand parser for --log-events without quoting games, and
  # "HEALTHPROBE_OK" cannot match the filter pattern for "HEALTHPROBE_FAIL".
  input = jsonencode({
    executionTimeout = ["60"]
    commands = [
      "GROUP='${aws_cloudwatch_log_group.app["backend"].name}'",
      "STREAM=health-probe",
      "if curl -fsS --max-time 5 http://127.0.0.1:${local.ports.backend}/health >/dev/null 2>&1; then MSG=HEALTHPROBE_OK; else MSG=HEALTHPROBE_FAIL; fi",
      "aws logs create-log-stream --region ${var.region} --log-group-name \"$GROUP\" --log-stream-name \"$STREAM\" >/dev/null 2>&1 || true",
      "aws logs put-log-events --region ${var.region} --log-group-name \"$GROUP\" --log-stream-name \"$STREAM\" --log-events \"timestamp=$(date +%s000),message=$MSG\" >/dev/null",
      "echo \"$MSG\"",
    ]
  })
}

resource "aws_cloudwatch_log_metric_filter" "backend_health_probe" {
  name           = "${var.project}-backend-health-probe"
  log_group_name = aws_cloudwatch_log_group.app["backend"].name
  pattern        = "\"HEALTHPROBE_FAIL\""

  metric_transformation {
    name          = "HealthProbeFailure"
    namespace     = "${var.project}/backend"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "backend_health_probe" {
  alarm_name          = "${var.project}-backend-health-probe"
  alarm_description   = "GET /health on EC2 #1 failed two consecutive scheduled probes."
  namespace           = "${var.project}/backend"
  metric_name         = "HealthProbeFailure"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # A stopped instance produces no probe result at all, which is missing data
  # rather than a failure. The nightly stop schedule is deliberate; the
  # status-check alarm is what covers an instance that should be up and is not.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.backend_health_probe]
}

# Who watches the watcher. A probe that stops running produces no HEALTHPROBE_FAIL
# lines, which the alarm above reads as missing data and therefore as "not
# breaching" -- a broken prober looks exactly like a healthy system, which is
# H27's failure re-created one level up. FailedInvocations is EventBridge's own
# count of deliveries it could not make, and it catches the realistic causes:
# the role trust broken by an edit, the SendCommand policy narrowed, throttling.
#
# It does NOT catch a command that was delivered and then failed on the box
# (EventBridge counts that as delivered). The residual check for that case is
# `aws ssm list-command-invocations --details` in the no-SSH runbook.
resource "aws_cloudwatch_metric_alarm" "health_probe_not_running" {
  alarm_name          = "${var.project}-health-probe-not-running"
  alarm_description   = "EventBridge could not deliver the scheduled /health probe; the health alarm is blind until this clears."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.backend_health_probe.name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
