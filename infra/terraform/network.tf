# ---------------------------------------------------------------------------
# Network: one VPC, two public and two private subnets, an internet gateway,
# and one security group per tier.
#
# There is NO NAT gateway. A NAT gateway costs roughly a third of the $100
# monthly ceiling on its own and buys nothing here: the three EC2 instances sit
# in the public subnets behind per-tier ingress allowlists with no port 22 and
# IMDSv2 required, and RDS sits in the private subnets with no internet path at
# all. Recorded as an accepted trade in the AWS foundation spec section 11.
# ---------------------------------------------------------------------------

# The port contract. This mirrors infra/exposure.py exactly; the Python side and
# this map are compared by test, so a drift on either side is a red build rather
# than a security group that quietly disagrees with the application.
#
# 8503 is listed here because it is real and it is deliberately unreachable: no
# security group in this file grants it ingress. See aws_security_group.reviewer.
locals {
  ports = {
    backend     = 8000
    frontend    = 8501
    monitoring  = 8502
    reviewer_ui = 8503
  }
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.project }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = var.project }
}

# Public subnets. map_public_ip_on_launch is required, not optional: cloud-init
# runs before aws_eip_association completes, so without an auto-assigned public
# address the instance has no route out at exactly the moment user data is
# running `dnf install docker` and registering the SSM agent. The Elastic IP
# replaces the auto-assigned address once the association lands.
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[0]
  availability_zone       = var.azs[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-a", Tier = "public" }
}

resource "aws_subnet" "public_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidrs[1]
  availability_zone       = var.azs[1]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-b", Tier = "public" }
}

# Private subnets. RDS lives here. Two of them only because a DB subnet group
# requires two availability zones, not because anything is multi-AZ.
resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_cidrs[0]
  availability_zone       = var.azs[0]
  map_public_ip_on_launch = false

  tags = { Name = "${var.project}-private-a", Tier = "private" }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_cidrs[1]
  availability_zone       = var.azs[1]
  map_public_ip_on_launch = false

  tags = { Name = "${var.project}-private-b", Tier = "private" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public" }
}

# Deliberately routeless. No NAT gateway exists, and RDS needs no internet path.
# An empty aws_route_table still carries the VPC-local route, which is the only
# route the database has any use for.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${var.project}-private" }
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private.id
}

# ---------------------------------------------------------------------------
# Security groups: one per tier (premortem H16). There is no shared sg-app,
# because one group across three instances means a Streamlit remote-code-
# execution on the internet-facing box reaches the backend and the database as
# freely as the backend does.
#
# EVERY group that fronts an instance declares explicit egress. Terraform
# REMOVES the default 0.0.0.0/0 egress the instant an aws_security_group
# resource exists, and this design has no SSH, no bastion and no NAT, so a
# group without egress produces an instance that never registers with Systems
# Manager and cannot be reached at all (premortem C6). The only channel left is
# the one that is broken.
#
# 443 alone is NOT sufficient, and this is the part that is easy to miss twice:
#   * `ssm.us-west-2.amazonaws.com` is resolved by the VPC resolver over UDP 53
#     (TCP 53 for truncated answers), so without DNS egress there is nothing to
#     open a 443 connection to;
#   * SigV4 request signing and TLS certificate validation both fail on a
#     skewed clock, and the clock comes from the Amazon Time Sync Service at
#     169.254.169.123 over UDP 123.
# Egress restricted to 443/tcp alone reproduces C6 exactly one layer down.
#
# Cross-tier references are one-directional on purpose. An app group's EGRESS
# toward a downstream tier uses that tier's subnet CIDRs, while the downstream
# group's INGRESS uses the upstream group's id. Referencing security group ids
# in both directions is a dependency cycle Terraform refuses to plan.
#
# Port 22 is opened nowhere, in either direction. Operator access is
# `aws ssm start-session`, which rides the 443 egress below.
#
# The public ingress allowlist is written inline as
# concat(var.operator_cidrs, var.demo_cidrs) rather than through a local,
# because the assertion suite compares the rendered expression: the demo toggle
# must be visibly the only thing that widens an ingress rule, in the rule
# itself. var.demo_cidrs is [] outside a demo window.
# ---------------------------------------------------------------------------

locals {
  private_cidrs = var.private_subnet_cidrs
  public_cidrs  = var.public_subnet_cidrs
}

resource "aws_security_group" "backend" {
  name        = "${var.project}-backend"
  description = "EC2 #1 FastAPI. No port 22: operator access is SSM Session Manager."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "FastAPI /predict and /health"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = concat(var.operator_cidrs, var.demo_cidrs)
  }

  # The user UI and the reviewer console both run on EC2 #2, which carries the
  # frontend group. Matching on the group id rather than on a CIDR means the
  # rule keeps working when the instance is replaced and its address changes.
  ingress {
    description     = "FastAPI from the frontend tier"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.frontend.id]
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager, W&B"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback for truncated answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync; a skewed clock breaks SigV4 and TLS"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  egress {
    description = "Postgres in the private subnets"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-backend", Component = "backend" }
}

resource "aws_security_group" "frontend" {
  name        = "${var.project}-frontend"
  description = "EC2 #2 Streamlit user UI on 8501. It holds no database credential."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Streamlit user UI"
    from_port   = 8501
    to_port     = 8501
    protocol    = "tcp"
    cidr_blocks = concat(var.operator_cidrs, var.demo_cidrs)
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback for truncated answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync; a skewed clock breaks SigV4 and TLS"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  # The backend API, and deliberately nothing else. There is no 5432 rule here:
  # the user UI reaches Postgres only through the backend, which is the whole
  # point of having a backend tier (premortem H16). No UI container holds a
  # database credential, so an RCE on the internet-facing box buys no direct
  # write path to the graded tables.
  egress {
    description = "Backend /predict"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = local.public_cidrs
  }

  tags = { Name = "${var.project}-frontend", Component = "frontend" }
}

# The reviewer console tier. Egress only: 8503 has NO ingress rule of any kind,
# on any group, anywhere in this module. That is not an omission, it is the sole
# reason docs/tls-decision.md can accept cleartext HTTP (premortem H15 and H12).
# The reviewer shared secret and the raw comment text never cross the internet,
# because the listener is not on the internet. The operator reaches it with
#   aws ssm start-session --document-name AWS-StartPortForwardingSession \
#     --parameters '{"portNumber":["8503"],"localPortNumber":["8503"]}'
# which rides this group's 443 egress and is TLS-encrypted end to end.
#
# This group is attached to EC2 #2 alongside aws_security_group.frontend: the
# instance needs the frontend group's 8501 ingress and this group's egress, and
# neither group opens 8503.
resource "aws_security_group" "reviewer" {
  name        = "${var.project}-reviewer"
  description = "Reviewer console on ${local.ports.reviewer_ui}. No ingress, by design."
  vpc_id      = aws_vpc.main.id

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback for truncated answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync; a skewed clock breaks SigV4 and TLS"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  # Backend API. The backend runs on EC2 #1 in a PUBLIC subnet, so this is
  # local.public_cidrs. The reviewer console writes its labels through the
  # backend and holds no database credential, so there is no 5432 rule.
  egress {
    description = "Backend API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = local.public_cidrs
  }

  tags = { Name = "${var.project}-reviewer", Component = "reviewer" }
}

resource "aws_security_group" "monitoring" {
  name        = "${var.project}-monitoring"
  description = "EC2 #3 monitoring dashboard. Reads Postgres as monitor_ro only."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "Monitoring dashboard"
    from_port   = 8502
    to_port     = 8502
    protocol    = "tcp"
    cidr_blocks = concat(var.operator_cidrs, var.demo_cidrs)
  }

  egress {
    description = "ECR, SSM, CloudWatch Logs, Secrets Manager"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "VPC DNS resolver, UDP"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "VPC DNS resolver, TCP fallback for truncated answers"
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Amazon Time Sync; a skewed clock breaks SigV4 and TLS"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["169.254.169.123/32"]
  }

  egress {
    description = "Postgres, read-only monitor_ro role"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.private_cidrs
  }

  tags = { Name = "${var.project}-monitoring", Component = "monitoring" }
}

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS Postgres. Reachable from the backend and monitoring tiers only."
  vpc_id      = aws_vpc.main.id

  # By security group id and never by CIDR, and never from the user-facing UI
  # tiers: the frontend and reviewer containers reach Postgres through the
  # backend API and hold no database credential at all (premortem H16).
  ingress {
    description = "Postgres from the backend and monitoring tiers"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    security_groups = [
      aws_security_group.backend.id,
      aws_security_group.monitoring.id,
    ]
  }

  # No egress block, on purpose, and this is the one deliberate exception to the
  # C6 rule above. RDS initiates nothing outbound here: it has no SSM agent to
  # register, no image to pull and no internet path in any case, because the
  # private route table is routeless. Security groups are stateful, so replies
  # to an allowed inbound connection are permitted without an egress rule. An
  # empty egress set is therefore the correct posture rather than an omission.

  tags = { Name = "${var.project}-db", Component = "db" }
}
