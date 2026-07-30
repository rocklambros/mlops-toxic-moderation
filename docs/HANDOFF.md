# Handoff

Where the project stands, and exactly what to do next. Update this file whenever the stage changes or a machine changes.

- Last updated: 2026-07-30
- Owner: Rock Lambros
- Primary execution machine: Jetson (aarch64 build box)
- Branch: `feat/aws-account-foundation`

## Current stage

**Design complete. Nothing has been executed against AWS.**

No AWS API call has been made, no account exists, no resource has been created, no credential has been issued. The repository is the only artifact.

---

# Do this next

Three operator actions, in this order. Everything after them is scripted.

## 1. Install tooling on the Jetson

Both are missing on the Mac and need checking on the Jetson. Versions are pinned deliberately.

```bash
# AWS CLI v2 for aarch64 Linux. v1 predates every root-credential
# operation this project needs.
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install --update
aws --version        # expect aws-cli/2.x, not 1.x

# Terraform 1.15.8 linux_arm64. Anything below 1.11 lacks GA S3 native
# state locking, which this design depends on.
curl -fsSL https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_linux_arm64.zip -o /tmp/tf.zip
unzip -q /tmp/tf.zip -d /tmp && sudo install -m 755 /tmp/terraform /usr/local/bin/terraform
terraform version    # expect v1.15.8
```

## 2. Make the repository public

One-way door for git history. The history was scanned clean before this was recommended: nine files, all documentation, no secret patterns.

```bash
gh repo edit rocklambros/mlops-toxic-moderation \
  --visibility public --accept-visibility-change-consequences
```

Required by the assignment deliverable, and it unlocks free unlimited `ubuntu-24.04-arm` runners so CI builds Graviton images natively.

## 3. Enable IAM Identity Center in the management account

Console, four operations, one time. This is the only manual surface in the entire project. It exists because `sso-admin:CreateInstance` rejects creation inside an organization management account, so there is no API path.

Sign in to the management account (`rock@rockcyber.com`), then:

1. Open IAM Identity Center. Set the region selector to **`us-west-2`** before enabling, because the home region is fixed at creation. Choose **Enable**.
2. **Users**, then **Add user**. Create your user. Turn on MFA when prompted.
3. **Permission sets**, then **Create permission set**. Predefined, `AdministratorAccess`.
4. **AWS accounts**, select the **management account**, **Assign users or groups**, pick your user and that permission set.

Then wire the CLI on the Jetson:

```bash
aws configure sso          # start URL is on the Identity Center dashboard
aws sts get-caller-identity   # must return an ARN in the management account
```

That last command succeeding is the gate. Once it does, the bootstrap script can do everything else.

---

## Root user: break-glass, preserved

**Nothing in this project deletes, disables, or revokes a root user.** AWS Organizations centralized root access management is deliberately not enabled. Two reasons, both verified:

1. **It has no OU or per-account scoping.** It is organization-wide, so it would reach RCAP `<MGMT_ACCOUNT_ID>` and change that account's root recovery posture. This project's blast-radius boundary is the `Sandbox` OU, and anything that cannot be scoped to it is disqualified.
2. **`sts:AssumeRoot` is not a substitute for root.** It covers exactly five managed task policies. Restoring IAM user permissions after an admin lockout, activating IAM access to the Billing console, S3 MFA Delete, certain tax invoices, RI Marketplace seller registration, and the KMS unmanageable-key path all still require real root sign-in.

Root is hardened instead: MFA enrolled, no access keys, never used for routine work, strong password in a password manager, and a CloudTrail plus EventBridge alarm that fires on any root activity.

**The root email matters because it is the break-glass path.** Organizations creates member accounts with no root password, so establishing break-glass means running root password recovery once through `rock+aws-mlops-toxic@rockcyber.com`. `rockcyber.com` routes through Mimecast, whose recipient validation sometimes rejects plus-addressed mail.

This is a thing to confirm early, not a one-way door. The bootstrap sets BILLING, OPERATIONS, and SECURITY alternate contacts to `rock@rockcyber.com`, so operational mail lands regardless, and the management account can change a member account's root email without root credentials if the address turns out to be bad. To skip the question entirely, make `aws-mlops@rockcyber.com` an alias onto `rock@rockcyber.com` and use it as the root address. An alias is not a mailbox and costs no license seat.

---

## Why switching machines is safe

Three properties make this project machine-portable, so moving costs one `aws configure sso` run:

1. **No static credentials exist.** Every AWS credential is a short-lived Identity Center session or an OIDC web identity.
2. **Terraform state lives in S3**, not on local disk.
3. **Every artifact lives in the repository.**

## Stages

| Stage | Where | Produces | Status |
|---|---|---|---|
| A. Author spec, bootstrap script, Terraform, workflows | Any machine, zero AWS calls | Committed code | Spec done. Code not yet written |
| B. Install tooling, enable Identity Center, `aws configure sso` | The machine that will operate the account | An SSO profile | Not started, see "Do this next" |
| C. Run `bootstrap.sh`, then `terraform apply` | Same machine, or CI | Live AWS resources | Not started |

## What exists right now

| Artifact | Path | State |
|---|---|---|
| AWS foundation design spec | `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` | Written and API-verified |
| Application design spec v1.1 | `docs/2026-07-01-toxic-moderation-mlops-design.md` | Amended |
| Master plan with Phase A | `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` | Amended |
| Jetson session prompt | `claudedocs/jetson-execution-prompt.md` | Revised. Paste block is in it |
| Phase 0 detailed plan | `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md` | Written, not executed |
| Phase A detailed plan | not yet written | **Next artifact to produce** |
| `infra/aws/`, `infra/terraform/` | not yet written | Phase A |
| AWS account | does not exist | Phase A |

## Resume

Spec section 15 records every AWS API claim, its source, and whether it was confirmed or refuted. Use it instead of re-researching. Spec section 5.1 carries two SCP traps that will silently deny the workload if ignored.

Next artifact: the Phase A detailed plan at `docs/superpowers/plans/2026-07-30-phase-a-aws-foundation.md`, then execution on branch `feat/phase-a-aws-foundation`.

Phase A and Phase 0 are independent. Phase 0 needs no cloud access and runs entirely offline against a synthetic fixture, so it is the productive thing to do while the three actions above are pending.
