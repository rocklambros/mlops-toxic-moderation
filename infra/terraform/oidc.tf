# ---------------------------------------------------------------------------
# GitHub Actions OIDC. ONE role, and `terraform apply` is not among its powers.
#
# premortem H4, first clause: a `sub` written as a two-element array --
#
#   "StringEquals": {"...:sub": ["repo:owner/repo:ref:refs/heads/main",
#                                "repo:owner/repo:environment:production"]}
#
# -- is evaluated by IAM as a logical OR, so a workflow declaring
# `environment: production` on ANY branch satisfies it and bypasses the
# required-review gate on main. Every condition below is therefore SINGLE-valued,
# and separate condition blocks AND: the token must simultaneously carry the
# production environment in `sub` AND come from deploy.yml at refs/heads/main in
# `job_workflow_ref`. Pinning the workflow file is what pins the ref, because a
# branch has no representation in `sub` once the job declares an environment.
#
# premortem H4, second clause, and H36: `gha-deploy` previously needed `iam:*` to
# apply iam.tf, which is de-facto account administrator. That need is removed at
# the root rather than scoped -- `terraform apply` does not run in GitHub Actions
# at all. Apply is an operator action from the IAM Identity Center session, which
# is what the delivery spec's day 10-11 schedule already assumed. deploy.yml
# builds images, pushes to ECR, and rolls containers through SSM; nothing more.
#
# premortem H36 also removes `gha-ci` entirely. With `terraform plan` gone from
# pull-request CI, the pull-request workflow needs no AWS identity whatsoever, so
# no role exists that a pull request could assume. That is strictly stronger than
# scoping one, and it supersedes the AWS foundation spec section 7.3 row that
# described a read-only `gha-ci` trusted on any ref.
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  # thumbprint_list is omitted on purpose. The attribute is Optional + Computed,
  # so AWS fetches and maintains the current thumbprint itself. Hardcoding a pair
  # is the pattern that goes stale when GitHub rotates its CA, and the attribute
  # cannot be cleared once set: an empty list produces no diff and the API rejects
  # an empty update.
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "gha_deploy_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    # Blocks the confused-deputy case where a token minted for another audience
    # is replayed at STS.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Single-valued, and StringEquals rather than StringLike: no wildcard, no OR.
    # This is the exact clause premortem H4 is about.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:environment:production"]
    }

    # ANDed with the above by IAM because it is a separate condition block. It
    # pins the identity to one workflow FILE at one REF, so a new workflow added
    # on a feature branch -- or an edited copy of deploy.yml anywhere but
    # refs/heads/main -- cannot assume this role even if it declares the
    # production environment.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:job_workflow_ref"
      values   = ["${var.github_repo}/.github/workflows/deploy.yml@refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "gha_deploy" {
  name               = "${var.project}-gha-deploy"
  assume_role_policy = data.aws_iam_policy_document.gha_deploy_trust.json

  # One hour, matching the --duration-seconds the deploy workflow requests. A
  # longer ceiling only widens the window a leaked session token is useful in.
  max_session_duration = 3600
}

data "aws_iam_policy_document" "gha_deploy" {
  statement {
    sid       = "EcrAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Push and pull on the four project repositories by ARN. Tags are immutable on
  # every repository, so this cannot overwrite an already-published SHA tag.
  statement {
    sid    = "EcrPushToTheFourProjectRepositories"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repo in aws_ecr_repository.app : repo.arn]
  }

  # SendCommand needs BOTH an instance resource and a document resource to
  # authorize, so these two statements are a pair rather than a duplication. The
  # instance side is fenced by the Project tag every instance carries.
  statement {
    sid       = "SsmSendCommandToProjectInstancesOnly"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/Project"
      values   = [var.project]
    }
  }

  statement {
    sid       = "SsmSendCommandRunShellScriptDocumentOnly"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  # premortem H5: SendCommand is fire-and-forget. A tag match of zero instances
  # returns a CommandId and exits 0, so the deploy job must poll to a terminal
  # state and count invocations. These four reads are what make that possible.
  # Resource "*" is deliberate: none of them is usefully resource-scopable, and
  # all four are read-only.
  statement {
    sid    = "SsmObserveTheRollToATerminalState"
    effect = "Allow"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
      "ssm:DescribeInstanceInformation",
    ]
    resources = ["*"]
  }

  # Second layer. The Allow set above is already narrow; this makes privilege
  # escalation and infrastructure change impossible to reintroduce by editing one
  # statement, and an explicit Deny cannot be overridden by any later Allow.
  #
  #   iam / sso / organizations / sts:AssumeRole -- no path to another identity
  #   ec2 / rds / s3                             -- apply belongs to the operator
  #   ssm:StartSession                           -- CI never gets a shell
  #   ecr delete verbs                           -- rollback targets survive a
  #                                                 compromise of this role
  statement {
    sid    = "DenyPrivilegeEscalationAndInfrastructureChange"
    effect = "Deny"
    actions = [
      "iam:*",
      "organizations:*",
      "sso:*",
      "sso-admin:*",
      "sts:AssumeRole",
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "rds:*",
      "s3:*",
      "ssm:StartSession",
      "ecr:DeleteRepository",
      "ecr:BatchDeleteImage",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "gha_deploy" {
  name   = "${var.project}-gha-deploy"
  role   = aws_iam_role.gha_deploy.id
  policy = data.aws_iam_policy_document.gha_deploy.json
}
