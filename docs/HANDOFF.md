# Handoff

Current stage, what exists, and how to resume. Update this file whenever the stage changes or a machine changes.

- Last updated: 2026-07-30
- Owner: Rock Lambros
- Primary execution machine: Jetson (aarch64 build box)

## Current stage

**Stage A complete. Nothing has been executed against AWS.**

The AWS account foundation is designed and committed. No AWS API call has been made, no account exists, no resource has been created, and no credential has been issued. The repository is the only artifact.

## Why switching machines is safe

Three properties make this project machine-portable:

1. **No static credentials exist.** Every AWS credential is a short-lived IAM Identity Center session or an OIDC web identity. There is nothing to copy between machines and nothing to leak.
2. **Terraform state lives in S3**, not on local disk. Any machine with a valid session sees the same state.
3. **Every artifact lives in the repository.** No local scratch state is load-bearing.

Moving machines costs one `aws configure sso` run.

## Stages

| Stage | Where | Produces | Status |
|---|---|---|---|
| A. Author spec, bootstrap script, Terraform, workflows | Any machine, zero AWS calls | Committed code | **Done for the spec. Code not yet written** |
| B. Install tooling, enable Identity Center, `aws configure sso` | The machine that will operate the account | An SSO profile | Not started |
| C. Run `bootstrap.sh`, then `terraform apply` | Same machine, or CI | Live AWS resources | Not started |

## What exists right now

| Artifact | Path | State |
|---|---|---|
| AWS foundation design spec | `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` | Written |
| Application design spec v1.1 | `docs/2026-07-01-toxic-moderation-mlops-design.md` | Amended |
| Master plan with Phase A | `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` | Amended |
| Phase 0 detailed plan | `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md` | Written, not executed |
| Phase A detailed plan | not yet written | **Next artifact to produce** |
| `infra/aws/`, `infra/terraform/` | not yet written | Phase A task 2 onward |
| AWS account | does not exist | Phase A task 3 |

## Blocking prerequisites

Verify all four before running anything in Phase A.

| Prerequisite | State as of 2026-07-30 | Fix |
|---|---|---|
| AWS CLI v2 | **Missing.** v1.35.0 installed via pip | Install v2. v1 lacks the SSO and root-credential operations this project needs |
| Terraform 1.10+ | **Missing.** 1.5.7 installed | Upgrade. Required for S3 native state locking |
| `gh` authenticated | Present, v2.92.0 | none |
| Mail to `rock+aws-mlops-toxic@rockcyber.com` delivers | **Unverified** | Send a test message. `rockcyber.com` routes through Mimecast, whose recipient validation is the common cause of plus-addressed mail bouncing. Changing a root email after root credentials are deleted is painful, so confirm first |

## Resume

Read `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` first, then the Phase A section of the master plan. Both are the source of truth. This file only tracks position.

The next artifact to produce is the Phase A detailed plan file at `docs/superpowers/plans/2026-07-30-phase-a-aws-foundation.md`, followed by execution on branch `feat/phase-a-aws-foundation`.

Phase A and Phase 0 are independent. Phase 0 needs no cloud access at all and runs entirely offline against a synthetic fixture, so it is the safe thing to work on while the AWS prerequisites are being sorted out.
