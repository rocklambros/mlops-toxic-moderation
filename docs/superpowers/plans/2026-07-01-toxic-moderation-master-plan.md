# Toxic Comment Moderation MLOps Implementation Plan (Master Roadmap)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement each phase task-by-task. This master roadmap is the index. Each phase has its own detailed bite-sized plan file (`docs/superpowers/plans/2026-07-01-phase-N-*.md`), authored just before that phase runs. Steps in the phase files use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, deploy, and operate a production-grade multi-label toxic comment moderation system on AWS, with experiment tracking, a model registry, a served API, a human-review feedback loop, monitoring, and a CI/CD gate.

**Architecture:** Build-time and runtime are separate. RunPod trains the classical model and fine-tunes DistilBERT on ephemeral GPU pods, then registers both to Weights & Biases. The running system lives entirely in one AWS account: EC2 #1 serves FastAPI `/predict` `/health` (classical model, CPU) plus the Streamlit user and reviewer UI; EC2 #2 runs the monitoring dashboard and the DistilBERT async re-scorer worker; RDS Postgres holds shared state. W&B is a deploy-time source only, never in the request path.

**Tech Stack:** Python 3.11, scikit-learn + skops, iterative-stratification, pandas/numpy/scipy, transformers + torch (build-time GPU only), onnxruntime + optimum (ONNX int8), FastAPI + uvicorn + pydantic, Streamlit, SQLAlchemy + Postgres, Weights & Biases, pytest + ruff + gitleaks + semgrep, Docker + docker-compose, GitHub Actions, RunPod (build-time), CycloneDX (AIBOM/SBOM).

## Global Constraints

Every task inherits these. Values are copied verbatim from the design spec (`docs/2026-07-01-toxic-moderation-mlops-design.md`) and the project constraints.

- **Labels (ordered, single source of truth):** `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`.
- **Runtime is 100% AWS, zero RunPod dependence.** The running system never calls RunPod. RunPod is build-time GPU only.
- **Two registered models.** Classical TF-IDF (word 1-2 grams plus char 3-5 grams) One-vs-Rest LogisticRegression with `class_weight='balanced'` is the online Production model on CPU. DistilBERT, exported to ONNX with int8 dynamic quantization, is the async challenger that re-scores the human-review queue on EC2 #2 CPU. Both emit the same six-label vector.
- **W&B is registry plus tracking, fetched at deploy time only.** Verify SHA-256 against the model card, bake the artifact into the image or a local volume. Never fetch W&B in the runtime request path.
- **Leakage / overfitting firewall (hard requirement).** Near-duplicate dedup before any split. Lock a 15% held-out test set at the start with a fixed seed and iterative multi-label stratification; touch it exactly once, at the very end. Remaining data into stratified CV folds where all six labels (including `threat` under 0.3%) appear in every fold. Fit TF-IDF vocabulary and IDF inside the sklearn Pipeline inside each CV fold. Tune thresholds on validation only, never on the held-out test. DistilBERT uses early stopping on validation, weight decay, dropout, 2-3 epochs, and logs the train/val loss gap every epoch. Seed hygiene and git SHA logged to W&B for every run.
- **Safe model loading only.** Classical via skops (`skops.io.dump` / `skops.io.load` with a trusted allowlist). DistilBERT via safetensors and ONNX. Never pickle or joblib. Pin the exact W&B artifact digest and verify SHA-256 before loading. Fail closed on mismatch.
- **Headline metrics: macro-F1 and per-label PR-AUC with confidence intervals.** Accuracy is banned as a headline metric because the class imbalance makes it misleading. It may be logged, never promoted on.
- **RunPod cost governance.** Ephemeral pods only, no persistent GPU pods. Teardown in a `trap EXIT` / `finally` so the pod dies on success, failure, and interrupt. A scheduled GitHub Action reaper kills pods past a hard TTL or idle threshold. Spending cap plus alarm. Prefer interruptible spot pods for the sweep. Right-size to a mid-tier GPU (L4 / 4090 / A40) and fan out several pods for the sweep rather than renting one large card.
- **Model output contract (stable interface).** See the interface contracts section. The database and UI never change when the model swaps.
- **Supply chain (QC.1 / NIST SP 800-218).** Pinned dependencies with hashes where practical, gitleaks secret scanning, semgrep SAST gate on executable additions, an SBOM. The repo is public as of 2026-07-30, so the VDP `SECURITY.md` is mandatory.
- **AWS foundation (added 2026-07-30).** Runtime lives in a dedicated AWS Organizations member account `rockcyber-mlops-toxic` in `us-west-2`, inside a `Sandbox` OU carrying a service control policy. No static AWS credentials exist anywhere: humans use IAM Identity Center, CI uses GitHub OIDC, EC2 uses instance profiles. Deployment runs over SSM Run Command with no SSH and no open port 22. See `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`.
- **Git workflow.** Feature-branch and PR flow. Never commit to main directly (the genesis commit that established main is the sole exception). Human author (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or docs. If a partner joins, record them and keep both partners' work attributable through branch-and-PR.

## Repository Structure

```
mlops-toxic-moderation/
  model/
    __init__.py           # empty, no heavy imports at package load
    labels.py             # LABELS tuple, single source of truth (Phase 0)
    contract.py           # pydantic model output contract (Phase 0)
    seeds.py              # set_all_seeds, run_metadata (Phase 0)
    data/
      __init__.py
      load.py             # raw Jigsaw loader + column validation (Phase 0)
      dedup.py            # near-duplicate dedup before split (Phase 0)
      split.py            # iterative multi-label stratified split (Phase 0)
      firewall_check.py   # executable leakage gate (Phase 0)
      prepare.py          # prepare_dataset() orchestration (Phase 0)
    train_classical.py    # TF-IDF + OneVsRest LogisticRegression (Phase 1)
    train_distilbert.py   # HF fine-tune on RunPod GPU (Phase 1)
    sweep.py              # W&B sweep config, parallel fan-out (Phase 1)
    evaluate.py           # stratified CV, thresholds, CIs, held-out-once (Phase 1)
    thresholds.py         # per-label threshold tuning on validation (Phase 1)
    tracking.py           # W&B logging + registry + promote (Phase 1)
    export_onnx.py        # DistilBERT -> ONNX int8 (Phase 1)
  backend/
    __init__.py
    schemas.py            # request models (Phase 2)
    model_loader.py       # safe skops load + SHA-256 verify (Phase 2)
    policy.py             # thresholds -> flags + decision (Phase 2)
    db.py                 # SQLAlchemy models + writes (Phase 2)
    feedback.py           # reviewer truth vs prediction -> feedback (Phase 3)
    app.py                # FastAPI /predict /health (Phase 2)
    Dockerfile            # CPU serve image (Phase 2, finalized Phase 5)
  frontend/
    ui.py                 # Streamlit user + reviewer views (Phase 3)
    Dockerfile
  monitoring/
    dashboard.py          # Streamlit, reads RDS (Phase 3)
    Dockerfile
  rescorer/
    worker.py             # EC2 #2 worker, ONNX DistilBERT, polls RDS (Phase 3)
    Dockerfile
  infra/
    docker-compose.yml    # local full stack (Phase 5)
    aws/
      bootstrap.sh                  # org + account + state bucket (Phase A)
      scp-sandbox-guardrails.json   # SCP on the Sandbox OU (Phase A)
    terraform/            # VPC, EC2, RDS, ECR, IAM, OIDC, budget (Phase A)
    runpod/               # pod lifecycle + reaper (Phase 1)
  requirements/
    base.txt dev.txt train.txt serve.txt   # pinned per surface (Phase 0+)
  .github/workflows/
    ci.yml                # lint + tests + scans + tf plan gate on PR (Phase 4)
    deploy.yml            # arm64 build, ECR push, apply, SSM roll (Phase 5)
    runpod-reaper.yml     # scheduled TTL reaper (Phase 1)
  tests/
    unit/
    integration/
  docs/
  pyproject.toml
  README.md               # operator guide, finalized Phase 5
  MODEL_CARD.md           # metrics + provenance, drafted Phase 1, final Phase 5
  SECURITY.md             # VDP (Phase 5, mandatory, repo is public)
  aibom.json              # CycloneDX AIBOM (Phase 5)
```

## Interface Contracts (cross-phase glue)

These signatures are the seams between phases. A phase implementer sees only their own phase file, so these types are the contract they build against. Names and types here are authoritative; phase files must match them exactly.

**Labels (Phase 0 → all).**
```python
# model/labels.py
LABELS: tuple[str, ...] = (
    "toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate",
)
```

**Text normalization (Phase 0 → Phase 1/2). Two functions, deliberately different.**
```python
# model/normalize.py
def normalize(text: str) -> str: ...
# FROZEN corpus normalizer: NFKC + casefold + whitespace collapse. Dedup, the leakage
# gate, and split_version all depend on it. Changing it moves the locked test set.

def normalize_for_serving(text: str) -> str: ...
# Serving normalizer: normalize() plus confusable/homoglyph folding, combining-mark
# stripping, and a MAX_INPUT_CHARS cap. Never imported by model/data/dedup.py.
```

**Dataset preparation (Phase 0 → Phase 1).**
```python
# model/data/split.py
def make_splits(
    df: "pd.DataFrame",
    seed: int,
    test_size: float = 0.15,
    n_folds: int = 5,
) -> tuple["pd.DataFrame", "pd.DataFrame", list[tuple["np.ndarray", "np.ndarray"]]]: ...

# model/data/prepare.py
@dataclass(frozen=True)
class SplitConfig:
    seed: int = 42
    test_size: float = 0.15
    n_folds: int = 5

@dataclass(frozen=True, eq=False)
class DatasetBundle:
    train_df: "pd.DataFrame"
    test_df: "pd.DataFrame"
    fold_indices: list[tuple["np.ndarray", "np.ndarray"]]
    raw_sha256: str
    split_version: str
    env_version: str
    config: SplitConfig = field(default_factory=SplitConfig)

DEFAULT_SPLIT = SplitConfig()

def prepare_dataset(raw_csv: "Path", config: SplitConfig = DEFAULT_SPLIT) -> DatasetBundle: ...
```

**Model interface (Phase 1 produces artifacts; Phase 2/Phase 3 load).**
```python
# both models satisfy this shape
def predict_proba(texts: list[str]) -> "np.ndarray": ...   # shape (len(texts), 6), columns ordered by LABELS
```

**Safe loader (Phase 2).**
```python
# backend/model_loader.py
TRUSTED_TYPES: tuple[str, ...]        # explicit static allowlist; never get_untrusted_types()

@dataclass
class LoadedModel:
    model_version: str                # full, internal:  "toxic-clf:v3@sha256:abcd..."
    public_version: str               # opaque, public:  "toxic-clf:v3"
    def predict_proba(self, texts: list[str]) -> "np.ndarray": ...

def load_model(artifact_path: "Path", expected_sha256: str,
               artifact_name: str, registry_version: int) -> LoadedModel: ...
def load_from_settings(settings: "Settings") -> LoadedModel: ...
# expected_sha256 is the digest of record read from the git-committed MODEL_CARD.md and
# cross-checked against MODEL_DIGEST. Fails closed on any mismatch.
```

Two version fields, not one. `/health` strips the digest so the exact artifact cannot be
fingerprinted by an attacker crafting evasions; returning it on every `/predict` response
would make that control inert. The public label needs the registry version, so `load_model`
takes it rather than guessing it from the filename.

**Policy (Phase 1 tunes thresholds; Phase 2 applies).**
```python
# backend/policy.py
@dataclass(frozen=True)
class DecisionResult:
    flags: dict[str, bool]            # per LABELS
    decision: str                     # "allow" | "review" | "block"
    max_prob: float

def decide(probs: dict[str, float], thresholds: dict[str, float]) -> DecisionResult: ...
# thresholds.json artifact shape: {label: float} for each label in LABELS
```

**Output contract (Phase 0 defines; Phase 2 returns; Phase 3 consumes).**
```python
# model/contract.py  (pydantic)
def probs_to_dict(row: "np.ndarray") -> dict[str, float]: ...
def enforce_hierarchy(probs: dict[str, float]) -> dict[str, float]: ...

class LabelScore(BaseModel):
    prob: float = Field(ge=0.0, le=1.0)
    flag: bool

class PredictionResponse(BaseModel):
    request_id: str
    model_version: str
    labels: dict[str, LabelScore]
    decision: Literal["allow", "review", "block"]
    max_prob: float = Field(ge=0.0, le=1.0)
    latency_ms: int = Field(ge=0)
```

**Database writes (Phase 2 defines; Phase 3 consumes).**
```python
# backend/db.py
@dataclass(frozen=True)
class PredictionRow: ...     # request_id, ts, input_text, input_chars, model_version, probs,
                             # decision, max_prob, latency_ms, status, persist_status,
                             # error_kind, client_fp

@dataclass(frozen=True)
class ReviewIntent: ...      # request_id, source, sample_rate, input_text_snapshot, enqueued_ts

@dataclass(frozen=True)
class PendingWrite:
    prediction: PredictionRow
    review: "ReviewIntent | None"

def init_db(engine) -> None: ...                                       # init_schema is an alias
def write_pending(session, pending: PendingWrite, stamp) -> int: ...   # returns latency_ms
def insert_prediction(session, row: PredictionRow) -> None: ...        # idempotent on request_id
def enqueue_review(session, intent: ReviewIntent) -> None: ...         # idempotent on request_id
def fetch_pending_reviews(session, limit: int) -> list["ReviewQueue"]: ...

# Table vocabularies, enforced by CHECK constraints of the same names.
REVIEW_SOURCES = ("flagged", "random-audit", "user-report")      # ck_review_source
REVIEW_STATUSES = ("pending", "rescored", "reviewed", "expired") # ck_review_status
FEEDBACK_SOURCES = ("reviewer", "user")                          # ck_feedback_source

# backend/audit.py
FLAGGED_SAMPLE_RATE: float = 1.0     # written onto a review row whose source is 'flagged'
```

`write_pending` is the seam, not `insert_prediction(session, response, input_text)`: a failed
request has no `PredictionResponse` and must still write a row, and the spool has to replay a
prediction *and* its review row through one code path.

`review_queue.sample_rate` is **nullable**, and `review_queue_sample_rate_ck` is why: a design
stratum (`flagged` or `random-audit`) must carry the inclusion probability it was drawn with,
and a `user-report` must carry NULL because it has none, so the estimator skips it until a
human reviews it under a known design. Stratified collection without stratified estimation is
still biased, and the weight cannot be reconstructed later because the audit rate is a
deploy-time setting that may change between rows.

`PredictionResponse.model_version` carries `LoadedModel.public_version`. The full version goes
to `predictions.model_version` and to the structured request log, never to a client.

Phase 3 owns `submit_review(...)` and `write_distilbert_probs(...)`. Both need reviewer session
identity and re-scorer status semantics that do not exist in Phase 2; the tables they write are
defined here.

**W&B artifact naming.** Classical artifact `toxic-clf`, DistilBERT artifact `toxic-distilbert`. Versions `:vN`. The pinned digest travels as `@sha256:...` in the model card and the deploy env var `MODEL_DIGEST`.

## Phase Dependency Graph

```
Phase A (AWS account foundation)  ---------------+   [independent, runs any time]
                                                 |
Phase 0 (data + firewall + contract)             |
   -> Phase 1 (train, register, promote, ONNX)   |   [needs: Jigsaw data, W&B, RunPod]
        -> Phase 2 (FastAPI + safe load + RDS) <-+   [needs: Phase A RDS, promoted artifact + digest, thresholds.json]
             -> Phase 3 (UI + monitoring + rescorer)  [needs: /predict, DB schema, ONNX DistilBERT artifact]
                  -> Phase 4 (tests consolidation + CI gate)  [runs the suites built across 0-3]
                       -> Phase 5 (Docker + ECR + SSM deploy + docs + AIBOM)  [needs: Phase A, all images, digests, measured ONNX throughput]
```

Phase A has no dependency on Phases 0 or 1 and can run in parallel with them. It must finish before Phase 2 needs a real RDS instance.

Each phase produces a working, testable increment and lands on its own feature branch merged by PR.

---

## Phase A: AWS account foundation

**Deliverable:** A dedicated AWS Organizations member account with guardrails, identity, network, compute, data, registry, and pipeline roles in place, provisioned with zero static credentials and reproducible from code. **Design spec:** `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`. Read it before starting.

**Branch:** `feat/phase-a-aws-foundation`.

**Prerequisites (verify before task 1):** AWS CLI v2 installed (v1.35.0 is currently installed and is not sufficient), Terraform 1.11 or newer (1.5.7 is currently installed, and S3 native state locking went GA in 1.11), `gh` authenticated, the repository made public, and IAM Identity Center enabled with a working `aws configure sso` profile. `docs/HANDOFF.md` carries the exact commands under "Do this next". Root-address mail delivery is not a prerequisite, it is the operator gate at bootstrap step 7.

**Files:** `infra/aws/bootstrap.sh`, `infra/aws/scp-sandbox-guardrails.json`, `infra/terraform/*.tf`, `.github/workflows/deploy.yml`, `docs/HANDOFF.md`, `docs/rcap-iam-audit.md`, `SECURITY.md`.

**Tasks:**
1. Console, one time, four operations: enable IAM Identity Center in the management account with home region `us-west-2`, create the directory user, create an `AdministratorAccess` permission set, assign it to the management account. Then `aws configure sso`. Verify with `aws sts get-caller-identity`.
2. `infra/aws/scp-sandbox-guardrails.json`: region lock on `aws:RequestedRegion`, deny `iam:CreateUser` and `iam:CreateAccessKey`, Graviton instance-type allowlist plus GPU and metal denial via `ec2:InstanceType` **scoped to `Resource: "arn:aws:ec2:*:*:instance/*"`**, deny `rds:CreateDBCluster`, require `rds:ManageMasterUserPassword` and deny `rds:PubliclyAccessible` on `rds:CreateDBInstance`, deny leaving the org, deny CloudTrail and GuardDuty tampering. **Do not attempt an RDS class cap with `rds:DatabaseClass`.** It is unsupported on `CreateDBInstance` and would deny all RDS creation. See spec section 5.1 for both traps. Validate the JSON before attaching.
3. `infra/aws/bootstrap.sh`: idempotent, ten ordered steps per spec section 6, which names every verified CLI operation. **The script never calls `iam enable-organizations-root-credentials-management` and never deletes a root credential.** Root is break-glass and stays, per spec section 5.2. Step 7 is an operator step that establishes the break-glass: root password recovery, strong password, MFA enrolled, no access keys. Every org-level write the script performs is scoped to the new `Sandbox` OU, so no existing account changes posture.
4. `infra/terraform/`: VPC with two public and two private subnets and no NAT gateway, security groups with no port 22, both EC2 instances with IMDSv2 required, RDS Postgres 16 with `manage_master_user_password = true`, four ECR repositories, instance profiles, the `gha-ci` and `gha-deploy` OIDC roles, CloudTrail, GuardDuty, CloudWatch log groups at 14-day retention, and the $100 budget with alerts at 50, 80, and 100 percent.
5. `.github/workflows/deploy.yml`: build four arm64 images on `ubuntu-24.04-arm`, tag by git SHA, push to ECR, `terraform apply`, roll containers through SSM Run Command. Gated by the `production` environment with required review.
6. Seed Secrets Manager by CLI with the W&B API key and the reviewer shared secret. No secret value enters Terraform state or the repository.
7. `Makefile` targets `aws-up` and `aws-down` that start and stop EC2 and RDS between sessions. Document that a stopped RDS instance restarts automatically after seven days.
8. `docs/rcap-iam-audit.md`: read-only audit of account `<MGMT_ACCOUNT_ID>`, which is the organization **management** account and also runs RCAP. Access key age, attached policy breadth, MFA state, root credential state, public S3, CloudTrail coverage, and the fact that a production workload sits in the management account where SCPs cannot constrain it. **Read-only API calls only, no writes to that account.**
9. `SECURITY.md`: **done 2026-07-30**, ahead of Phase A. Review only. GitHub private vulnerability reporting, secret scanning, and push protection are enabled on the repository.
10. `docs/HANDOFF.md`: current stage, what exists where, exact resume command.

**Test strategy:** Re-run `bootstrap.sh` and confirm it is a no-op. Run `terraform plan` and confirm no drift after apply. Confirm the SCP actually denies by attempting a denied action (a `t3.large` launch or an `iam:CreateAccessKey` call) and observing the denial. Confirm `gha-ci` cannot assume deploy permissions. Confirm no security group allows port 22. Confirm `terraform destroy` cleanly removes everything.

**Exit criteria:** account created inside the `Sandbox` OU with the SCP attached, root break-glass established (MFA on, no access keys, password stored, root-usage alarm firing on a test), no organization-wide setting changed and RCAP's posture provably unchanged, SSO login works from both the Mac and the Jetson, `terraform apply` and `terraform destroy` both succeed, a denied action is observed to fail, no static AWS access key exists in the account, budget alerts configured, RCAP audit written, and the whole thing merged by PR.

**Enforcement skills:** `writing-deny-allow-rules`, `triaging-vulnerability-findings`, `writing-vdp-and-coordinated-disclosure`, `building-rollback-plan`.

---

## Phase 0: Repo scaffold, deterministic data pipeline, leakage firewall, seed hygiene

**Deliverable:** A reproducible offline data pipeline that turns raw Jigsaw into deduplicated, iteratively-stratified, locked splits with a `data_version` hash, plus the label constants, the output contract types, seed-hygiene utilities, and an executable firewall gate. No cloud, no model training. Runs and tests fully on a laptop against a small synthetic fixture.

**Branch:** `feat/phase-0-data-firewall`. **Detailed plan:** `docs/superpowers/plans/2026-07-31-phase-0-data-firewall-v2.md`.

**Files:** `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, `model/labels.py`, `model/contract.py`, `model/seeds.py`, `model/data/{load,dedup,split,prepare,firewall_check}.py`, `tests/unit/test_*`, `tests/fixtures/mini_jigsaw.csv`, `Makefile`.

**Interfaces produced:** `LABELS`, `SplitConfig`, `DatasetBundle`, `prepare_dataset()`, `PredictionResponse`/`LabelScore`, `set_all_seeds()`, `run_metadata()`.

**Tasks (right-sized, each ends testable):**
1. Project scaffold: `pyproject.toml` (ruff + pytest config), pinned `requirements/base.txt` + `requirements/dev.txt`, package skeleton with `__init__.py`, `Makefile` targets (`lint`, `test`, `data`), `.env.example`. Test: `ruff check` clean, `pytest` collects.
2. `model/labels.py`: ordered `LABELS` tuple. Test: length 6, exact order, immutability.
3. `model/data/load.py`: `load_raw(csv) -> DataFrame`, validates the `id` + `comment_text` + six label columns, rejects nulls in labels and values outside {0,1}. Test against `mini_jigsaw.csv` and a malformed fixture.
4. `model/data/dedup.py`: `dedup(df) -> DataFrame` runs before any split, in **two stages**. Stage 1 collapses exact `normalize()`d duplicates. Stage 2 is MinHash **LSH blocking only** — banding passed explicitly as `params=(LSH_BANDS=16, LSH_ROWS=6)`, never via `threshold=`, because datasketch's auto-tuner resolves `threshold=0.80` to `b=9, r=13` whose recall at J=0.80 is 0.399 — followed by **exact char-shingle Jaccard verification** against `DEDUP_JACCARD = 0.80`. LSH nominates; exact Jaccard decides. Labels are OR-reconciled across each collapsed group and the survivor is `min()` over the verified candidate ids, never `hits[0]` (`MinHashLSH.query` returns `list(set(...))`, whose order varies with `PYTHONHASHSEED`). Test: known duplicates and near-duplicates collapse, distinct rows survive, dedup is idempotent and row-order independent, and `lsh_recall(0.80) >= 0.99`.
5. `model/data/split.py`: `make_splits(df, seed, test_size=0.15, n_folds=5) -> (train_df, test_df, fold_indices)` using `MultilabelStratifiedShuffleSplit` for the locked 15% test and `MultilabelStratifiedKFold` for folds. The seed is a positional argument, not a config object, so the split tests are **parametrized over five seeds** rather than passing at the one seed they were written against. Test, at every seed: every label including `threat` present in test and in every fold; test size ~15%; same seed reproduces identical ids; no id overlaps train and test.
6. `model/seeds.py`: `set_all_seeds(seed)` (random, numpy, torch guarded, PYTHONHASHSEED note), `run_metadata()` returns git SHA + seed + timestamp. Test: seeded RNG determinism; metadata carries a git SHA.
7. `model/contract.py`: pydantic `LabelScore`, `PredictionResponse` with a validator that `labels` keys equal `LABELS` and `decision` is one of allow/review/block. Test: the spec example JSON validates; wrong decision and wrong label keys are rejected.
8. `model/data/prepare.py` + `model/data/firewall_check.py`: `prepare_dataset()` wires load → dedup → split and computes **three separate version fields** — `raw_sha256` (the bytes of the CSV as delivered), `split_version` (the realized train/test/fold membership plus the per-id label fingerprint and the `SplitConfig`), and `env_version` (pinned library versions plus the dedup and normalizer parameters). One string could not say whether the corpus, the split, or the environment moved; all three are logged to W&B separately and `data_version` survives only as a derived composite property for single-string display. `assert_no_leakage(bundle)` asserts no train/test id overlap, no exact-text leak, and no cross-split near-duplicate — and is **independent of dedup by construction**, deciding on exact Jaccard at `GATE_JACCARD = 0.70`, strictly below `DEDUP_JACCARD = 0.80`, so it can catch the band dedup deliberately leaves. `Makefile` `data` target runs prepare then the firewall check. Test: all three version fields are stable across repeat runs and each moves only for its own cause; the gate passes on clean data and raises on an injected leak.

**Test strategy:** Pure unit tests against a committed synthetic `mini_jigsaw.csv` (68 rows, all six labels represented, four planted duplicates and near-duplicates). No network. Determinism asserted by running prepare twice and comparing `data_version`.

**Exit criteria:** `make data` produces an identical `data_version` on repeat runs; `assert_no_leakage` passes; every Phase 0 test green; `ruff check` clean; merged via PR.

**Enforcement skills:** `building-deterministic-data-pipelines`, `enforcing-leakage-firewall`, `auditing-train-test-split`, `enforcing-seed-hygiene`, `deduplicating-records`, `auditing-data-quality`.

**External prerequisites:** none (synthetic fixture). Real Jigsaw CSV needed only when Phase 1 trains.

---

## Phase 1: Train classical + sweep + DistilBERT fine-tune on RunPod; W&B registry; promote; export ONNX

**Deliverable:** Two registered W&B artifacts with digests and metric-bearing model cards: the classical winner promoted to Production, and DistilBERT as an ONNX int8 challenger. A `thresholds.json` artifact tuned on validation. Held-out test evaluated exactly once. RunPod cost governance active.

**Branch:** `feat/phase-1-train-register`. **Needs:** the Kaggle Jigsaw archive (`julian3833/jigsaw-multilingual-toxic-comment-classification`); use the `jigsaw-toxic-comment-train.csv` English six-label file inside it, not the multilingual `validation.csv`/`test.csv` or `jigsaw-unintended-bias-train.csv`. A new W&B project, RunPod API key + budget cap, GPU pods. RunPod, W&B, and Kaggle credentials live on the Jetson. AWS region `us-west-2`. See Resolved Decisions.

**Files:** `model/train_classical.py`, `model/evaluate.py`, `model/thresholds.py`, `model/tracking.py`, `model/sweep.py`, `model/train_distilbert.py`, `model/export_onnx.py`, `infra/runpod/*`, `.github/workflows/runpod-reaper.yml`, `requirements/train.txt`, draft `MODEL_CARD.md`.

**Interfaces produced:** classical skops artifact + `predict_proba` shape, `thresholds.json`, DistilBERT ONNX int8 artifact, W&B digests (`MODEL_DIGEST`).

**Tasks:**
1. Baseline-first: log a trivial prior/most-frequent baseline, then the classical Pipeline (`FeatureUnion` of word `TfidfVectorizer(ngram_range=(1,2))` and char `TfidfVectorizer(analyzer="char_wb", ngram_range=(3,5))` into `OneVsRestClassifier(LogisticRegression(class_weight="balanced"))`), TF-IDF fit inside the pipeline inside each fold. LinearSVC with `CalibratedClassifierCV` as a separate tracked run. (`building-baseline-models`, `comparing-models-fairly`.)
2. `model/evaluate.py`: stratified CV over Phase 0 folds, macro-F1 + per-label PR-AUC + per-label F1 with bootstrap confidence intervals. A single guarded `evaluate_on_test(...)` that may run once. (`evaluating-multiclass-classifiers`, `comparing-models-fairly`.)
3. `model/thresholds.py`: per-label thresholds tuned on validation with asymmetric cost (recall-weighted for rare severe labels like `threat`). Emit `thresholds.json`. (`tuning-classification-threshold`.)
4. `model/tracking.py`: log git SHA + hyperparams + `data_version` + split seed + metrics/CIs, save skops artifact + thresholds, register to the Model Registry, promote the winner to Production. Record the digest.
5. `model/sweep.py`: W&B sweep config, parallel fan-out across mid-tier pods, interruptible spot. (`running-hyperparameter-sweep`.)
6. `model/train_distilbert.py`: HF Trainer, early stopping on validation, weight decay, dropout, 2-3 epochs, per-epoch train/val loss gap logged. Save with safetensors. (`auditing-deep-learning-overfit`, `scaffolding-pytorch-training-loop`.)
7. `model/export_onnx.py`: export to ONNX, int8 dynamic quantization (optimum/onnxruntime), verify logit parity on a sample, register to W&B with digest. (`packaging-model-for-deployment`.)
8. Safe serialization throughout: skops for classical, safetensors + ONNX for DistilBERT, no pickle.
9. `infra/runpod/`: pod launch with `trap EXIT` teardown, `.github/workflows/runpod-reaper.yml` scheduled TTL/idle reaper, spending cap note, spot preference.
10. Draft `MODEL_CARD.md` carrying metrics + CIs + data provenance + the pretraining-contamination caveat + digests. (`writing-model-cards`, `running-eval-before-after-finetune`.)

**Test strategy:** Unit tests on the pipeline factory (produces `(n,6)` probabilities, TF-IDF lives inside the pipeline), threshold tuning (never sees test), and the once-only test guard. A fast smoke run on a data subset in CI-safe mode; full training runs on RunPod. ONNX parity test compares quantized vs float logits within tolerance.

**Exit criteria:** classical Production artifact registered with digest + metric card; DistilBERT ONNX int8 challenger registered with digest; `thresholds.json` present; held-out test touched once; train/val gap logged every epoch; sweep completed; reaper workflow live; every run reproducible from SHA + seed + `data_version`.

**Enforcement skills:** `building-baseline-models`, `comparing-models-fairly`, `running-hyperparameter-sweep`, `auditing-deep-learning-overfit`, `evaluating-multiclass-classifiers`, `tuning-classification-threshold`, `packaging-model-for-deployment`, `running-eval-before-after-finetune`, `writing-model-cards`, `enforcing-seed-hygiene`.

---

## Phase 2: FastAPI backend, safe model loading, RDS Postgres, prediction logging

**Deliverable:** `/predict` and `/health` serving the classical Production model on CPU, loading through the safe skops loader with SHA-256 verification (fail closed), applying the policy for flags + decision, and writing every prediction to RDS `predictions` (enqueuing review rows per policy).

**Branch:** `feat/phase-2-backend-rds`. **Needs:** promoted classical artifact + digest, `thresholds.json`, an RDS Postgres (or local Postgres for tests).

**Files:** `backend/config.py`, `backend/schemas.py`, `backend/preprocess.py`, `backend/model_card.py`, `backend/model_loader.py`, `backend/policy.py`, `backend/audit.py`, `backend/db.py`, `backend/spool.py`, `backend/persistence.py`, `backend/ratelimit.py`, `backend/auth.py`, `backend/retention.py`, `backend/app.py`, `backend/Dockerfile`, `.dockerignore`, `requirements/serve.in`, `requirements/serve.txt`, `MODEL_CARD.md` (digest of record), `docs/latency-baseline.md`, `tests/unit/*`, `tests/integration/*`, `tests/perf/test_latency_budget.py`.

**Interfaces produced:** the three RDS tables (`predictions`, `review_queue`, `feedback`), `load_model()`, `decide()`, the `PendingWrite` DB seam, the durable spool, the retention purge, the live `/predict` `/health` endpoints.

**Tasks:**
1. `backend/schemas.py`: `PredictRequest{text}`, reuse `model/contract.py` for the response. `backend/config.py` holds `Settings`; `MAX_INPUT_CHARS` is a hard literal with no environment key, because an abuse control a deploy-time variable can widen is not a control.
2. `backend/model_loader.py`: verify SHA-256 against `expected_sha256`, `skops.io.load` under an **explicit static** `TRUSTED_TYPES` allowlist, return `LoadedModel` with `model_version`, `public_version`, and `predict_proba`. Fail closed on mismatch. The digest of record is read from the git-committed `MODEL_CARD.md` and cross-checked against `MODEL_DIGEST`, so forging an artifact requires compromising the registry **and** the repository. Test with a fixture skops artifact, a tampered one, and one carrying a type outside the allowlist.
3. `backend/policy.py`: `decide(probs, thresholds)` sets per-label flags and the allow/review/block decision. `severe_toxic` implies `toxic` before the response is built. Deterministic, unit tested at boundaries.
4. `backend/db.py`: SQLAlchemy models for the three tables, `init_db` idempotent create, `PredictionRow` / `ReviewIntent` / `PendingWrite`, `write_pending`, `insert_prediction`, `enqueue_review`, `fetch_pending_reviews`. Bounded pool with a 2 s checkout timeout. DSN from env.
5. `backend/preprocess.py` imports `normalize` from `model/data/dedup.py` — the **same function object**, asserted by a test — so serving cannot drift from the corpus normalizer and reintroduce train/serve skew.
6. Abuse controls on the public listener, all three in one `_gate` middleware that runs **before** the body is parsed: a 16 KB body cap, a demo `X-API-Key` compared with `hmac.compare_digest`, and a per-key token-bucket rate limit (`backend/ratelimit.py`, `backend/auth.py`). `/health` stays unauthenticated for the grader, the deploy gate, and the container `HEALTHCHECK`. Each control has a test that fails when the control is removed.
7. `backend/app.py`: `POST /predict` (validate → `predict_proba` → `decide` → persist + maybe enqueue → return with `latency_ms`), `GET /health` (opaque model version + DB readiness + spool depth). `request_id` is a uuid. One structured JSON log line per request, carrying the full model version and never the input text.
8. Complete prediction logging **without an availability switch** (`backend/spool.py`, `backend/persistence.py`): direct insert, then an fsync'd bounded local spool with an idempotent drain, and 503 only once the spool is saturated. Rubric 2.2 demands durability, not a 503 an attacker can trigger by pressuring the database.
9. `latency_ms` is stamped **after** the inserts, inside the transaction, so the graded latency chart includes the persistence component; the commit round trip is measured separately as `commit_ms`. Failed requests write a row with `status='error'`, so the slow tail is present rather than structurally absent. One load pass records p50/p95/p99 against a real Postgres in `docs/latency-baseline.md`; budget p95 < 500 ms.
10. `backend/retention.py`: expire pending reviews at a hard TTL, then null `predictions.input_text` past `INPUT_TEXT_RETENTION_DAYS` except where a review is genuinely still open, then null `review_queue.input_text_snapshot` at its own TTL regardless of status. Expiring first is what keeps the pending exemption from becoming attacker-controlled retention.
11. `backend/Dockerfile` + `.dockerignore`: CPU serve image, base pinned by digest, dependencies from the hashed `requirements/serve.txt` installed wheels-only, non-root `appuser`, `HEALTHCHECK` whose `--start-period` covers the measured cold-start model load, no credential in any layer. The artifact is mounted at deploy time; the image never holds a registry credential.
12. Integration tests against a **real Postgres** (testcontainers or a CI service): `/predict` and `/health`, a full prediction-to-DB round trip, the degraded and failed paths, and the retention purge.

**Test strategy:** Unit for policy, loader (including the fail-closed path), rate limiter, spool, and persistence. Integration spins a Postgres and asserts a `/predict` call returns contract-valid JSON, writes one `predictions` row, and enqueues a `review_queue` row when policy says review. Mocks are for unit tests; the phase gate does not accept them.

**Exit criteria:** `/predict` returns contract-valid JSON and persists exactly one `predictions` row per request, including on the degraded and failed paths; policy enqueues review rows correctly; `/health` reports the opaque version + DB ok; no public response carries the artifact digest; a tampered artifact, a digest disagreeing with the model card, and a type outside the allowlist all refuse to load; every abuse control has a test that fails when the control is removed; with the database unreachable `/predict` still answers 200 and the row reaches Postgres after the drain; p95 latency under 500 ms recorded in `docs/latency-baseline.md`; the retention purge expires pending reviews at the hard TTL; integration tests green.

**Enforcement skills:** `packaging-model-for-deployment` (loader boundary), plus the spec section 9 trust-boundary rules.

---

## Phase 3: Streamlit user + reviewer UI, monitoring dashboard, DistilBERT re-scorer worker

**Deliverable:** A user UI (submit text, see decision + per-label probabilities), a reviewer UI (drain `review_queue`, see model probs plus the DistilBERT second opinion, confirm/override the six labels, write `reviewer_labels` and derive `feedback`), a monitoring dashboard on EC2 #2 (latency over time, predicted-class distribution as target drift, live accuracy from the feedback join), and the DistilBERT ONNX re-scorer worker that polls `review_queue` and writes `distilbert_probs`.

**Branch:** `feat/phase-3-ui-monitoring-rescorer`. **Needs:** running `/predict`, the RDS schema, the DistilBERT ONNX int8 artifact + digest.

**Files:** `frontend/ui.py`, `frontend/Dockerfile`, `rescorer/worker.py`, `rescorer/Dockerfile`, `monitoring/dashboard.py`, `monitoring/Dockerfile`, `backend/feedback.py`, tests under `tests/integration/`.

**Tasks:**
1. `frontend/ui.py`: user view calls backend `/predict` over HTTP and renders decision + per-label prob/flag; reviewer view reads pending review rows, shows model probs beside `distilbert_probs`, lets the reviewer set the six labels and submit, writing `reviewer_labels` + `status=reviewed` and deriving a `feedback` row. Minimal single-reviewer auth (shared secret), per the spec open question default.
2. `rescorer/worker.py`: poll `review_queue` for `status=pending`, load ONNX int8 DistilBERT through onnxruntime (digest-verified, baked artifact), tokenize, score six labels, write `distilbert_probs` jsonb + `status=rescored`. Backoff loop, idempotent, batched, CPU.
3. `monitoring/dashboard.py`: Streamlit reading RDS. Latency over time from `predictions.latency_ms`/`ts`; predicted-class distribution over time (flag counts per label per time bucket) as target drift; live per-label precision/recall/accuracy from `feedback` joined to `predictions`. (`monitoring-prediction-drift`, `monitoring-data-drift`.)
4. `backend/feedback.py`: derive feedback from `reviewer_labels` vs the logged prediction, insert `feedback` rows.
5. Dockerfiles for frontend, monitoring, rescorer on pinned requirements.

**Test strategy:** Integration test seeds `review_queue`, runs one worker pass, asserts `distilbert_probs` written and status advanced. A reviewer-submit test asserts `reviewer_labels` + a derived `feedback` row. Dashboard query functions tested against a seeded DB (assert the aggregations return expected shapes).

**Exit criteria:** local docker-compose end to end: submit → predict → logged → enqueued → rescored → human review → feedback → dashboard shows live accuracy. Worker and reviewer integration tests green.

**Enforcement skills:** `monitoring-prediction-drift`, `monitoring-data-drift`, `evaluating-multiclass-classifiers`.

---

## Phase 4: Unit and integration tests consolidation, CI/CD gate

**Deliverable:** The full pytest suite runs in GitHub Actions and blocks merges on failure, alongside ruff, gitleaks, and semgrep gates, plus the scheduled RunPod reaper. Tests written during Phases 0-3 (TDD) are consolidated and any cross-cutting gaps filled.

**Branch:** `feat/phase-4-ci-gate`.

**Files:** `.github/workflows/ci.yml`, security-scan steps, `pyproject.toml` test markers + coverage config, any missing integration tests under `tests/integration/`.

**Tasks:**
1. `.github/workflows/ci.yml`: on PR to main, set up Python 3.11, install pinned deps, run `ruff check`, run `pytest` (unit plus integration with a Postgres service). Any failure blocks the merge. Cache dependencies.
2. Security gates: gitleaks (secret scan) and semgrep (SAST) on PRs that touch executable code, failing on high-severity findings (QC.1). (`triaging-vulnerability-findings`.)
3. Test markers (unit vs integration) and a coverage threshold.
4. Fill cross-cutting integration gaps (the full round trip, the worker drain) if not already covered by earlier phases.
5. Confirm `.github/workflows/runpod-reaper.yml` is present and scheduled.

**Test strategy:** Prove the gate: open a PR with a deliberately failing test and confirm the merge is blocked, then fix and confirm green. `auditing-pinned-dependencies` on the lockfiles.

**Exit criteria:** a failing test blocks merge (demonstrated); ruff + pytest + gitleaks + semgrep all gate; the reaper runs on schedule.

**Enforcement skills:** `auditing-pinned-dependencies`, `triaging-vulnerability-findings`, `pinning-reproducible-environments`.

---

## Phase 5: Docker, two-EC2 deploy, README, model card, AIBOM

**Deliverable:** One arm64 Docker image per component in ECR, a docker-compose local full stack, a working `deploy.yml` that builds, pushes, applies, and rolls containers through SSM Run Command onto the Phase A instances, deploy-time model artifact fetch by digest (verify SHA-256, bake), and the finished operator docs: `README.md`, `MODEL_CARD.md`, CycloneDX `aibom.json`, an `input_text` retention purge job, a rollback plan, and the mandatory `SECURITY.md` VDP.

**Branch:** `feat/phase-5-deploy-docs`. **Needs:** Phase A complete (account, VPC, EC2, RDS, ECR, OIDC roles), all component images, W&B digests, measured ONNX int8 throughput (drives the EC2 #2 sizing confirmation).

**Files:** `infra/docker-compose.yml`, `.github/workflows/deploy.yml`, `infra/aws/fetch_artifacts.sh` (deploy-time digest-verified fetch), finalized `MODEL_CARD.md`, `aibom.json`, `README.md`, `SECURITY.md`, `infra/ROLLBACK.md`.

**Tasks:**
1. `infra/docker-compose.yml`: backend, frontend, monitoring, rescorer, postgres with mounted/baked artifacts + env.
2. Deployment on the Phase A infrastructure: `deploy.yml` builds four arm64 images on `ubuntu-24.04-arm`, tags by git SHA, pushes to ECR through `gha-deploy`, runs `terraform apply`, and rolls containers through SSM Run Command with no SSH. `infra/aws/fetch_artifacts.sh` fetches the W&B artifact by digest, verifies SHA-256, and bakes it. Confirm or resize the EC2 #2 instance class from the measured ONNX throughput, staying inside the SCP allowlist.
3. Finalize `MODEL_CARD.md` + generate CycloneDX `aibom.json`, verify it scores 100% on the AIBOM evaluator. (`writing-model-cards`.)
4. `README.md` operator guide: architecture, run locally, deploy, endpoints, input_text retention policy, reviewer auth note, cost governance. (`writing-repo-documentation`.)
5. `input_text` retention purge: a scheduled job (cron or a small container) that nulls `predictions.input_text` older than `INPUT_TEXT_RETENTION_DAYS` (default 30), keeping the rest of the row for monitoring. Note it in the model card and README.
6. Dependency SBOM. (`generating-sbom`.)
7. Rollback plan. (`building-rollback-plan`; optional `building-canary-rollout`.)
8. `SECURITY.md` VDP + coordinated disclosure. Mandatory, the repo is public. Written in Phase A, reviewed here. (`writing-vdp-and-coordinated-disclosure`.)

**Test strategy:** `docker compose up` runs the full stack locally and serves a `/predict` end to end. Deploy scripts validated against a throwaway EC2 + RDS. AIBOM verified by the evaluator.

**Exit criteria:** local compose serves the full stack; deploy scripts stand up both EC2 + RDS and serve; artifacts verified by digest at deploy; README + model card + AIBOM + SECURITY + rollback complete.

**Enforcement skills:** `writing-model-cards`, `generating-sbom`, `writing-repo-documentation`, `writing-vdp-and-coordinated-disclosure`, `packaging-model-for-deployment`, `building-rollback-plan`.

---

## Self-Review: Spec Coverage Matrix

| Spec section | Requirement | Covered by |
|---|---|---|
| 1 | Six graded components on AWS | Phases 1-5 (W&B, FastAPI, RDS, Streamlit, monitoring, CI) |
| 2 | Multi-label, six labels, imbalance; macro-F1 + PR-AUC + CIs; accuracy banned | Phase 0 labels/contract; Phase 1 evaluate |
| 3 | Build-time vs runtime split; W&B deploy-time only | Global constraints; Phase 1 registry; Phase 5 deploy fetch |
| 3.1 | AWS account, identity, guardrails | Phase A (all tasks) |
| 4 | Classical online + DistilBERT async; rationale | Phase 1 tasks 1-8 |
| 5 | Output contract JSON; `/health` | Phase 0 contract; Phase 2 app |
| 6 | Leakage/overfitting firewall (all clauses) | Phase 0 dedup/split/firewall_check; Phase 1 thresholds/overfit |
| 7 | RDS schema (3 tables); monitoring reads | Phase 2 db; Phase 3 dashboard |
| 8 | Moderation + human-review + feedback loop | Phase 2 policy; Phase 3 UI/rescorer/feedback |
| 9 | Safe loading (skops, safetensors/ONNX, digest) | Phase 2 model_loader; Phase 1 serialization |
| 10 | RunPod cost governance | Phase 1 infra/runpod + reaper |
| 11 | W&B tracking + registry + promote | Phase 1 tracking |
| 12 | CI/CD gate + scheduled reaper | Phase 4 ci.yml; Phase 1 reaper |
| 13 | One image per component; compose; deploy scripts | Phase 2/3 Dockerfiles; Phase 5 compose/deploy |
| 14 | Unit + integration tests | Phases 0-4 tests |
| 15 | Repo layout | Repository Structure section |
| 16 | Reuse map (hw1/2/3/5/7/8) | Per-phase reuse notes below |
| 17 | Phase decomposition | Phases 0-5 |
| 18 | Risks/open questions | Open Decisions section |

## Reuse Map (pull code forward from the monorepo)

Monorepo root: `/Users/klambros/github_projects/MLOPS-Comp-4450-1`.

| Seed | Path | Pulled into |
|---|---|---|
| hw1 model + Streamlit | `assignments/hw1` | Phase 1 classical pattern, Phase 3 Streamlit |
| hw2 Docker | `assignments/hw2` | Phase 2/3 Dockerfiles, Phase 5 compose |
| hw3 FastAPI | `assignments/hw3` | Phase 2 backend |
| hw5 AWS/EC2 | `assignments/hw5` | Phase 5 deploy |
| hw7 monitoring | `assignments/hw7` | Phase 3 dashboard (move file-based to DB-backed) |
| hw8 CI/CD | `assignments/hw8` | Phase 4 ci.yml |

Read the seed before each phase and adapt it; do not rebuild from scratch.

## Resolved Decisions (2026-07-01)

Answers from the owner. These close the spec section 18 risks and the plan prerequisites.

1. **Accounts and secrets (revised 2026-07-30).** A new W&B project. The Jetson is the operator and build box, not the runtime host. It holds the W&B entity, the RunPod API key, and the Kaggle credentials. It holds **no static AWS credentials**, because none exist in this project. AWS access on both the Jetson and the Mac comes from an IAM Identity Center SSO profile issuing short-lived sessions, so either machine can build and deploy and neither is a credential store. CI is the primary deployment path and the Jetson is the manual fallback. AWS region `us-west-2` (Oregon), changed from `us-west-1` because N. California carries a price premium and has two AZs rather than four. The Jetson is aarch64, which still pairs cleanly with Graviton for local builds, though CI now builds arm64 natively on free `ubuntu-24.04-arm` runners. Provisioning is Phase A.
2. **Jigsaw data.** Kaggle download: `https://www.kaggle.com/api/v1/datasets/download/julian3833/jigsaw-multilingual-toxic-comment-classification`. That archive packages several Jigsaw competitions. Use the file `jigsaw-toxic-comment-train.csv` inside it: the original English Toxic Comment Classification Challenge training set with the six labels (`id, comment_text, toxic, severe_toxic, obscene, threat, insult, identity_hate`), which matches the loader's `REQUIRED_COLUMNS`. Do not train on `jigsaw-unintended-bias-train.csv` (different schema) or `validation.csv` / `test.csv` (multilingual, single label); either would break the English six-label scope.
3. **Reviewer auth.** Single reviewer role behind a shared secret. Confirmed.
4. **input_text retention (recommendation).** Retain raw `predictions.input_text` for 30 days, then a scheduled purge nulls `input_text` while keeping the rest of the row (probabilities, decision, flags, timestamps, latency) for long-term monitoring. Rationale: the human-review queue and the drift and accuracy windows need the raw text short-term; keeping user comments (potentially toxic or PII-bearing) indefinitely is an avoidable privacy liability. Configurable via `INPUT_TEXT_RETENTION_DAYS` (default 30). Never write raw text to W&B or application logs; only the access-restricted RDS row holds it. Documented in the model card and README, implemented as a purge job in Phase 5.
5. **Partner.** Solo. No partner attribution.
6. **Repo visibility (revised 2026-07-30).** Public. The assignment deliverable requires a public repository, and a public repository grants free unlimited `ubuntu-24.04-arm` runners, which lets CI build Graviton images natively instead of under QEMU. The VDP `SECURITY.md` is now mandatory under QC.1. gitleaks, semgrep, and the SBOM stay. Everything is designed public-safe from the first commit, since flipping public later would expose the full history anyway.
7. **Python.** 3.11, confirmed (pinned `requires-python = ">=3.11,<3.12"`).
8. **Phase plan expansion.** Just-in-time per phase (Phase 0 detailed now; 1-5 expanded at the start of each phase). Unchanged.

### AWS instance sizing (us-west-2, low spend, class project)

Recommendation. All Graviton (arm64) to match the aarch64 Jetson build box and to cut roughly 20% off the equivalent x86 on-demand rate. Every dependency (numpy, scipy, scikit-learn, onnxruntime, pydantic) ships aarch64 wheels; skops and datasketch are pure Python. If any wheel misbehaves on arm64, the x86 fallback is in the last column.

| Component | Recommended | vCPU / RAM | Why | x86 fallback |
|---|---|---|---|---|
| EC2 #1 (FastAPI classical + Streamlit) | `t4g.medium` | 2 / 4 GB | Low-traffic bursty web serving; 4 GB holds the word + char n-gram TF-IDF vocab, the model, and Streamlit with headroom | `t3.medium` |
| EC2 #2 (monitoring + DistilBERT ONNX int8 re-scorer) | `t4g.large` (start) | 2 / 8 GB | Async, intermittent queue drain suits a burstable instance; 8 GB covers onnxruntime + tokenizer + dashboard. Confirm or upsize to `c7g.xlarge` (4 vCPU) after measuring ONNX int8 throughput (spec section 18) if the drain is slow or the queue is large and continuous | `c6i.large` / `c6i.xlarge` |
| RDS Postgres | `db.t4g.micro` | 2 / 1 GB | Tiny predictions table, low write rate, a few time-bucket aggregations and one join. Bump to `db.t4g.small` (2 GB) if the dashboard queries lag | same (RDS engine is arch-agnostic to clients) |

RDS config: Postgres 16, 20 GB gp3 (minimum, cheap), Single-AZ (no Multi-AZ for a class project, it halves the cost), `us-west-2`, private subnets, `manage_master_user_password = true` so the password lives in Secrets Manager rather than Terraform state.

**Cost control matters more than instance class here.** Stop both EC2 instances and the RDS instance between work sessions and run them only during development and the demo. Approximate `us-west-2` on-demand rates (verify in the AWS Pricing Calculator, do not treat as exact): EC2 #1 around $0.034/hr, EC2 #2 around $0.067/hr, RDS around $0.016/hr, so roughly $0.12/hr with everything on. Left running continuously that approaches the $100 monthly ceiling. Run only during sessions and it stays in single-digit dollars. gp3 and the 20 GB RDS volume persist at cents/month when stopped.

Controls, strongest first: the SCP instance-type allowlist is a hard denial, `terraform destroy` is the full teardown, `make aws-down` and `make aws-up` stop and start between sessions, and budget alerts fire at 50, 80, and 100 percent of $100. There is no automated stop action, by owner decision.

**Gotcha to remember.** A stopped RDS instance restarts automatically after seven days. Stopped is not off. For gaps longer than a week, destroy rather than stop.

**No NAT gateway.** It would cost roughly a third of the monthly ceiling on its own. EC2 sits in public subnets behind an ingress allowlist with IMDSv2 required and no port 22, and RDS sits private with no internet path.

## Execution Handoff

Two phases are ready to start. **Phase A** is specified in `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md` and summarized above, and it needs a detailed plan file written before execution. **Phase 0** is fully detailed in `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md`. They are independent, so either can go first. See `docs/HANDOFF.md` for the current stage and the resume command.

Two execution options:

1. **Subagent-Driven (recommended):** a fresh subagent per task with review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** execute tasks in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.
