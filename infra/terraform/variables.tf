# ---------------------------------------------------------------------------
# Every input to the root module, declared exactly once.
#
# Two variables deliberately have NO default and must be supplied:
#   operator_cidrs   an ingress allowlist that defaults to something is an
#                    ingress allowlist someone forgets to set
#   alert_email      an alerts topic with no subscriber pages nobody, and a
#                    committed address in a public repository is spam bait
#   ami_id           a default would let an unset AMI apply silently (premortem C7)
#
# Validation blocks here are the cheapest place to catch a Sandbox OU service
# control policy denial. An instance type or region outside the allowlist fails
# at `terraform plan` with a sentence that names the reason, instead of failing
# at RunInstances with an opaque AccessDenied and no explanation.
# ---------------------------------------------------------------------------

# ---- identity and tagging -------------------------------------------------

variable "project" {
  description = "Name prefix for every resource in this account, and the value of the Project tag that ssm:SendCommand and every cost query filter on."
  type        = string
  default     = "toxic-mod"

  validation {
    # ECR repository names and RDS identifiers are lowercase-only, and both are
    # built as "${var.project}-<component>". An uppercase project name fails at
    # create time, several minutes into an apply.
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.project))
    error_message = "The project name must be 3 to 24 characters of lowercase letters, digits and hyphens, starting with a letter, because it prefixes ECR repository names and RDS identifiers."
  }
}

variable "environment" {
  description = "Environment tag applied to every resource through the provider's default_tags. One environment exists for this project; the name matches the GitHub deployment environment that gates deploy.yml."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "The environment name must be 2 to 16 characters of lowercase letters, digits and hyphens, starting with a letter."
  }
}

# ---- region and availability zones ----------------------------------------

variable "region" {
  description = "The only region this workload runs in. The Sandbox OU service control policy denies every region except us-west-2 and us-east-1."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = contains(["us-west-2", "us-east-1"], var.region)
    error_message = "The Sandbox OU service control policy denies every region except us-west-2 and us-east-1; any other value is denied at the API, not here."
  }
}

variable "azs" {
  description = "Availability zones for the two public and two private subnets. Two zones because RDS requires a subnet group spanning at least two, even for a single-AZ instance."
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b"]

  validation {
    condition     = length(var.azs) == 2 && length(distinct(var.azs)) == 2
    error_message = "Exactly two distinct availability zones are required; the RDS subnet group spans both and the subnet resources index [0] and [1]."
  }

  validation {
    # Terraform 1.9 and newer allow a validation condition to reference another
    # variable. A zone in a different region than the provider is a plan-time
    # error with a confusing message and an apply-time failure without this.
    condition     = alltrue([for az in var.azs : startswith(az, var.region)])
    error_message = "Every availability zone must be in var.region."
  }
}

# ---- network addressing ---------------------------------------------------

variable "vpc_cidr" {
  description = "VPC address space. Referenced directly by the DNS egress rules, which are scoped to the VPC resolver rather than the internet."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "The VPC CIDR must be a valid IPv4 CIDR block, for example 10.42.0.0/16."
  }
}

variable "public_subnet_cidrs" {
  description = "Public subnets, one per availability zone. All three EC2 instances live here; there is no NAT gateway, which would cost roughly a third of the monthly ceiling on its own."
  type        = list(string)
  default     = ["10.42.0.0/24", "10.42.1.0/24"]

  validation {
    condition     = length(var.public_subnet_cidrs) == 2
    error_message = "Exactly two public subnet CIDRs are required, one per availability zone."
  }

  validation {
    condition     = alltrue([for cidr in var.public_subnet_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every public subnet CIDR must be a valid IPv4 CIDR block."
  }
}

variable "private_subnet_cidrs" {
  description = "Private subnets, one per availability zone. RDS lives here with no route to the internet gateway. The application tiers' 5432 egress is scoped to these blocks."
  type        = list(string)
  default     = ["10.42.10.0/24", "10.42.11.0/24"]

  validation {
    condition     = length(var.private_subnet_cidrs) == 2
    error_message = "Exactly two private subnet CIDRs are required, one per availability zone, because an RDS subnet group must span two."
  }

  validation {
    condition     = alltrue([for cidr in var.private_subnet_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every private subnet CIDR must be a valid IPv4 CIDR block."
  }
}

# ---- public ingress allowlist ---------------------------------------------

variable "operator_cidrs" {
  description = <<-EOT
    The always-on ingress allowlist for the three public listeners: the operator
    address and nothing else. Deliberately has no default -- an allowlist with a
    default is one nobody sets -- and is rejected below if it contains 0.0.0.0/0
    or any prefix wider than a /24. Widening the exposure is what demo_cidrs is
    for, so that opening the stack to the world is a separate, visible, revertible
    edit rather than a quiet loosening of the standing rule. Set it with:
      printf 'operator_cidrs = ["%s/32"]\n' "$(curl -fsS https://checkip.amazonaws.com)"
  EOT
  type        = list(string)

  validation {
    condition     = length(var.operator_cidrs) > 0
    error_message = "At least one operator CIDR is required; an empty allowlist makes every public listener unreachable, including to you."
  }

  validation {
    condition     = alltrue([for cidr in var.operator_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every operator CIDR must be a valid IPv4 CIDR block, for example 203.0.113.7/32."
  }

  validation {
    # 0.0.0.0/0 here would put the standing allowlist on the whole internet with
    # no record of the decision. The demo window belongs in demo_cidrs.
    condition     = alltrue([for cidr in var.operator_cidrs : can(regex("/(2[4-9]|3[0-2])$", cidr))])
    error_message = "Each operator CIDR must be a /24 or narrower. Opening the listeners more widely is a deliberate act and belongs in demo_cidrs, which is a separate variable so that it is visible and revertible."
  }
}

variable "demo_cidrs" {
  description = <<-EOT
    Temporary public demo window, appended to operator_cidrs on the three public
    listeners. Set it to ["0.0.0.0/0"] only while a grader is looking and set it
    back to [] immediately afterwards; closing it again is on the post-demo
    checklist in docs/tls-decision.md. It never reaches port 8503: the reviewer
    interface has no ingress rule on any security group, which is the structural
    control the no-TLS decision rests on.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.demo_cidrs : can(cidrhost(cidr, 0))])
    error_message = "Every demo CIDR must be a valid IPv4 CIDR block; use 0.0.0.0/0 for a fully open demo window."
  }
}

# ---- compute sizing -------------------------------------------------------

variable "ami_id" {
  description = <<-EOT
    Pinned Amazon Linux 2023 arm64 AMI, supplied by the committed
    infra/terraform/ami.auto.tfvars. Deliberately NOT resolved from
    /aws/service/ami-amazon-linux-latest/... at plan time: an AL2023
    republication would otherwise force replacement of all three instances at an
    arbitrary moment, destroying baked artifacts and pulled images (premortem
    C7). Bump it on purpose with the command in docs/runbooks/no-ssh-debug.md,
    then `terraform apply -replace=aws_instance.backend`, one instance at a time.
  EOT
  type        = string

  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.ami_id))
    error_message = "The ami_id must be a literal ami-xxxxxxxx identifier, not an SSM parameter path."
  }
}

variable "backend_instance_type" {
  description = "EC2 #1, the FastAPI backend. Memory is bounded by the max_features cap on TF-IDF; measure before resizing."
  type        = string
  default     = "t4g.medium"

  validation {
    condition     = contains(["t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"], var.backend_instance_type)
    error_message = "The Sandbox OU service control policy allows only t4g.small, t4g.medium, t4g.large and c7g.xlarge; every other type, and all GPU and metal families, are denied at RunInstances."
  }
}

variable "frontend_instance_type" {
  description = "EC2 #2, the Streamlit user interface and the reviewer console. A thin client that calls the backend API and holds no database credential."
  type        = string
  default     = "t4g.small"

  validation {
    condition     = contains(["t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"], var.frontend_instance_type)
    error_message = "The Sandbox OU service control policy allows only t4g.small, t4g.medium, t4g.large and c7g.xlarge; every other type, and all GPU and metal families, are denied at RunInstances."
  }
}

variable "monitoring_instance_type" {
  description = "EC2 #3, the monitoring dashboard that rubric 3.2 requires on a different server, and the DistilBERT re-scorer if it survives the cut-line. Upsize only against measured ONNX int8 throughput."
  type        = string
  default     = "t4g.medium"

  validation {
    condition     = contains(["t4g.small", "t4g.medium", "t4g.large", "c7g.xlarge"], var.monitoring_instance_type)
    error_message = "The Sandbox OU service control policy allows only t4g.small, t4g.medium, t4g.large and c7g.xlarge; every other type, and all GPU and metal families, are denied at RunInstances."
  }
}

# ---- database -------------------------------------------------------------

variable "db_instance_class" {
  description = "RDS Postgres 16 class. Single-AZ and private. There is no service control policy cap on RDS class -- rds:DatabaseClass is not supported on CreateDBInstance -- so this variable and the budget alerts are the only controls on RDS spend."
  type        = string
  default     = "db.t4g.micro"

  validation {
    condition     = can(regex("^db\\.t4g\\.(micro|small|medium)$", var.db_instance_class))
    error_message = "Only db.t4g.micro, db.t4g.small and db.t4g.medium are sanctioned. The service control policy cannot cap RDS class, so this check is the cap."
  }
}

variable "db_backup_retention_days" {
  description = "RDS automated backup retention. Must be at least 1: a retention of 0 disables automated backups, and premortem H6 requires a teardown that is recoverable as well as possible."
  type        = number
  default     = 7

  validation {
    condition     = var.db_backup_retention_days >= 1 && var.db_backup_retention_days <= 35
    error_message = "The backup retention must be between 1 and 35 days. Zero disables automated backups entirely, which reopens premortem H6."
  }
}

# ---- log retention --------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention for every component log group. The CloudWatch default is forever and log storage is a silent recurring cost against a $100 ceiling; the project's whole life is nineteen days."
  type        = number
  default     = 14

  validation {
    condition = contains(
      [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_days
    )
    error_message = "CloudWatch Logs accepts only a fixed set of retention values; 14 is the value this project uses. See the AWS PutRetentionPolicy documentation for the full list."
  }
}

# ---- continuous integration and deployment --------------------------------

variable "github_repo" {
  description = "owner/name of the single repository allowed to assume the deploy role. Pinned into the OIDC trust policy's sub condition, so no other repository can assume it."
  type        = string
  default     = "rocklambros/mlops-toxic-moderation"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "The github_repo must be in owner/name form, for example rocklambros/mlops-toxic-moderation."
  }
}

# ---- the deployed model and its reviewer ----------------------------------

variable "wandb_artifact" {
  description = "Registry path infra/deploy/instance/fetch_artifacts.sh asks for, in the entity/project/collection:alias form the wandb CLI takes. Published to SSM rather than hardcoded in the script, because promoting a new model version must not require a code change on three instances. Public: MODEL_CARD.md section 9 records the same path, and the registry page is logged-out readable."
  type        = string
  default     = "rockcyber-org/wandb-registry-model/toxic-clf:production"

  validation {
    condition     = can(regex("^[^/]+/[^/]+/[^:]+:.+$", var.wandb_artifact))
    error_message = "The wandb_artifact must be entity/project/collection:alias, for example rockcyber-org/wandb-registry-model/toxic-clf:production."
  }
}

variable "reviewer_id" {
  description = "The single reviewer identity backend/reviewer_auth.py returns when a session token verifies. Not a credential: the shared secret is the authenticator and lives in Secrets Manager. An unset value makes every token authenticate nobody, which is why it has a default rather than being optional."
  type        = string
  default     = "rock"

  validation {
    condition     = length(var.reviewer_id) > 0
    error_message = "The reviewer_id must not be empty; an empty identity authenticates nobody and the review queue is unusable."
  }
}

# ---- alerting and budget --------------------------------------------------

variable "alert_email" {
  description = "Address subscribed to the SNS topic that carries budget alerts, the two health alarms, and root-usage events. No default: a committed address in a public repository is spam bait, and the subscription must be confirmed by clicking the link before the topic has any confirmed subscriber at all."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[A-Za-z]{2,}$", var.alert_email))
    error_message = "The alert_email must be a single valid email address."
  }
}

variable "monthly_budget_usd" {
  description = "Hard ceiling the budget alerts fire against, at 50, 80 and 100 percent of both actual and forecast spend. See docs/cost-model.md; scenario C reaches this ceiling without a single service control policy violation."
  type        = number
  default     = 100

  validation {
    condition     = var.monthly_budget_usd > 0 && var.monthly_budget_usd <= 500
    error_message = "The monthly budget must be greater than 0 and no more than 500 USD. The project ceiling is 100; anything above 500 is a typo."
  }
}

# ---- nightly stop, the hard duration control ------------------------------

variable "nightly_stop_enabled" {
  description = <<-EOT
    Whether the EventBridge schedule stops the three instances and the database
    every night. On by default, because the service control policy's instance-type
    allowlist caps the hourly rate and says nothing about duration: three
    allowlisted instances plus RDS left running reach the $100 ceiling inside a
    month without a single policy violation (premortem H7, cost-model scenario C).
    Turn it off for the grading window with
    `terraform apply -var nightly_stop_enabled=false`, and turn it back on
    afterwards. It also disarms the seven-day RDS auto-restart, because a database
    stopped nightly never accumulates seven stopped days.
  EOT
  type        = bool
  default     = true
}

variable "nightly_stop_cron" {
  description = "EventBridge Scheduler expression for the nightly stop, evaluated in nightly_stop_timezone."
  type        = string
  default     = "cron(0 23 * * ? *)"

  validation {
    condition     = can(regex("^(cron|rate|at)\\(.+\\)$", var.nightly_stop_cron))
    error_message = "The schedule must be an EventBridge Scheduler expression: cron(...), rate(...) or at(...)."
  }
}

variable "nightly_stop_timezone" {
  description = "IANA timezone the nightly stop expression is evaluated in. Named explicitly so the schedule follows the operator's clock across daylight-saving changes rather than drifting an hour twice a year."
  type        = string
  default     = "America/Denver"

  validation {
    condition     = can(regex("^[A-Za-z]+/[A-Za-z_+-]+$", var.nightly_stop_timezone))
    error_message = "The timezone must be an IANA name such as America/Denver, not an abbreviation or a UTC offset."
  }
}
