# Design Spec: AWS Account Foundation

- Version: 1.0
- Owner: Rock Lambros
- Date: 2026-07-30
- Status: approved
- Amends: `docs/2026-07-01-toxic-moderation-mlops-design.md` (v1.0 to v1.1)
- Scope: the AWS account, identity model, guardrails, and deployment pipeline that the toxic-moderation MLOps system runs on

## 1. Why this exists

The original plan targeted an AWS Academy lab account. A real AWS account is now available, which removes the lab's expiry and permission ceilings and replaces them with two obligations the lab did not impose: the spend is real, and the identity model is ours to get right.

This spec covers the account and everything under it. It does not change the application architecture, the model strategy, the leakage firewall, or the database schema. Those stay exactly as written in the v1.0 design.

## 2. Starting position

| Fact | Value |
|---|---|
| AWS Organization | Exists. Management account under `rock@rockcyber.com` |
| Existing member account | `<MGMT_ACCOUNT_ID>` (RCAP), out of scope for changes |
| Local credentials | IAM users `rc-script-user` and `llm-safety-study-admin`, both in `<MGMT_ACCOUNT_ID>`, both static keys |
| Management account CLI access | None |
| IAM Identity Center | Not enabled |
| Repository | `rocklambros/mlops-toxic-moderation`, private, flipping to public |
| Region | `us-west-2` (changed from `us-west-1`) |

The absence of management-account credentials is the constraint that shapes the bootstrap. The correct fix is not a new static key. It is IAM Identity Center.

## 3. Account and organization topology

```
Organization root (management account, rock@rockcyber.com)
  |
  +-- (existing OU / root)          <MGMT_ACCOUNT_ID>  RCAP        [untouched]
  |
  +-- Sandbox OU                    <new>         mlops-toxic [this project]
        SCP: sandbox-guardrails
```

The new member account:

| Property | Value |
|---|---|
| Account name | `rockcyber-mlops-toxic` |
| Root email | `rock+aws-mlops-toxic@rockcyber.com` |
| Account alias | `rockcyber-mlops-toxic` |
| Created by | `organizations:CreateAccount` from the management account |
| Cross-account role | `OrganizationAccountAccessRole` (created automatically) |
| Root credentials | Deleted after creation, per section 5 |

Creating the account through Organizations is what makes root access keys unnecessary. The management account can assume `OrganizationAccountAccessRole` into the new account from the moment it exists, so no phase of this project ever handles a root credential. This is the single largest security improvement over the RCAP bootstrap, which required temporary root access keys.

**Mail delivery caveat.** `rockcyber.com` routes inbound mail through Mimecast. Mimecast recipient validation is a known cause of plus-addressed mail being rejected before it reaches the mailbox. Verify delivery to `rock+aws-mlops-toxic@rockcyber.com` with a test message **before** running the bootstrap. Changing a root email after root credentials are deleted is painful. If it bounces, use a mail alias such as `aws-mlops@rockcyber.com` pointing at `rock@rockcyber.com`, which requires no new mailbox and no new license seat.

## 4. Identity model

### 4.1 The console minimum

Four one-time console operations in the management account. There is no API to create an IAM Identity Center organization instance, which makes this the irreducible manual surface.

1. Enable IAM Identity Center. Home region `us-west-2`.
2. Create the directory user for Rock Lambros.
3. Create an `AdministratorAccess` permission set.
4. Assign that user and permission set to the management account.

Then `aws configure sso` locally. Every remaining action in this project is an API call.

### 4.2 Principals and how each authenticates

| Principal | Credential | Lifetime |
|---|---|---|
| Rock, from the Mac or the Jetson | Identity Center SSO session | Short-lived, refreshed by `aws sso login` |
| GitHub Actions CI | OIDC web identity, `gha-ci` role | Per-job |
| GitHub Actions deploy | OIDC web identity, `gha-deploy` role | Per-job |
| EC2 instances | Instance profile | Rotated by AWS |
| RDS master password | Secrets Manager, managed by RDS | Rotatable |

**No static AWS access key exists anywhere in this project.** Section 5 turns that from a convention into an enforced control.

### 4.3 Permission sets on the new account

| Permission set | Assigned to | Purpose |
|---|---|---|
| `MlopsToxicAdmin` | Rock | Day-to-day build and deploy |
| `MlopsToxicReadOnly` | unassigned, available | Grader or reviewer access if ever needed |

## 5. Guardrails

### 5.1 Service control policy, attached to the Sandbox OU only

RCAP inherits nothing from this policy. Each statement earns its place.

| Deny | Rationale |
|---|---|
| All regions except `us-west-2` and `us-east-1` | Blocks resource sprawl and crypto-mining in unwatched regions. `us-east-1` stays allowed because IAM, billing, Route 53, and CloudFront endpoints live there |
| `iam:CreateUser`, `iam:CreateAccessKey`, `iam:CreateLoginProfile` | Makes "no static credentials" an enforced property rather than a habit |
| `ec2:RunInstances` outside the Graviton size allowlist | Primary cost guardrail |
| All GPU and metal instance families | A single accidental GPU instance would exceed the monthly ceiling in days |
| RDS instance classes above `db.t4g.small` | Cost guardrail on the second-largest line item |
| `organizations:LeaveOrganization` | Prevents the account escaping its own guardrails |
| `cloudtrail:StopLogging`, `cloudtrail:DeleteTrail`, `guardduty:Delete*`, `guardduty:Disassociate*` | Tamper resistance on the detective controls |

The instance-type allowlist is the compensating control for declining an auto-stop budget action. Cost overrun requires an SCP violation first.

### 5.2 Root credential removal

After the account is created, its root credentials are removed using AWS Organizations centralized root access management, so the member account has no root password and no root access keys. Privileged root operations, if ever needed, run as short-lived sessions initiated from the management account.

**Accepted trade-off.** This removes the ability to recover the member account independently of the organization. For a project account inside an organization you control, that is the correct trade. It would be the wrong trade for an account that must survive the loss of its management account.

**Verify at implementation.** The exact API and CLI surface for centralized root access management was not confirmed during design. Documentation retrieval returned unreliable content. Confirm the operation names against current AWS documentation before writing the bootstrap step, and confirm the installed AWS CLI v2 version supports them.

### 5.3 Budget

`$100` per month with SNS email alerts at 50, 80, and 100 percent of forecast and actual. No automated stop action, by owner decision. The SCP and the manual teardown targets in section 9 carry that load instead.

## 6. Bootstrap script

`infra/aws/bootstrap.sh`. Runs once, from an Identity Center session against the management account. Idempotent throughout: every step checks for existence before creating, so a re-run after a partial failure is safe.

Ordered steps:

1. Preflight. Verify AWS CLI v2, verify the caller is in the management account, verify Terraform version, verify `gh` authentication.
2. Enable the `SERVICE_CONTROL_POLICY` policy type on the organization root.
3. Create the `Sandbox` OU.
4. Create the SCP from `infra/aws/scp-sandbox-guardrails.json` and attach it to the OU.
5. Create the member account. Poll `CreateAccountStatus` until `SUCCEEDED`. Capture the account ID.
6. Move the account into the `Sandbox` OU.
7. Remove root credentials for the new account.
8. Create the two Identity Center permission sets, provision them to the new account, assign Rock to `MlopsToxicAdmin`.
9. Assume `OrganizationAccountAccessRole` into the new account. Set the account alias. Create the Terraform state bucket with versioning, SSE, and public access blocked.
10. Write `infra/aws/bootstrap-outputs.env` with the account ID, region, and state bucket name. This file is gitignored.

The script never prints or stores a long-lived credential. Its only durable output is the outputs file and the resources it created.

**SCP ordering note.** The SCP attaches to the OU before the account moves into it, so the account is never inside the OU unprotected. Terraform runs after the SCP is live, which means the Terraform configuration must itself satisfy the guardrails. That is intentional: it proves the guardrails permit the real workload.

## 7. Terraform scope

`infra/terraform/`, flat layout with one file per concern. Roughly forty resources, where module indirection would cost more than it returns.

State in the bootstrap-created S3 bucket using S3 native locking, which requires Terraform 1.10 or newer. This drops the DynamoDB lock table that RCAP uses.

### 7.1 Network

One VPC, `10.42.0.0/16`. Two public and two private subnets across `us-west-2a` and `us-west-2b`. Internet gateway and a public route table.

**No NAT gateway.** A NAT gateway costs roughly a third of the monthly ceiling by itself and buys nothing here. Both EC2 instances sit in a public subnet with restrictive security groups and reach ECR and Weights & Biases directly. RDS sits in the private subnets with no internet path.

Security groups:

| Group | Ingress | From |
|---|---|---|
| `sg-app` | 8000 (FastAPI), 8501 (user UI), 8502 (monitoring) | IP allowlist variable, defaults to the operator address |
| `sg-db` | 5432 | `sg-app` only |

**Port 22 is not opened on any security group.** Section 8 explains what replaces it.

IMDSv2 is required on both instances, which closes the SSRF-to-credential-theft path.

### 7.2 Compute and data

| Resource | Spec | Notes |
|---|---|---|
| EC2 #1 | `t4g.medium`, AL2023 arm64 | FastAPI backend and Streamlit user and reviewer UI |
| EC2 #2 | `t4g.large`, AL2023 arm64 | Monitoring dashboard and DistilBERT ONNX re-scorer. Confirm or resize after measuring ONNX int8 throughput |
| RDS | Postgres 16, `db.t4g.micro`, 20 GB gp3, single-AZ | `manage_master_user_password = true` |
| ECR | 4 repositories, scan on push, immutable tags, keep last 10 | backend, frontend, monitoring, rescorer |

AMI resolved from the SSM public parameter for AL2023 arm64 rather than a hardcoded AMI ID, so the image stays current without a code change.

`manage_master_user_password = true` matters more than it looks. It puts RDS in charge of generating and storing the master password in Secrets Manager, which keeps the password out of Terraform state entirely. A `random_password` resource would write the plaintext into state.

Root volumes are encrypted. RDS storage is encrypted with the default `aws/rds` key rather than a customer-managed key, which saves the monthly CMK charge. A customer-managed key would be the right call for regulated data and is not warranted for a public dataset.

### 7.3 IAM inside the account

| Role | Trust | Permissions |
|---|---|---|
| `ec2-app-role` | EC2 | `AmazonSSMManagedInstanceCore`, ECR pull, Secrets Manager read on named ARNs, CloudWatch Logs write |
| `gha-ci` | GitHub OIDC, any ref on this repo | Read-only plus `terraform plan`. No write, no push, no deploy |
| `gha-deploy` | GitHub OIDC, `refs/heads/main` and `environment:production` only | `terraform apply`, ECR push, SSM `SendCommand` on tagged instances |

Splitting CI from deploy is what stops a pull request from a fork reaching production credentials. The OIDC trust conditions pin both `aud` and `sub`, and `sub` is pinned to the full repository path so no other repository can assume either role.

No policy in this account uses `Resource: "*"` outside the operations that genuinely require it, such as `ecr:GetAuthorizationToken`.

### 7.4 Secrets

Secrets Manager holds the Weights & Biases API key and the reviewer shared secret. Both are seeded once by CLI, never by Terraform, so no secret value passes through Terraform state or the repository. The RDS master password is managed by RDS as described above.

### 7.5 Observability

Single-account CloudTrail to a dedicated S3 bucket. GuardDuty enabled. CloudWatch log groups at 14-day retention, because default retention is forever and log storage is a silent cost.

GuardDuty is the one optional recurring cost in this design, likely a few dollars a month at this account's event volume. It is the strongest detective control available for the price.

## 8. Build and deployment pipeline

The repository goes public, which grants free unlimited GitHub-hosted arm64 runners (`ubuntu-24.04-arm`, 4 vCPU, 16 GB RAM). Graviton images build natively rather than under QEMU emulation.

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | Pull request to `main` | ruff, pytest, gitleaks, semgrep, `terraform fmt`, `validate`, and `plan` through `gha-ci`. Blocks merge on failure |
| `deploy.yml` | Push to `main` | Builds four arm64 images tagged by git SHA, pushes to ECR, runs `terraform apply`, rolls containers through SSM. Gated by the `production` GitHub environment with required review |
| `runpod-reaper.yml` | Schedule | Unchanged from the v1.0 design |

**Deployment runs over SSM Run Command.** No SSH, no key material, no bastion host, no open port 22. The deploy job sends a command to instances selected by tag, and the command pulls the pinned image digest and restarts the compose stack. Removing SSH removes an entire class of key-management and exposure problems, and it is why the instance security groups need no administrative ingress.

Image tags are immutable and keyed to the git SHA, so a deployed container traces back to an exact commit.

## 9. Cost model and controls

Approximate `us-west-2` on-demand rates, to be verified in the AWS Pricing Calculator rather than trusted from this document:

| Resource | Rough hourly |
|---|---|
| EC2 #1 `t4g.medium` | ~$0.034 |
| EC2 #2 `t4g.large` | ~$0.067 |
| RDS `db.t4g.micro` | ~$0.016 |

Roughly $0.12 per hour with everything running. Left up continuously that approaches the $100 ceiling within a month. Run only during work sessions and it stays in single-digit dollars.

Controls, in order of strength:

1. The SCP instance-type allowlist. Hard denial, cannot be overspent past.
2. `terraform destroy`. Full teardown between phases.
3. `make aws-down` and `make aws-up`. Stop and start EC2 and RDS between sessions.
4. Budget alerts at 50, 80, and 100 percent.

**Documented gotcha.** A stopped RDS instance restarts automatically after seven days. Stopped is not off. For gaps longer than a week, destroy rather than stop.

## 10. Machine portability and the handoff checkpoint

This project is executed from the Jetson build box. The design is machine-portable by construction, so switching machines costs one `aws configure sso` run.

Three properties make that true. No static credentials exist to copy. Terraform state lives in S3 rather than on local disk. Every artifact lives in the repository.

| Stage | Location | Produces | Safe to switch machines |
|---|---|---|---|
| A. Author spec, bootstrap script, Terraform, workflows | Any machine, zero AWS calls | Committed code | Yes, at any point |
| B. Install AWS CLI v2 and Terraform 1.10+, enable Identity Center, `aws configure sso` | The machine that will operate the account | An SSO profile | Yes, repeat per machine |
| C. Run `bootstrap.sh`, then `terraform apply` | Same machine, or CI | Live AWS resources | Yes, state is remote |

Both the Mac and the Jetson get SSO profiles, so either can deploy. CI is the primary deployment path and the Jetson is the manual fallback.

`docs/HANDOFF.md` carries the current stage, what exists, and the exact resume command.

## 11. Security decisions and accepted trade-offs

Recorded honestly, including the ones that cut against maximum security.

| Decision | Trade-off accepted |
|---|---|
| No auto-stop budget action | A runaway cost is caught by alert and SCP rather than stopped automatically. Owner decision, compensated by the instance-type allowlist |
| No NAT gateway, EC2 in public subnets | Instances have public IPs. Mitigated by an ingress allowlist, no port 22, and IMDSv2. The alternative costs roughly a third of the monthly budget |
| Default `aws/rds` encryption key | No customer-managed key, no key policy control, no independent key rotation schedule. Correct for a public dataset, wrong for regulated data |
| Root credentials deleted on the member account | The account cannot be recovered independently of the organization |
| Repository public from the start | A leaked secret is exposed immediately rather than after a detection window. Mitigated by gitleaks in CI and by no secret ever entering the repository |
| RCAP left unchanged | Two static access keys continue to exist in `<MGMT_ACCOUNT_ID>`. Documented in the audit, deliberately out of scope here |
| Single reviewer shared secret | Unchanged from v1.0. Not a real authentication system. Acceptable for a class project, named as such in the model card |

## 12. Read-only RCAP audit

A separate, non-modifying deliverable at `docs/rcap-iam-audit.md` covering account `<MGMT_ACCOUNT_ID>`: access key age on `rc-script-user` and `llm-safety-study-admin`, attached policy breadth, MFA state on IAM users and root, root credential state, public S3 exposure, and CloudTrail coverage. Read-only API calls only. Its purpose is to let the Identity Center migration be decided on evidence, later, as separate work.

## 13. Changes required in existing documents

| Document | Change |
|---|---|
| `docs/2026-07-01-toxic-moderation-mlops-design.md` | New section 3.1 on account, identity, and guardrails. Edits to 3, 12, 13, 15, 18. Region to `us-west-2`. Version to 1.1 |
| `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` | Region to `us-west-2`. Resolved decisions 1 and 6 rewritten. New Phase A. Phase 5 deployment rewritten for ECR, OIDC, and SSM |
| `docs/HANDOFF.md` | New |
| `SECURITY.md` | Now mandatory under QC.1, because the repository is public |

## 14. Prerequisites

| Prerequisite | Current state |
|---|---|
| AWS CLI v2 | **Missing.** v1.35.0 installed via pip. Install v2 on the operating machine |
| Terraform 1.10+ | **Missing.** 1.5.7 installed. Required for S3 native state locking |
| `gh` CLI authenticated | Present, v2.92.0 |
| Test mail to `rock+aws-mlops-toxic@rockcyber.com` delivers | **Unverified.** Mimecast recipient validation is the risk |
| IAM Identity Center enabled | Not yet. Four console operations, section 4.1 |

## 15. Open items

1. Centralized root access management API surface, per section 5.2. Verify against current documentation before implementing step 7 of the bootstrap.
2. EC2 #2 instance class stays provisional until ONNX int8 throughput is measured, carried forward from v1.0 section 18.
3. The ingress IP allowlist defaults to the operator address. Opening it for a public demo window is a variable toggle, not a code change, and should be closed again afterward.
