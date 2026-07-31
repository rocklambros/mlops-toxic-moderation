# ---------------------------------------------------------------------------
# Data tier: the private Postgres 16 instance, the Secrets Manager containers the
# three tiers read from, and the SSM-delivered bootstrap that creates the
# SELECT-only role the monitoring dashboard connects with.
#
# No password in this file ever reaches Terraform state. The master password is
# generated and held by RDS itself (manage_master_user_password), and the
# monitor_ro password is seeded by CLI into an empty container after the apply.
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = [aws_subnet.private_a.id, aws_subnet.private_b.id]

  tags = { Name = "${var.project}-db" }
}

# Captured once at creation and then held still in state. `timestamp()` would
# re-evaluate on every plan and produce a permanent diff; a constant identifier
# would fail the second `terraform destroy` with DBSnapshotAlreadyExists.
resource "time_static" "db" {}

# RDS creates /aws/rds/instance/<identifier>/postgresql itself the first time it
# exports, at the CloudWatch default retention of "never expire", and leaves it
# behind after `terraform destroy`. Declaring it here puts the export under the
# same 14-day retention as every other log group in the account (foundation spec
# section 7.5) and makes the teardown complete. aws_db_instance.main depends on
# it, so it is created before RDS would create it and deleted after the instance
# on the way down. The name repeats the identifier expression rather than
# referencing aws_db_instance.main.identifier, which would be a dependency cycle.
resource "aws_cloudwatch_log_group" "rds_postgresql" {
  name              = "/aws/rds/instance/${var.project}-pg/postgresql"
  retention_in_days = var.log_retention_days

  tags = { Name = "${var.project}-pg", Component = "db" }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-pg"
  engine         = "postgres"
  engine_version = "16"

  # var.db_instance_class, not a literal. No service control policy can cap RDS
  # class -- rds:DatabaseClass is not a supported condition key on
  # CreateDBInstance -- so that variable's validation block IS the cap, and a
  # literal here would make it dead code. Default is db.t4g.micro.
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 0
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "toxicmod"
  username = "toxicadmin"

  # Keeps the master password out of Terraform state entirely, and satisfies the
  # Sandbox OU service control policy, which denies CreateDBInstance without it.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  # premortem H6. Backups are retained so a teardown is recoverable, and the final
  # snapshot means `terraform destroy` neither fails nor destroys the dataset the
  # monitoring dashboard is graded on. The identifier is unique per database
  # lifecycle because time_static is created and destroyed alongside the instance.
  backup_retention_period   = var.db_backup_retention_days
  backup_window             = "09:00-09:30"
  maintenance_window        = "sun:10:00-sun:10:30"
  copy_tags_to_snapshot     = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-final-${formatdate("YYYYMMDDhhmmss", time_static.db.rfc3339)}"

  # Teardown is cost control #2, so nothing may block it.
  deletion_protection = false

  auto_minor_version_upgrade      = true
  apply_immediately               = true
  performance_insights_enabled    = false
  enabled_cloudwatch_logs_exports = ["postgresql"]

  depends_on = [aws_cloudwatch_log_group.rds_postgresql]

  tags = { Name = "${var.project}-pg", Component = "db" }
}

# ---------------------------------------------------------------------------
# Secret containers only. Values are seeded once by CLI (foundation spec section
# 7.4) so that no secret value ever passes through Terraform state or the
# repository. A 7-day recovery window rather than the 30-day default, because a
# deleted secret keeps billing until its window closes.
#
# Operational note, because it bites on the second apply: `terraform destroy`
# schedules these for deletion rather than removing them, and Secrets Manager
# refuses to create a secret whose name is still inside another secret's recovery
# window. Re-applying within seven days of a teardown therefore needs
#   aws secretsmanager delete-secret --secret-id <name> --force-delete-without-recovery
# for each of the three names first.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "wandb_api_key" {
  name                    = "${var.project}/wandb-api-key"
  description             = "Weights & Biases API key. Seeded by CLI, read by the backend only."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "reviewer_shared_secret" {
  name                    = "${var.project}/reviewer-shared-secret"
  description             = "Reviewer UI shared secret. Seeded by CLI, read by the frontend only."
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "db_readonly" {
  name                    = "${var.project}/db-readonly"
  description             = "monitor_ro Postgres credentials. Seeded by CLI, read by monitoring only."
  recovery_window_in_days = 7
}

# ---------------------------------------------------------------------------
# The read-only role (premortem H16). The database is private with no bastion,
# so the SQL runs from inside the VPC through SSM Run Command against the
# backend instance. Neither password is ever in Terraform state: the master one
# is RDS-managed and the monitor_ro one is seeded into Secrets Manager by CLI.
# The document is idempotent, so the same invocation creates the role the first
# time and rotates its password on every run after that.
# ---------------------------------------------------------------------------

resource "aws_ssm_document" "db_bootstrap_readonly" {
  name            = "${var.project}-db-bootstrap-readonly"
  document_type   = "Command"
  document_format = "JSON"
  target_type     = "/AWS::EC2::Instance"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Create or update the read-only monitor_ro Postgres role from inside the VPC (monitoring_readonly.sql)."
    parameters = {
      MasterSecretArn = {
        type        = "String"
        description = "ARN of the RDS-managed master credentials secret"
      }
      ReadonlySecretArn = {
        type        = "String"
        description = "ARN of the CLI-seeded monitor_ro credentials secret"
      }
      DbHost = {
        type        = "String"
        description = "RDS endpoint address"
      }
      DbName = {
        type        = "String"
        description = "Database name"
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "createReadOnlyRole"
        inputs = {
          timeoutSeconds = "300"
          runCommand = split("\n", templatefile("${path.module}/sql/bootstrap_readonly.sh.tftpl", {
            sql = file("${path.module}/sql/monitoring_readonly.sql")
          }))
        }
      },
    ]
  })
}
