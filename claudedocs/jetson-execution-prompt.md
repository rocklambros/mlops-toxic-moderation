# Jetson Execution Prompt: build the toxic-moderation MLOps system

How to use: on the Jetson, clone or pull this repo, open Claude Code at the repo
root, and paste the prompt block below as your first message. It orients a fresh
session and starts phased execution. Everything it references is committed here.

---

## PROMPT (paste into Claude Code on the Jetson)

You are Claude Code on Rock's Jetson (aarch64 / arm64). Your job is to build the
COMP 4450 final project in this repository, the production-grade Toxic Comment
Moderation MLOps system, by executing the committed implementation plan phase by
phase. The design and the plan are already written and approved. Do not
re-litigate the locked decisions.

### Read first (in this order)
1. `docs/2026-07-01-toxic-moderation-mlops-design.md` — the approved design spec.
2. `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` — the phased
   roadmap: global constraints, interface contracts, the six phases, the spec
   coverage matrix, the Resolved Decisions section (accounts, data, instance
   sizing), and the reuse map.
3. `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md` — the fully
   detailed, bite-sized, ready-to-run plan for Phase 0.

### This machine and account
- The Jetson is the operator and build box. It holds the W&B credentials, the
  RunPod API key, and the AWS credentials. The runtime target is AWS EC2 + RDS in
  region **us-west-1**, per the locked architecture. The Jetson runs training and
  drives deploy; it is not the runtime host.
- Confirm the credentials are present before the phases that need them:
  `wandb login` state, `RUNPOD_API_KEY` in the environment, and `aws sts
  get-caller-identity` returning an identity in the right account. Phase 0 needs
  none of these; it runs fully offline.
- Jetson is arm64. Build arm64 Docker images here; they run directly on Graviton
  (`t4g` / `c7g`) EC2. Recommended classes are in the roadmap's Resolved
  Decisions: EC2 #1 `t4g.medium`, EC2 #2 `t4g.large` (confirm after measuring ONNX
  int8 throughput), RDS `db.t4g.micro`, all Single-AZ, us-west-1.

### Locked constraints (do not change, do not re-litigate)
- Runtime is 100% AWS, zero RunPod. RunPod is build-time GPU only: ephemeral
  spot pods, `trap EXIT` teardown, the scheduled reaper GitHub Action, a spending
  cap, mid-tier GPU fan-out for the sweep. No persistent pods.
- Two registered models: classical TF-IDF (word 1-2 + char 3-5 grams) One-vs-Rest
  LogisticRegression (`class_weight='balanced'`) as the online Production model on
  CPU; DistilBERT exported to ONNX int8 as the async challenger re-scoring the
  review queue on EC2 #2 CPU. Both emit the same six-label vector.
- W&B is registry plus tracking, fetched at deploy time only: verify SHA-256, bake
  the artifact into the image. Never fetch W&B in the request path.
- Leakage / overfitting firewall is a hard requirement: near-dup dedup before any
  split, a locked 15% held-out test touched exactly once, iterative multi-label
  stratification, TF-IDF fit inside the CV Pipeline inside each fold, DistilBERT
  early stopping + weight decay + per-epoch train/val gap logging, seed hygiene
  and git SHA on every run.
- Safe model loading only: skops for the classical model, safetensors and ONNX for
  DistilBERT. Never pickle or joblib. Verify the pinned artifact digest and fail
  closed on mismatch.
- Headline metrics: macro-F1 and per-label PR-AUC with confidence intervals.
  Accuracy is banned as a headline metric.
- Git: a feature branch per phase (`feat/phase-N-*`), never commit to main
  directly, human author (`rocklambros <rock@rockcyber.com>`), no AI attribution
  anywhere. Solo project (no partner). Repo is private.

### Data gotcha (Phase 1)
The Kaggle archive
`julian3833/jigsaw-multilingual-toxic-comment-classification` bundles several
competitions. Train only on `jigsaw-toxic-comment-train.csv` inside it (the
English six-label set matching the loader's `REQUIRED_COLUMNS`). Do not use
`validation.csv` / `test.csv` (multilingual, single label) or
`jigsaw-unintended-bias-train.csv` (different schema).

### How to execute
- Use the `superpowers:executing-plans` skill (batch with checkpoints) or
  `superpowers:subagent-driven-development` (a fresh subagent per task with review
  between tasks). Prefer subagent-driven for the code-heavy phases.
- Go phase by phase. Do not start a phase until the prior phase's exit criteria in
  the roadmap are met and merged.
- Within a phase, follow the detailed plan's TDD rhythm: write the failing test,
  run it to see it fail, write the minimal code, run it green, commit. One small
  commit per task.
- Phases 1-5 do not have detailed plan files yet. At the start of each, invoke
  `superpowers:writing-plans` to expand that phase's roadmap tasks into a
  bite-sized plan file at `docs/superpowers/plans/2026-07-01-phase-N-*.md`, using
  the interface contracts in the roadmap as the type seams, then execute it.
- Pull code forward from the monorepo where the roadmap's reuse map points
  (`/Users/klambros/github_projects/MLOPS-Comp-4450-1`, assignments hw1/2/3/5/7/8).
  Adapt it, do not rebuild from scratch. If the monorepo is not on this machine,
  say so and proceed from the spec.

### Cost governance (every phase that spends)
- RunPod: confirm the spending cap and the reaper before launching pods. Every pod
  launch script tears down in `trap EXIT` / `finally`. Prefer interruptible spot
  for the sweep. Never leave a pod running.
- AWS: stop both EC2 instances and the RDS instance between work sessions. Set a
  small AWS Budget alarm. The instance classes above are the low-spend picks.

### First actions
1. Confirm a clean baseline: create branch `feat/phase-0-data-firewall`, then run
   through the Phase 0 plan. Its exit gate is `make test && make lint` green plus a
   reproducible `data_version` from `make data` run twice.
2. Open a PR to main for Phase 0, confirm it is green, merge, delete the branch.
3. Move to Phase 1: verify W&B / RunPod / AWS credentials, download the Kaggle
   training file, then expand and execute the Phase 1 plan.

Report progress at each phase boundary with the exit criteria met and the evidence
(test output, W&B run link, registered artifact digest). Do not claim a phase done
without running its checks.
