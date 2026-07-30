# Final Project Design Spec: Toxic Comment Moderation MLOps System

- Version: 1.1
- Owner: Rock Lambros
- Date: 2026-07-01, amended 2026-07-30
- Course: COMP 4450 (MLOps), final project, 25 points, due 2026-08-18
- Status: approved for planning
- Target: standalone repository (not the COMP 4450 monorepo)

This is the cross-cutting design that the six implementation phases hang off. It is the handoff artifact for the standalone project repo. Read it before writing the implementation plan.

**v1.1 change summary (2026-07-30).** The runtime target moved from an AWS Academy lab account to a dedicated member account in the `rock@rockcyber.com` AWS Organization. Region changed from `us-west-1` to `us-west-2`. The repository becomes public, which makes `SECURITY.md` mandatory. Section 3.1 is new and carries the account, identity, and guardrail design. Sections 12, 13, 15, and 18 were revised. The full account design lives in `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`. Nothing about the model strategy, the leakage firewall, the output contract, or the database schema changed.

## 1. Goal and grading context

Build, deploy, and operate a production-grade, end-to-end MLOps system on AWS. Problem: Toxic Comment Moderation. Classify comments into six toxicity categories and expose a moderation endpoint with a human-review workflow. Dataset: Jigsaw Toxic Comment Classification Challenge (English).

The grade rewards the MLOps lifecycle, not model state-of-the-art. Six graded components, all deployed on AWS:

1. Experiment tracking and model registry (Weights & Biases).
2. FastAPI model backend serving a registered model version.
3. Persistent cloud database (AWS RDS Postgres) logging every prediction.
4. Frontend interface (Streamlit).
5. Model monitoring dashboard on a separate EC2, reading the database.
6. CI/CD pipeline (GitHub Actions) that blocks merges on failure.

## 2. Problem definition

Multi-label classification. Six independent labels: `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`. About 160k training comments. Severe class imbalance (`toxic` around 10 percent, `threat` under 0.3 percent).

Headline metrics: macro-F1 and per-label PR-AUC, reported with confidence intervals. Accuracy is banned as a headline metric because the imbalance makes it misleading (a model that predicts all-negative scores about 90 percent accuracy and catches nothing).

## 3. Architecture: build-time versus runtime

The completed application has zero RunPod dependence. RunPod is a build-time GPU tool only. Once models are trained and registered, the running system never calls RunPod again.

```
BUILD-TIME (offline, GPU)         RUNTIME (AWS account rockcyber-mlops-toxic, us-west-2)
RunPod ephemeral pods:            +---------------------------------------------------+
  train_classical + sweep         | VPC 10.42.0.0/16                                  |
  fine-tune DistilBERT            |  public subnet          |  private subnet         |
    export ONNX int8              |   EC2 #1 (API tier):    |   RDS Postgres          |
    register + digest             |     FastAPI /predict    |     (shared state)      |
  Weights & Biases registry       |     Streamlit user+rev  |                         |
      |                           |   EC2 #2 (background):  |   no internet path      |
      | deploy-time fetch ------> |     monitoring dash     |                         |
      |                           |     DistilBERT rescorer |                         |
      |                           +---------------------------------------------------+
GitHub Actions (arm64 runners):        ^                 ^
  build 4 images -> ECR ---------------+                 |
  deploy via SSM Run Command (no SSH) -------------------+
```

Weights & Biases is a deploy-time source, not a runtime dependency. At deploy, each EC2 pulls its pinned artifact by digest, verifies SHA-256, and bakes it into the image or a local volume. The steady-state request path touches only EC2 and RDS. This satisfies both the rubric ("load from the Model Registry") and the self-contained constraint.

### 3.1 AWS account, identity, and guardrails

Full design in `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`. The load-bearing decisions:

**Dedicated member account.** `rockcyber-mlops-toxic` is created by `organizations:CreateAccount` from the `rock@rockcyber.com` management account, inside a new `Sandbox` OU. Organizations creates `OrganizationAccountAccessRole` automatically, which is why no routine phase of this project ever handles a root credential. The existing RCAP workloads run in the organization **management** account, and SCPs never apply to a management account, so RCAP is structurally immune to these guardrails rather than merely excluded. Every org-level write is scoped to the new OU.

**Root stays as break-glass.** The account root user is hardened, not removed: MFA enrolled, no access keys, never used for routine work, strong password in a password manager, and a CloudTrail plus EventBridge alarm on any root activity. AWS Organizations centralized root access management is deliberately **not** enabled, because it has no OU-level scoping and would reach RCAP, and because `sts:AssumeRoot` covers only five task policies rather than substituting for root. Full reasoning in spec section 5.2.

**No static AWS credentials exist.** Humans authenticate through IAM Identity Center with short-lived SSO sessions. GitHub Actions authenticates through OIDC. EC2 authenticates through instance profiles. The SCP on the OU denies `iam:CreateUser` and `iam:CreateAccessKey`, which turns this from a convention into an enforced control.

**The SCP is also the cost guardrail.** It denies every region except `us-west-2` and `us-east-1`, denies GPU and metal instance families, restricts `ec2:RunInstances` to a Graviton size allowlist through the resource-level `ec2:InstanceType` key, denies Aurora cluster creation, forces RDS to be private and Secrets-Manager-managed, and blocks tampering with CloudTrail and GuardDuty. A budget alert fires at 50, 80, and 100 percent of $100 per month. There is no automated stop action, by owner decision, so the SCP carries that load for compute. It cannot carry it for RDS instance class, because `rds:DatabaseClass` is not supported on `rds:CreateDBInstance`. Every condition key was verified against the AWS service reference.

**Console work is four one-time operations.** Enable IAM Identity Center, create the directory user, create an `AdministratorAccess` permission set, assign it to the management account. There is no API to create an Identity Center organization instance, which makes this the irreducible manual surface. Everything after it is an API call.

**No SSH and no NAT gateway.** Deployment runs over SSM Run Command, so port 22 never opens and there is no key to manage. EC2 sits in public subnets behind an ingress allowlist with IMDSv2 required, and RDS sits private with no internet path. A NAT gateway would consume roughly a third of the monthly budget and buy nothing here.

## 4. Model strategy (evidence-grounded)

Two registered models.

**Classical (Production, online).** TF-IDF over word 1-2 grams plus char 3-5 grams, feeding a One-vs-Rest LogisticRegression with `class_weight='balanced'`. Compared against LinearSVC and tuned variants as separate tracked runs; the best is promoted to Production. Served on CPU on EC2 #1. This is the online `/predict` model.

**DistilBERT (challenger, async).** Fine-tuned on RunPod GPU, exported to ONNX with int8 dynamic quantization, registered to Weights & Biases. Runs as a background worker on EC2 #2 that re-scores the human-review queue. Not in the online request path.

Rationale, grounded in the reference library:

- Baseline-first is doctrine. Domingos 2012: try the simplest learners first. Guyon and Elisseeff 2003 recommend a linear predictor as the text baseline.
- With about 160k labels, data beats cleverness (Domingos). Devlin et al. 2019 show BERT's largest absolute gains on the smallest datasets; the margin over a strong linear baseline shrinks at this data scale.
- The wins are in features (Domingos). Char n-grams catch obfuscation (`f*ck`, `sh!t`, `n1gger`) that word features miss. Guyon: features useless alone can be useful together.
- Stratified k-fold for model selection (Kohavi 1995), extended to iterative multi-label stratification here.
- The infrastructure is the deliverable (Domingos: the bottleneck is human cycles and experimentation infrastructure). That is the whole point of this course. Spend effort on the pipeline.
- The transformer earns its place as a second registered model: a real baseline-versus-transformer comparison and a genuine promote decision, at no online-latency cost because it runs async.

## 5. Model output contract (stable interface)

Both models emit the same six-label vector, so the database and UI never change when the model swaps.

```json
POST /predict {"text": "..."} ->
{
  "request_id": "uuid",
  "model_version": "toxic-clf:v3@<wandb-digest>",
  "labels": {
    "toxic":        {"prob": 0.87, "flag": true},
    "severe_toxic": {"prob": 0.12, "flag": false},
    "obscene":      {"prob": 0.44, "flag": false},
    "threat":       {"prob": 0.03, "flag": false},
    "insult":       {"prob": 0.71, "flag": true},
    "identity_hate":{"prob": 0.05, "flag": false}
  },
  "decision": "allow|review|block",
  "max_prob": 0.87,
  "latency_ms": 42
}
```

Per-label calibrated probability plus a per-label tuned threshold produces `flag`. `decision` comes from a moderation policy (this is the moderation endpoint). `request_id` threads prediction to review-queue to re-score to feedback.

`/health` returns the loaded model version and a readiness check on the DB connection.

## 6. Data pipeline and leakage/overfitting firewall

Deterministic and versioned. The firewall is a hard requirement.

- Near-duplicate dedup before any split. Jigsaw contains near-duplicate comments; a duplicate across train and test inflates the test score.
- Lock a 15 percent held-out test set at the start with a fixed seed and iterative multi-label stratification. Touch it exactly once, at the very end (Domingos' rule).
- Remaining data into stratified CV folds. All six labels, including `threat` under 0.3 percent, appear in every fold.
- Fit the TF-IDF vocabulary and IDF inside an sklearn Pipeline inside each CV fold. Fitting the vectorizer on the full corpus is the classic silent leak.
- Threshold tuning happens on validation only, never on the held-out test set.
- DistilBERT: early stopping on validation, weight decay, dropout, 2-3 epochs (Devlin's recipe), and log the train/val loss gap every epoch to catch overfit as it happens.
- Seed hygiene and git SHA logged to Weights & Biases so every number is reproducible.
- Model-card caveat: DistilBERT's pretraining corpus may already contain some of these public comments. That contamination is not gradeable or fixable, but naming it is the mark of rigor.

Enforced by these Superpowers skills during implementation: `enforcing-leakage-firewall`, `auditing-train-test-split`, `auditing-deep-learning-overfit`, `enforcing-seed-hygiene`, `comparing-models-fairly`, `building-deterministic-data-pipelines`.

## 7. Database schema (RDS Postgres)

SQL over DynamoDB because the monitoring dashboard does time-bucketed aggregations and a predictions-to-feedback join, which is SQL's home turf.

- `predictions`: `request_id` (PK), `ts`, `input_text`, `model_version`, `prob_toxic`, `prob_severe_toxic`, `prob_obscene`, `prob_threat`, `prob_insult`, `prob_identity_hate`, `decision`, `max_prob`, `latency_ms`.
- `review_queue`: `request_id` (FK), `enqueued_ts`, `status` (pending/rescored/reviewed), `distilbert_probs` (jsonb), `reviewer_labels` (jsonb, the six human labels), `reviewer_id`, `reviewed_ts`.
- `feedback`: derived from reviewer truth versus prediction, used to compute live per-label precision, recall, and accuracy.

Monitoring reads: prediction latency over time (`predictions.latency_ms`, `ts`); predicted class distribution over time, which is target drift (flag counts per label); live accuracy (`feedback` joined to `predictions`).

## 8. Moderation and human-review workflow with feedback loop

One design serves both the problem's signature ("human-review workflow") and the rubric's "feedback to calculate live accuracy."

1. Policy: per-label calibrated probability plus tuned threshold produces a `decision` of allow, review, or block. Toxicity is asymmetric-cost: a missed `threat` is worse than a false flag on `toxic`, so thresholds are tuned per label.
2. The review bucket (near-threshold or any toxic flag) enqueues to `review_queue`.
3. The DistilBERT re-scorer worker on EC2 #2 drains the queue and writes `distilbert_probs`, giving the reviewer a higher-accuracy second opinion and a prioritization signal.
4. The human reviewer confirms or overrides labels in the reviewer UI, writing `reviewer_labels`.
5. `feedback` derives from reviewer labels versus the model prediction, and the monitoring dashboard shows live accuracy.

## 9. Safe model loading (trust boundary)

The registry hands an artifact over the network into a process that holds the EC2 instance's IAM role, so a poisoned artifact is remote code execution.

- Classical model serialized with skops (`skops.io.dump` / `skops.io.load` with a trusted allowlist), never pickle or joblib.
- DistilBERT via safetensors and ONNX, never pickle.
- Pin the exact Weights & Biases artifact digest, verify SHA-256 against the model card before loading, and bake the artifact into the image at deploy.
- Enforced at the `backend/model_loader.py` boundary.

## 10. RunPod cost governance (build-time only)

- Ephemeral pods only. No persistent GPU pods.
- Teardown in a `finally` or `trap EXIT`, so the pod dies on success, failure, and interrupt.
- A reaper (scheduled GitHub Action) lists pods and kills any past a hard TTL or idle threshold.
- Spending cap and alarm. Prefer interruptible (community/spot) pods for the sweep because sweep runs are restartable.
- Right-size the GPU: a 66M-param DistilBERT on 160k short comments fine-tunes in minutes on a mid-tier GPU (L4 / 4090 / A40). Fan out several mid-tier pods for the hyperparameter sweep rather than renting one large card. The large card is wasted spend for this workload.
- RunPod Serverless for any on-demand batch training job, since it scales to zero.

## 11. Experiment tracking and registry (Weights & Biases)

Per run, log: git SHA, hyperparameters, data version and dedup/split seed, and metrics (macro-F1, per-label PR-AUC, per-label F1, with confidence intervals). Save models as artifacts. Use the Model Registry to version them and promote the best to a Production stage. The classical winner is the online Production model; DistilBERT is registered as the challenger.

## 12. CI/CD

`.github/workflows/ci.yml` triggers on pull requests to `main`. It runs ruff (lint), the full pytest suite (unit plus integration), gitleaks, semgrep, and `terraform fmt`, `validate`, and `plan`. A pull request cannot merge if any of them fails.

`.github/workflows/deploy.yml` triggers on push to `main`. It builds the four component images natively on `ubuntu-24.04-arm` runners (free and unlimited on a public repository), tags them by git SHA, pushes to ECR, runs `terraform apply`, and rolls the containers through SSM Run Command. The `production` GitHub environment gates it with required review.

Two OIDC roles, not one. `gha-ci` is trusted on any ref and holds read plus `terraform plan`. `gha-deploy` is trusted only on `refs/heads/main` and `environment:production` and holds apply, ECR push, and SSM `SendCommand`. That split is what stops a pull request from a fork reaching production credentials. No AWS access key is stored in GitHub.

A separate scheduled workflow runs the RunPod reaper.

## 13. Containerization and deployment

One Docker image per component, built for arm64 and stored in ECR with immutable tags and scan on push. EC2 #1 runs the FastAPI backend and the Streamlit UI. EC2 #2 runs the monitoring dashboard and the DistilBERT re-scorer worker. A `docker-compose.yml` brings the stack up locally.

Terraform stands up the VPC, both EC2 instances, RDS, ECR, IAM, and the observability and budget resources. Instance user data installs Docker and the compose plugin. Deployment pulls the pinned image digest and the pinned model artifact, verifies SHA-256, and restarts the stack.

Deployment reaches the instances through SSM Run Command. No SSH, no key material, no bastion, no open port 22. The deploy job selects instances by tag rather than by address, so a replaced instance needs no pipeline change.

## 14. Testing

- Unit: preprocessing and dedup, thresholding, the moderation policy, DB row mapping.
- Integration: FastAPI `/predict` and `/health` against a test DB, a full prediction-to-DB round trip, and the re-scorer worker draining a seeded queue.

## 15. Repository layout

```
mlops-toxic-moderation/
  model/
    data/                 # deterministic prep, dedup, split (leakage firewall)
    train_classical.py
    train_distilbert.py   # RunPod GPU
    sweep.py              # W&B sweep config, parallel fan-out
    evaluate.py           # stratified CV, thresholds, CIs
    export_onnx.py        # DistilBERT -> ONNX int8
  backend/
    app.py                # FastAPI /predict /health
    model_loader.py       # safe load from registry (skops)
    policy.py             # thresholds -> decision
    db.py
    Dockerfile
  frontend/
    ui.py                 # Streamlit user + reviewer views
    Dockerfile
  monitoring/
    dashboard.py          # Streamlit, reads RDS
    Dockerfile
  rescorer/
    worker.py             # EC2 #2 background worker, ONNX DistilBERT
    Dockerfile
  infra/
    docker-compose.yml
    aws/
      bootstrap.sh                  # one-time org + account + state bucket
      scp-sandbox-guardrails.json   # SCP attached to the Sandbox OU
    terraform/                      # everything inside the account
      network.tf compute.tf data.tf
      iam.tf oidc.tf ecr.tf
      observability.tf budget.tf
      backend.tf variables.tf outputs.tf
    runpod/               # pod lifecycle, reaper
  .github/workflows/
    ci.yml                # lint, tests, scans, terraform plan
    deploy.yml            # arm64 build, ECR push, apply, SSM roll
    runpod-reaper.yml
  tests/
    unit/
    integration/
  README.md
  SECURITY.md             # VDP, mandatory now that the repo is public
  MODEL_CARD.md           # plus CycloneDX AIBOM companion
```

## 16. Reuse map (from the COMP 4450 monorepo)

Pull working code forward rather than starting from scratch. Monorepo root: `/Users/klambros/github_projects/MLOPS-Comp-4450-1`.

| Component | Seed | Path |
|---|---|---|
| Model + Streamlit pattern | hw1 | `assignments/hw1` (retrain on Jigsaw, multi-label) |
| Docker packaging | hw2 | `assignments/hw2` |
| FastAPI backend | hw3 | `assignments/hw3` |
| AWS EC2 deploy | hw5 | `assignments/hw5` |
| Monitoring dashboard | hw7 | `assignments/hw7` (move file-based to DB-backed) |
| CI/CD | hw8 | `assignments/hw8` |

## 17. Phase decomposition (implementation-plan seed)

- Phase 0: repo scaffold, deterministic data pipeline, leakage firewall, seed hygiene.
- Phase 1: train classical plus sweep and fine-tune DistilBERT on RunPod; W&B tracking and registry; promote best; export ONNX.
- Phase 2: FastAPI backend, safe model loading, RDS Postgres, prediction logging.
- Phase 3: Streamlit user and reviewer UI, monitoring dashboard, DistilBERT re-scorer worker.
- Phase 4: unit and integration tests, CI/CD gate.
- Phase 5: Docker, two-EC2 deploy, README, model card, AIBOM.

## 18. Risks and open questions

Open:

- EC2 sizing for the re-scorer. Confirm the instance class after measuring ONNX int8 throughput on a representative queue.
- Ingress exposure. The security group allowlist defaults to the operator address. Opening it for a public demo window is a variable toggle, and it must be closed again afterward.
- RDS cost guardrail. No SCP-enforceable instance-class cap exists for standalone RDS instances, verified against the AWS service reference. The budget alarm and the Terraform-pinned class are the only controls.

Closed since v1.0:

- Reviewer identity and access. Single reviewer role behind a shared secret. Named in the model card as not being a real authentication system.
- Input text retention. Retain `predictions.input_text` for 30 days, then a scheduled purge nulls the text and keeps the rest of the row. Configurable via `INPUT_TEXT_RETENTION_DAYS`.
- Pairs. Solo project, no partner attribution.
- Repository visibility. Public, which makes the `SECURITY.md` VDP mandatory under QC.1.
- AWS account model. Dedicated Organizations member account, section 3.1.

## 19. References

- Domingos 2012, A Few Useful Things to Know about Machine Learning.
- Devlin, Chang, Lee, Toutanova 2019, BERT.
- Guyon and Elisseeff 2003, An Introduction to Variable and Feature Selection.
- Kohavi 1995, A Study of Cross-Validation and Bootstrap for Accuracy Estimation and Model Selection.
- Reference library: `/Users/klambros/Library/CloudStorage/OneDrive-RockCyber/Document_Library/_source/REFERENCE/Research/Data Science and ML Papers`.
