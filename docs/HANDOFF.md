# Handoff

Where the project stands, and exactly what to do next. Update this file whenever the stage
changes or a machine changes.

- Last updated: 2026-08-10
- Owner: Rock Lambros
- Primary execution machine: Jetson (aarch64 build box)
- Branch: `main`

## Current stage

**Phase 5 complete, submission ready. The system is deployed, live, and reachable from the
public internet.**

Three EC2 instances and one RDS instance are running in the dedicated member account, rolled
by the SHA-pinned deploy workflow and gated on three live `/health` checks. The database
holds 2033 predictions and 650 reviewed items. The four submission deliverables are verified
from a logged-out browser and recorded in `docs/submission-manifest.yml`.

| Where | What | Port |
|---|---|---|
| EC2 #1 `t4g.medium` | FastAPI backend, `/predict` and `/health` | 8000 |
| EC2 #2 `t4g.small` | Streamlit user interface | 8501 |
| EC2 #2, operator only | Reviewer console, no ingress rule on any security group | 8503 |
| EC2 #3 `t4g.medium` | Monitoring dashboard | 8502 |
| RDS `db.t4g.micro` | PostgreSQL `toxic-mod-pg` | 5432 |

The public addresses are deliberately not written here. `README.md` refers to them as
`<eip-1..3>` so a public repository does not advertise three cleartext listeners, and they
resolve from SSM:

```bash
for p in backend frontend monitoring; do
  aws ssm get-parameter --name "/toxic/endpoints/$p" --query Parameter.Value --output text
done
```

**Availability.** Live continuously since 2026-08-02, and reachable from the internet since
2026-08-10, when `infra/terraform/demo.auto.tfvars` opened the three graded listeners to
`0.0.0.0/0`. There is no scheduled close: the operator closes it on request after grading.
`docs/tls-decision.md` records what that open-ended cleartext exposure costs and why it is
accepted; `docs/post-demo-closure.md` owns closing it.

## Resume

```bash
export AWS_PROFILE=rc-mlops AWS_REGION=us-west-2
aws sso login --profile rc-mlops --use-device-code   # PKCE cannot work headless
make aws-up && make deploy-verify && make submission-check
```

`make aws-up` is idempotent: it starts anything stopped, applies the schema, and refuses to
report success until all three health endpoints answer.

## What is not finished

Two things, all recorded rather than hidden. `docs/rubric-conformance.md` carries the same
list at the bottom of the self-grade.

1. ~~`survives_stop_start` is unverified.~~ **Done, 2026-08-10.** The full cycle ran and
   the stack came back healthy with the data intact: `docs/evidence/p5-stop-start-cycle.md`.
   Note that `make aws-up` prints a missing-boot-marker warning on the current fleet. That is
   expected and explained in the evidence document; it disappears on any instance that is
   replaced.
2. **The demo window is open with no scheduled close.** `tests/unit/test_demo_window.py`
   goes red on 2026-09-15 as a backstop. Close it with `make close-demo`, which also rotates
   the reviewer secret and the demo API key, then records nothing — you do that in the
   manifest, and `tests/unit/test_post_demo_closure.py` stays red until you do.
3. **The SNS alert subscription is unconfirmed.** The budget alarm and both health alarms
   publish to `toxic-mod-alerts`, which has no confirmed subscriber, so they notify nobody.
   A confirmation email was sent to `rock@rockcyber.com` on 2026-08-10; AWS drops unconfirmed
   email subscriptions after three days, which is why there was no subscriber to begin with.
   Re-send by re-applying Terraform.

## Rubric self-grade

`docs/rubric-conformance.md` grades the live system clause by clause, parsing the clauses out
of `docs/week9_FinalProject.md` so the matrix cannot drift from the assignment. Every clause
is PASS except rubric 1.3, which is **PARTIAL**: it asks for the best-performing model to be
promoted, and the promoted classical pipeline scores macro PR-AUC 0.6632 against the
DistilBERT challenger's 0.7268. The challenger is not promoted because its int8 export is
refused at 0.5728 against a 0.05 parity ceiling and float32 does not fit the instance budget.

## Operations

| Task | Command |
|---|---|
| Bring the stack up and gate on health | `make aws-up` |
| Check the three live endpoints | `make deploy-verify` |
| Offline submission check | `make submission-check` |
| Logged-out deliverable check | `pytest tests/integration/test_submission_logged_out.py` |
| Roll a new SHA | `gh workflow run deploy.yml --ref main` |
| Roll back to the previous SHA | `make rollback SHA=$(aws ssm get-parameter --name /toxic/deploy/previous-sha --query Parameter.Value --output text)` |
| Dump the database and stop everything | `make aws-down` |
| Close the public demo window | `make close-demo` |
| Debug without SSH | `docs/runbooks/no-ssh-debug.md` |

**The deploy workflow requires the repository variable `AWS_DEPLOY_ROLE_ARN`.** It was unset
until 2026-08-10, which is why every deploy run before then failed in 14 seconds at
`configure-aws-credentials` with "Could not load credentials from any providers". If deploys
start failing that way again, check `gh variable list` first.

---

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
| A. Author spec, bootstrap script, Terraform, workflows | Any machine, zero AWS calls | Committed code | **Complete** |
| B. Install tooling, enable Identity Center, `aws configure sso` | The machine that will operate the account | An SSO profile | **Complete on Mac and Jetson, 2026-07-30** |
| C. Run `bootstrap.sh`, then `terraform apply` | Same machine, or CI | Live AWS resources | **Complete.** 102 Terraform resources; last apply 2026-08-10 opened the demo window, 1 add 3 change 0 destroy |
| 0-5. Data firewall through deployment | Jetson, RunPod, CI | The graded system | **Complete.** See `docs/rubric-conformance.md` |

## What exists right now

| Artifact | Path | State |
|---|---|---|
| Live AWS account and stack | three EC2, one RDS, `us-west-2` | Running, internet-reachable |
| Terraform | `infra/terraform/` | Applied, no drift |
| Deploy workflow | `.github/workflows/deploy.yml` | Green; first successful run 2026-08-10 |
| Promoted model | W&B `toxic-clf` at `production` | Public, verified logged out |
| Submission manifest | `docs/submission-manifest.yml` | Four deliverables verified logged out |
| Rubric self-grade | `docs/rubric-conformance.md` | All PASS except 1.3 PARTIAL |
| Model card | `MODEL_CARD.md` | v1.1.0, digest of record |
| Security policy | `SECURITY.md` | Claim / status / evidence table |
| Rollback | `infra/ROLLBACK.md`, `infra/aws/rollback.sh` | Rehearsed against the live stack |
| No-SSH recovery | `docs/runbooks/no-ssh-debug.md` | Written 2026-08-10 |
| SBOM and AIBOM | `sbom.json`, `aibom.json` | Generated from the hashed lock, provably severable |

## Machine notes

Two Jetson gotchas, because they are the ones most likely to cost an hour on a fresh session:

- `/usr/local/bin` shadows `~/.local/bin` on this box. Check `which -a aws` and
  `which -a terraform` before believing a version string; the pinned pair is AWS CLI 2.36.3
  and Terraform 1.15.8.
- A plain `aws sso login` opens a PKCE flow against `127.0.0.1`, which cannot complete on a
  headless machine. Use `aws sso login --profile rc-mlops --use-device-code`.

The account-foundation reference — Identity Center setup, the two SCP traps that silently
deny the workload, and the root-user break-glass reasoning — is in the two sections above,
**Root user: break-glass, preserved** and **Why switching machines is safe**. Both still
apply; they describe work that is now done rather than work to do.

`docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` section 15 records every
AWS API claim, its source, and whether it was confirmed or refuted. Use it instead of
re-researching.
