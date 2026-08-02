# ---------------------------------------------------------------------------
# The seam Phase 5 consumes. `terraform -chdir=infra/terraform output -json` is
# the only source for addresses, ARNs and names; nothing downstream hardcodes
# them. Adding, renaming or removing an output here is a contract change.
#
# ACCOUNT ID HYGIENE. Five outputs necessarily embed the member account id --
# every ARN does, and an ECR registry host is the account id followed by
# .dkr.ecr. Those five are marked `sensitive` so `terraform apply` and
# `terraform output` print `<sensitive>` instead of writing the account id into
# a terminal, a CI log, or the evidence files that get committed to this public
# repository. It is not a secrecy claim: `terraform output -raw <name>` and
# `-json` still return the value, which is how the runbooks and
# `gh variable set AWS_DEPLOY_ROLE_ARN` consume them. The account id is not a
# credential; it is an enumeration aid, and the same reasoning already keeps
# infra/aws/bootstrap-outputs.env out of version control.
#
# The reviewer interface on port 8503 is deliberately absent from every output.
# It has no ingress rule on any security group -- that is the structural control
# docs/tls-decision.md rests on -- and publishing a URL for it would invite
# someone to try it over the internet, where it is unreachable by design. The
# port-forward is documented in docs/runbooks/no-ssh-debug.md §3 instead.
# ---------------------------------------------------------------------------

# ---- public endpoints, on the stable Elastic IPs --------------------------

output "backend_url" {
  description = "FastAPI base URL on the stable Elastic IP of EC2 #1. /predict and /health hang off this. Built from the Elastic IP rather than the instance's public_ip, because an auto-assigned address is released on stop and a different one is assigned on start, and the cost model instructs stopping between sessions."
  value       = "http://${aws_eip.backend.public_ip}:8000"
}

output "frontend_url" {
  description = "Streamlit user interface on EC2 #2. The reviewer console on the same instance is NOT published here and has no ingress on any security group; see docs/tls-decision.md."
  value       = "http://${aws_eip.frontend.public_ip}:8501"
}

output "monitoring_url" {
  description = "Monitoring dashboard on EC2 #3, which rubric 3.2 requires to be a different EC2 server from the backend and the frontend."
  value       = "http://${aws_eip.monitoring.public_ip}:8502"
}

# ---- instance identity, for SSM ------------------------------------------

output "instance_ids" {
  description = "Instance ids by tier, for aws ssm send-command, aws ssm start-session and the no-SSH debug runbook. There is no SSH and no bastion, so this is the only handle on a running box."
  value = {
    backend    = aws_instance.backend.id
    frontend   = aws_instance.frontend.id
    monitoring = aws_instance.monitoring.id
  }
}

output "ssm_target_tag" {
  description = "Tag key that deploy.yml selects instances on; the tag value is the tier name. Matches the Component tag each aws_instance actually carries, so a SendCommand targeting by tag resolves rather than silently matching zero instances."
  value       = "Component"
}

# ---- container registry and logging --------------------------------------

output "ecr_repository_urls" {
  description = "Push targets for the four component images, keyed by component: backend, frontend, monitoring, rescorer. Key-identical to log_group_names, because both are for_each over the same local.components list. Marked sensitive only because an ECR registry host is the account id in plain sight; read it with `terraform output -json ecr_repository_urls`."
  value       = { for name, repo in aws_ecr_repository.app : name => repo.repository_url }
  sensitive   = true
}

output "log_group_names" {
  description = "awslogs driver targets, keyed by component. The groups are created by Terraform, so the driver needs no logs:CreateLogGroup permission and each instance profile can be scoped to its own group."
  value       = { for name, group in aws_cloudwatch_log_group.app : name => group.name }
}

# ---- database ------------------------------------------------------------

output "db_endpoint" {
  description = "Postgres endpoint including the port, for a DSN. Private: it resolves only inside the VPC and there is no public path to it."
  value       = aws_db_instance.main.endpoint
}

output "db_host" {
  description = "Postgres hostname without the port, for psql --host and the PGHOST variable in the read-only role bootstrap. db_endpoint carries the port; this one does not, and mixing them up is the usual cause of a psql name-resolution failure."
  value       = aws_db_instance.main.address
}

# The identifier, not the endpoint. `aws rds stop-db-instance` and `aws rds
# start-db-instance` take --db-instance-identifier, and `infra/aws/aws_down.sh`
# and `infra/aws/aws_up.sh` read it from here. Without this output those two
# commands fail with `Output "db_instance_id" not found` -- aws_down.sh after the
# dump has already run, which is the most confusing possible moment. Not
# sensitive: it is `<project>-pg` and carries no account id.
output "db_instance_id" {
  description = "RDS instance identifier, for aws rds stop-db-instance and start-db-instance in the session lifecycle commands."
  value       = aws_db_instance.main.identifier
}

output "db_name" {
  description = "Initial database name, declared in exactly one place and read from here by the SSM bootstrap document, the user-data environment file and the application connection strings."
  value       = aws_db_instance.main.db_name
}

output "db_master_secret_arn" {
  description = "ARN of the RDS-managed master credentials secret. RDS generates and stores the password, so it never passes through Terraform state. Readable by the backend instance role only; the two user-interface tiers deliberately cannot read it (premortem H16). Marked sensitive because the ARN carries the account id -- the secret VALUE is never an output at all."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
  sensitive   = true
}

output "db_readonly_secret_arn" {
  description = "ARN of the monitor_ro credentials container. Terraform creates the container and never writes a value into it; the value is seeded once by CLI. Readable by the monitoring instance role only. Marked sensitive because the ARN carries the account id."
  value       = aws_secretsmanager_secret.db_readonly.arn
  sensitive   = true
}

output "db_bootstrap_document" {
  description = "Name of the SSM document that creates or rotates the SELECT-only monitor_ro Postgres role from inside the VPC. The database is private with no bastion, so this document is the only way to run that SQL. Procedure in docs/runbooks/no-ssh-debug.md §8."
  value       = aws_ssm_document.db_bootstrap_readonly.name
}

# ---- deployment identity and alerting ------------------------------------

output "gha_deploy_role_arn" {
  description = "Set this as the GitHub repository variable AWS_DEPLOY_ROLE_ARN. It is the only OIDC role in the account: pull-request CI validates Terraform offline and holds no AWS identity at all (premortem H36), so there is deliberately no second, read-only CI role. Marked sensitive because the ARN carries the account id, which must not be committed; read it with `terraform output -raw gha_deploy_role_arn`."
  value       = aws_iam_role.gha_deploy.arn
  sensitive   = true
}

output "alerts_topic_arn" {
  description = "SNS topic carrying budget alerts, the two health alarms and root-usage events. An email subscription sits in PendingConfirmation until the link is clicked, so confirm it and prove one delivery before believing anything pages. Marked sensitive because the ARN carries the account id."
  value       = aws_sns_topic.alerts.arn
  sensitive   = true
}
