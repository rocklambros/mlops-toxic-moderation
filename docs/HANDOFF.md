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

## 1. Install tooling

Both are missing on the Mac and need checking on the Jetson. Versions are pinned deliberately. Do this on whichever machine will run the bootstrap. Doing it on both is fine and costs nothing.

### On the Jetson (aarch64 Linux)

```bash
# AWS CLI v2. v1 predates every root-credential and SSO operation this
# project needs. --update is safe whether or not a v2 already exists.
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
unzip -q -o /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install --update
aws --version                       # must print aws-cli/2.x

# Terraform 1.15.8. Anything below 1.11 lacks GA S3 native state locking,
# which this design depends on.
curl -fsSL https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_linux_arm64.zip -o /tmp/tf.zip
unzip -q -o /tmp/tf.zip -d /tmp
sudo install -m 755 /tmp/terraform /usr/local/bin/terraform
terraform version                   # must print v1.15.8
```

### On the Mac (Apple silicon)

```bash
# AWS CLI v2. Installs to /usr/local/bin/aws.
curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o /tmp/AWSCLIV2.pkg
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /

# Terraform 1.15.8 darwin_arm64.
curl -fsSL https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_darwin_arm64.zip -o /tmp/tf.zip
unzip -q -o /tmp/tf.zip -d /tmp
sudo install -m 755 /tmp/terraform /usr/local/bin/terraform
```

**PATH conflict on the Mac.** A pip-installed AWS CLI **v1** currently sits at `/Library/Frameworks/Python.framework/Versions/3.12/bin/aws` and will shadow the new v2 if that directory comes first on `PATH`. Check and fix:

```bash
which -a aws                        # v2 at /usr/local/bin/aws must be first
aws --version                       # must print aws-cli/2.x, not 1.35.0
pip uninstall awscli                # cleanest fix, removes the v1 shadow
```

Do not proceed while `aws --version` reports 1.x. Every root-credential and SSO command in this project will fail in confusing ways.

## 2. Make the repository public

One-way door for git history. The history was scanned clean before this was recommended: nine files, all documentation, no secret patterns.

```bash
gh repo edit rocklambros/mlops-toxic-moderation \
  --visibility public --accept-visibility-change-consequences
```

Required by the assignment deliverable, and it unlocks free unlimited `ubuntu-24.04-arm` runners so CI builds Graviton images natively.

## 3. Enable IAM Identity Center in the management account

Console, one time. This is the only manual surface in the entire project. It exists because `sso-admin:CreateInstance` rejects creation inside an organization management account, so there is no API path.

Sign in to the **management account** at `rock@rockcyber.com`. Note that the management account ID is not `<MGMT_ACCOUNT_ID>`. That one is RCAP, a member account. The bootstrap script records the management account ID on its first run.

Every value below is literal. Type it exactly. Anything not listed, leave at its default or blank.

### 3a. Enable the instance

1. Go to `https://console.aws.amazon.com/singlesignon/`
2. **Set the Region selector in the top right to `US West (Oregon) us-west-2` before you click anything else.** The home Region is fixed at creation and cannot be moved afterward.
3. Click **Enable**. If offered a choice between an organization instance and an account instance, choose the **organization** one.
4. On the dashboard, copy the **AWS access portal URL**. It looks like `https://d-XXXXXXXXXX.awsapps.com/start`. You need it in step 3e. Paste it somewhere now.

### 3b. Create the user

Left nav **Users**, then **Add user**.

| Field | Value |
|---|---|
| Username | `rock.lambros` |
| Password | Select **Send an email to this user with password setup instructions** |
| Email address | `rock@rockcyber.com` |
| Confirm email address | `rock@rockcyber.com` |
| First name | `Rock` |
| Last name | `Lambros` |
| Display name | `Rock Lambros` |
| Everything else | Leave blank |

`rock.lambros` matches the admin username already used in the RCAP account, so the convention stays consistent across the org. Change it if you prefer, and if you do, use the same value in step 3d.

Click **Next**. On **Add user to groups**, add nothing and click **Next**. On the review screen click **Add user**.

Then open `rock@rockcyber.com`, click **Accept invitation** in the mail, set a password, and **enroll MFA when prompted**. MFA enrollment happens here, at first sign-in to the access portal, not in the Add user wizard.

### 3c. Create the permission set

Left nav **Permission sets**, then **Create permission set**.

| Field | Value |
|---|---|
| Permission set type | **Predefined permission set** |
| Policy for predefined permission set | `AdministratorAccess` |
| Permission set name | `AdministratorAccess` (leave the prefilled value) |
| Description | `Org bootstrap admin` |
| Session duration | `4 hours` |
| Relay state | Leave blank |
| Tags | Skip |

Four hours rather than the one-hour default so a long `terraform apply` does not expire mid-run. Twelve is available and is more standing privilege than this needs.

Click **Next**, then **Create**.

### 3d. Assign the user to the management account

Left nav **AWS accounts**. You will see the organization tree.

1. Tick the checkbox next to the **management account**, which is the account at the root of the tree. Do **not** select `<MGMT_ACCOUNT_ID>`, which is RCAP.
2. Click **Assign users or groups**.
3. **Users** tab, select `rock.lambros`, click **Next**.
4. Select the `AdministratorAccess` permission set, click **Next**.
5. Click **Submit**.
6. Wait until the status shows **Provisioned**. It takes under a minute.

### 3e. Wire the CLI on the Jetson

Run `aws configure sso` and answer exactly this:

| Prompt | Answer |
|---|---|
| `SSO session name` | `rockcyber` |
| `SSO start URL` | the portal URL you copied in step 3a |
| `SSO region` | `us-west-2` |
| `SSO registration scopes` | press Enter to accept `sso:account:access` |

A browser opens. Approve the request. Then:

| Prompt | Answer |
|---|---|
| account selection | the **management account**, not `<MGMT_ACCOUNT_ID>` |
| `CLI default client Region` | `us-west-2` |
| `CLI default output format` | `json` |
| `CLI profile name` | `rc-mgmt` |

Verify:

```bash
aws sts get-caller-identity --profile rc-mgmt
```

It must return an ARN of the form `arn:aws:sts::<management-account-id>:assumed-role/AWSReservedSSO_AdministratorAccess_.../rock.lambros`. **That command succeeding is the gate.** Once it does, the bootstrap script can do everything else.

Re-authenticate any time with `aws sso login --profile rc-mgmt`.

Console click-paths above are written against the current console. AWS moves labels around. The **values** in the tables are the part that matters and are fixed by this design. If a label has moved, match on the value.

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
