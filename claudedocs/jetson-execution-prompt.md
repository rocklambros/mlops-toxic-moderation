# Jetson Execution Prompt: build the toxic-moderation MLOps system

How to use: on the Jetson, clone or pull this repo, check out the branch named in
`docs/HANDOFF.md`, open Claude Code at the repo root, and paste the prompt block
below as your first message. It orients a fresh session and starts phased
execution. Everything it references is committed here.

Last revised 2026-07-30 for the AWS account foundation (Phase A).

---

## PROMPT (paste into Claude Code on the Jetson)

You are Claude Code on Rock's Jetson (aarch64 / arm64). Your job is to build the
COMP 4450 final project in this repository, the production-grade Toxic Comment
Moderation MLOps system, by executing the committed implementation plan phase by
phase. The design and the plan are already written and approved. Do not
re-litigate the locked decisions.

### Read first (in this order)
1. `docs/HANDOFF.md`. Current stage, what exists, blocking prerequisites. Read
   this before anything that touches AWS.
2. `docs/2026-07-01-toxic-moderation-mlops-design.md`. The approved design spec,
   v1.1. Section 3.1 covers the AWS account, identity, and guardrails.
3. `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`. The full
   AWS foundation design that Phase A implements.
4. `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`. The phased
   roadmap: global constraints, interface contracts, Phase A plus Phases 0 through
   5, the spec coverage matrix, the Resolved Decisions section, and the reuse map.
5. `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md`. The fully
   detailed, bite-sized, ready-to-run plan for Phase 0.

### This machine and the AWS account
- The Jetson is the operator and build box, not the runtime host. It holds the W&B
  credentials, the RunPod API key, and the Kaggle credentials.
- **The Jetson holds no static AWS credentials, because none exist in this
  project.** AWS access comes from an IAM Identity Center SSO profile issuing
  short-lived sessions. If any task appears to need an AWS access key, the design
  is wrong. Stop and say so.
- Runtime target is a dedicated AWS Organizations member account
  `rockcyber-mlops-toxic` in region **us-west-2**. It is not the AWS Academy lab
  and not the RCAP account `<MGMT_ACCOUNT_ID>`.
- Confirm credentials before the phases that need them: `wandb login` state,
  `RUNPOD_API_KEY` in the environment, and `aws sts get-caller-identity` returning
  an identity in the mlops account. Phase 0 needs none of these and runs fully
  offline.
- Jetson is arm64, so local Docker builds run natively for Graviton. CI also builds
  arm64 natively on free `ubuntu-24.04-arm` runners, and CI is the primary deploy
  path. The Jetson is the manual fallback. Recommended classes: EC2 #1
  `t4g.medium`, EC2 #2 `t4g.large` (confirm after measuring ONNX int8 throughput),
  RDS `db.t4g.micro`, all Single-AZ, us-west-2.

### Locked constraints (do not change, do not re-litigate)
- Runtime is 100% AWS, zero RunPod. RunPod is build-time GPU only: ephemeral spot
  pods, `trap EXIT` teardown, the scheduled reaper GitHub Action, a spending cap,
  mid-tier GPU fan-out for the sweep. No persistent pods.
- Two registered models: classical TF-IDF (word 1-2 + char 3-5 grams) One-vs-Rest
  LogisticRegression (`class_weight='balanced'`) as the online Production model on
  CPU, and DistilBERT exported to ONNX int8 as the async challenger re-scoring the
  review queue on EC2 #2 CPU. Both emit the same six-label vector.
- W&B is registry plus tracking, fetched at deploy time only: verify SHA-256, bake
  the artifact into the image. Never fetch W&B in the request path.
- Leakage and overfitting firewall is a hard requirement: near-dup dedup before any
  split, a locked 15% held-out test touched exactly once, iterative multi-label
  stratification, TF-IDF fit inside the CV Pipeline inside each fold, DistilBERT
  early stopping plus weight decay plus per-epoch train/val gap logging, seed
  hygiene and git SHA on every run.
- Safe model loading only: skops for the classical model, safetensors and ONNX for
  DistilBERT. Never pickle or joblib. Verify the pinned artifact digest and fail
  closed on mismatch.
- Headline metrics: macro-F1 and per-label PR-AUC with confidence intervals.
  Accuracy is banned as a headline metric.
- **No static AWS credentials.** Humans use Identity Center, CI uses GitHub OIDC,
  EC2 uses instance profiles. Enforced by an SCP that denies `iam:CreateUser` and
  `iam:CreateAccessKey`.
- **No SSH.** Deployment runs over SSM Run Command. No security group opens port 22.
- **The RCAP account `<MGMT_ACCOUNT_ID>` is read-only in scope.** Audit it, never modify
  it.
- Git: a feature branch per phase (`feat/phase-N-*`), never commit to main
  directly, human author (`rocklambros <rock@rockcyber.com>`), no AI attribution
  anywhere. Solo project, no partner. **Repo is public**, so `SECURITY.md` is
  mandatory and nothing sensitive may ever enter a commit.

### Data gotcha (Phase 1)
The Kaggle archive
`julian3833/jigsaw-multilingual-toxic-comment-classification` bundles several
competitions. Train only on `jigsaw-toxic-comment-train.csv` inside it (the
English six-label set matching the loader's `REQUIRED_COLUMNS`). Do not use
`validation.csv` or `test.csv` (multilingual, single label) or
`jigsaw-unintended-bias-train.csv` (different schema).

### How to execute
- Use the `superpowers:executing-plans` skill (batch with checkpoints) or
  `superpowers:subagent-driven-development` (a fresh subagent per task with review
  between tasks). Prefer subagent-driven for the code-heavy phases.
- **Phase A and Phase 0 are independent.** Phase A builds the AWS account. Phase 0
  needs no cloud access and runs entirely offline against a synthetic fixture, so
  it is the safe thing to work on while AWS prerequisites are sorted out. Phase A
  must finish before Phase 2 needs a real RDS instance.
- Phases 1 through 5 run in order. Do not start a phase until the prior phase's
  exit criteria in the roadmap are met and merged.
- Within a phase, follow the detailed plan's TDD rhythm: write the failing test,
  run it to see it fail, write the minimal code, run it green, commit. One small
  commit per task.
- Phase A and Phases 1 through 5 do not have detailed plan files yet. At the start
  of each, invoke `superpowers:writing-plans` to expand that phase's roadmap tasks
  into a bite-sized plan file under `docs/superpowers/plans/`, using the interface
  contracts in the roadmap as the type seams, then execute it.
- Pull code forward from the monorepo where the roadmap's reuse map points
  (`/Users/klambros/github_projects/MLOPS-Comp-4450-1`, assignments hw1/2/3/5/7/8).
  Adapt it, do not rebuild from scratch. If the monorepo is not on this machine,
  say so and proceed from the spec.

### Cost governance (every phase that spends)
- RunPod: confirm the spending cap and the reaper before launching pods. Every pod
  launch script tears down in `trap EXIT` or `finally`. Prefer interruptible spot
  for the sweep. Never leave a pod running.
- AWS: the SCP instance-type allowlist is the hard stop. `terraform destroy` is the
  full teardown. `make aws-down` and `make aws-up` stop and start EC2 and RDS
  between sessions. Budget alerts fire at 50, 80, and 100 percent of $100 per
  month, with no automated stop action by owner decision.
- A stopped RDS instance restarts automatically after seven days. Stopped is not
  off. For gaps longer than a week, destroy rather than stop.

### First actions
1. Read `docs/HANDOFF.md` and report the current stage back before doing anything.
2. Check the four blocking prerequisites it lists. AWS CLI v2 and Terraform 1.10 or
   newer were both missing as of 2026-07-30, and mail delivery to the new account's
   root address was unverified. Report which are still unmet. Do not work around a
   missing prerequisite.
3. If the prerequisites are met, write the Phase A detailed plan with
   `superpowers:writing-plans`, then execute it on branch
   `feat/phase-a-aws-foundation`. **Verify the centralized root access management
   API surface against current AWS documentation before implementing the root
   credential removal step.** It was not confirmed during design.
4. If the prerequisites are not met, start Phase 0 instead on branch
   `feat/phase-0-data-firewall`. Its exit gate is `make test && make lint` green
   plus a reproducible `data_version` from `make data` run twice.
5. Open a PR to main per phase, confirm it is green, merge, delete the branch.

Report progress at each phase boundary with the exit criteria met and the evidence
(test output, W&B run link, registered artifact digest, `terraform plan` output).
Do not claim a phase done without running its checks.
