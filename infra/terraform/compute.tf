# ---------------------------------------------------------------------------
# THREE instances, one per graded tier (premortem H2).
#
# Rubric 5.1 names "one container for the FastAPI backend, one for the
# frontend", 5.2 requires deployment "to separate EC2 instances", and 3.2 puts
# the monitoring dashboard on "a different EC2 server". Two instances satisfied
# 3.2 only on a permissive reading and left 5.1 plus 5.2 arguable, so the
# delivery spec 4 settles on three. Because the re-scorer sits behind a
# cut-line, EC2 #3 is a t4g.medium rather than a t4g.large, which makes three
# instances cheaper than the two they replace.
#
# Every instance class here is inside the Sandbox OU SCP allowlist
# {t4g.small, t4g.medium, t4g.large, c7g.xlarge}. Anything else is a hard deny
# on ec2:RunInstances and the launch never happens.
#
# ---------------------------------------------------------------------------
# IMDSv2 AND THE HOP-LIMIT TRADEOFF -- read before changing either value.
# ---------------------------------------------------------------------------
#
# http_tokens = "required" makes IMDSv1 unavailable. A credential read now needs
# a PUT to /latest/api/token carrying a custom header, which the classic
# GET-only SSRF primitive (image fetcher, URL preview, webhook tester) cannot
# issue. That is the single highest-value metadata control and it is not
# negotiable here.
#
# http_put_response_hop_limit is a genuine trade with no free option:
#
#   Hop limit 1 -- the AWS default. The IMDS response TTL is 1, so a request
#   originating INSIDE a Docker container on the default bridge network is
#   already one hop too far and is dropped. The container therefore cannot
#   obtain instance-profile credentials at all: no `ecr:GetAuthorizationToken`,
#   no `secretsmanager:GetSecretValue` for the W&B key, the reviewer shared
#   secret, or the RDS credential. The only ways to run the application from
#   there are static access keys baked into the image or mounted on disk, or
#   host networking for every container. Both are worse than what we are
#   defending against.
#
#   Hop limit 2 -- chosen. Containerised code reaches IMDS and the application
#   runs on short-lived, automatically rotated instance-profile credentials with
#   no static keys anywhere. The cost is real and is stated plainly: an SSRF or
#   RCE inside a container can now reach 169.254.169.254 and mint that
#   instance's role credentials, where at hop limit 1 it could not.
#
# Compensating controls, in force, that bound the blast radius of that choice:
#
#   1. IMDSv1 is off (http_tokens = "required"), so a GET-only SSRF still
#      cannot mint a token; the attacker needs full request control.
#   2. One instance profile per tier (premortem H16, iam.tf). Credentials
#      obtained on the internet-facing Streamlit box are the FRONTEND role:
#      the reviewer shared secret, pull on the frontend ECR repository, and
#      write to the frontend log group. That role holds no RDS master secret,
#      and sg-frontend has no path to 5432 at all -- the user UI reaches
#      Postgres only through the backend API (Task 5a).
#   3. No static credentials exist on any instance, so stolen credentials are
#      short-lived and every use is attributable to a named role in CloudTrail.
#   4. GuardDuty is enabled, and
#      UnauthorizedAccess:EC2/InstanceCredentialExfiltration fires when
#      instance-role credentials are used from outside this account.
#   5. Egress is closed by default: 443, VPC DNS, NTP and 5432 only, on every
#      tier. There is no open outbound path for a bulk exfiltration channel.
#   6. No port 22 on any group, so the metadata path is not compounded by an
#      interactive shell.
#
# Revisit this only if every container gains its own credential source. Do not
# "fix" it by lowering the hop limit: the symptom is that `docker login` in user
# data succeeds (the host is hop 1) while an in-container `aws` call fails with
# "Unable to locate credentials", and the box then looks healthy while the
# application is dead.
#
# ---------------------------------------------------------------------------
# C7 -- THE AMI IS PINNED TWICE, ON PURPOSE
# ---------------------------------------------------------------------------
#
# deploy.yml runs `terraform apply` unattended on every push to main. With the
# AMI resolved from /aws/service/ami-amazon-linux-latest/... a routine AL2023
# republication makes `ami` force replacement of ALL THREE instances at an
# arbitrary moment -- destroying baked artifacts, pulled images and the compose
# file, while the SSM roll fires at instances that have not re-registered. A
# README typo fix on day 14 is enough to trigger it.
#
# Both remedies the premortem offers are taken, because they fail differently:
#   - `ami = var.ami_id` with the literal in the committed ami.auto.tfvars and
#     a regex validation, so no data source can move it; and
#   - `lifecycle { ignore_changes = [ami] }`, so even an edited tfvars or a
#     provider-side change cannot roll the fleet unattended.
# Bumping it is a deliberate two-step act, one instance at a time, per
# docs/runbooks/no-ssh-debug.md 7.
#
# ---------------------------------------------------------------------------
# WHAT THE SCP MAKES IMMUTABLE AFTER FIRST APPLY
# ---------------------------------------------------------------------------
#
# infra/aws/scp-sandbox-guardrails.json denies ec2:ModifyInstanceAttribute
# UNCONDITIONALLY on arn:aws:ec2:*:*:instance/*. Terraform uses exactly that API
# to change instance_type, user_data, and vpc_security_group_ids in place, so
# none of those can be edited later without replacing the instance by hand from
# the management account. Two consequences are baked in below:
#   - `ignore_changes` covers user_data as well as ami, so a template edit is
#     never attempted in place (it would fail the whole apply with an SCP deny);
#   - the frontend instance is given both security groups it will ever need at
#     creation time, because adding one later is denied.
# ---------------------------------------------------------------------------

locals {
  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"

  # Docker Compose v2 plugin. AL2023 ships no docker-compose-plugin package, so
  # the binary is fetched in user data and verified against this digest. The
  # digest is recorded HERE, independently of the download, and reviewed with
  # the code; user data does not trust the .sha256 file published next to the
  # binary. Bump both values together, from
  # https://github.com/docker/compose/releases/download/<tag>/docker-compose-linux-aarch64.sha256
  compose_version = "v5.3.1"
  compose_sha256  = "aa611e811d0ea25897839c404bfb5bf93ce706dc51c500a4457890f5d0606a86"

  # Instance-to-instance traffic uses private addresses. sg-frontend permits
  # egress to 8000 only inside the public subnet CIDRs, so the Elastic IP is for
  # humans and graders and would be dropped on the wire between tiers.
  backend_internal_url = "http://${aws_instance.backend.private_ip}:8000"
}

# ---------------------------------------------------------------------------
# EC2 #1 -- backend. FastAPI /predict and /health.
# ---------------------------------------------------------------------------

resource "aws_instance" "backend" {
  ami = var.ami_id

  # var.backend_instance_type, not a literal. The variable carries a validation
  # block against the SCP allowlist, and a literal here makes that validation
  # dead code: an operator who resizes by editing the variable would see no
  # change at all, and one who edits the literal would skip the check that turns
  # an opaque RunInstances AccessDenied into a sentence naming the reason.
  # Default is t4g.medium, so this is the same plan it was.
  instance_type = var.backend_instance_type

  # Public subnet with map_public_ip_on_launch, because cloud-init runs BEFORE
  # aws_eip_association completes; without an auto-assigned address there is no
  # route out while user data is installing Docker. The Elastic IP replaces it
  # when the association lands.
  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.backend.id]
  iam_instance_profile   = aws_iam_instance_profile.backend.name

  # An in-guest `shutdown -h` must stop, never terminate: the stack is stopped
  # between sessions by design and a terminate would destroy the graded box.
  instance_initiated_shutdown_behavior = "stop"

  # Teardown is cost control #2; nothing may block `terraform destroy`.
  disable_api_termination = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "backend"
    log_group       = aws_cloudwatch_log_group.app["backend"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    compose_sha256  = local.compose_sha256
    db_host         = aws_db_instance.main.address
    db_port         = aws_db_instance.main.port
    db_name         = aws_db_instance.main.db_name
    backend_url     = "http://127.0.0.1:8000"
  })

  # The SCP denies ec2:ModifyInstanceAttribute outright, so an in-place user_data
  # update is not an available operation; a change here is a deliberate
  # `terraform apply -replace=aws_instance.backend`, one instance at a time.
  user_data_replace_on_change = false

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  # ORDERING, NOT DECORATION. None of these is reachable through an attribute
  # reference, so without this block Terraform is free to launch the instance in
  # parallel with them -- and user data runs ONCE, with `set -Eeuxo pipefail` and
  # an ERR trap, under `ignore_changes = [user_data]`. A bootstrap that loses this
  # race does not retry and cannot be repaired in place, because the SCP denies
  # ec2:ModifyInstanceAttribute; the only remedy is a hand-driven replace.
  #
  #   route table association  the public subnet uses the VPC MAIN route table
  #                            until the association lands, and the main table has
  #                            no 0.0.0.0/0 route. `dnf -y install docker` on the
  #                            first line of user data then hangs and trips the
  #                            trap. This is premortem C6 one layer down.
  #   internet gateway         the route above is meaningless without it.
  #   *_ssm attachment         AmazonSSMManagedInstanceCore is what lets the agent
  #                            register. There is no SSH to fall back on.
  #   aws_iam_role_policy      ECR pull, logs:PutLogEvents and GetSecretValue. The
  #                            ECR warm-up is retried and non-fatal, but the
  #                            awslogs daemon default set in section 4 is not.
  depends_on = [
    aws_route_table_association.public_a,
    aws_internet_gateway.main,
    aws_iam_role_policy_attachment.backend_ssm,
    aws_iam_role_policy.backend,
  ]

  tags = {
    Name      = "${var.project}-backend"
    Project   = var.project
    Component = "backend"
  }
}

# ---------------------------------------------------------------------------
# EC2 #2 -- frontend. Streamlit user UI on 8501 and the reviewer console on
# 8503, which has NO ingress on any group anywhere; the operator reaches it with
# `aws ssm start-session --document-name AWS-StartPortForwardingSession`, and
# that fact is the entire basis on which docs/tls-decision.md accepts cleartext
# HTTP (premortem H15).
#
# Both security groups are attached at creation because the SCP denies
# ec2:ModifyInstanceAttribute, so a group cannot be added to a live instance
# later. Neither group opens 8503; sg-reviewer contributes egress only.
# ---------------------------------------------------------------------------

resource "aws_instance" "frontend" {
  ami = var.ami_id

  # See the note on aws_instance.backend. Default is t4g.small.
  instance_type = var.frontend_instance_type

  subnet_id = aws_subnet.public_b.id
  vpc_security_group_ids = [
    aws_security_group.frontend.id,
    aws_security_group.reviewer.id,
  ]
  iam_instance_profile = aws_iam_instance_profile.frontend.name

  instance_initiated_shutdown_behavior = "stop"
  disable_api_termination              = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "frontend"
    log_group       = aws_cloudwatch_log_group.app["frontend"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    compose_sha256  = local.compose_sha256
    db_host         = aws_db_instance.main.address
    db_port         = aws_db_instance.main.port
    db_name         = aws_db_instance.main.db_name
    backend_url     = local.backend_internal_url
  })

  user_data_replace_on_change = false

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  # See the note on aws_instance.backend. This instance is in public_b, so it is
  # the public_b association that has to land before cloud-init starts.
  depends_on = [
    aws_route_table_association.public_b,
    aws_internet_gateway.main,
    aws_iam_role_policy_attachment.frontend_ssm,
    aws_iam_role_policy.frontend,
  ]

  tags = {
    Name      = "${var.project}-frontend"
    Project   = var.project
    Component = "frontend"
  }
}

# ---------------------------------------------------------------------------
# EC2 #3 -- monitoring. The dashboard rubric 3.2 requires on "a different EC2
# server", plus the ONNX re-scorer if it survives the cut-line. It reads
# Postgres as monitor_ro and holds no RDS master credential.
# ---------------------------------------------------------------------------

resource "aws_instance" "monitoring" {
  ami = var.ami_id

  # See the note on aws_instance.backend. Default is t4g.medium.
  instance_type = var.monitoring_instance_type

  subnet_id              = aws_subnet.public_a.id
  vpc_security_group_ids = [aws_security_group.monitoring.id]
  iam_instance_profile   = aws_iam_instance_profile.monitoring.name

  instance_initiated_shutdown_behavior = "stop"
  disable_api_termination              = false

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "enabled"
  }

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    region          = var.region
    component       = "monitoring"
    log_group       = aws_cloudwatch_log_group.app["monitoring"].name
    ecr_registry    = local.ecr_registry
    compose_version = local.compose_version
    compose_sha256  = local.compose_sha256
    db_host         = aws_db_instance.main.address
    db_port         = aws_db_instance.main.port
    db_name         = aws_db_instance.main.db_name
    backend_url     = local.backend_internal_url
  })

  user_data_replace_on_change = false

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  # See the note on aws_instance.backend. This instance shares public_a with the
  # backend, so it is the public_a association that gates it.
  depends_on = [
    aws_route_table_association.public_a,
    aws_internet_gateway.main,
    aws_iam_role_policy_attachment.monitoring_ssm,
    aws_iam_role_policy.monitoring,
  ]

  tags = {
    Name      = "${var.project}-monitoring"
    Project   = var.project
    Component = "monitoring"
  }
}

# ---------------------------------------------------------------------------
# Elastic IPs -- one per instance, so the submitted URL survives stop/start.
#
# An auto-assigned public IPv4 address is RELEASED on stop and a different one
# assigned on start, and both the cost model and the nightly EventBridge
# schedule stop these instances every night. Without an Elastic IP, every URL
# captured during development, every screenshot caption, and every entry in the
# security-group allowlist is stale by the next morning.
#
# Since February 2024 AWS charges for EVERY public IPv4 address, auto-assigned
# ones included, so an Elastic IP attached to a running instance costs exactly
# what the auto-assigned address already cost. The only marginal charge is the
# hours the address is held while its instance is stopped -- about $11/month
# worst case for three addresses against a $100 ceiling, and far less because
# the stack is destroyed after grading.
#
# `terraform destroy` releases all three; an EIP left allocated and unassociated
# is the one resource here that bills while everything else is off.
# ---------------------------------------------------------------------------

resource "aws_eip" "backend" {
  domain = "vpc"

  tags = {
    Name      = "${var.project}-backend"
    Component = "backend"
  }
}

resource "aws_eip" "frontend" {
  domain = "vpc"

  tags = {
    Name      = "${var.project}-frontend"
    Component = "frontend"
  }
}

resource "aws_eip" "monitoring" {
  domain = "vpc"

  tags = {
    Name      = "${var.project}-monitoring"
    Component = "monitoring"
  }
}

resource "aws_eip_association" "backend" {
  instance_id   = aws_instance.backend.id
  allocation_id = aws_eip.backend.id
}

resource "aws_eip_association" "frontend" {
  instance_id   = aws_instance.frontend.id
  allocation_id = aws_eip.frontend.id
}

resource "aws_eip_association" "monitoring" {
  instance_id   = aws_instance.monitoring.id
  allocation_id = aws_eip.monitoring.id
}
