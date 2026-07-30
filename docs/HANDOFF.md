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

**Mac: done, 2026-07-30. Jetson: still to do.** Versions are pinned deliberately. Do this on whichever machine will run the bootstrap. Doing it on both is fine and costs nothing.

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

### On the Mac (Apple silicon): COMPLETE

Installed 2026-07-30. Verified state:

| Tool | Version | Path | Notes |
|---|---|---|---|
| AWS CLI | `2.36.12` | `~/bin/aws` to `~/aws-cli/aws` | User-local install, no `sudo` |
| Terraform | `1.15.8` | `~/bin/terraform` | Direct binary, no `sudo` |

**How the PATH conflict was solved without removing anything.** A pip-installed AWS CLI **v1** sits at `/Library/Frameworks/Python.framework/Versions/3.12/bin/aws` (PATH position 10), and Homebrew holds Terraform 1.5.7 at `/opt/homebrew/bin/terraform` (position 8). Homebrew's terraform formula is frozen at 1.5.7 because of the license change, so `brew upgrade` cannot reach 1.11 or later.

Rather than uninstalling either, both new binaries went into `~/bin`, which is **PATH position 4** and therefore wins. The old copies remain in place and still work when called by absolute path.

Integrity checks performed before installing:

- Terraform zip SHA-256 matched the published `terraform_1.15.8_SHA256SUMS`.
- The AWS CLI package passed `pkgutil --check-signature`: signed by `Developer ID Installer: AMZN Mobile LLC (94KV3E626L)` and notarized by Apple.

Acceptance tests passed: all ten AWS subcommands this project needs are present (`sso login`, `configure sso`, `organizations create-account`, `organizations describe-create-account-status`, `account put-alternate-contact`, `sts assume-root`, `iam list-organizations-features`, `sso-admin create-permission-set`, `identitystore create-user`, `ssm send-command`), the four existing profiles still resolve, and a backend block with `use_lockfile = true` under `required_version = ">= 1.11"` initializes and validates.

To reproduce on another Mac:

```bash
# Terraform 1.15.8, checksum-verified, into ~/bin
curl -fsSL -O https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_darwin_arm64.zip
curl -fsSL -O https://releases.hashicorp.com/terraform/1.15.8/terraform_1.15.8_SHA256SUMS
grep darwin_arm64 terraform_1.15.8_SHA256SUMS | shasum -a 256 -c -   # must print OK
unzip -q -o terraform_1.15.8_darwin_arm64.zip && install -m 755 terraform ~/bin/terraform

# AWS CLI v2 pinned, signature-verified, user-local install
curl -fsSL -o AWSCLIV2-2.36.12.pkg https://awscli.amazonaws.com/AWSCLIV2-2.36.12.pkg
pkgutil --check-signature AWSCLIV2-2.36.12.pkg                        # AMZN Mobile LLC, notarized
printf '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><array><dict><key>choiceAttribute</key><string>customLocation</string><key>attributeSetting</key><string>%s</string><key>choiceIdentifier</key><string>default</string></dict></array></plist>\n' "$HOME" > choices.xml
installer -pkg AWSCLIV2-2.36.12.pkg -target CurrentUserHomeDirectory -applyChoiceChangesXML choices.xml
ln -sf ~/aws-cli/aws ~/bin/aws && ln -sf ~/aws-cli/aws_completer ~/bin/aws_completer

aws --version        # must print aws-cli/2.x, not 1.35.0
terraform version    # must print v1.15.8
```

Do not proceed anywhere while `aws --version` reports 1.x. Every SSO command in this project fails in confusing ways under v1.

## 2. Make the repository public: COMPLETE

Flipped 2026-07-30. `visibility: PUBLIC`, `isPrivate: false`.

Pre-flight before the flip, in this order:

1. The account ID was scrubbed from all 8 branch commits with `git filter-branch`, rewriting both file contents and commit messages, then force-pushed **while the repo was still private**. That sequencing is what closed the exposure window rather than merely hiding it.
2. `gitleaks detect` over the full history: 13 commits scanned, **no leaks found**.
3. Confirmed the nine tracked files are all documentation, and that `docs/account-ids.local.md` is untracked and ignored.

Required by the assignment deliverable, and it unlocks free unlimited `ubuntu-24.04-arm` runners so CI builds Graviton images natively.

**QC.1 gap closed 2026-07-30.** `SECURITY.md` is written and committed, GitHub private vulnerability reporting is enabled, and secret scanning with push protection is on. Phase A task 9 is therefore done ahead of schedule and only needs a review pass.

## 3. Enable IAM Identity Center in the management account

Console, one time. This is the only manual surface in the entire project. It exists because `sso-admin:CreateInstance` rejects creation inside an organization management account, so there is no API path.

Sign in to the **management account** at `rock@rockcyber.com`.

**Correction, 2026-07-30.** An earlier version of this file said the management account was some account other than the one RCAP runs in. That was wrong. The organization currently contains exactly one account, named `RockCyber`, and it **is** the management account. RCAP's workloads run inside it. The new mlops account will be the organization's first true member account. Concrete IDs are in the gitignored `docs/account-ids.local.md`.

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

1. Tick the checkbox next to **`RockCyber`**, the single account shown under `Root`. That is the management account. Right now it is the only account in the organization, so there is nothing else to pick.
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
| account selection | `RockCyber`, the management account. It is the only choice today |
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

1. **It has no OU or per-account scoping.** It is organization-wide, so it binds every current and future member account rather than just this project's. This project's blast-radius boundary is the `Sandbox` OU, and anything that cannot be scoped to it is disqualified. Note that an earlier version of this file claimed it would reach RCAP. That was wrong: RCAP runs in the management account, and the feature applies to member accounts only. Reason 2 is the load-bearing one.
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
