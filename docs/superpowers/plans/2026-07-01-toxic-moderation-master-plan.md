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
- **Supply chain (QC.1 / NIST SP 800-218).** Pinned dependencies with hashes where practical, gitleaks secret scanning, semgrep SAST gate on executable additions, an SBOM. The repo is private, so the VDP `SECURITY.md` (QC.1 scopes it to public projects) is not mandatory; add it only if the repo is later made public.
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
    ec2_deploy/           # 2x EC2 + RDS stand-up, deploy-time artifact fetch (Phase 5)
    runpod/               # pod lifecycle + reaper (Phase 1)
  requirements/
    base.txt dev.txt train.txt serve.txt   # pinned per surface (Phase 0+)
  .github/workflows/
    ci.yml                # lint + tests gate on PR (Phase 4)
    runpod-reaper.yml     # scheduled TTL reaper (Phase 1)
  tests/
    unit/
    integration/
  docs/
  pyproject.toml
  README.md               # operator guide, finalized Phase 5
  MODEL_CARD.md           # metrics + provenance, drafted Phase 1, final Phase 5
  SECURITY.md             # VDP (Phase 5, only if repo goes public)
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

**Dataset preparation (Phase 0 → Phase 1).**
```python
# model/data/prepare.py
@dataclass(frozen=True)
class SplitConfig:
    seed: int = 42
    test_size: float = 0.15
    n_folds: int = 5

@dataclass(frozen=True)
class DatasetBundle:
    train_df: "pd.DataFrame"          # deduped, contains comment_text + 6 label columns
    test_df: "pd.DataFrame"           # locked 15% held-out
    fold_indices: list[tuple["np.ndarray", "np.ndarray"]]  # (train_idx, val_idx) into train_df
    data_version: str                 # sha256 over sorted deduped ids + config

def prepare_dataset(raw_csv: "Path", config: SplitConfig) -> DatasetBundle: ...
```

**Model interface (Phase 1 produces artifacts; Phase 2/Phase 3 load).**
```python
# both models satisfy this shape
def predict_proba(texts: list[str]) -> "np.ndarray": ...   # shape (len(texts), 6), columns ordered by LABELS
```

**Safe loader (Phase 2).**
```python
# backend/model_loader.py
@dataclass
class LoadedModel:
    model_version: str                # e.g. "toxic-clf:v3@sha256:abcd..."
    def predict_proba(self, texts: list[str]) -> "np.ndarray": ...

def load_model(artifact_path: "Path", expected_sha256: str) -> LoadedModel: ...
# verifies SHA-256, skops.io.load with a trusted allowlist, fails closed on mismatch
```

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
class LabelScore(BaseModel):
    prob: float
    flag: bool

class PredictionResponse(BaseModel):
    request_id: str
    model_version: str
    labels: dict[str, LabelScore]     # keys == LABELS
    decision: str                     # "allow" | "review" | "block"
    max_prob: float
    latency_ms: int
```

**Database writes (Phase 2 defines; Phase 3 consumes).**
```python
# backend/db.py
def insert_prediction(session, response: PredictionResponse, input_text: str) -> None: ...
def enqueue_review(session, request_id: str) -> None: ...
def fetch_pending_reviews(session, limit: int) -> list["ReviewRow"]: ...
def write_distilbert_probs(session, request_id: str, probs: dict[str, float]) -> None: ...
def submit_review(session, request_id: str, reviewer_labels: dict[str, int], reviewer_id: str) -> None: ...
```

**W&B artifact naming.** Classical artifact `toxic-clf`, DistilBERT artifact `toxic-distilbert`. Versions `:vN`. The pinned digest travels as `@sha256:...` in the model card and the deploy env var `MODEL_DIGEST`.

## Phase Dependency Graph

```
Phase 0 (data + firewall + contract)
   -> Phase 1 (train, register, promote, ONNX)  [needs: Jigsaw data, W&B, RunPod]
        -> Phase 2 (FastAPI + safe load + RDS)   [needs: promoted classical artifact + digest, thresholds.json]
             -> Phase 3 (UI + monitoring + rescorer)  [needs: /predict, DB schema, ONNX DistilBERT artifact]
                  -> Phase 4 (tests consolidation + CI gate)  [runs the suites built across 0-3]
                       -> Phase 5 (Docker + 2x EC2 deploy + docs + AIBOM)  [needs: all images, digests, measured ONNX throughput]
```

Each phase produces a working, testable increment and lands on its own feature branch merged by PR.

---

## Phase 0: Repo scaffold, deterministic data pipeline, leakage firewall, seed hygiene

**Deliverable:** A reproducible offline data pipeline that turns raw Jigsaw into deduplicated, iteratively-stratified, locked splits with a `data_version` hash, plus the label constants, the output contract types, seed-hygiene utilities, and an executable firewall gate. No cloud, no model training. Runs and tests fully on a laptop against a small synthetic fixture.

**Branch:** `feat/phase-0-data-firewall`. **Detailed plan:** `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md`.

**Files:** `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, `model/labels.py`, `model/contract.py`, `model/seeds.py`, `model/data/{load,dedup,split,prepare,firewall_check}.py`, `tests/unit/test_*`, `tests/fixtures/mini_jigsaw.csv`, `Makefile`.

**Interfaces produced:** `LABELS`, `SplitConfig`, `DatasetBundle`, `prepare_dataset()`, `PredictionResponse`/`LabelScore`, `set_all_seeds()`, `run_metadata()`.

**Tasks (right-sized, each ends testable):**
1. Project scaffold: `pyproject.toml` (ruff + pytest config), pinned `requirements/base.txt` + `requirements/dev.txt`, package skeleton with `__init__.py`, `Makefile` targets (`lint`, `test`, `data`), `.env.example`. Test: `ruff check` clean, `pytest` collects.
2. `model/labels.py`: ordered `LABELS` tuple. Test: length 6, exact order, immutability.
3. `model/data/load.py`: `load_raw(csv) -> DataFrame`, validates the `id` + `comment_text` + six label columns, rejects nulls in labels and values outside {0,1}. Test against `mini_jigsaw.csv` and a malformed fixture.
4. `model/data/dedup.py`: `dedup(df) -> DataFrame` runs before any split. Normalize text (NFKC, lowercase, collapse whitespace) then drop exact-normalized duplicates; near-duplicate collapse via MinHash LSH (datasketch) over char shingles with a documented Jaccard threshold. Deterministic. Test: known duplicates and near-duplicates collapse, distinct rows survive, dedup is idempotent.
5. `model/data/split.py`: `make_splits(df, config) -> (train_df, test_df, fold_indices)` using `MultilabelStratifiedShuffleSplit` for the locked 15% test and `MultilabelStratifiedKFold` for folds. Test: every label including `threat` present in test and every fold; test size ~15%; same seed reproduces identical ids; no id overlaps train and test.
6. `model/seeds.py`: `set_all_seeds(seed)` (random, numpy, torch guarded, PYTHONHASHSEED note), `run_metadata()` returns git SHA + seed + timestamp. Test: seeded RNG determinism; metadata carries a git SHA.
7. `model/contract.py`: pydantic `LabelScore`, `PredictionResponse` with a validator that `labels` keys equal `LABELS` and `decision` is one of allow/review/block. Test: the spec example JSON validates; wrong decision and wrong label keys are rejected.
8. `model/data/prepare.py` + `model/data/firewall_check.py`: `prepare_dataset()` wires load → dedup → split and computes `data_version`; `assert_no_leakage(bundle)` asserts no train/test id overlap and no cross-split near-dup. `Makefile` `data` target runs prepare then the firewall check. Test: `prepare_dataset` is deterministic (same `data_version` twice); firewall check passes on clean data and raises on an injected leak.

**Test strategy:** Pure unit tests against a committed synthetic `mini_jigsaw.csv` (about 60 rows, all six labels represented, a few planted duplicates and near-duplicates). No network. Determinism asserted by running prepare twice and comparing `data_version`.

**Exit criteria:** `make data` produces an identical `data_version` on repeat runs; `assert_no_leakage` passes; every Phase 0 test green; `ruff check` clean; merged via PR.

**Enforcement skills:** `building-deterministic-data-pipelines`, `enforcing-leakage-firewall`, `auditing-train-test-split`, `enforcing-seed-hygiene`, `deduplicating-records`, `auditing-data-quality`.

**External prerequisites:** none (synthetic fixture). Real Jigsaw CSV needed only when Phase 1 trains.

---

## Phase 1: Train classical + sweep + DistilBERT fine-tune on RunPod; W&B registry; promote; export ONNX

**Deliverable:** Two registered W&B artifacts with digests and metric-bearing model cards: the classical winner promoted to Production, and DistilBERT as an ONNX int8 challenger. A `thresholds.json` artifact tuned on validation. Held-out test evaluated exactly once. RunPod cost governance active.

**Branch:** `feat/phase-1-train-register`. **Needs:** the Kaggle Jigsaw archive (`julian3833/jigsaw-multilingual-toxic-comment-classification`); use the `jigsaw-toxic-comment-train.csv` English six-label file inside it, not the multilingual `validation.csv`/`test.csv` or `jigsaw-unintended-bias-train.csv`. A new W&B project, RunPod API key + budget cap, GPU pods. Credentials live on the Jetson; AWS region us-west-1. See Resolved Decisions.

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

**Files:** `backend/schemas.py`, `backend/model_loader.py`, `backend/policy.py`, `backend/db.py`, `backend/app.py`, `backend/Dockerfile`, `requirements/serve.txt`, `tests/unit/test_policy.py`, `tests/unit/test_model_loader.py`, `tests/integration/test_api_roundtrip.py`.

**Interfaces produced:** the three RDS tables (`predictions`, `review_queue`, `feedback`), `load_model()`, `decide()`, DB write functions, the live `/predict` `/health` endpoints.

**Tasks:**
1. `backend/schemas.py`: `PredictRequest{text}`, reuse `model/contract.py` for the response.
2. `backend/model_loader.py`: verify SHA-256 against `expected_sha256`, `skops.io.load` with a trusted allowlist, return `LoadedModel` with `model_version` and `predict_proba`. Fail closed on mismatch. Test with a fixture skops artifact and a tampered one.
3. `backend/policy.py`: `decide(probs, thresholds)` sets per-label flags and the allow/review/block decision (block on high-confidence severe labels, review on near-threshold or any toxic flag, else allow). Deterministic, unit tested at boundaries.
4. `backend/db.py`: SQLAlchemy models for the three tables, idempotent create, `insert_prediction`, `enqueue_review`. DSN from env.
5. `backend/app.py`: `POST /predict` (validate → `predict_proba` → `decide` → build `PredictionResponse` → persist + maybe enqueue → return with `latency_ms`), `GET /health` (model version + DB readiness). `request_id` is a uuid.
6. `backend/Dockerfile`: CPU serve image on pinned `requirements/serve.txt`, artifact path + digest as build/deploy args.
7. Integration tests: `/predict` and `/health` against a test Postgres, a full prediction-to-DB round trip.

**Test strategy:** Unit for policy and loader (including the fail-closed path). Integration spins a Postgres (testcontainers or a CI service) and asserts a `/predict` call returns contract-valid JSON, writes one `predictions` row, and enqueues a `review_queue` row when policy says review.

**Exit criteria:** `/predict` returns contract-valid JSON and persists a row; policy enqueues review rows correctly; `/health` reports version + DB ok; a tampered artifact refuses to load; integration tests green.

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

**Deliverable:** One Docker image per component, a docker-compose local full stack, deploy scripts that stand up EC2 #1 (backend + frontend) and EC2 #2 (monitoring + rescorer) with Docker and pull pinned model artifacts by digest at deploy (verify SHA-256, bake), an RDS Postgres, and the finished operator docs: `README.md`, `MODEL_CARD.md`, CycloneDX `aibom.json`, an `input_text` retention purge job, and a rollback plan (plus a `SECURITY.md` VDP only if the repo becomes public).

**Branch:** `feat/phase-5-deploy-docs`. **Needs:** all component images, W&B digests, measured ONNX int8 throughput (drives EC2 #2 sizing).

**Files:** `infra/docker-compose.yml`, `infra/ec2_deploy/*` (provision + `fetch_artifacts.sh` deploy-time digest-verified fetch + run), finalized `MODEL_CARD.md`, `aibom.json`, `README.md`, `SECURITY.md`, `infra/ec2_deploy/ROLLBACK.md`.

**Tasks:**
1. `infra/docker-compose.yml`: backend, frontend, monitoring, rescorer, postgres with mounted/baked artifacts + env.
2. `infra/ec2_deploy/`: provision two EC2 + RDS (scripted or documented), install Docker, deploy-time artifact fetch from W&B by digest with SHA-256 verify and bake, run per host, least-privilege security groups (EC2 #1 public API, EC2 #2 restricted) and IAM. Resolve EC2 #2 instance class from the measured ONNX throughput.
3. Finalize `MODEL_CARD.md` + generate CycloneDX `aibom.json`, verify it scores 100% on the AIBOM evaluator. (`writing-model-cards`.)
4. `README.md` operator guide: architecture, run locally, deploy, endpoints, input_text retention policy, reviewer auth note, cost governance. (`writing-repo-documentation`.)
5. `input_text` retention purge: a scheduled job (cron or a small container) that nulls `predictions.input_text` older than `INPUT_TEXT_RETENTION_DAYS` (default 30), keeping the rest of the row for monitoring. Note it in the model card and README.
6. Dependency SBOM. (`generating-sbom`.)
7. Rollback plan. (`building-rollback-plan`; optional `building-canary-rollout`.)
8. Conditional `SECURITY.md` VDP + coordinated disclosure, only if the repo is made public (it is private now). (`writing-vdp-and-coordinated-disclosure`.)

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

1. **Accounts and secrets.** A new W&B project. Credentials (W&B entity, RunPod API key, AWS creds) live on the Jetson, which is the operator and build box: it holds the creds and runs training and deploy. AWS region us-west-1 (N. California). Provisioning happens on the Jetson when we proceed. **Assumption:** the runtime target stays AWS EC2 + RDS per the locked architecture; the Jetson is the control and build machine, not the runtime host. (Say so if you meant the runtime to live on the Jetson; that would contradict the locked "100% AWS runtime.") The Jetson is aarch64, which pairs cleanly with the Graviton (arm64) instances below: build arm64 images on the Jetson, run them on Graviton EC2 directly.
2. **Jigsaw data.** Kaggle download: `https://www.kaggle.com/api/v1/datasets/download/julian3833/jigsaw-multilingual-toxic-comment-classification`. That archive packages several Jigsaw competitions. Use the file `jigsaw-toxic-comment-train.csv` inside it: the original English Toxic Comment Classification Challenge training set with the six labels (`id, comment_text, toxic, severe_toxic, obscene, threat, insult, identity_hate`), which matches the loader's `REQUIRED_COLUMNS`. Do not train on `jigsaw-unintended-bias-train.csv` (different schema) or `validation.csv` / `test.csv` (multilingual, single label); either would break the English six-label scope.
3. **Reviewer auth.** Single reviewer role behind a shared secret. Confirmed.
4. **input_text retention (recommendation).** Retain raw `predictions.input_text` for 30 days, then a scheduled purge nulls `input_text` while keeping the rest of the row (probabilities, decision, flags, timestamps, latency) for long-term monitoring. Rationale: the human-review queue and the drift and accuracy windows need the raw text short-term; keeping user comments (potentially toxic or PII-bearing) indefinitely is an avoidable privacy liability. Configurable via `INPUT_TEXT_RETENTION_DAYS` (default 30). Never write raw text to W&B or application logs; only the access-restricted RDS row holds it. Documented in the model card and README, implemented as a purge job in Phase 5.
5. **Partner.** Solo. No partner attribution.
6. **Repo visibility.** Private. The VDP `SECURITY.md` requirement (QC.1, scoped to public projects) is not mandatory. gitleaks, semgrep, and the SBOM stay. Add `SECURITY.md` only if the repo is later made public.
7. **Python.** 3.11, confirmed (pinned `requires-python = ">=3.11,<3.12"`).
8. **Phase plan expansion.** Just-in-time per phase (Phase 0 detailed now; 1-5 expanded at the start of each phase). Unchanged.

### AWS instance sizing (us-west-1, low spend, class project)

Recommendation. All Graviton (arm64) to match the aarch64 Jetson build box and to cut roughly 20% off the equivalent x86 on-demand rate. Every dependency (numpy, scipy, scikit-learn, onnxruntime, pydantic) ships aarch64 wheels; skops and datasketch are pure Python. If any wheel misbehaves on arm64, the x86 fallback is in the last column.

| Component | Recommended | vCPU / RAM | Why | x86 fallback |
|---|---|---|---|---|
| EC2 #1 (FastAPI classical + Streamlit) | `t4g.medium` | 2 / 4 GB | Low-traffic bursty web serving; 4 GB holds the word + char n-gram TF-IDF vocab, the model, and Streamlit with headroom | `t3.medium` |
| EC2 #2 (monitoring + DistilBERT ONNX int8 re-scorer) | `t4g.large` (start) | 2 / 8 GB | Async, intermittent queue drain suits a burstable instance; 8 GB covers onnxruntime + tokenizer + dashboard. Confirm or upsize to `c7g.xlarge` (4 vCPU) after measuring ONNX int8 throughput (spec section 18) if the drain is slow or the queue is large and continuous | `c6i.large` / `c6i.xlarge` |
| RDS Postgres | `db.t4g.micro` | 2 / 1 GB | Tiny predictions table, low write rate, a few time-bucket aggregations and one join. Bump to `db.t4g.small` (2 GB) if the dashboard queries lag | same (RDS engine is arch-agnostic to clients) |

RDS config: Postgres 16, 20 GB gp3 (minimum, cheap), Single-AZ (no Multi-AZ for a class project; it halves the cost), us-west-1.

**Cost control matters more than instance class here.** Stop both EC2 instances and the RDS instance between work sessions; run them only during development and the demo. Approximate us-west-1 on-demand rates (verify in the AWS Pricing Calculator, do not treat as exact): EC2 #1 around $0.037/hr, EC2 #2 around $0.074/hr, RDS around $0.017/hr, so roughly $0.13/hr with everything on. Left running 24/7 that is roughly $90/month; run only during sessions (tens of hours total) and it is a few dollars. gp3 and the 20 GB RDS volume persist at cents/month when stopped. Set a small AWS Budget alarm.

## Execution Handoff

Phase 0 is fully detailed and ready to execute in `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md`. Two execution options:

1. **Subagent-Driven (recommended):** a fresh subagent per task with review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** execute tasks in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.
