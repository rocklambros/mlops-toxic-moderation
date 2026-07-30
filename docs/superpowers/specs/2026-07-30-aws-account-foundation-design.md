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
| AWS Organization | Exists, `<ORG_ID>`, root `<ROOT_ID>` |
| Accounts in the organization | Exactly one: `<MGMT_ACCOUNT_ID>`, name `RockCyber`, root email `rock@rockcyber.com`. **It is the management account, and RCAP's workloads run inside it.** Corrected 2026-07-30 after inspecting the console. An earlier draft called it a member account |
| Local credentials | IAM users `rc-script-user` and `llm-safety-study-admin`, both in `<MGMT_ACCOUNT_ID>`, both static keys |
| Management account CLI access | None |
| IAM Identity Center | Not enabled |
| Repository | `rocklambros/mlops-toxic-moderation`, **public as of 2026-07-30**. History scrubbed of identifiers and gitleaks-clean before the flip |
| Region | `us-west-2` (changed from `us-west-1`) |

The absence of management-account credentials is the constraint that shapes the bootstrap. The correct fix is not a new static key. It is IAM Identity Center.

## 3. Account and organization topology

```
Organization <ORG_ID>
  Root <ROOT_ID>
    |
    +-- RockCyber  <MGMT_ACCOUNT_ID>  rock@rockcyber.com
    |     MANAGEMENT account. Runs RCAP. SCPs cannot apply here.  [untouched]
    |
    +-- Sandbox OU                    SCP: sandbox-guardrails
          +-- rockcyber-mlops-toxic   <new>   [this project]
```

**RCAP is structurally immune to the guardrails, not merely excluded from them.** AWS Organizations documentation is explicit: "SCPs don't affect users or roles in the management account. They affect only the member accounts in your organization." Because RCAP runs in the management account, no SCP this project creates can reach it, even by misattachment. That is a stronger isolation guarantee than the original design assumed, and it is why the `Sandbox` OU carries no risk to existing workloads.

**Noted for the audit, not for this project to fix.** Running a production workload in the organization management account is an AWS anti-pattern. That account cannot be constrained by SCPs, holds organization-wide authority, and is the billing root. This becomes a finding in `docs/rcap-iam-audit.md`.

The new member account:

| Property | Value |
|---|---|
| Account name | `rockcyber-mlops-toxic` |
| Root email | `rock+aws-mlops-toxic@rockcyber.com` |
| Account alias | `rockcyber-mlops-toxic` |
| Created by | `organizations:CreateAccount` from the management account |
| Cross-account role | `OrganizationAccountAccessRole` (created automatically) |
| Root user | Preserved as break-glass and hardened, never deleted, per section 5.2 |

Creating the account through Organizations is what makes root access keys unnecessary. The management account can assume `OrganizationAccountAccessRole` into the new account from the moment it exists, so no phase of this project ever handles a root credential. This is the single largest security improvement over the RCAP bootstrap, which required temporary root access keys.

**Mail delivery is the break-glass dependency.** `rockcyber.com` routes inbound mail through Mimecast, whose recipient validation is a known cause of plus-addressed mail being rejected before it reaches the mailbox.

This matters because of what section 5.2 preserves rather than what it removes. Organizations creates member accounts with no root password, so establishing the break-glass means running root password recovery once, which sends mail to the root address. If that address does not deliver, there is no break-glass. Root recovery is the whole point of keeping root, so the address has to work.

Two things make this safe rather than fragile:

1. The bootstrap sets BILLING, OPERATIONS, and SECURITY alternate contacts to `rock@rockcyber.com` through `account:PutAlternateContact`, which the management account is permitted to do for a member account in the same organization. Operational mail reaches a known-good address regardless of the root address.
2. The management account can change a member account's root email address without root credentials, because the organization has all features enabled. A bad address is fixable rather than terminal.

So this is a thing to confirm early, not a one-way door. Confirm root password recovery reaches you, set a strong password, enroll MFA, and store both. A mail alias such as `aws-mlops@rockcyber.com` pointing at `rock@rockcyber.com` sidesteps plus-addressing entirely and costs no mailbox or license seat.

## 4. Identity model

### 4.1 The console minimum

Four one-time console operations in the management account. This is the irreducible manual surface, for a verified reason: the `sso-admin:CreateInstance` model states the request "is rejected if... the instance is created within the organization management account." An organization instance cannot be created by API. Steps 2 through 4 are scriptable in principle (`identitystore:CreateUser`, `sso-admin:CreatePermissionSet`, `sso-admin:CreateAccountAssignment` all exist), but they cannot run before a management-account credential exists, and this is how that credential is established.

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

Every condition key below was verified against the AWS machine-readable service reference (`https://servicereference.us-east-1.amazonaws.com/`) rather than assumed.

| Deny | Condition key | Rationale |
|---|---|---|
| All regions except `us-west-2` and `us-east-1` | `aws:RequestedRegion` | Blocks resource sprawl and crypto-mining in unwatched regions. `us-east-1` stays allowed because IAM, billing, Route 53, and CloudFront endpoints live there |
| `iam:CreateUser`, `iam:CreateAccessKey`, `iam:CreateLoginProfile` | none needed | Makes "no static credentials" an enforced property rather than a habit |
| `ec2:RunInstances` outside the Graviton size allowlist, and all GPU and metal families | `ec2:InstanceType` | Primary cost guardrail. A single accidental GPU instance would exceed the monthly ceiling in days |
| `rds:CreateDBInstance` with a public endpoint | `rds:PubliclyAccessible` | Hard-blocks an internet-reachable database |
| `rds:CreateDBInstance` without Secrets Manager password management | `rds:ManageMasterUserPassword` | Forces the path that keeps the password out of Terraform state |
| `rds:CreateDBCluster` entirely | none needed | Blocks Aurora, which is where runaway RDS cost actually lives |
| `organizations:LeaveOrganization` | none needed | Prevents the account escaping its own guardrails |
| `cloudtrail:StopLogging`, `cloudtrail:DeleteTrail`, `guardduty:Delete*`, `guardduty:Disassociate*` | none needed | Tamper resistance on the detective controls |

**Two implementation traps, both verified.**

`ec2:InstanceType` is a **resource-level** condition key attached to the `instance` resource of `ec2:RunInstances`, not an action-level key. The deny statement must scope `"Resource": "arn:aws:ec2:*:*:instance/*"`. Scoping it to `"*"` denies every other resource the call creates (volume, network interface, security group), because those resources carry no `ec2:InstanceType` key, so a `StringNotEquals` test evaluates true and denies the whole request. That failure mode denies all instance launches including the intended ones.

`rds:DatabaseClass` is **not supported on `rds:CreateDBInstance`.** The service reference shows it only on `CreateDBCluster`, `ModifyDBCluster`, `CreateBlueGreenDeployment`, `RestoreDBClusterFromSnapshot`, and `RestoreDBClusterToPointInTime`, all of which are Aurora cluster operations. An SCP capping instance class with `StringNotEquals rds:DatabaseClass` would therefore deny **every** `CreateDBInstance` call, because the key is absent from the request context. The original design of this spec contained that defect. There is no SCP-enforceable class cap for a standalone RDS instance, so the RDS cost guardrail is the budget alarm plus the class pinned in Terraform, and the SCP instead enforces the two properties it genuinely can.

The EC2 instance-type allowlist is the compensating control for declining an auto-stop budget action. Cost overrun on compute requires an SCP violation first. RDS does not get that protection, which is a stated gap rather than an assumed control.

### 5.2 Root user posture: hardened, preserved, never revoked

**Decision, owner-directed 2026-07-30: this project does not enable AWS Organizations centralized root access management, and does not delete any root credentials.** Root stays as break-glass. An earlier draft of this spec specified root credential removal. That was wrong for three reasons, recorded here so the decision is not re-litigated.

**Reason one, unscopable blast radius. Corrected 2026-07-30, and it is weaker than first written.** `iam:EnableOrganizationsRootCredentialsManagement` has no OU-level or per-account scoping. It is organization-wide. The first version of this reason claimed that enabling it would reach RCAP. **That was wrong.** RCAP runs in the management account, and the feature applies to member accounts only, so RCAP would not have been affected. The valid residue of the argument is narrower: the feature still cannot be scoped to the `Sandbox` OU, so it would bind every current and future member account rather than just this project's. That conflicts with the blast-radius rule in section 6, though less dramatically than originally stated.

Reasons two and three below are the load-bearing ones. This reason alone would not have justified the reversal.

**Reason two, the mitigation was overstated.** `sts:AssumeRoot` is scoped to exactly five AWS managed task policies: `IAMAuditRootUserCredentials`, `IAMCreateRootUserPassword`, `IAMDeleteRootUserCredentials`, `S3UnlockBucketPolicy`, `SQSUnlockQueuePolicy`. It is not general root access. Tasks that still require real root sign-in include restoring IAM user permissions after an administrator revokes their own, activating IAM access to the Billing console, configuring S3 MFA Delete, retrieving certain tax invoices, registering as a Reserved Instance Marketplace seller, and the KMS unmanageable-key support path. Trading a full break-glass for five tasks is a bad trade.

**Reason three, root is the break-glass.** Removing it removes the recovery path of last resort and makes account recovery depend on the organization surviving. For a project account that is a poor trade, and it is not the property this design needs.

**What replaces it.** The standard root hardening posture, which achieves the security goal without burning the break-glass:

| Control | Note |
|---|---|
| MFA on the root user | Hardware key preferred. AWS enforces MFA for root by default but requires the operator to add the device |
| No root access keys, ever | Verify none exist. This is the credential that actually leaks |
| Root never used for routine work | Daily access is Identity Center. Root is opened only for a task on the list above |
| CloudTrail plus EventBridge alarm on any root usage | Detective control. Root sign-in should page you, because it should never happen unannounced |
| Strong unique password in a password manager | Break-glass credential, stored where you can reach it under duress |
| Alternate contacts set to a monitored address | Section 3 |

**Useful consequence of Organizations.** With all features enabled, the management account can already close member accounts and update member root email addresses, account names, contact information, alternate contacts, and enabled Regions **without root credentials**. Several things people assume require member root do not.

**Note on the new account's starting state.** Organizations creates member accounts with no root password. That is AWS default behavior, not something this design does. Establishing the break-glass therefore means running root password recovery once through the root email address, setting a strong password, and enrolling MFA. Section 3 covers why that makes root-address deliverability a break-glass dependency rather than a formality.

### 5.3 Budget

`$100` per month with SNS email alerts at 50, 80, and 100 percent of forecast and actual. No automated stop action, by owner decision. The SCP and the manual teardown targets in section 9 carry that load instead.

## 6. Bootstrap script

`infra/aws/bootstrap.sh`. Runs once, from an Identity Center session against the management account. Idempotent throughout: every step checks for existence before creating, so a re-run after a partial failure is safe.

Ordered steps:

1. Preflight. Verify AWS CLI v2, verify the caller is in the management account, verify Terraform 1.11 or newer, verify `gh` authentication.
2. `organizations enable-policy-type` for `SERVICE_CONTROL_POLICY` on the organization root.
3. `organizations create-organizational-unit` for `Sandbox`.
4. `organizations create-policy` from `infra/aws/scp-sandbox-guardrails.json`, then `organizations attach-policy` to the OU.
5. `organizations create-account`. Poll `organizations describe-create-account-status` until `SUCCEEDED`. Capture the account ID.
6. `organizations move-account` into the `Sandbox` OU. Then `account put-alternate-contact` three times, for `BILLING`, `OPERATIONS`, and `SECURITY`, all pointing at `rock@rockcyber.com`, so operational mail reaches a known-good address independent of the root address.
7. **Operator step, break-glass setup.** Run root password recovery for the new account through its root address, set a strong password, store it in the password manager, and enroll MFA. Confirm no root access keys exist. Section 5.2 is the checklist. The script prints the instructions and waits. **It does not touch root credentials itself and never calls `iam enable-organizations-root-credentials-management`.**
8. `identitystore:CreateUser` if needed, `sso-admin:CreatePermissionSet` for both sets, `AttachManagedPolicyToPermissionSet`, `ProvisionPermissionSet`, then `CreateAccountAssignment` for Rock on `MlopsToxicAdmin`, polling `DescribeAccountAssignmentCreationStatus`.
9. Assume `OrganizationAccountAccessRole` into the new account. Set the account alias. Create the Terraform state bucket with versioning, SSE, and public access blocked.
10. Write `infra/aws/bootstrap-outputs.env` with the account ID, region, and state bucket name. This file is gitignored.

**Organization-wide operations are out of scope for this script.** The only org-level writes it performs are creating an OU, creating and attaching an SCP to that OU, and creating an account inside it. Every one of those is scoped to the new `Sandbox` OU. Nothing this script does changes the posture of any existing account, RCAP included. Any operation without OU-level scoping is disqualified by that rule, which is what removed centralized root access management from the design.

Every operation named above was verified present in the botocore service models for `organizations`, `iam`, `sts`, `sso-admin`, and `identitystore` at version 1.43.60.

The script never prints or stores a long-lived credential. Its only durable output is the outputs file and the resources it created.

**SCP ordering note.** The SCP attaches to the OU before the account moves into it, so the account is never inside the OU unprotected. Terraform runs after the SCP is live, which means the Terraform configuration must itself satisfy the guardrails. That is intentional: it proves the guardrails permit the real workload.

## 7. Terraform scope

`infra/terraform/`, flat layout with one file per concern. Roughly forty resources, where module indirection would cost more than it returns.

State in the bootstrap-created S3 bucket using S3 native locking via the backend's `use_lockfile = true` argument. **Terraform 1.11 or newer**, not 1.10. The Terraform 1.11 changelog states that S3 native state locking became generally available in 1.11 and that the DynamoDB arguments were deprecated in the same release and will be removed in a future minor version. This drops the DynamoDB lock table that RCAP uses.

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

AMI resolved from the SSM public parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64` rather than a hardcoded AMI ID, so the image stays current without a code change. This matters more than it sounds: AL2023 AMIs carry a 90-day deprecation date, so a pinned AMI ID goes stale on a schedule.

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

**Omit `thumbprint_list` on the OIDC provider.** In the Terraform AWS provider the attribute is `Optional + Computed`, and AWS fetches the thumbprint itself when the argument is absent at create time. RCAP hardcodes two thumbprints, which is the pattern that goes stale when GitHub rotates its CA. Leaving it unset is both less code and more durable. Related provider caveat worth knowing: once set, `thumbprint_list` cannot be cleared, because an empty list produces no diff and the API rejects an empty update.

### 7.4 Secrets

Secrets Manager holds the Weights & Biases API key and the reviewer shared secret. Both are seeded once by CLI, never by Terraform, so no secret value passes through Terraform state or the repository. The RDS master password is managed by RDS as described above.

### 7.5 Observability

Single-account CloudTrail to a dedicated S3 bucket. GuardDuty enabled. CloudWatch log groups at 14-day retention, because default retention is forever and log storage is a silent cost.

**Root usage alarm.** An EventBridge rule on CloudTrail sign-in and API events where the principal is the account root, wired to the SNS topic that carries the budget alerts. This is the detective control that makes keeping the root user safe. Root should never be used unannounced, so any root activity is either you deliberately opening the break-glass or an incident. Terraform owns the rule, so it exists from the first apply.

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
| B. Install AWS CLI v2 and Terraform 1.11+, enable Identity Center, `aws configure sso` | The machine that will operate the account | An SSO profile | Yes, repeat per machine |
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
| Root user preserved as break-glass, not deleted | A live root user is a standing high-value credential. Accepted deliberately, because the alternative is organization-wide, unscopable, reaches RCAP, and buys only five task policies in return. Compensated by MFA, no access keys, no routine use, and a CloudTrail alarm on any root activity. Owner decision, section 5.2 |
| Repository public from the start | A leaked secret is exposed immediately rather than after a detection window. Mitigated by gitleaks in CI and by no secret ever entering the repository |
| RCAP left unchanged | Two static access keys continue to exist in `<MGMT_ACCOUNT_ID>`. Documented in the audit, deliberately out of scope here |
| Single reviewer shared secret | Unchanged from v1.0. Not a real authentication system. Acceptable for a class project, named as such in the model card |

## 12. Read-only RCAP audit

A separate, non-modifying deliverable at `docs/rcap-iam-audit.md` covering account `<MGMT_ACCOUNT_ID>`, which is the organization **management** account and also runs RCAP: access key age on `rc-script-user` and `llm-safety-study-admin`, attached policy breadth, MFA state on IAM users and root, root credential state, public S3 exposure, CloudTrail coverage, and the structural finding that a production workload runs in an account SCPs cannot constrain. Read-only API calls only. Its purpose is to let the Identity Center migration be decided on evidence, later, as separate work.

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
| AWS CLI v2 | **Missing.** v1.35.0 installed via pip. Install v2 on the operating machine. v1 predates every root-credential operation in section 5.2 |
| Terraform 1.11+ | **Missing.** 1.5.7 installed. Required for GA S3 native state locking |
| `gh` CLI authenticated | Present, v2.92.0 |
| IAM Identity Center enabled | Not yet. Four console operations, section 4.1 |
| Repository public | Done 2026-07-30 |

Mail delivery to the root address is **not** a blocking prerequisite. It is bootstrap step 7, where root password recovery establishes the break-glass, per section 3. A bad address is fixable from the management account.

The operator runbook with exact commands for these lives in `docs/HANDOFF.md`.

## 15. Verification record

Claims in this spec were checked against primary sources on 2026-07-30 rather than asserted from memory.

| Claim | Source | Result |
|---|---|---|
| Centralized root access management can be scoped to an OU or account | IAM User Guide `id_root-enable-root-access` | **Refuted.** Organization-wide only. Would have reached RCAP. Feature removed from the design, section 5.2 |
| `sts:AssumeRoot` substitutes for root | IAM User Guide `id_root-user` task list | **Refuted.** Five task policies only. Billing console activation, IAM permission restore, S3 MFA Delete, tax invoices, RI Marketplace, and KMS recovery all still need real root |
| Management account can change member root email, contacts, and close accounts without root | IAM User Guide `id_root-user` | Confirmed. A bad root address is fixable, not terminal |
| `sts:AssumeRoot` task policy ARNs, regional endpoint requirement, 900s cap | botocore model plus IAM User Guide `id_root-user-privileged-task` | Confirmed, retained as reference only since the feature is unused |
| Identity Center org instance cannot be created by API | botocore `sso-admin:CreateInstance` documentation | Confirmed. Console-only |
| Bootstrap automation operations all exist | botocore models for `organizations`, `sso-admin`, `identitystore` | Confirmed, all present |
| `ec2:InstanceType` is resource-level on `RunInstances` | AWS service reference `v1/ec2/ec2.json` | Confirmed. Drove the Resource-scoping trap in 5.1 |
| `rds:DatabaseClass` on `CreateDBInstance` | AWS service reference `v1/rds/rds.json` | **Refuted.** Cluster operations only. Defect corrected in 5.1 |
| `rds:PubliclyAccessible`, `rds:ManageMasterUserPassword` on `CreateDBInstance` | AWS service reference `v1/rds/rds.json` | Confirmed. Adopted as replacement controls |
| S3 native locking GA version | Terraform 1.11 changelog | **Corrected.** 1.11, not 1.10 |
| `thumbprint_list` optional and auto-fetched | terraform-provider-aws `internal/service/iam/openid_connect_provider.go` | Confirmed |
| `manage_master_user_password` conflicts with `password` | terraform-provider-aws `internal/service/rds/instance.go` | Confirmed |
| AL2023 arm64 SSM parameter path | AL2023 User Guide `get-started` | Confirmed |
| Free unlimited arm64 runners on public repos | GitHub Actions hosted-runners reference | Confirmed. 4 vCPU, 16 GB |

## 16. Open items

1. EC2 #2 instance class stays provisional until ONNX int8 throughput is measured, carried forward from v1.0 section 18.
2. The ingress IP allowlist defaults to the operator address. Opening it for a public demo window is a variable toggle, not a code change, and should be closed again afterward.
3. GuardDuty monthly cost is estimated, not measured. It scales with CloudTrail event volume plus VPC flow and DNS log volume, both of which are small here. Check the first full month's bill rather than trusting the estimate.
4. No SCP-enforceable cost cap exists for standalone RDS instance classes, per section 5.1. The budget alarm and the Terraform-pinned class are the only controls. This is a known gap, not an oversight.
