# ---------------------------------------------------------------------------
# Cost controls.
#
# Two things live here and they are not the same kind of thing:
#
#   1. A $100 monthly budget with six notifications -- 50, 80 and 100 percent of
#      both ACTUAL and FORECASTED spend, to SNS and to email. This is DETECTIVE.
#      It observes spend and sends mail. It cannot stop anything.
#
#   2. A nightly EventBridge Scheduler stop of all three instances and the
#      database. This is PREVENTIVE. It removes running hours whether or not
#      anyone reads the mail.
#
# The distinction is the whole point of premortem H7. See the block above the
# scheduler section for why an alert alone does not close the finding.
# ---------------------------------------------------------------------------


# ===========================================================================
# PREMORTEM H7: what the previous cost model left out
# ===========================================================================
#
# The superseded figure was "$0.101/hr with everything running". It counted four
# on-demand compute rates -- two t4g.medium, one t4g.small, one db.t4g.micro --
# and nothing else. Every line below was absent from it. They are enumerated
# here, next to the budget they blow, because a cost model that lives only in a
# document gets read once; one that sits beside `limit_amount` gets read every
# time somebody changes the ceiling.
#
# `docs/cost-model.md` is the authoritative, maintained version of this table
# with the scenario arithmetic worked out. This block is the summary that
# travels with the code. If the two disagree, the document wins and this block
# is stale -- fix it.
#
# Rates are approximate us-west-2 list prices as of 2026-07-31. They are to be
# confirmed against the AWS Pricing Calculator and the first real bill, not
# trusted from a comment.
#
# --- FIXED MONTHLY COST. Accrues with every instance stopped. --------------
#
#   Public IPv4 addresses    3 EIPs x 730 hr x $0.005/hr             $10.95
#       Since February 2024 AWS bills EVERY public IPv4 address, attached or
#       idle, auto-assigned or Elastic. This is the single largest omission in
#       the old number: 3 addresses cost more per month than the compute costs
#       in a full working week. It is a FIXED cost, not an hourly one, because
#       an EIP held while its instance is stopped bills at the same rate.
#
#   EBS root volumes         gp3, 30 + 20 + 30 = 80 GB x $0.08        $6.40
#       Backend 30 GB, frontend 20 GB, monitoring 30 GB. Billed on allocation,
#       not use, and billed in full while the instance is stopped.
#
#   GuardDuty                1 detector, volume-scaled       estimate $4.00
#       The only line here that is a guess rather than a published rate against
#       a known quantity. It scales with CloudTrail event, VPC flow log and DNS
#       log volume. Check it against the first full month in Cost Explorer.
#
#   RDS storage              20 GB gp3 x $0.115/GB-month              $2.30
#       Allocated, not used. max_allocated_storage = 0 in data.tf, so storage
#       autoscaling cannot quietly enlarge this.
#
#   Secrets Manager          4 secrets x $0.40/secret-month           $1.60
#       H7 said "x3" and counted only the Terraform-created containers
#       (wandb-api-key, reviewer-shared-secret, db-readonly). There is a
#       fourth: manage_master_user_password = true makes RDS create and bill a
#       managed master-credentials secret. Deleted secrets keep billing for
#       their recovery_window_in_days (7), so a destroy/apply cycle inside a
#       week is billed twice.
#
#   ECR storage              4 repos, arm64 ML images, ~6 GB x $0.10  $0.60
#       backend, frontend, monitoring, rescorer. The backend image carries the
#       vectorizers and the rescorer image carries ONNX weights, so these are
#       not small; the lifecycle policy in ecr.tf is what keeps the retained
#       tag count -- and therefore this line -- bounded.
#
#   CloudWatch Logs          ~1 GB/month ingest $0.50 + storage $0.03 $0.53
#       Ingestion dominates. 14-day retention (var.log_retention_days) is what
#       stops the storage half compounding; the CloudWatch default is forever.
#
#   CloudTrail               S3 storage ~10 GB x $0.023 + PUT requests $0.25
#       The first copy of management events is free. The bill is the S3 objects
#       and the per-request charge for writing them. The 90-day lifecycle
#       expiry in observability.tf caps it.
#
#   Terraform state          S3 standard, versioned, < 1 MB           $0.02
#
#   RDS backup storage       7-day retention on 20 GB allocated       $0.00
#       Free up to 100 percent of allocated storage. 7 days of a mostly-idle
#       20 GB database stays inside that. Billable at $0.095/GB-month beyond
#       it -- and the final snapshot that H6 requires SURVIVES destroy and
#       starts billing against this line once the instance is gone.
#
#   SNS                      ~50 email notifications/month            $0.00
#       First 1,000 email notifications per month are free.
#
#   CloudWatch alarms        3 standard alarms                        $0.00
#       First 10 per account are free. observability.tf declares three:
#       status check, 503 rate, and the health probe.
#
#   CloudWatch custom metrics 2 from log metric filters                $0.00
#       First 10 per account are free. A fourth alarm or an eleventh filter
#       moves both of these lines off zero, at $0.10 and $0.30 a month each.
#
#   EventBridge Scheduler    ~60 invocations/month                    $0.00
#       First 14 million per month are free. The hard control below is free.
#
#   Data transfer out        < 1 GB/month                             $0.00
#       First 100 GB/month out to the internet is free account-wide. A demo
#       serving text JSON does not approach it. It would matter if the
#       frontend served model artifacts, which it does not.
#
#   FIXED MONTHLY SUBTOTAL                                           $26.65
#
# --- VARIABLE, PER RUNNING HOUR. The only part the old number counted. -----
#
#   EC2 #1 backend      t4g.medium                                  $0.0336
#   EC2 #2 frontend     t4g.small                                   $0.0168
#   EC2 #3 monitoring   t4g.medium                                  $0.0336
#   RDS                 db.t4g.micro                                $0.0160
#   VARIABLE SUBTOTAL                                               $0.1000/hr
#
# --- WHY THIS MATTERS AT THE $100 CEILING ---------------------------------
#
#   Everything left running for one billing month:
#       730 hr x $0.100 = $73.00 variable + $26.65 fixed = $99.65
#
#   That is 99.65 percent of the ceiling, reached WITHOUT A SINGLE SERVICE
#   CONTROL POLICY VIOLATION, because the SCP instance-type allowlist caps the
#   hourly RATE and says nothing about DURATION. The old $0.101/hr figure
#   projected $73.73 for the same month and read as comfortable.
#
# ===========================================================================


locals {
  # Six notifications: three thresholds against two spend measures. Declared as
  # data rather than six copy-pasted blocks so the set is auditable at a glance
  # and a missing FORECASTED row is visible rather than buried.
  #
  # This local is owned by this file. See the ownership note at the top of
  # locals.tf: a second `budget_notifications` anywhere in the root module
  # fails `terraform validate` for the whole module, not just for the file that
  # introduced it.
  budget_notifications = [
    { type = "ACTUAL", threshold = 50 },
    { type = "ACTUAL", threshold = 80 },
    { type = "ACTUAL", threshold = 100 },
    { type = "FORECASTED", threshold = 50 },
    { type = "FORECASTED", threshold = 80 },
    { type = "FORECASTED", threshold = 100 },
  ]
}

# Foundation spec section 5.3: $100 per month, alerts at 50/80/100 percent of
# both actual and forecast, and NO automated budget action, by owner decision.
# That decision is honoured here -- there is no aws_budgets_budget_action in
# this file and there is not meant to be. The nightly schedule below is a fixed
# time-of-day stop, not a spend-triggered intervention, so it is a different
# mechanism rather than the declined one reintroduced under another name.
#
# Two properties of AWS Budgets that make this control weaker than it looks,
# and which are the reason a preventive control exists below:
#
#   - Cost data refreshes roughly three times a day. An ACTUAL alert can arrive
#     eight to twelve hours after the spend that triggered it.
#   - FORECASTED notifications need enough billing history to forecast from --
#     on a brand-new account that is around five weeks. This project's whole
#     life is nineteen days, so the three FORECASTED rows above will most
#     likely never fire at all. They are declared because the spec and the
#     rubric ask for forecast alerting and because the account outlives the
#     project, not because they will do any work during it.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = local.budget_notifications

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
      subscriber_email_addresses = [var.alert_email]
    }
  }

  # AWS Budgets validates at CreateBudget time that budgets.amazonaws.com may
  # publish to the named topic. Without this the apply races the topic policy
  # in observability.tf and fails with an unhelpful validation error on a
  # resource that looks correct.
  depends_on = [aws_sns_topic_policy.alerts]
}


# ---------------------------------------------------------------------------
# NIGHTLY STOP -- the hard control (premortem H7, second clause)
#
# The finding: with the automated budget action declined, the SCP instance-type
# allowlist was the only preventive cost control in the design. The allowlist
# caps the hourly RATE. It has nothing to say about DURATION. Three allowlisted
# instances plus an allowlisted database, left running, reach $99.65 in a
# billing month while every single API call passes the policy. An email at 80
# percent is not a control; it is a notification that the control was missing.
#
# This schedule is the duration control. It stops the three instances and the
# database at a fixed hour every night regardless of spend, regardless of
# whether anyone read the mail, and regardless of whether the operator
# remembered `make aws-down`. Cost: nothing -- EventBridge Scheduler's free
# tier is 14 million invocations a month and this uses about sixty.
#
# It is deliberately NOT the mechanism the owner declined. An aws_budgets_
# budget_action fires on a spend threshold and is unpredictable in time; this
# fires at 23:00 local and is predictable to the minute.
#
# Second benefit, which is why the RDS schedule exists and not only the EC2
# one: a stopped RDS instance auto-restarts after seven days (foundation spec
# section 9, premortem H29), and the documented remedy for that -- destroy
# rather than stop -- deletes the dataset the monitoring dashboard is graded
# on. A database stopped every night and started each working morning never
# accumulates seven consecutive stopped days, so the auto-restart never fires
# and the dataset never has to be destroyed to avoid it.
#
# Two operational consequences, recorded so they are not discovered:
#
#   - GRADING WINDOW. An instance stopped at 23:00 while a grader is looking
#     costs more than the spend it saves. Turn it off deliberately with
#     `terraform apply -var nightly_stop_enabled=false`, and turn it back on
#     afterwards. The variable defaults to true; the control is on unless
#     someone chose otherwise.
#   - AUTOMATED BACKUPS. RDS takes its automated backup inside
#     backup_window = "09:00-09:30" (data.tf), and that window is always UTC --
#     02:00 or 03:00 in America/Denver depending on daylight saving, which is
#     inside the nightly stop window. A stopped instance does not run its
#     backup, so on any night the database stays down through that hour, that
#     day's automated backup is skipped. Existing backups and the H6 final
#     snapshot are unaffected, and stopping does not shorten the 7-day
#     retention of backups already taken. If a daily backup becomes
#     load-bearing rather than a convenience, move backup_window in data.tf
#     into the working day rather than weakening this schedule.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    sid     = "EventBridgeSchedulerAssumesThisRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }

    # Confused-deputy guard. Without it, scheduler.amazonaws.com is trusted as
    # a bare service principal and a schedule in ANY AWS account could name
    # this role. aws:SourceAccount is the boundary that matters here; an
    # additional ArnLike on aws:SourceArn would narrow it further to these two
    # schedules, and is deliberately omitted because it would encode the
    # schedule ARN as a hand-built string whose silent failure mode is a cost
    # control that stops working with no error anywhere. The role can stop
    # exactly four named resources, so same-account scope is sufficient.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.project}-nightly-stop"
  description        = "Assumed by EventBridge Scheduler to stop the three instances and the database nightly."
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json

  tags = { Name = "${var.project}-nightly-stop", Component = "cost-control" }
}

# Every resource is named. No wildcard, in either statement.
#
# `ec2:StopInstances` on "*" would let anything that can reach this role stop
# every instance in the account, and the account is not exclusively this
# project's. The blast radius of a scheduler misconfiguration is bounded here
# rather than trusted to the schedule's input payload -- the payload says which
# instances to stop, and this policy says which ones it is ALLOWED to stop.
data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "StopTheThreeProjectInstances"
    effect  = "Allow"
    actions = ["ec2:StopInstances"]

    resources = [
      aws_instance.backend.arn,
      aws_instance.frontend.arn,
      aws_instance.monitoring.arn,
    ]
  }

  statement {
    sid       = "StopTheProjectDatabase"
    effect    = "Allow"
    actions   = ["rds:StopDBInstance"]
    resources = [aws_db_instance.main.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${var.project}-nightly-stop"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "nightly_stop_ec2" {
  name       = "${var.project}-nightly-stop-ec2"
  group_name = "default"

  # The toggle. ENABLED is the default; DISABLED keeps the schedule in state
  # and in code so that re-enabling it after the grading window is a one-word
  # change rather than a resurrection from memory.
  state = var.nightly_stop_enabled ? "ENABLED" : "DISABLED"

  # OFF, not a flexible window. A cost control that AWS may defer by up to
  # fifteen minutes is still a cost control, but "at a fixed hour" is the
  # property being asserted, and a fixed hour is also what makes the CloudWatch
  # alarms' treat_missing_data = "notBreaching" honest: the alarms tolerate
  # missing data because the instances are known to be down at a known time.
  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = var.nightly_stop_cron

  # Evaluated in a named IANA zone rather than UTC so the stop follows the
  # operator's clock across the daylight-saving change instead of drifting an
  # hour twice a year.
  schedule_expression_timezone = var.nightly_stop_timezone

  target {
    # Universal target: EventBridge Scheduler calls the EC2 API directly. No
    # Lambda, so nothing to package, version, or debug at 23:00.
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn

    # The three instances, named explicitly. Not a tag filter: a tag-based stop
    # would need ec2:DescribeInstances plus a wildcard StopInstances, which is
    # exactly the blast radius the policy above refuses. StopInstances on an
    # already-stopped instance is a no-op, so a hand-stopped stack is not a
    # failed invocation.
    input = jsonencode({
      InstanceIds = [
        aws_instance.backend.id,
        aws_instance.frontend.id,
        aws_instance.monitoring.id,
      ]
    })

    retry_policy {
      maximum_retry_attempts = 3

      # Five minutes, not the 24-hour default. A nightly stop that finally
      # succeeds twenty hours late stops the stack in the middle of the next
      # working day. Better to miss one night, keep the alarm quiet, and let
      # the next night's invocation do the job.
      maximum_event_age_in_seconds = 300
    }
  }
}

resource "aws_scheduler_schedule" "nightly_stop_rds" {
  name       = "${var.project}-nightly-stop-rds"
  group_name = "default"
  state      = var.nightly_stop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = var.nightly_stop_cron
  schedule_expression_timezone = var.nightly_stop_timezone

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance"
    role_arn = aws_iam_role.scheduler.arn

    # Unlike EC2, StopDBInstance on an already-stopped database returns
    # InvalidDBInstanceState. On a night the operator stopped the stack by
    # hand this invocation therefore fails, retries three times inside five
    # minutes, and is dropped. There is no dead-letter queue and that is
    # deliberate: the failure means the desired state already holds.
    input = jsonencode({
      DbInstanceIdentifier = aws_db_instance.main.identifier
    })

    retry_policy {
      maximum_retry_attempts       = 3
      maximum_event_age_in_seconds = 300
    }
  }
}
