# Phase 3: User UI, Reviewer UI, Monitoring Dashboard, Feedback, and the DistilBERT Re-scorer

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The three graded front-of-house components — a user-facing Streamlit UI that submits text and shows the decision, a reviewer UI that drains the review queue, and a monitoring dashboard on its own EC2 that answers "is the model still working" from the database — plus the feedback loop that makes live accuracy a real number and a severable DistilBERT re-scorer that gives the reviewer a second opinion.

**Architecture:** Four processes, three tiers, one write path.

```
 browser ──8501──> frontend/ui.py  (user)      ─┐
 operator ─8503──> frontend/reviewer.py        ─┤ HTTP only. No DB credentials.
                                                │
                                                └─8000─> backend (Phase 2 app + Phase 3 router)
                                                                    │ read/write
 grader ──8502──> monitoring/dashboard.py ──── read-only ──────> RDS Postgres
                                                                    │ read/write
 rescorer/worker.py (severable, no ingress) ────────────────────────┘
```

Three decisions shape everything below.

1. **No UI container holds a database write credential.** The user UI and the reviewer UI reach Postgres only through the backend API. The premortem's H12 and H16 both turn on "the frontend holds direct RDS write access to the graded metric behind one shared secret"; routing every write through the backend deletes that sentence rather than mitigating it. The monitoring dashboard is the single exception, because rubric 3.2 says the dashboard "connects to the cloud database" — and it connects with a **read-only** role and issues only `SELECT`.
2. **The reviewer UI is a different port on a different security group from everything the demo toggle opens.** Opening 8501 for a grader must not open the console that writes the graded metric.
3. **The dashboard has a data source.** `make seed-demo` replays the locked held-out Jigsaw split through `/predict` with back-dated timestamps, so latency-over-time spans ≥7 daily buckets, target drift compares against a stored reference, and live accuracy is estimated over ≥200 human-labelled items instead of dividing by zero.

**Tech Stack:** Python 3.11, Streamlit 1.39, FastAPI (router extension of the Phase 2 app), httpx, SQLAlchemy 2.0 + psycopg 3, Postgres 16, numpy/scipy/pandas, altair (bundled with Streamlit), onnxruntime + tokenizers (re-scorer only, lazily imported), pytest, ruff, Docker + docker compose.

## Global Constraints

Inherited from the master roadmap and the delivery spec (`docs/superpowers/specs/2026-07-30-delivery-plan-design.md`, which governs on conflict). The ones that bind Phase 3:

- **Labels ordered exactly:** `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`. Never re-derived by an ad-hoc `zip()`; the single adapter is `probs_to_dict` (premortem H23).
- **Three EC2 instances** (owner decision): backend, frontend, monitoring, each separate. EC2 #2 runs both Streamlit UIs on two ports with two security groups.
- **Streamlit never uses `unsafe_allow_html` for user content, and renders the comment verbatim** — `st.text`, never markdown. Markdown processing means the reviewer labels a *different string* than the classifier scored, which is attacker-controlled ground-truth poisoning that satisfies a naive "no `unsafe_allow_html`" check completely (delivery spec §6.3).
- **`reviewer_id` is derived server-side from the authenticated session.** The submit request body has no `reviewer_id` field at all (delivery spec §6.3).
- **`review_queue` is depth-capped and per-source rate-limited**, and `review_queue.source` distinguishes `flagged` from `random-audit` so live accuracy is not structurally blind to confidently-allowed false negatives (delivery spec §6.4).
- **`review_queue.input_text_snapshot` copies the comment at enqueue time**, because the 30-day retention purge nulls `predictions.input_text` (delivery spec §6.4).
- **Screenshots must not capture raw user text.** The dashboard therefore never selects `input_text` or `input_text_snapshot` (delivery spec §6.4).
- **The re-scorer sits behind the cut-line** (delivery spec §8). Everything else in this phase must be green with the re-scorer absent, disabled, and un-installed.
- **AWS Academy Learner Lab is dead.** No `LabRole`, no pasted STS credentials, no `us-east-1`, no `vockey`, no x86 `t3`. Region `us-west-2`, Graviton `t4g`.
- Pinned dependencies (`==`), hashed lock, feature-branch and PR, human author (`rocklambros <rock@rockcyber.com>`), **no AI attribution in commits, code, or docs**.

**Branch:** `feat/phase-3-ui-monitoring-rescorer` off `main`.

## File Structure

```
backend/
  schema_phase3.py     # idempotent DDL for the sampling-design and provenance columns
  fingerprint.py       # submitter_fp: the rate-limit key the schema did not have
  queue_guard.py       # review-queue depth cap + per-source quota; user-feedback quota
  feedback.py          # derive_feedback / FeedbackRecord / insert_feedback
  reviewer_auth.py     # HMAC session token; reviewer_id from server config only
  review_api.py        # APIRouter: /review/login /review/pending /review/submit /feedback/user
frontend/
  api_client.py        # BackendClient (httpx). The UIs' only I/O.
  render.py            # render_comment: verbatim, never markdown, never HTML
  ui.py                # user view, port 8501
  reviewer.py          # reviewer view, port 8503
  Dockerfile           # user UI
  Dockerfile.reviewer  # reviewer UI
monitoring/
  stats.py             # wilson_interval, horvitz_thompson_accuracy, psi, js_divergence
  baseline.py          # baseline_flag_rates.json + thresholds.json loaders (fail closed)
  queries.py           # latency_over_time, flag_rate_series, drift_report, live_accuracy,
                       # user_feedback_panel — all SELECT-only
  dashboard.py         # Streamlit, port 8502, read-only DSN
  Dockerfile
rescorer/
  challenger.py        # digest + problem_type + id2label + logit-parity gate
  onnx_session.py      # lazy onnxruntime/tokenizers adapter (import cost isolated here)
  worker.py            # idempotent batched drain via probs_to_dict
  Dockerfile
scripts/
  export_heldout.py    # writes data/heldout.csv from the locked Phase 0 test split
  seed_demo.py         # the dashboard's data source
infra/
  __init__.py
  exposure.py          # port -> exposure class contract (single Python source of truth)
  terraform/app_ingress.tf   # app-tier security groups; A2 consumes, must not redefine
  docker-compose.yml   # local full stack; rescorer behind a compose profile
  postgres-init/01-create-test-db.sql
requirements/
  ui.txt monitor.txt rescorer.txt   # per-surface pins; dev.txt includes ui + monitor
tests/
  fixtures/challenger_ok/, challenger_bad_objective/
  unit/test_stats.py test_drift.py test_fingerprint.py test_queue_guard.py
      test_feedback.py test_reviewer_auth.py test_render.py test_no_unsafe_html.py
      test_exposure_contract.py test_challenger.py test_severability.py
  integration/conftest.py test_schema_phase3.py test_review_api.py test_user_feedback.py
      test_queries.py test_seed_demo.py test_rescorer_drain.py test_end_to_end.py
```

## Interfaces Produced

```python
# backend/schema_phase3.py
def apply_phase3_schema(engine: "Engine") -> None: ...          # idempotent, safe to re-run

# backend/fingerprint.py
def caller_identity(peer_ip: str, session_fp_header: str | None, api_key_ok: bool) -> str: ...
def submitter_fp(identity: str, day: "date", key: bytes) -> str: ...   # 16 lowercase hex chars

# backend/queue_guard.py
@dataclass(frozen=True)
class AdmissionConfig:
    max_pending: int = 500
    max_pending_per_source: int = 20
    window_seconds: int = 3600
    max_enqueues_per_source_per_window: int = 30
    max_user_feedback_per_source_per_window: int = 20
    user_feedback_window_seconds: int = 86400

@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str            # "ok" | "queue_full" | "source_quota" | "duplicate" | "expired" | "unknown_request"

def admit_review(conn, *, request_id: str, source: str, submitter_fp: str | None,
                 now: "datetime", config: AdmissionConfig) -> Admission: ...
def admit_user_feedback(conn, *, request_id: str, submitter_fp: str | None,
                        now: "datetime", config: AdmissionConfig) -> Admission: ...

# backend/feedback.py
@dataclass(frozen=True)
class FeedbackRecord:
    request_id: str
    source: str                    # "user" | "reviewer"
    reviewer_id: str | None
    agreement: dict[str, bool]     # {} for source="user"
    exact_match: bool

def derive_feedback(request_id: str, reviewer_labels: dict[str, int],
                    model_flags: dict[str, bool], reviewer_id: str) -> FeedbackRecord: ...
def user_feedback(request_id: str, verdict: str) -> FeedbackRecord: ...   # verdict in {"agree","disagree"}
def insert_feedback(conn, record: FeedbackRecord, ts: "datetime | None" = None) -> None: ...

# backend/reviewer_auth.py
def issue_session_token(now: "datetime", secret: str, reviewer_id: str, ttl_seconds: int = 43200) -> str: ...
def current_reviewer(token: str | None, now: "datetime", secret: str, reviewer_id: str) -> str | None: ...

# backend/review_api.py
router: "APIRouter"      # mounted by backend/app.py via app.include_router(router)

# monitoring/stats.py
def wilson_interval(successes: float, n: float, z: float = 1.959963984540054) -> tuple[float, float]: ...
@dataclass(frozen=True)
class StratumStat:
    stratum: str
    n: int
    correct: int
    sample_rate: float
    accuracy: float | None
    lo: float | None
    hi: float | None

@dataclass(frozen=True)
class AccuracyReport:
    n: int
    point: float | None            # Horvitz-Thompson estimate; None when n == 0
    lo: float | None
    hi: float | None
    effective_n: float
    strata: list[StratumStat]

def horvitz_thompson_accuracy(rows: "Iterable[tuple[str, float, bool]]") -> AccuracyReport: ...
def psi(p_ref: float, p_prod: float, eps: float = 1e-6) -> float: ...
def js_divergence(p_ref: float, p_prod: float, eps: float = 1e-12) -> float: ...

# monitoring/baseline.py
@dataclass(frozen=True)
class Baseline:
    schema_version: int
    data_version: str
    model_version: str
    n: int
    flag_rates: dict[str, float]

class BaselineMissingError(RuntimeError): ...
class BaselineContractError(RuntimeError): ...

def load_baseline(path: "Path") -> Baseline: ...
def load_thresholds(path: "Path") -> dict[str, float]: ...

# monitoring/queries.py
@dataclass(frozen=True)
class LatencyBucket:
    bucket: "datetime"
    n: int
    p50: float
    p95: float

@dataclass(frozen=True)
class DriftRow:
    label: str
    baseline_rate: float
    production_rate: float
    psi: float
    js: float
    alert: bool

@dataclass(frozen=True)
class UserPanel:
    n: int
    agree: int
    rate: float | None
    lo: float | None
    hi: float | None

def latency_over_time(conn, since: "datetime") -> list[LatencyBucket]: ...
def flag_rate_series(conn, since: "datetime", thresholds: dict[str, float]) -> "pd.DataFrame": ...
def drift_report(conn, since: "datetime", thresholds: dict[str, float],
                 baseline: Baseline, alert_psi: float = 0.2) -> list[DriftRow]: ...
def live_accuracy(conn, since: "datetime") -> AccuracyReport: ...
def user_feedback_panel(conn, since: "datetime") -> UserPanel: ...

# rescorer/challenger.py
class ChallengerContractError(RuntimeError): ...
class Challenger:
    def predict_proba(self, texts: list[str]) -> "np.ndarray": ...   # (n, 6) ordered by LABELS
def load_challenger(artifact_dir: "Path", expected_sha256: str, *, session=None,
                    tokenizer=None) -> Challenger: ...

# rescorer/worker.py
def drain_once(conn, challenger: Challenger, batch_size: int = 16) -> int: ...

# scripts/seed_demo.py
@dataclass(frozen=True)
class SeedConfig:
    n: int = 2000
    days: int = 14
    seed: int = 42
    audit_rate: float = 0.10
    user_feedback_fraction: float = 0.08

@dataclass(frozen=True)
class SeedReport:
    predictions: int
    buckets: int
    flagged: int
    audited: int
    reviewed: int
    user_feedback: int
    labels_with_flags: int

def replay(conn, rows: list["SeedRow"], predict: "Callable[[str], dict]",
           config: SeedConfig, now: "datetime") -> SeedReport: ...

# infra/exposure.py
PORTS: dict[str, "Port"]
DEMO_EXPOSED_PORTS: frozenset[int]
OPERATOR_ONLY_PORTS: frozenset[int]
```

## Interfaces Consumed

These must exist when Phase 3 starts. Each is guarded by a test in this plan that fails loudly — never skips — if the seam is missing or renamed.

| From | Interface | Guarded by |
|---|---|---|
| Phase 0 | `model.labels.LABELS` | every task |
| Phase 0 | `model.contract.probs_to_dict(row: np.ndarray) -> dict[str, float]` (premortem H23 / Tier-1 item 1.8) | Task 20 Step 1 |
| Phase 0 | `model.contract.PredictionResponse` | Task 8 |
| Phase 0 | `model.data.prepare.prepare_dataset`, `SplitConfig` | Task 17 |
| Phase 1 | `artifacts/thresholds.json` — `{label: float}` for each label in `LABELS` | Task 3 |
| Phase 1 | `artifacts/baseline_flag_rates.json` — shape pinned in Task 3 | Task 3 |
| Phase 1 | challenger artifact dir: `model.onnx`, `config.json`, `tokenizer.json`, `parity.json` | Task 19 |
| Phase 2 | `backend.db.init_db(engine) -> None` (idempotent create of the three tables) | Task 1 |
| Phase 2 | `backend.app.app` (FastAPI instance) with `app.include_router(...)` available | Task 8 |
| Phase 2 | `predictions` columns `request_id, ts, input_text, model_version, prob_<label> ×6, decision, max_prob, latency_ms` | Task 1 Step 1 |
| Phase 2 | `POST /predict` with `X-API-Key`, and an input size cap | Task 10 |

## Interface Contract Corrections (premortem H24)

The master plan's Interface Contracts block is authoritative for type seams, and it has drifted. Where Phase 3 touches it, these supersede — apply them to `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` in Task 22.

| Master-plan text | Correction | Why |
|---|---|---|
| `def enqueue_review(session, request_id: str) -> None` | `admit_review(conn, *, request_id, source, submitter_fp, now, config) -> Admission` | The queue must be depth-capped, per-source rate-limited, and must record its sampling stratum and inclusion probability at enqueue time (delivery spec §6.4, premortem H8) |
| `def submit_review(session, request_id, reviewer_labels, reviewer_id) -> None` | `POST /review/submit {request_id, labels}` — no `reviewer_id` in the body, derived from the verified session token | A client-supplied reviewer identity is unauthenticated attribution (delivery spec §6.3) |
| `def fetch_pending_reviews(session, limit) -> list[ReviewRow]` | `GET /review/pending?limit=` returning rows with `status IN ('pending','rescored')` | The UI holds no DB credential, and the reviewer must still work when the re-scorer is cut (premortem C8, H12, H16) |
| `def write_distilbert_probs(session, request_id, probs) -> None` | `drain_once(conn, challenger, batch_size) -> int`, guarded by `WHERE status='pending'` and `FOR UPDATE SKIP LOCKED` | Row-at-a-time writes are neither batched nor idempotent, both of which the delivery spec §10 requires |
| `feedback` described only as "derived from reviewer truth versus prediction" | `feedback(request_id, ts, source, reviewer_id, agreement jsonb, exact_match bool)` with `source IN ('user','reviewer')` | Rubric 3.2 grades **user** feedback, which the design collected nowhere (premortem H9) |

## Design Notes That Bind Implementation

**Why flags are recomputed, not stored.** Target drift needs per-label flag rates. The `predictions` table stores calibrated probabilities, not flags. Phase 3 recomputes flags in SQL as `prob_<label> >= thresholds[<label>]` using the same pinned `thresholds.json` that produced `baseline_flag_rates.json`, so the reference series and the production series always share one decision rule — which is what makes a PSI comparison mean anything. The trade-off, stated because it is real: changing thresholds rewrites the historical series. That is why `thresholds.json` is pinned per model version and its SHA-256 is displayed in the dashboard footer.

**Why user feedback cannot move the graded accuracy number.** Horvitz–Thompson is unbiased only when every unit's inclusion probability is known by design. `flagged` (π = 1.0) and `random-audit` (π = `RANDOM_AUDIT_RATE`) are a valid stratified Bernoulli design. A self-selected user verdict has *unknown* inclusion probability, so pooling it would re-introduce exactly the bias H8 exists to remove — and it would hand an anonymous internet visitor a write path into the graded metric. Therefore:

- `feedback.source='user'` rows are **excluded** from the live-accuracy estimator and shown in their own panel with their own n and Wilson interval.
- A user *disagreement* enqueues the item for human review with `review_queue.source='user-report'` and `sample_rate = NULL`, so a human can label it — and `sample_rate IS NULL` keeps it out of the estimator until it is reviewed under a known design. If the item was already enqueued as `flagged` or `random-audit`, its π is untouched.
- Rubric 3.2's "mechanism to collect user feedback to calculate live accuracy" is satisfied by the control, the rows, the panel, and the referral path into review — not by letting anonymous clicks arithmetically move the number.

**Resolving the per-source rate-limit key, and weighing it against the privacy posture.** The premortem is right that `review_queue` has no submitter identifier, so "per-source rate limit" had nothing to key on. The resolution:

- `submitter_fp` = the first 16 hex characters of `HMAC-SHA256(key, f"{identity}|{utc_date}")`, stored on `predictions`.
- `identity` is the TCP peer address for direct API callers, and for traffic proxied by the user UI it is the frontend's **server-side** session token (a 32-byte value minted into `st.session_state`, never sent to the browser), carried in `X-Session-Fp` and accepted only from a caller that also presents the frontend's API key. A client-supplied `X-Forwarded-For` is never trusted.
- `key` is 32 random bytes generated per deploy, held in Secrets Manager, never logged, never committed.

Weighed against the project's privacy posture (30-day `input_text` purge, public repo, `SECURITY.md`): the stored value is a truncated keyed digest that cannot be reversed to an address without the key, is not linkable across days because the UTC date is inside the message, is not linkable across deploys because the key rotates, and is purged on the same 30-day TTL as `input_text`. It carries strictly less information than an ordinary web access log. The alternative — no key, therefore no per-source limit — leaves an unbounded anonymous write path into a graded metric on an internet-facing endpoint, and an unbounded queue of user-submitted toxic text is itself the larger data-protection problem. Documented in the README and the model card.

---

### Task 1: Sampling-design and provenance columns (premortem H8, delivery spec §6.4)

The estimator in Task 15 needs `sample_rate` **stored at enqueue time**, not reconstructed later. The reviewer needs `input_text_snapshot`. The seeder needs `is_seed`. The rate limiter needs `submitter_fp`. All of them are `ALTER`s on Phase 2's tables, applied by an idempotent migration so the two phases can land in either order.

**Files:**
- Create: `backend/schema_phase3.py`, `tests/integration/conftest.py`, `tests/integration/__init__.py`, `tests/integration/test_schema_phase3.py`, `infra/postgres-init/01-create-test-db.sql`
- Modify: `Makefile`, `requirements/dev.txt`

- [ ] **Step 1: Write the failing test**

`infra/postgres-init/01-create-test-db.sql`:
```sql
CREATE DATABASE toxic_test;
```

`tests/integration/conftest.py`:
```python
"""Integration fixtures. A missing database FAILS the suite; it never skips it.

A skipped integration test is how a normative requirement quietly stops being enforced,
which is the failure mode this whole phase exists to prevent.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from backend.schema_phase3 import apply_phase3_schema

DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5433/toxic_test",
)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(DSN, future=True)
    with eng.connect() as probe:
        probe.execute(text("SELECT 1"))
    from backend.db import init_db  # Phase 2 seam: idempotent create of the three tables

    init_db(eng)
    apply_phase3_schema(eng)
    return eng


@pytest.fixture()
def conn(engine):
    with engine.connect() as c:
        c.execute(text("TRUNCATE TABLE feedback, review_queue, predictions CASCADE"))
        c.commit()
        yield c
        c.rollback()
```

`tests/integration/test_schema_phase3.py`:
```python
import pytest
from sqlalchemy import text

from backend.schema_phase3 import apply_phase3_schema
from model.labels import LABELS

pytestmark = pytest.mark.integration

EXPECTED_PREDICTION_COLUMNS = {
    "request_id", "ts", "input_text", "model_version", "decision", "max_prob", "latency_ms",
    *(f"prob_{label}" for label in LABELS),
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    ).scalars()
    return set(rows)


def test_phase2_predictions_contract_is_intact(conn):
    assert EXPECTED_PREDICTION_COLUMNS <= _columns(conn, "predictions")


def test_phase3_columns_exist(conn):
    assert {"is_seed", "submitter_fp"} <= _columns(conn, "predictions")
    assert {"source", "sample_rate", "input_text_snapshot"} <= _columns(conn, "review_queue")
    assert {"source", "reviewer_id", "agreement", "exact_match", "ts"} <= _columns(conn, "feedback")


def test_migration_is_idempotent(engine, conn):
    apply_phase3_schema(engine)
    apply_phase3_schema(engine)
    assert {"is_seed", "submitter_fp"} <= _columns(conn, "predictions")


def _insert_prediction(conn, request_id: str) -> None:
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.1" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms) VALUES (:rid, now(), 'x', 'm', {vals}, "
            "'allow', 0.1, 5)"
        ),
        {"rid": request_id},
    )


def test_design_stratum_without_sample_rate_is_rejected(conn):
    """H8: an unweighted pool is only possible if a row can exist without its inclusion
    probability. The database refuses."""
    _insert_prediction(conn, "r1")
    with pytest.raises(Exception, match="review_queue_sample_rate_ck"):
        conn.execute(
            text(
                "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
                "VALUES ('r1', now(), 'pending', 'flagged', NULL)"
            )
        )


def test_user_report_stratum_must_have_null_sample_rate(conn):
    _insert_prediction(conn, "r2")
    with pytest.raises(Exception, match="review_queue_sample_rate_ck"):
        conn.execute(
            text(
                "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate) "
                "VALUES ('r2', now(), 'pending', 'user-report', 1.0)"
            )
        )


def test_one_user_feedback_row_per_request(conn):
    _insert_prediction(conn, "r3")
    for _ in range(1):
        conn.execute(
            text(
                "INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                "VALUES ('r3', now(), 'user', '{}'::jsonb, true)"
            )
        )
    with pytest.raises(Exception, match="feedback_one_user_row"):
        conn.execute(
            text(
                "INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                "VALUES ('r3', now(), 'user', '{}'::jsonb, false)"
            )
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f infra/docker-compose.yml up -d postgres && .venv/bin/pytest tests/integration/test_schema_phase3.py -v -m integration`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'backend.schema_phase3'`.

- [ ] **Step 3: Write minimal implementation**

`backend/schema_phase3.py`:
```python
"""Idempotent DDL for the columns Phase 3 needs on Phase 2's tables.

Every statement is safe to re-run, so Phase 2 and Phase 3 may land in either order and a
partially-applied migration is not a broken database.

The CHECK constraints are load-bearing, not decoration. `review_queue_sample_rate_ck` is
what makes the Horvitz-Thompson estimator in monitoring/stats.py sound: a row in a design
stratum cannot exist without its inclusion probability, so the estimator can never silently
degrade to the unweighted pool the premortem (H8) found biased.
"""

from sqlalchemy import Engine

_STATEMENTS: tuple[str, ...] = (
    # --- predictions: seed provenance and the rate-limit key ---
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS is_seed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS submitter_fp CHAR(16)",
    "CREATE INDEX IF NOT EXISTS predictions_ts_idx ON predictions (ts)",
    "CREATE INDEX IF NOT EXISTS predictions_fp_ts_idx ON predictions (submitter_fp, ts)",
    # --- review_queue: the sampling design ---
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'flagged'",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS sample_rate DOUBLE PRECISION",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS input_text_snapshot TEXT",
    "ALTER TABLE review_queue ADD COLUMN IF NOT EXISTS reviewed_ts TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS review_queue_status_idx ON review_queue (status)",
    """
    DO $$ BEGIN
      ALTER TABLE review_queue ADD CONSTRAINT review_queue_source_ck
        CHECK (source IN ('flagged', 'random-audit', 'user-report'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    """
    DO $$ BEGIN
      ALTER TABLE review_queue ADD CONSTRAINT review_queue_sample_rate_ck
        CHECK (
          (source IN ('flagged', 'random-audit')
             AND sample_rate IS NOT NULL AND sample_rate > 0 AND sample_rate <= 1)
          OR (source = 'user-report' AND sample_rate IS NULL)
        );
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    # --- feedback: created here if Phase 2 has not, then pinned column by column ---
    """
    CREATE TABLE IF NOT EXISTS feedback (
      id BIGSERIAL PRIMARY KEY,
      request_id TEXT NOT NULL,
      ts TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'reviewer'",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS reviewer_id TEXT",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS agreement JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE feedback ADD COLUMN IF NOT EXISTS exact_match BOOLEAN NOT NULL DEFAULT FALSE",
    """
    DO $$ BEGIN
      ALTER TABLE feedback ADD CONSTRAINT feedback_source_ck
        CHECK (source IN ('user', 'reviewer'));
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    """
    DO $$ BEGIN
      ALTER TABLE feedback ADD CONSTRAINT feedback_reviewer_agreement_ck
        CHECK (source <> 'reviewer' OR agreement <> '{}'::jsonb);
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS feedback_one_user_row "
    "ON feedback (request_id) WHERE source = 'user'",
    "CREATE INDEX IF NOT EXISTS feedback_ts_idx ON feedback (ts)",
)


def apply_phase3_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in _STATEMENTS:
            conn.exec_driver_sql(statement)
```

Append to `requirements/dev.txt`:
```
-r ui.txt
-r monitor.txt
-r serve.txt
```

Create `requirements/monitor.txt`:
```
-r base.txt
streamlit==1.39.0
SQLAlchemy==2.0.36
psycopg[binary]==3.2.3
```

Create `requirements/ui.txt`:
```
-r base.txt
streamlit==1.39.0
httpx==0.27.2
```

Add to `Makefile`:
```makefile
TEST_DATABASE_URL ?= postgresql+psycopg://postgres:postgres@localhost:5433/toxic_test
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: db-up db-down test-integration
db-up:
	$(COMPOSE) up -d postgres
	until $(COMPOSE) exec -T postgres pg_isready -U postgres; do sleep 1; done
db-down:
	$(COMPOSE) down -v
test-integration: db-up
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) PYTHONHASHSEED=0 $(BIN)/pytest -m integration
```

- [ ] **Step 4: Run test to verify it passes**

Run: `make db-up && TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5433/toxic_test .venv/bin/pytest tests/integration/test_schema_phase3.py -v -m integration`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/schema_phase3.py tests/integration requirements Makefile infra/postgres-init
git commit -m "Add Phase 3 schema: sampling design, seed provenance, and feedback source"
```

---

### Task 2: Wilson intervals and the Horvitz-Thompson estimator (premortem H8)

H8: live accuracy pools a 100%-sampled flagged stratum with a p-sampled audit stratum and reports the ratio unweighted, which is still biased. This task is the arithmetic that fixes it, isolated from the database so the bias is provable in a unit test.

**Files:**
- Create: `monitoring/__init__.py`, `monitoring/stats.py`
- Test: `tests/unit/test_stats.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_stats.py`:
```python
import math

import pytest

from monitoring.stats import (
    AccuracyReport,
    horvitz_thompson_accuracy,
    js_divergence,
    psi,
    wilson_interval,
)


def test_wilson_matches_published_value():
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.4901, abs=1e-4)
    assert hi == pytest.approx(0.9433, abs=1e-4)


def test_wilson_on_zero_denominator_is_the_unit_interval_not_a_crash():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_is_clamped_to_zero_one():
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0
    assert 0.0 < hi < 1.0


def _stratified_rows():
    # flagged: pi = 1.0, n = 200, 120 correct (0.600)
    # random-audit: pi = 0.05, n = 20, 19 correct (0.950)
    return (
        [("flagged", 1.0, True)] * 120
        + [("flagged", 1.0, False)] * 80
        + [("random-audit", 0.05, True)] * 19
        + [("random-audit", 0.05, False)] * 1
    )


def test_horvitz_thompson_differs_from_the_unweighted_pool():
    """The whole point of H8. The unweighted pool is 0.6318; the design-weighted estimate
    is 0.8333. A pooled implementation fails this test."""
    rows = _stratified_rows()
    pooled = sum(1 for _, _, c in rows if c) / len(rows)
    report = horvitz_thompson_accuracy(rows)
    assert pooled == pytest.approx(0.63182, abs=1e-5)
    assert report.point == pytest.approx(0.83333, abs=1e-5)
    assert abs(report.point - pooled) > 0.15


def test_report_carries_per_stratum_n_and_intervals_not_a_bare_point():
    report = horvitz_thompson_accuracy(_stratified_rows())
    by_name = {s.stratum: s for s in report.strata}
    assert by_name["flagged"].n == 200
    assert by_name["flagged"].accuracy == pytest.approx(0.60)
    assert by_name["flagged"].lo == pytest.approx(0.5308, abs=1e-4)
    assert by_name["flagged"].hi == pytest.approx(0.6654, abs=1e-4)
    assert by_name["random-audit"].n == 20
    assert by_name["random-audit"].sample_rate == pytest.approx(0.05)
    assert report.effective_n == pytest.approx(43.9024, abs=1e-3)
    assert report.lo == pytest.approx(0.6975, abs=1e-3)
    assert report.hi == pytest.approx(0.9156, abs=1e-3)
    assert report.lo < report.point < report.hi


def test_empty_input_returns_none_not_nan_and_not_zero_division():
    report = horvitz_thompson_accuracy([])
    assert isinstance(report, AccuracyReport)
    assert report.n == 0
    assert report.point is None
    assert report.lo is None and report.hi is None
    assert report.strata == []


def test_missing_inclusion_probability_is_an_error_not_a_default():
    with pytest.raises(ValueError, match="sample_rate"):
        horvitz_thompson_accuracy([("flagged", None, True)])
    with pytest.raises(ValueError, match="sample_rate"):
        horvitz_thompson_accuracy([("flagged", 0.0, True)])


def test_psi_flags_a_known_shift_and_stays_quiet_on_none():
    assert psi(0.10, 0.30) == pytest.approx(0.26999, abs=1e-5)
    assert psi(0.10, 0.10) == 0.0
    assert psi(0.10, 0.13) == pytest.approx(0.00889, abs=1e-5)


def test_psi_is_finite_at_the_boundaries():
    assert math.isfinite(psi(0.0, 0.5))
    assert math.isfinite(psi(0.5, 0.0))
    assert math.isfinite(psi(0.0, 0.0))


def test_js_divergence_is_bounded_and_zero_on_identity():
    assert js_divergence(0.10, 0.10) == 0.0
    assert js_divergence(0.10, 0.30) == pytest.approx(0.04678, abs=1e-5)
    assert 0.0 <= js_divergence(0.0, 1.0) <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'monitoring.stats'`.

- [ ] **Step 3: Write minimal implementation**

`monitoring/__init__.py`: empty.

`monitoring/stats.py`:
```python
"""Estimation primitives for the monitoring dashboard.

Live accuracy is collected from two strata with different inclusion probabilities: every
flagged item is reviewed (pi = 1.0) and a fraction of the rest is audited (pi =
RANDOM_AUDIT_RATE). Reporting correct/total over the union is biased toward whichever
stratum happens to be larger. The Horvitz-Thompson estimator weights each observation by
1/pi, which is unbiased for the population mean under this design.

The interval is a Wilson score interval evaluated at Kish's effective sample size,
n_eff = (sum w)^2 / sum(w^2). Wilson is used rather than the normal approximation because
the counts here are small and the proportion sits near 1, where the Wald interval overruns
1.0 and reports an impossible upper bound on the graded screenshot.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

Z95 = 1.959963984540054


def wilson_interval(successes: float, n: float, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    p = min(max(p, 0.0), 1.0)
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class StratumStat:
    stratum: str
    n: int
    correct: int
    sample_rate: float
    accuracy: float | None
    lo: float | None
    hi: float | None


@dataclass(frozen=True)
class AccuracyReport:
    n: int
    point: float | None
    lo: float | None
    hi: float | None
    effective_n: float
    strata: list[StratumStat]


def horvitz_thompson_accuracy(
    rows: Iterable[tuple[str, float, bool]],
) -> AccuracyReport:
    materialised = list(rows)
    if not materialised:
        return AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[])

    numerator = 0.0
    denominator = 0.0
    sum_w = 0.0
    sum_w2 = 0.0
    buckets: dict[str, dict[str, float]] = {}

    for stratum, sample_rate, correct in materialised:
        if sample_rate is None or sample_rate <= 0.0 or sample_rate > 1.0:
            raise ValueError(
                f"sample_rate must be in (0, 1] for stratum {stratum!r}; got {sample_rate!r}. "
                "A reviewed row without a recorded inclusion probability cannot be weighted."
            )
        weight = 1.0 / sample_rate
        numerator += weight * (1.0 if correct else 0.0)
        denominator += weight
        sum_w += weight
        sum_w2 += weight * weight
        bucket = buckets.setdefault(stratum, {"n": 0.0, "correct": 0.0, "rate": sample_rate})
        bucket["n"] += 1
        bucket["correct"] += 1.0 if correct else 0.0

    point = numerator / denominator
    effective_n = (sum_w * sum_w) / sum_w2
    lo, hi = wilson_interval(point * effective_n, effective_n)

    strata = []
    for name in sorted(buckets):
        bucket = buckets[name]
        n = int(bucket["n"])
        correct = int(bucket["correct"])
        s_lo, s_hi = wilson_interval(correct, n)
        strata.append(
            StratumStat(
                stratum=name,
                n=n,
                correct=correct,
                sample_rate=bucket["rate"],
                accuracy=correct / n if n else None,
                lo=s_lo,
                hi=s_hi,
            )
        )

    return AccuracyReport(
        n=len(materialised),
        point=point,
        lo=lo,
        hi=hi,
        effective_n=effective_n,
        strata=strata,
    )


def psi(p_ref: float, p_prod: float, eps: float = 1e-6) -> float:
    """Population Stability Index over the two-bin distribution [p, 1-p].

    Bands are the industry-standard reading: < 0.1 no meaningful shift, 0.1-0.2 moderate,
    >= 0.2 major. `eps` floors each bin so an all-zero reference cannot produce log(0).
    """
    total = 0.0
    for ref, prod in ((p_ref, p_prod), (1.0 - p_ref, 1.0 - p_prod)):
        ref = max(ref, eps)
        prod = max(prod, eps)
        total += (prod - ref) * math.log(prod / ref)
    return total


def js_divergence(p_ref: float, p_prod: float, eps: float = 1e-12) -> float:
    """Jensen-Shannon divergence in bits over [p, 1-p]. Bounded in [0, 1]."""

    def kl(p: list[float], q: list[float]) -> float:
        total = 0.0
        for a, b in zip(p, q, strict=True):
            a = max(a, eps)
            b = max(b, eps)
            total += a * math.log2(a / b)
        return total

    ref = [p_ref, 1.0 - p_ref]
    prod = [p_prod, 1.0 - p_prod]
    mid = [(a + b) / 2.0 for a, b in zip(ref, prod, strict=True)]
    return 0.5 * kl(ref, mid) + 0.5 * kl(prod, mid)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_stats.py -v`
Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/__init__.py monitoring/stats.py tests/unit/test_stats.py
git commit -m "Add Horvitz-Thompson accuracy estimator with Wilson intervals and PSI"
```

---

### Task 3: The drift reference (rubric 3.2, delivery spec §11 drift row)

Target drift without a reference cannot answer whether anything changed. This task loads the Phase 1 `baseline_flag_rates.json` and `thresholds.json`, fails closed when either is missing or malformed, and pins their shapes as a contract.

**Files:**
- Create: `monitoring/baseline.py`, `tests/fixtures/baseline_flag_rates.json`, `tests/fixtures/thresholds.json`
- Test: `tests/unit/test_drift.py`

- [ ] **Step 1: Write the failing test**

`tests/fixtures/baseline_flag_rates.json`:
```json
{
  "schema_version": 1,
  "data_version": "3f1c0a9d5b7e2c4408a6d1f9e0b3c7a25d84f6019c2b7e35a1d0c8f4b6e9a273",
  "model_version": "toxic-clf:v3",
  "generated_at": "2026-08-02T18:00:00+00:00",
  "n": 23851,
  "flag_rates": {
    "toxic": 0.0961,
    "severe_toxic": 0.0100,
    "obscene": 0.0530,
    "threat": 0.0030,
    "insult": 0.0494,
    "identity_hate": 0.0088
  }
}
```

`tests/fixtures/thresholds.json`:
```json
{
  "toxic": 0.45,
  "severe_toxic": 0.30,
  "obscene": 0.50,
  "threat": 0.18,
  "insult": 0.47,
  "identity_hate": 0.25
}
```

`tests/unit/test_drift.py`:
```python
import json
from pathlib import Path

import pytest

from model.labels import LABELS
from monitoring.baseline import (
    Baseline,
    BaselineContractError,
    BaselineMissingError,
    load_baseline,
    load_thresholds,
)

FIXTURES = Path("tests/fixtures")


def test_load_baseline_returns_all_six_rates():
    baseline = load_baseline(FIXTURES / "baseline_flag_rates.json")
    assert isinstance(baseline, Baseline)
    assert tuple(baseline.flag_rates) == LABELS
    assert baseline.flag_rates["threat"] == pytest.approx(0.0030)
    assert baseline.n == 23851
    assert baseline.model_version == "toxic-clf:v3"


def test_missing_baseline_fails_closed(tmp_path):
    """Without this, the drift panel plots a production-only series that cannot answer
    whether anything changed -- and looks identical to a working chart."""
    with pytest.raises(BaselineMissingError, match="baseline_flag_rates.json"):
        load_baseline(tmp_path / "baseline_flag_rates.json")


def test_baseline_missing_a_label_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "baseline_flag_rates.json").read_text())
    payload["flag_rates"].pop("threat")
    bad = tmp_path / "baseline_flag_rates.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="threat"):
        load_baseline(bad)


def test_baseline_rate_outside_unit_interval_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "baseline_flag_rates.json").read_text())
    payload["flag_rates"]["toxic"] = 1.4
    bad = tmp_path / "baseline_flag_rates.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="toxic"):
        load_baseline(bad)


def test_unknown_schema_version_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "baseline_flag_rates.json").read_text())
    payload["schema_version"] = 99
    bad = tmp_path / "baseline_flag_rates.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="schema_version"):
        load_baseline(bad)


def test_load_thresholds_returns_one_float_per_label():
    thresholds = load_thresholds(FIXTURES / "thresholds.json")
    assert tuple(thresholds) == LABELS
    assert all(0.0 < value < 1.0 for value in thresholds.values())


def test_missing_threshold_label_is_rejected(tmp_path):
    payload = json.loads((FIXTURES / "thresholds.json").read_text())
    payload.pop("identity_hate")
    bad = tmp_path / "thresholds.json"
    bad.write_text(json.dumps(payload))
    with pytest.raises(BaselineContractError, match="identity_hate"):
        load_thresholds(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_drift.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'monitoring.baseline'`.

- [ ] **Step 3: Write minimal implementation**

`monitoring/baseline.py`:
```python
"""Load the drift reference and the decision rule, and fail closed on either.

`baseline_flag_rates.json` is written by Phase 1 over the locked held-out split using the
same `thresholds.json` this module also loads. Sharing one decision rule between the
reference and the production series is what makes a PSI comparison meaningful; a chart of
production flag rates alone cannot answer whether anything changed.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from model.labels import LABELS

SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


class BaselineMissingError(RuntimeError):
    """The reference artifact is absent. The drift panel must not render without it."""


class BaselineContractError(RuntimeError):
    """The reference artifact is present but does not match the pinned shape."""


@dataclass(frozen=True)
class Baseline:
    schema_version: int
    data_version: str
    model_version: str
    n: int
    flag_rates: dict[str, float]


def _read_json(path: Path, what: str) -> dict:
    if not path.is_file():
        raise BaselineMissingError(
            f"{what} not found at {path}. Phase 1 must publish it alongside the promoted "
            "model; the dashboard refuses to plot drift without a reference."
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BaselineContractError(f"{what} at {path} is not valid JSON: {exc}") from exc


def _rates(raw: dict, path: Path, what: str) -> dict[str, float]:
    ordered: dict[str, float] = {}
    for label in LABELS:
        if label not in raw:
            raise BaselineContractError(f"{what} at {path} is missing label {label!r}")
        value = raw[label]
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            raise BaselineContractError(
                f"{what} at {path} has {label!r}={value!r}, outside [0, 1]"
            )
        ordered[label] = float(value)
    return ordered


def load_baseline(path: Path) -> Baseline:
    payload = _read_json(path, "baseline_flag_rates.json")
    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BaselineContractError(
            f"baseline_flag_rates.json at {path} has schema_version={version!r}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )
    rates = payload.get("flag_rates")
    if not isinstance(rates, dict):
        raise BaselineContractError(f"baseline_flag_rates.json at {path} has no flag_rates object")
    return Baseline(
        schema_version=int(version),
        data_version=str(payload.get("data_version", "")),
        model_version=str(payload.get("model_version", "")),
        n=int(payload.get("n", 0)),
        flag_rates=_rates(rates, path, "baseline_flag_rates.json"),
    )


def load_thresholds(path: Path) -> dict[str, float]:
    payload = _read_json(path, "thresholds.json")
    return _rates(payload, path, "thresholds.json")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_drift.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/baseline.py tests/fixtures/baseline_flag_rates.json tests/fixtures/thresholds.json tests/unit/test_drift.py
git commit -m "Add fail-closed loaders for the drift baseline and decision thresholds"
```

---

### Task 4: The submitter fingerprint (delivery spec §6.4, the missing rate-limit key)

"Per-source rate limit" needs a source, and the schema has none. This task creates one that is a rate-limit token rather than an identity, and proves it is not the raw address, not client-controllable, and not linkable across days.

**Files:**
- Create: `backend/fingerprint.py`
- Test: `tests/unit/test_fingerprint.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_fingerprint.py`:
```python
import datetime as dt

import pytest

from backend.fingerprint import caller_identity, submitter_fp

KEY = bytes(range(32))
DAY = dt.date(2026, 8, 6)


def test_fingerprint_is_sixteen_lowercase_hex_chars():
    value = submitter_fp("203.0.113.7", DAY, KEY)
    assert len(value) == 16
    assert all(c in "0123456789abcdef" for c in value)


def test_fingerprint_is_stable_within_a_day():
    assert submitter_fp("203.0.113.7", DAY, KEY) == submitter_fp("203.0.113.7", DAY, KEY)


def test_fingerprint_is_not_linkable_across_days():
    tomorrow = DAY + dt.timedelta(days=1)
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.7", tomorrow, KEY)


def test_fingerprint_is_not_linkable_across_deploys():
    other_key = bytes(range(1, 33))
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.7", DAY, other_key)


def test_fingerprint_does_not_contain_the_address():
    value = submitter_fp("203.0.113.7", DAY, KEY)
    assert "203" not in value
    assert "113" not in value


def test_distinct_addresses_get_distinct_fingerprints():
    assert submitter_fp("203.0.113.7", DAY, KEY) != submitter_fp("203.0.113.8", DAY, KEY)


def test_empty_key_is_refused():
    with pytest.raises(ValueError, match="key"):
        submitter_fp("203.0.113.7", DAY, b"")


def test_identity_is_the_peer_for_a_direct_caller():
    assert caller_identity("203.0.113.7", None, api_key_ok=False) == "peer:203.0.113.7"


def test_identity_ignores_a_session_header_without_the_frontend_api_key():
    """An unauthenticated client must not be able to choose its own rate-limit bucket."""
    assert caller_identity("203.0.113.7", "deadbeefdeadbeef", api_key_ok=False) == "peer:203.0.113.7"


def test_identity_uses_the_session_header_only_from_the_authenticated_frontend():
    assert (
        caller_identity("10.42.1.20", "deadbeefdeadbeef", api_key_ok=True)
        == "session:deadbeefdeadbeef"
    )


def test_identity_rejects_a_malformed_session_header():
    assert caller_identity("10.42.1.20", "not hex; drop table", api_key_ok=True) == "peer:10.42.1.20"


def test_identity_never_reads_x_forwarded_for():
    """caller_identity has no parameter for it. A spoofed hop cannot reach this function."""
    import inspect

    assert "forwarded" not in inspect.signature(caller_identity).parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_fingerprint.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.fingerprint'`.

- [ ] **Step 3: Write minimal implementation**

`backend/fingerprint.py`:
```python
"""A rate-limit key for a schema that had no submitter identifier.

What is stored is the first 16 hex characters of HMAC-SHA256(key, "identity|utc_date"):
not reversible to an address without the per-deploy key, not linkable across days because
the date is inside the message, not linkable across deploys because the key rotates, and
purged with `predictions.input_text` on the same 30-day TTL. It carries strictly less
information than an ordinary web access log, and without it the review queue is an
unbounded anonymous write path into a graded metric.

`identity` is the TCP peer for direct callers. Traffic proxied by the user UI all shares
one peer address, so the frontend passes its own server-side session token (never sent to
the browser) in X-Session-Fp -- accepted only when the caller also presents the frontend's
API key. X-Forwarded-For is never consulted: this function has no parameter for it.
"""

import datetime as dt
import hashlib
import hmac
import re

_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


def caller_identity(peer_ip: str, session_fp_header: str | None, api_key_ok: bool) -> str:
    if api_key_ok and session_fp_header and _HEX16.match(session_fp_header):
        return f"session:{session_fp_header}"
    return f"peer:{peer_ip}"


def submitter_fp(identity: str, day: dt.date, key: bytes) -> str:
    if not key:
        raise ValueError("submitter fingerprint key must not be empty")
    message = f"{identity}|{day.isoformat()}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_fingerprint.py -v`
Expected: 12 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/fingerprint.py tests/unit/test_fingerprint.py
git commit -m "Add keyed submitter fingerprint as the per-source rate-limit key"
```

---

### Task 5: Review-queue depth cap and per-source quota (delivery spec §6.4)

A flood of toxic submissions would otherwise bury real items and poison the graded metric. This task is the admission control, and it is also where each row's sampling stratum and inclusion probability are recorded.

**Files:**
- Create: `backend/queue_guard.py`
- Test: `tests/integration/test_queue_guard.py`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_queue_guard.py`:
```python
import datetime as dt

import pytest
from sqlalchemy import text

from backend.queue_guard import AdmissionConfig, admit_review, admit_user_feedback
from model.labels import LABELS

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 10, 12, 0, tzinfo=dt.timezone.utc)


def _predict_row(conn, request_id: str, fp: str | None = "aaaabbbbccccdddd", ts=NOW) -> None:
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.1" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms, submitter_fp) VALUES (:rid, :ts, 'hello', 'm', "
            f"{vals}, 'allow', 0.1, 5, :fp)"
        ),
        {"rid": request_id, "ts": ts, "fp": fp},
    )


def test_admits_a_flagged_row_and_records_its_inclusion_probability(conn):
    _predict_row(conn, "r1")
    result = admit_review(
        conn, request_id="r1", source="flagged", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(),
    )
    assert result.admitted and result.reason == "ok"
    row = conn.execute(
        text("SELECT source, sample_rate, status, input_text_snapshot FROM review_queue "
             "WHERE request_id = 'r1'")
    ).one()
    assert row.source == "flagged"
    assert row.sample_rate == pytest.approx(1.0)
    assert row.status == "pending"
    assert row.input_text_snapshot == "hello"


def test_random_audit_records_the_configured_rate(conn):
    _predict_row(conn, "r2")
    admit_review(
        conn, request_id="r2", source="random-audit", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(random_audit_rate=0.05),
    )
    rate = conn.execute(
        text("SELECT sample_rate FROM review_queue WHERE request_id = 'r2'")
    ).scalar_one()
    assert rate == pytest.approx(0.05)


def test_user_report_records_a_null_rate(conn):
    _predict_row(conn, "r3")
    admit_review(
        conn, request_id="r3", source="user-report", submitter_fp="aaaabbbbccccdddd",
        now=NOW, config=AdmissionConfig(),
    )
    rate = conn.execute(
        text("SELECT sample_rate FROM review_queue WHERE request_id = 'r3'")
    ).scalar_one()
    assert rate is None


def test_depth_cap_rejects_once_the_queue_is_full(conn):
    config = AdmissionConfig(max_pending=3, max_pending_per_source=99,
                             max_enqueues_per_source_per_window=99)
    for i in range(3):
        _predict_row(conn, f"d{i}")
        assert admit_review(conn, request_id=f"d{i}", source="flagged",
                            submitter_fp="aaaabbbbccccdddd", now=NOW, config=config).admitted
    _predict_row(conn, "d3")
    result = admit_review(conn, request_id="d3", source="flagged",
                          submitter_fp="aaaabbbbccccdddd", now=NOW, config=config)
    assert not result.admitted and result.reason == "queue_full"
    assert conn.execute(text("SELECT count(*) FROM review_queue")).scalar_one() == 3


def test_per_source_quota_rejects_a_flood_from_one_fingerprint(conn):
    config = AdmissionConfig(max_pending=99, max_pending_per_source=2,
                             max_enqueues_per_source_per_window=99)
    for i in range(2):
        _predict_row(conn, f"f{i}", fp="1111111111111111")
        assert admit_review(conn, request_id=f"f{i}", source="flagged",
                            submitter_fp="1111111111111111", now=NOW, config=config).admitted
    _predict_row(conn, "f2", fp="1111111111111111")
    result = admit_review(conn, request_id="f2", source="flagged",
                          submitter_fp="1111111111111111", now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"


def test_a_second_fingerprint_is_not_starved_by_the_first(conn):
    config = AdmissionConfig(max_pending=99, max_pending_per_source=2,
                             max_enqueues_per_source_per_window=99)
    for i in range(2):
        _predict_row(conn, f"g{i}", fp="1111111111111111")
        admit_review(conn, request_id=f"g{i}", source="flagged",
                     submitter_fp="1111111111111111", now=NOW, config=config)
    _predict_row(conn, "h0", fp="2222222222222222")
    assert admit_review(conn, request_id="h0", source="flagged",
                        submitter_fp="2222222222222222", now=NOW, config=config).admitted


def test_enqueueing_the_same_request_twice_is_a_no_op(conn):
    _predict_row(conn, "dup")
    assert admit_review(conn, request_id="dup", source="flagged",
                        submitter_fp="aaaabbbbccccdddd", now=NOW,
                        config=AdmissionConfig()).admitted
    second = admit_review(conn, request_id="dup", source="user-report",
                          submitter_fp="aaaabbbbccccdddd", now=NOW, config=AdmissionConfig())
    assert not second.admitted and second.reason == "duplicate"
    row = conn.execute(
        text("SELECT source, sample_rate FROM review_queue WHERE request_id = 'dup'")
    ).one()
    assert row.source == "flagged" and row.sample_rate == pytest.approx(1.0)


def test_user_feedback_is_refused_for_an_unknown_request(conn):
    result = admit_user_feedback(conn, request_id="nope", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW, config=AdmissionConfig())
    assert not result.admitted and result.reason == "unknown_request"


def test_user_feedback_is_refused_outside_the_window(conn):
    stale = NOW - dt.timedelta(days=3)
    _predict_row(conn, "old", ts=stale)
    result = admit_user_feedback(conn, request_id="old", submitter_fp="aaaabbbbccccdddd",
                                 now=NOW, config=AdmissionConfig(user_feedback_window_seconds=86400))
    assert not result.admitted and result.reason == "expired"


def test_user_feedback_quota_is_enforced_per_fingerprint(conn):
    config = AdmissionConfig(max_user_feedback_per_source_per_window=2)
    for i in range(3):
        _predict_row(conn, f"u{i}", fp="3333333333333333")
    for i in range(2):
        assert admit_user_feedback(conn, request_id=f"u{i}", submitter_fp="3333333333333333",
                                   now=NOW, config=config).admitted
        conn.execute(
            text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                 "VALUES (:rid, :ts, 'user', '{}'::jsonb, true)"),
            {"rid": f"u{i}", "ts": NOW},
        )
    result = admit_user_feedback(conn, request_id="u2", submitter_fp="3333333333333333",
                                 now=NOW, config=config)
    assert not result.admitted and result.reason == "source_quota"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_queue_guard.py -v -m integration`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.queue_guard'`.

- [ ] **Step 3: Write minimal implementation**

`backend/queue_guard.py`:
```python
"""Admission control for the review queue and the user-feedback path.

Two properties the delivery spec (section 6.4) makes normative and that nothing else
enforces: the queue is depth-capped, and it is rate-limited per source. A flood of toxic
submissions would otherwise bury real items and poison the graded live-accuracy metric.

This is also the single place where a row's sampling stratum and inclusion probability are
recorded. `sample_rate` is written here, at enqueue time, because it cannot be recovered
afterwards: RANDOM_AUDIT_RATE is configuration and configuration changes.
"""

import datetime as dt
import os
from dataclasses import dataclass

from sqlalchemy import text

RANDOM_AUDIT_RATE: float = float(os.environ.get("RANDOM_AUDIT_RATE", "0.05"))

_SAMPLE_RATE_BY_SOURCE = {
    "flagged": 1.0,          # every flagged item is reviewed: inclusion probability 1
    "random-audit": None,    # filled from config.random_audit_rate
    "user-report": None,     # self-selected: inclusion probability unknown, stays NULL
}


@dataclass(frozen=True)
class AdmissionConfig:
    max_pending: int = 500
    max_pending_per_source: int = 20
    window_seconds: int = 3600
    max_enqueues_per_source_per_window: int = 30
    max_user_feedback_per_source_per_window: int = 20
    user_feedback_window_seconds: int = 86400
    random_audit_rate: float = RANDOM_AUDIT_RATE


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason: str


def _sample_rate(source: str, config: AdmissionConfig) -> float | None:
    if source == "flagged":
        return 1.0
    if source == "random-audit":
        return config.random_audit_rate
    if source == "user-report":
        return None
    raise ValueError(f"unknown review_queue source {source!r}")


def admit_review(
    conn,
    *,
    request_id: str,
    source: str,
    submitter_fp: str | None,
    now: dt.datetime,
    config: AdmissionConfig,
) -> Admission:
    rate = _sample_rate(source, config)

    existing = conn.execute(
        text("SELECT 1 FROM review_queue WHERE request_id = :rid"), {"rid": request_id}
    ).first()
    if existing is not None:
        return Admission(False, "duplicate")

    snapshot = conn.execute(
        text("SELECT input_text FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar()
    if snapshot is None:
        return Admission(False, "unknown_request")

    pending = conn.execute(
        text("SELECT count(*) FROM review_queue WHERE status = 'pending'")
    ).scalar_one()
    if pending >= config.max_pending:
        return Admission(False, "queue_full")

    if submitter_fp is not None:
        per_source_pending = conn.execute(
            text(
                "SELECT count(*) FROM review_queue q JOIN predictions p "
                "ON p.request_id = q.request_id "
                "WHERE q.status = 'pending' AND p.submitter_fp = :fp"
            ),
            {"fp": submitter_fp},
        ).scalar_one()
        if per_source_pending >= config.max_pending_per_source:
            return Admission(False, "source_quota")

        window_start = now - dt.timedelta(seconds=config.window_seconds)
        recent = conn.execute(
            text(
                "SELECT count(*) FROM review_queue q JOIN predictions p "
                "ON p.request_id = q.request_id "
                "WHERE q.enqueued_ts >= :start AND p.submitter_fp = :fp"
            ),
            {"start": window_start, "fp": submitter_fp},
        ).scalar_one()
        if recent >= config.max_enqueues_per_source_per_window:
            return Admission(False, "source_quota")

    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', :src, :rate, :snap)"
        ),
        {"rid": request_id, "ts": now, "src": source, "rate": rate, "snap": snapshot},
    )
    conn.commit()
    return Admission(True, "ok")


def admit_user_feedback(
    conn,
    *,
    request_id: str,
    submitter_fp: str | None,
    now: dt.datetime,
    config: AdmissionConfig,
) -> Admission:
    ts = conn.execute(
        text("SELECT ts FROM predictions WHERE request_id = :rid"), {"rid": request_id}
    ).scalar()
    if ts is None:
        return Admission(False, "unknown_request")
    if (now - ts).total_seconds() > config.user_feedback_window_seconds:
        return Admission(False, "expired")

    already = conn.execute(
        text("SELECT 1 FROM feedback WHERE request_id = :rid AND source = 'user'"),
        {"rid": request_id},
    ).first()
    if already is not None:
        return Admission(False, "duplicate")

    if submitter_fp is not None:
        window_start = now - dt.timedelta(seconds=config.user_feedback_window_seconds)
        recent = conn.execute(
            text(
                "SELECT count(*) FROM feedback f JOIN predictions p "
                "ON p.request_id = f.request_id "
                "WHERE f.source = 'user' AND f.ts >= :start AND p.submitter_fp = :fp"
            ),
            {"start": window_start, "fp": submitter_fp},
        ).scalar_one()
        if recent >= config.max_user_feedback_per_source_per_window:
            return Admission(False, "source_quota")

    return Admission(True, "ok")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_queue_guard.py -v -m integration`
Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/queue_guard.py tests/integration/test_queue_guard.py
git commit -m "Add review-queue depth cap, per-source quota, and stratum recording"
```

---
### Task 6: Feedback derivation with distinct sources (rubric 3.2, premortem H9)

Rubric 3.2 grades a mechanism to collect **user** feedback. The design collected only reviewer feedback. This task is the record type that keeps the two apart, so a graded metric can use one and display the other.

**Files:**
- Create: `backend/feedback.py`
- Test: `tests/unit/test_feedback.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_feedback.py`:
```python
import pytest

from backend.feedback import FeedbackRecord, derive_feedback, user_feedback
from model.labels import LABELS


def _labels(**overrides) -> dict[str, int]:
    base = {label: 0 for label in LABELS}
    base.update(overrides)
    return base


def _flags(**overrides) -> dict[str, bool]:
    base = {label: False for label in LABELS}
    base.update(overrides)
    return base


def test_full_agreement_is_an_exact_match():
    record = derive_feedback(
        "r1", _labels(toxic=1, insult=1), _flags(toxic=True, insult=True), "rock"
    )
    assert isinstance(record, FeedbackRecord)
    assert record.source == "reviewer"
    assert record.reviewer_id == "rock"
    assert record.exact_match is True
    assert record.agreement == {label: True for label in LABELS}


def test_one_disagreement_breaks_the_exact_match_and_is_localised():
    record = derive_feedback(
        "r2", _labels(toxic=1, threat=1), _flags(toxic=True), "rock"
    )
    assert record.exact_match is False
    assert record.agreement["threat"] is False
    assert record.agreement["toxic"] is True
    assert sum(1 for ok in record.agreement.values() if not ok) == 1


def test_agreement_keys_are_the_labels_in_order():
    record = derive_feedback("r3", _labels(), _flags(), "rock")
    assert tuple(record.agreement) == LABELS


def test_missing_reviewer_label_is_rejected():
    partial = _labels()
    partial.pop("identity_hate")
    with pytest.raises(ValueError, match="identity_hate"):
        derive_feedback("r4", partial, _flags(), "rock")


def test_non_binary_reviewer_label_is_rejected():
    with pytest.raises(ValueError, match="toxic"):
        derive_feedback("r5", _labels(toxic=2), _flags(), "rock")


def test_empty_reviewer_id_is_rejected():
    """A reviewer row with no attributable reviewer is not a review."""
    with pytest.raises(ValueError, match="reviewer_id"):
        derive_feedback("r6", _labels(), _flags(), "")


def test_user_feedback_is_a_single_bit_with_no_free_text():
    record = user_feedback("r7", "disagree")
    assert record.source == "user"
    assert record.reviewer_id is None
    assert record.agreement == {}
    assert record.exact_match is False
    assert user_feedback("r8", "agree").exact_match is True


def test_user_verdict_is_a_closed_vocabulary_so_there_is_nothing_to_size_cap():
    with pytest.raises(ValueError, match="verdict"):
        user_feedback("r9", "x" * 5000)
    with pytest.raises(ValueError, match="verdict"):
        user_feedback("r10", "AGREE ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_feedback.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.feedback'`.

- [ ] **Step 3: Write minimal implementation**

`backend/feedback.py`:
```python
"""Feedback records, and the line between the two kinds of them.

`source='reviewer'` rows carry a per-label agreement vector from a human who saw the
comment. `source='user'` rows carry one bit from an anonymous visitor. They are stored in
the same table and used for different things: the reviewer rows feed the design-weighted
live-accuracy estimate, the user rows feed their own panel and a referral into the review
queue. Pooling them would make a graded metric writable by anyone with a browser.

The user verdict is a closed two-value vocabulary, which is the size cap: there is no
free-text field on the internet-facing feedback path to cap.
"""

import datetime as dt
import json
from dataclasses import dataclass

from sqlalchemy import text

from model.labels import LABELS

USER_VERDICTS: frozenset[str] = frozenset({"agree", "disagree"})


@dataclass(frozen=True)
class FeedbackRecord:
    request_id: str
    source: str
    reviewer_id: str | None
    agreement: dict[str, bool]
    exact_match: bool


def derive_feedback(
    request_id: str,
    reviewer_labels: dict[str, int],
    model_flags: dict[str, bool],
    reviewer_id: str,
) -> FeedbackRecord:
    if not reviewer_id:
        raise ValueError("reviewer_id must be a non-empty server-derived identity")
    agreement: dict[str, bool] = {}
    for label in LABELS:
        if label not in reviewer_labels:
            raise ValueError(f"reviewer_labels is missing {label!r}")
        value = reviewer_labels[label]
        if value not in (0, 1, True, False):
            raise ValueError(f"reviewer_labels[{label!r}]={value!r} is outside {{0, 1}}")
        agreement[label] = bool(value) == bool(model_flags.get(label, False))
    return FeedbackRecord(
        request_id=request_id,
        source="reviewer",
        reviewer_id=reviewer_id,
        agreement=agreement,
        exact_match=all(agreement.values()),
    )


def user_feedback(request_id: str, verdict: str) -> FeedbackRecord:
    if verdict not in USER_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(USER_VERDICTS)}")
    return FeedbackRecord(
        request_id=request_id,
        source="user",
        reviewer_id=None,
        agreement={},
        exact_match=verdict == "agree",
    )


def insert_feedback(conn, record: FeedbackRecord, ts: dt.datetime | None = None) -> None:
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES (:rid, COALESCE(:ts, now()), :src, :who, CAST(:agree AS jsonb), :exact)"
        ),
        {
            "rid": record.request_id,
            "ts": ts,
            "src": record.source,
            "who": record.reviewer_id,
            "agree": json.dumps(record.agreement),
            "exact": record.exact_match,
        },
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_feedback.py -v`
Expected: 8 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/feedback.py tests/unit/test_feedback.py
git commit -m "Add feedback derivation with separate user and reviewer sources"
```

---

### Task 7: Reviewer session, with `reviewer_id` derived server-side (delivery spec §6.3)

A client-supplied reviewer identity is unauthenticated attribution. This task makes the identity unreachable from the client: it comes from server configuration, gated by an HMAC token the server issued.

**Files:**
- Create: `backend/reviewer_auth.py`
- Test: `tests/unit/test_reviewer_auth.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_reviewer_auth.py`:
```python
import datetime as dt

import pytest

from backend.reviewer_auth import current_reviewer, issue_session_token

SECRET = "s3cr3t-shared-with-the-reviewer"
REVIEWER = "rock"
NOW = dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.timezone.utc)


def test_issued_token_resolves_to_the_configured_reviewer():
    token = issue_session_token(NOW, SECRET, REVIEWER)
    assert current_reviewer(token, NOW, SECRET, REVIEWER) == REVIEWER


def test_no_token_is_no_reviewer():
    assert current_reviewer(None, NOW, SECRET, REVIEWER) is None
    assert current_reviewer("", NOW, SECRET, REVIEWER) is None


def test_forged_token_is_rejected():
    assert current_reviewer("rock.9999999999.deadbeef", NOW, SECRET, REVIEWER) is None


def test_token_from_a_different_secret_is_rejected():
    token = issue_session_token(NOW, "some-other-secret", REVIEWER)
    assert current_reviewer(token, NOW, SECRET, REVIEWER) is None


def test_expired_token_is_rejected():
    token = issue_session_token(NOW, SECRET, REVIEWER, ttl_seconds=60)
    later = NOW + dt.timedelta(seconds=61)
    assert current_reviewer(token, later, SECRET, REVIEWER) is None


def test_a_token_minted_for_another_identity_cannot_borrow_this_one():
    """The identity is inside the signed payload AND compared to server config, so a valid
    token for 'mallory' does not authenticate as the configured reviewer."""
    token = issue_session_token(NOW, SECRET, "mallory")
    assert current_reviewer(token, NOW, SECRET, REVIEWER) is None


def test_identity_is_never_taken_from_the_token_alone():
    """current_reviewer returns the SERVER's reviewer_id, never a value parsed out of a
    client-held string. Renaming the configured reviewer changes what a fixed token
    resolves to -- to None, never to the token's own claim."""
    token = issue_session_token(NOW, SECRET, REVIEWER)
    assert current_reviewer(token, NOW, SECRET, "someone-else") is None


def test_token_comparison_is_constant_time():
    import inspect

    import backend.reviewer_auth as module

    assert "compare_digest" in inspect.getsource(module)


def test_empty_secret_is_refused():
    with pytest.raises(ValueError, match="secret"):
        issue_session_token(NOW, "", REVIEWER)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_reviewer_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.reviewer_auth'`.

- [ ] **Step 3: Write minimal implementation**

`backend/reviewer_auth.py`:
```python
"""Reviewer session tokens.

One reviewer behind a shared secret, which the design already records as not being a real
authentication system. What this module does guarantee is the property the delivery spec
(section 6.3) makes normative: `reviewer_id` is derived server-side. The identity returned
is the process's configured REVIEWER_ID, and a token only ever decides whether to return it
or None. No code path parses an identity out of a client-held value.
"""

import datetime as dt
import hashlib
import hmac


def _signature(reviewer_id: str, expiry: int, secret: str) -> str:
    message = f"{reviewer_id}.{expiry}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def issue_session_token(
    now: dt.datetime, secret: str, reviewer_id: str, ttl_seconds: int = 43200
) -> str:
    if not secret:
        raise ValueError("reviewer shared secret must not be empty")
    if not reviewer_id:
        raise ValueError("reviewer_id must not be empty")
    expiry = int(now.timestamp()) + ttl_seconds
    return f"{reviewer_id}.{expiry}.{_signature(reviewer_id, expiry, secret)}"


def current_reviewer(
    token: str | None, now: dt.datetime, secret: str, reviewer_id: str
) -> str | None:
    if not token or not secret or not reviewer_id:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    claimed, raw_expiry, signature = parts
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return None
    expected = _signature(claimed, expiry, secret)
    if not hmac.compare_digest(signature, expected):
        return None
    if expiry <= int(now.timestamp()):
        return None
    if not hmac.compare_digest(claimed, reviewer_id):
        return None
    return reviewer_id  # the server's value, never the token's
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_reviewer_auth.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/reviewer_auth.py tests/unit/test_reviewer_auth.py
git commit -m "Derive reviewer identity from a server-issued session token"
```

---

### Task 8: The review and user-feedback API (premortem H9, H12, H16)

Every UI write goes through here, so neither Streamlit container ever holds a database credential. The submit body has no `reviewer_id` field, so the identity cannot be supplied even by a caller who holds the reviewer token.

**Files:**
- Create: `backend/review_api.py`
- Modify: `backend/app.py` (one line: `app.include_router(router)`)
- Test: `tests/integration/test_review_api.py`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_review_api.py`:
```python
import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.review_api import router
from model.labels import LABELS

pytestmark = pytest.mark.integration

SECRET = "reviewer-shared-secret"
REVIEWER = "rock"


@pytest.fixture()
def client(conn, monkeypatch, engine):
    monkeypatch.setenv("REVIEWER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("REVIEWER_ID", REVIEWER)
    monkeypatch.setenv("SUBMITTER_FP_KEY", "0" * 64)
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    return TestClient(app)


def _seed(conn, request_id: str, probs: dict[str, float] | None = None) -> None:
    probs = probs or {label: 0.1 for label in LABELS}
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)
    params = {f"p_{label}": probs[label] for label in LABELS}
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms) VALUES (:rid, now(), 'you are an idiot', "
            f"'toxic-clf:v3', {binds}, 'review', :mx, 12)"
        ),
        {"rid": request_id, "mx": max(probs.values()), **params},
    )
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, now(), 'pending', 'flagged', 1.0, "
            "'you are an idiot')"
        ),
        {"rid": request_id},
    )
    conn.commit()


def _login(client) -> str:
    response = client.post("/review/login", json={"secret": SECRET})
    assert response.status_code == 200
    return response.json()["token"]


def test_login_rejects_a_wrong_secret(client):
    assert client.post("/review/login", json={"secret": "nope"}).status_code == 401


def test_pending_requires_a_token(client):
    assert client.get("/review/pending").status_code == 401


def test_pending_returns_the_snapshot_verbatim(client, conn):
    _seed(conn, "a1")
    token = _login(client)
    response = client.get("/review/pending", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["request_id"] == "a1"
    assert items[0]["input_text_snapshot"] == "you are an idiot"
    assert set(items[0]["model_probs"]) == set(LABELS)


def test_submit_body_rejects_a_client_supplied_reviewer_id(client, conn):
    """H12/section 6.3: the field does not exist, so the identity cannot be asserted."""
    _seed(conn, "a2")
    token = _login(client)
    response = client.post(
        "/review/submit",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "request_id": "a2",
            "labels": {label: 0 for label in LABELS},
            "reviewer_id": "admin",
        },
    )
    assert response.status_code == 422


def test_submit_writes_labels_status_and_a_derived_feedback_row(client, conn):
    _seed(conn, "a3", {**{label: 0.1 for label in LABELS}, "toxic": 0.9, "insult": 0.8})
    token = _login(client)
    labels = {label: 0 for label in LABELS}
    labels["toxic"] = 1
    labels["insult"] = 1
    response = client.post(
        "/review/submit",
        headers={"Authorization": f"Bearer {token}"},
        json={"request_id": "a3", "labels": labels},
    )
    assert response.status_code == 200
    row = conn.execute(
        text("SELECT status, reviewer_id, reviewer_labels, reviewed_ts FROM review_queue "
             "WHERE request_id = 'a3'")
    ).one()
    assert row.status == "reviewed"
    assert row.reviewer_id == REVIEWER
    assert row.reviewer_labels["toxic"] == 1
    assert row.reviewed_ts is not None
    feedback = conn.execute(
        text("SELECT source, reviewer_id, exact_match FROM feedback WHERE request_id = 'a3'")
    ).one()
    assert feedback.source == "reviewer"
    assert feedback.reviewer_id == REVIEWER
    assert feedback.exact_match is True


def test_submitting_twice_does_not_double_count(client, conn):
    _seed(conn, "a4")
    token = _login(client)
    labels = {label: 0 for label in LABELS}
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/review/submit", headers=headers,
                       json={"request_id": "a4", "labels": labels}).status_code == 200
    second = client.post("/review/submit", headers=headers,
                         json={"request_id": "a4", "labels": labels})
    assert second.status_code == 409
    count = conn.execute(
        text("SELECT count(*) FROM feedback WHERE request_id = 'a4'")
    ).scalar_one()
    assert count == 1


def test_user_feedback_writes_a_user_sourced_row(client, conn):
    _seed(conn, "a5")
    response = client.post("/feedback/user", json={"request_id": "a5", "verdict": "agree"})
    assert response.status_code == 200
    row = conn.execute(
        text("SELECT source, reviewer_id, exact_match FROM feedback WHERE request_id = 'a5'")
    ).one()
    assert row.source == "user"
    assert row.reviewer_id is None
    assert row.exact_match is True


def test_user_feedback_needs_no_reviewer_token_but_is_rate_limited(client, conn, monkeypatch):
    monkeypatch.setenv("MAX_USER_FEEDBACK_PER_SOURCE_PER_WINDOW", "1")
    _seed(conn, "a6")
    _seed(conn, "a7")
    assert client.post("/feedback/user",
                       json={"request_id": "a6", "verdict": "agree"}).status_code == 200
    limited = client.post("/feedback/user", json={"request_id": "a7", "verdict": "disagree"})
    assert limited.status_code == 429


def test_user_feedback_rejects_an_unknown_verdict(client, conn):
    _seed(conn, "a8")
    response = client.post("/feedback/user", json={"request_id": "a8", "verdict": "spam"})
    assert response.status_code == 422


def test_user_feedback_rejects_free_text(client, conn):
    _seed(conn, "a9")
    response = client.post(
        "/feedback/user",
        json={"request_id": "a9", "verdict": "agree", "comment": "x" * 100000},
    )
    assert response.status_code == 422


def test_user_disagreement_enqueues_a_user_report_with_no_inclusion_probability(client, conn):
    """The referral is how user feedback reaches live accuracy: through a human, not
    through arithmetic. sample_rate stays NULL so the estimator ignores it."""
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.05" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms) VALUES ('a10', now(), 'looks fine', 'm', {vals}, "
            "'allow', 0.05, 7)"
        )
    )
    conn.commit()
    assert client.post("/feedback/user",
                       json={"request_id": "a10", "verdict": "disagree"}).status_code == 200
    row = conn.execute(
        text("SELECT source, sample_rate, status FROM review_queue WHERE request_id = 'a10'")
    ).one()
    assert row.source == "user-report"
    assert row.sample_rate is None
    assert row.status == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_review_api.py -v -m integration`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.review_api'`.

- [ ] **Step 3: Write minimal implementation**

`backend/review_api.py`:
```python
"""Review and feedback endpoints.

Mounted onto the Phase 2 FastAPI app. This router exists so that neither Streamlit
container needs a database credential: the premortem (H12, H16) found that opening the demo
port also exposed a console with direct RDS write access to the graded metric, and the
cheapest way to stop that being true is for the UI to have no database access at all.
"""

import datetime as dt
import json
import os

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from backend.feedback import derive_feedback, insert_feedback, user_feedback
from backend.queue_guard import AdmissionConfig, admit_review, admit_user_feedback
from backend.reviewer_auth import current_reviewer, issue_session_token
from model.labels import LABELS

router = APIRouter()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _config() -> AdmissionConfig:
    return AdmissionConfig(
        max_pending=int(os.environ.get("REVIEW_QUEUE_MAX_PENDING", "500")),
        max_pending_per_source=int(os.environ.get("REVIEW_QUEUE_MAX_PENDING_PER_SOURCE", "20")),
        max_user_feedback_per_source_per_window=int(
            os.environ.get("MAX_USER_FEEDBACK_PER_SOURCE_PER_WINDOW", "20")
        ),
        random_audit_rate=float(os.environ.get("RANDOM_AUDIT_RATE", "0.05")),
    )


def _connection(request: Request):
    return request.app.state.engine.connect()


def _reviewer(authorization: str | None) -> str:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    who = current_reviewer(
        token,
        _now(),
        os.environ.get("REVIEWER_SHARED_SECRET", ""),
        os.environ.get("REVIEWER_ID", ""),
    )
    if who is None:
        raise HTTPException(status_code=401, detail="reviewer session required")
    return who


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str = Field(max_length=512)


class SubmitRequest(BaseModel):
    # No reviewer_id field, and extra="forbid": the identity is unassertable by a client.
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(max_length=64)
    labels: dict[str, int]


class UserFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(max_length=64)
    verdict: str = Field(pattern="^(agree|disagree)$")


@router.post("/review/login")
def login(payload: LoginRequest) -> dict:
    import hmac

    secret = os.environ.get("REVIEWER_SHARED_SECRET", "")
    reviewer_id = os.environ.get("REVIEWER_ID", "")
    if not secret or not hmac.compare_digest(payload.secret, secret):
        raise HTTPException(status_code=401, detail="invalid reviewer secret")
    return {"token": issue_session_token(_now(), secret, reviewer_id)}


@router.get("/review/pending")
def pending(request: Request, limit: int = 20, authorization: str | None = Header(None)) -> dict:
    _reviewer(authorization)
    prob_columns = ", ".join(f"p.prob_{label}" for label in LABELS)
    with _connection(request) as conn:
        rows = conn.execute(
            text(
                f"SELECT q.request_id, q.enqueued_ts, q.source, q.status, "
                f"q.input_text_snapshot, q.distilbert_probs, {prob_columns} "
                "FROM review_queue q JOIN predictions p ON p.request_id = q.request_id "
                "WHERE q.status IN ('pending', 'rescored') "
                "ORDER BY q.enqueued_ts LIMIT :limit"
            ),
            {"limit": min(max(limit, 1), 100)},
        ).mappings().all()
    items = [
        {
            "request_id": row["request_id"],
            "enqueued_ts": row["enqueued_ts"].isoformat(),
            "source": row["source"],
            "status": row["status"],
            "input_text_snapshot": row["input_text_snapshot"],
            "model_probs": {label: float(row[f"prob_{label}"]) for label in LABELS},
            "distilbert_probs": row["distilbert_probs"],
        }
        for row in rows
    ]
    return {"items": items}


@router.post("/review/submit")
def submit(
    request: Request, payload: SubmitRequest, authorization: str | None = Header(None)
) -> dict:
    reviewer_id = _reviewer(authorization)
    missing = [label for label in LABELS if label not in payload.labels]
    if missing:
        raise HTTPException(status_code=422, detail=f"missing labels: {missing}")
    if any(payload.labels[label] not in (0, 1) for label in LABELS):
        raise HTTPException(status_code=422, detail="labels must be 0 or 1")

    with _connection(request) as conn:
        row = conn.execute(
            text(
                "SELECT q.status, " + ", ".join(f"p.prob_{label}" for label in LABELS) + " "
                "FROM review_queue q JOIN predictions p ON p.request_id = q.request_id "
                "WHERE q.request_id = :rid"
            ),
            {"rid": payload.request_id},
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="request_id is not in the review queue")
        if row["status"] == "reviewed":
            raise HTTPException(status_code=409, detail="already reviewed")

        thresholds = _thresholds()
        model_flags = {
            label: float(row[f"prob_{label}"]) >= thresholds[label] for label in LABELS
        }
        record = derive_feedback(
            payload.request_id, payload.labels, model_flags, reviewer_id
        )
        now = _now()
        conn.execute(
            text(
                "UPDATE review_queue SET status = 'reviewed', reviewer_labels = "
                "CAST(:labels AS jsonb), reviewer_id = :who, reviewed_ts = :ts "
                "WHERE request_id = :rid AND status <> 'reviewed'"
            ),
            {
                "labels": json.dumps({label: int(payload.labels[label]) for label in LABELS}),
                "who": reviewer_id,
                "ts": now,
                "rid": payload.request_id,
            },
        )
        insert_feedback(conn, record, ts=now)
        conn.commit()
    return {"request_id": payload.request_id, "exact_match": record.exact_match}


@router.post("/feedback/user")
def submit_user_feedback(request: Request, payload: UserFeedbackRequest) -> dict:
    now = _now()
    config = _config()
    fp = getattr(request.state, "submitter_fp", None)
    with _connection(request) as conn:
        if fp is None:
            fp = conn.execute(
                text("SELECT submitter_fp FROM predictions WHERE request_id = :rid"),
                {"rid": payload.request_id},
            ).scalar()
        decision = admit_user_feedback(
            conn, request_id=payload.request_id, submitter_fp=fp, now=now, config=config
        )
        if not decision.admitted:
            status = {
                "unknown_request": 404,
                "expired": 410,
                "duplicate": 409,
                "source_quota": 429,
            }[decision.reason]
            raise HTTPException(status_code=status, detail=decision.reason)

        insert_feedback(conn, user_feedback(payload.request_id, payload.verdict), ts=now)
        conn.commit()

        if payload.verdict == "disagree":
            admit_review(
                conn,
                request_id=payload.request_id,
                source="user-report",
                submitter_fp=fp,
                now=now,
                config=config,
            )
    return {"request_id": payload.request_id, "verdict": payload.verdict}


def _thresholds() -> dict[str, float]:
    from pathlib import Path

    from monitoring.baseline import load_thresholds

    return load_thresholds(Path(os.environ.get("THRESHOLDS_PATH", "artifacts/thresholds.json")))
```

Add one line to `backend/app.py`, immediately after the `app = FastAPI(...)` construction:
```python
from backend.review_api import router as review_router

app.include_router(review_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `THRESHOLDS_PATH=tests/fixtures/thresholds.json .venv/bin/pytest tests/integration/test_review_api.py -v -m integration`
Expected: 11 PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/review_api.py backend/app.py tests/integration/test_review_api.py
git commit -m "Add review and user-feedback endpoints so no UI holds a database credential"
```

---

### Task 9: Verbatim comment rendering (delivery spec §6.3)

Two controls, and the second is the one that is easy to miss. Never `unsafe_allow_html`, **and** never markdown — because markdown processing means the reviewer labels a different string than the classifier scored, which is attacker-controlled ground-truth poisoning that satisfies the first control completely.

**Files:**
- Create: `frontend/__init__.py`, `frontend/render.py`
- Test: `tests/unit/test_render.py`, `tests/unit/test_no_unsafe_html.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_render.py`:
```python
from frontend.render import RENDERER_NAME, render_comment

ADVERSARIAL = (
    "**bold**  <img src=x onerror=alert(1)>\n"
    "# heading\n"
    "  leading and trailing spaces  \n"
    "- list item\n"
    "&lt;already escaped&gt;\n"
    "|table|cell|\n"
    "`code`  ~~strike~~ \\backslash\n"
    "\u200b\u202e zero width and bidi \u202c"
)


def test_rendered_payload_is_byte_identical_to_the_input():
    """If this ever fails, the reviewer is labelling a different string than the
    classifier scored, and the labels are attacker-shaped."""
    calls: list[str] = []
    payload = render_comment(ADVERSARIAL, renderer=calls.append)
    assert payload == ADVERSARIAL
    assert calls == [ADVERSARIAL]


def test_markdown_metacharacters_survive_untouched():
    calls: list[str] = []
    render_comment("**not bold** _not italic_ # not a heading", renderer=calls.append)
    assert calls[0] == "**not bold** _not italic_ # not a heading"


def test_whitespace_is_not_collapsed():
    calls: list[str] = []
    render_comment("a     b\n\n\nc", renderer=calls.append)
    assert calls[0] == "a     b\n\n\nc"


def test_default_renderer_is_a_non_markdown_streamlit_primitive():
    assert RENDERER_NAME in {"st.text", "st.code"}


def test_render_comment_accepts_no_html_flag():
    import inspect

    params = inspect.signature(render_comment).parameters
    assert "unsafe_allow_html" not in params
    assert set(params) == {"text", "renderer"}


def test_none_and_empty_render_as_an_explicit_placeholder_not_a_crash():
    calls: list[str] = []
    render_comment("", renderer=calls.append)
    render_comment(None, renderer=calls.append)
    assert calls == ["(empty comment)", "(empty comment)"]
```

`tests/unit/test_no_unsafe_html.py`:
```python
from pathlib import Path

import pytest

SCANNED_DIRS = ("frontend", "monitoring", "backend", "rescorer", "scripts")
FORBIDDEN = ("unsafe_allow_html", "st.markdown(", "st.write(", "st.html(")


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        root = Path(directory)
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_directories_under_scan_actually_exist():
    """A scan over nothing passes vacuously, which is how this control dies."""
    assert Path("frontend").is_dir()
    assert Path("monitoring").is_dir()
    assert len(_python_files()) >= 2


@pytest.mark.parametrize("path", _python_files(), ids=str)
def test_no_html_or_markdown_rendering_primitives(path: Path):
    source = path.read_text()
    for needle in FORBIDDEN:
        assert needle not in source, (
            f"{path} uses {needle!r}. User and reviewer content is rendered verbatim through "
            "frontend.render.render_comment; markdown and HTML paths are forbidden because "
            "inputs here are adversarial by definition."
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_render.py tests/unit/test_no_unsafe_html.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'frontend.render'`.

- [ ] **Step 3: Write minimal implementation**

`frontend/__init__.py`: empty.

`frontend/render.py`:
```python
"""Render user-supplied comments verbatim.

Two rules, and the second is the one a checklist misses.

1. Never `unsafe_allow_html`. Inputs here are adversarial by definition, and stored XSS
   would steal the reviewer session.
2. Never markdown either. `st.markdown` transforms the string: it eats asterisks, collapses
   runs of whitespace, turns a leading '#' into a heading, and drops raw angle brackets. The
   reviewer would then be labelling a DIFFERENT string than the classifier scored, which is
   attacker-controlled ground-truth poisoning that satisfies rule 1 perfectly.

`st.text` writes the string with no markdown and no HTML parsing, which is the only
property this module needs. The renderer is injectable so the guarantee is testable without
a Streamlit runtime.
"""

from collections.abc import Callable

RENDERER_NAME = "st.text"
EMPTY_PLACEHOLDER = "(empty comment)"


def _default_renderer(payload: str) -> None:
    import streamlit as st

    st.text(payload)


def render_comment(text: str | None, renderer: Callable[[str], None] | None = None) -> str:
    payload = text if text else EMPTY_PLACEHOLDER
    (renderer or _default_renderer)(payload)
    return payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_render.py tests/unit/test_no_unsafe_html.py -v`
Expected: 6 PASS in `test_render.py`, and one parametrised PASS per scanned file plus the non-vacuity test.

- [ ] **Step 5: Commit**

```bash
git add frontend/__init__.py frontend/render.py tests/unit/test_render.py tests/unit/test_no_unsafe_html.py
git commit -m "Render comments verbatim and forbid markdown and HTML rendering paths"
```

---
### Task 10: User UI with the two-click agree/disagree control (rubric 3.1 and 3.2, premortem H9)

Rubric 3.1 wants a frontend that sends data to the backend and displays the prediction. Rubric 3.2 wants a mechanism to collect **user** feedback. The design had the first and not the second.

**Files:**
- Create: `frontend/api_client.py`, `frontend/ui.py`
- Test: `tests/unit/test_api_client.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_api_client.py`:
```python
import httpx
import pytest

from frontend.api_client import MAX_INPUT_CHARS, BackendClient, new_session_fp
from model.labels import LABELS


def _client(handler) -> BackendClient:
    transport = httpx.MockTransport(handler)
    return BackendClient(
        base_url="http://backend:8000",
        api_key="demo-key",
        session_fp="a1b2c3d4e5f60718",
        transport=transport,
    )


def test_new_session_fp_is_sixteen_hex_chars_and_unique():
    first, second = new_session_fp(), new_session_fp()
    assert len(first) == 16 and all(c in "0123456789abcdef" for c in first)
    assert first != second


def test_predict_sends_the_api_key_and_the_session_fingerprint():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(
            200,
            json={
                "request_id": "r1",
                "model_version": "toxic-clf:v3",
                "labels": {label: {"prob": 0.1, "flag": False} for label in LABELS},
                "decision": "allow",
                "max_prob": 0.1,
                "latency_ms": 9,
            },
        )

    result = _client(handler).predict("hello")
    assert result["request_id"] == "r1"
    assert seen["x-api-key"] == "demo-key"
    assert seen["x-session-fp"] == "a1b2c3d4e5f60718"


def test_predict_refuses_oversized_input_before_the_network():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(str(request.url))
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="MAX_INPUT_CHARS"):
        _client(handler).predict("x" * (MAX_INPUT_CHARS + 1))
    assert calls == []


def test_user_feedback_posts_the_closed_verdict_vocabulary():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"request_id": "r1", "verdict": "agree"})

    _client(handler).user_feedback("r1", "agree")
    assert captured["url"].endswith("/feedback/user")
    assert '"verdict": "agree"' in captured["body"] or '"verdict":"agree"' in captured["body"]


def test_user_feedback_rejects_an_invented_verdict_client_side():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("must not reach the network")

    with pytest.raises(ValueError, match="verdict"):
        _client(handler).user_feedback("r1", "maybe")


def test_rate_limited_feedback_surfaces_as_a_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "source_quota"})

    from frontend.api_client import RateLimited

    with pytest.raises(RateLimited):
        _client(handler).user_feedback("r1", "agree")


def test_submit_never_sends_a_reviewer_id():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"request_id": "r1", "exact_match": True})

    _client(handler).submit("token", "r1", {label: 0 for label in LABELS})
    assert "reviewer_id" not in captured["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_api_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'frontend.api_client'`.

- [ ] **Step 3: Write minimal implementation**

`frontend/api_client.py`:
```python
"""The UIs' only I/O. Neither Streamlit process opens a database connection.

`session_fp` is minted server-side into st.session_state and never reaches the browser. It
is the rate-limit bucket for UI traffic, because every UI request shares one TCP peer as far
as the backend is concerned.
"""

import os
import secrets
from dataclasses import dataclass, field

import httpx

from backend.feedback import USER_VERDICTS

MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "5000"))


class BackendError(RuntimeError):
    """The backend answered with a status the UI must surface rather than swallow."""


class RateLimited(BackendError):
    """429. The user is told to slow down; nothing is retried automatically."""


def new_session_fp() -> str:
    return secrets.token_hex(8)


@dataclass
class BackendClient:
    base_url: str
    api_key: str
    session_fp: str
    timeout: float = 10.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"X-API-Key": self.api_key, "X-Session-Fp": self.session_fp}
        headers.update(extra or {})
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            raise RateLimited(response.json().get("detail", "rate limited"))
        if response.status_code >= 400:
            raise BackendError(f"{response.status_code}: {response.text[:200]}")

    def predict(self, text: str) -> dict:
        if len(text) > MAX_INPUT_CHARS:
            raise ValueError(f"comment exceeds MAX_INPUT_CHARS={MAX_INPUT_CHARS}")
        with self._client() as client:
            response = client.post("/predict", json={"text": text}, headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def user_feedback(self, request_id: str, verdict: str) -> dict:
        if verdict not in USER_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(USER_VERDICTS)}")
        with self._client() as client:
            response = client.post(
                "/feedback/user",
                json={"request_id": request_id, "verdict": verdict},
                headers=self._headers(),
            )
        self._raise_for_status(response)
        return response.json()

    def login(self, secret: str) -> str:
        with self._client() as client:
            response = client.post("/review/login", json={"secret": secret})
        self._raise_for_status(response)
        return response.json()["token"]

    def pending(self, token: str, limit: int = 20) -> list[dict]:
        with self._client() as client:
            response = client.get(
                "/review/pending",
                params={"limit": limit},
                headers=self._headers({"Authorization": f"Bearer {token}"}),
            )
        self._raise_for_status(response)
        return response.json()["items"]

    def submit(self, token: str, request_id: str, labels: dict[str, int]) -> dict:
        with self._client() as client:
            response = client.post(
                "/review/submit",
                json={"request_id": request_id, "labels": labels},
                headers=self._headers({"Authorization": f"Bearer {token}"}),
            )
        self._raise_for_status(response)
        return response.json()
```

`frontend/ui.py`:
```python
"""User-facing moderation UI. Streamlit, port 8501.

Submits a comment to the backend, shows the decision and the six calibrated probabilities,
and offers the two-click agree/disagree control that rubric 3.2 grades. The control writes a
feedback row with source='user'; a disagreement additionally refers the item into the human
review queue, which is how a user's opinion reaches live accuracy -- through a reviewer,
never through arithmetic on an anonymous click.
"""

import os

import pandas as pd
import streamlit as st

from frontend.api_client import MAX_INPUT_CHARS, BackendClient, BackendError, RateLimited, new_session_fp
from frontend.render import render_comment
from model.labels import LABELS

st.set_page_config(page_title="Toxic Comment Moderation", layout="centered")


def get_client() -> BackendClient:
    if "session_fp" not in st.session_state:
        # Server-side only. This value is never sent to the browser; it is the rate-limit
        # bucket for UI-originated traffic.
        st.session_state["session_fp"] = new_session_fp()
    return BackendClient(
        base_url=os.environ["BACKEND_URL"],
        api_key=os.environ.get("DEMO_API_KEY", ""),
        session_fp=st.session_state["session_fp"],
    )


def main() -> None:
    st.title("Toxic comment moderation")
    st.caption(
        "Submit a comment to see the moderation decision and the six per-label calibrated "
        "probabilities. Comments are retained for 30 days and then the text is purged."
    )

    text = st.text_area("Comment", max_chars=MAX_INPUT_CHARS, height=140)
    if st.button("Check comment", type="primary", disabled=not text.strip()):
        try:
            st.session_state["result"] = get_client().predict(text)
            st.session_state["submitted_text"] = text
            st.session_state.pop("feedback_sent", None)
        except BackendError as exc:
            st.error(f"The backend refused the request: {exc}")

    result = st.session_state.get("result")
    if not result:
        return

    st.subheader("Comment as scored")
    render_comment(st.session_state.get("submitted_text"))

    decision = result["decision"]
    st.metric("Decision", decision.upper())
    st.progress(min(max(result["max_prob"], 0.0), 1.0), text=f"max probability {result['max_prob']:.3f}")

    table = pd.DataFrame(
        [
            {
                "label": label,
                "probability": result["labels"][label]["prob"],
                "flagged": result["labels"][label]["flag"],
            }
            for label in LABELS
        ]
    )
    st.dataframe(table, hide_index=True, use_container_width=True)

    st.subheader("Was this decision right?")
    st.caption(
        "Your answer is stored as user feedback. A disagreement also sends the comment to a "
        "human reviewer."
    )
    agree, disagree = st.columns(2)
    sent = st.session_state.get("feedback_sent")
    if agree.button("Agree", disabled=bool(sent), use_container_width=True):
        _send_feedback(result["request_id"], "agree")
    if disagree.button("Disagree", disabled=bool(sent), use_container_width=True):
        _send_feedback(result["request_id"], "disagree")
    if sent:
        st.success(f"Recorded: {sent}. Thank you.")


def _send_feedback(request_id: str, verdict: str) -> None:
    try:
        get_client().user_feedback(request_id, verdict)
        st.session_state["feedback_sent"] = verdict
    except RateLimited:
        st.warning("Too much feedback from this session. Try again later.")
    except BackendError as exc:
        st.error(f"Feedback was not recorded: {exc}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_api_client.py tests/unit/test_no_unsafe_html.py -v`
Expected: 7 PASS in `test_api_client.py`, and the HTML scan still green over the new files.

- [ ] **Step 5: Commit**

```bash
git add frontend/api_client.py frontend/ui.py tests/unit/test_api_client.py
git commit -m "Add user UI with a two-click agree or disagree feedback control"
```

---

### Task 11: Reviewer UI on its own port (premortem H12)

The reviewer console writes the graded metric. It gets its own process, its own port, and — in the next task — its own security group, so opening the demo port never opens it.

**Files:**
- Create: `frontend/reviewer.py`
- Test: `tests/unit/test_reviewer_ui.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_reviewer_ui.py`:
```python
from pathlib import Path

from frontend.reviewer import REVIEWER_PORT, build_label_payload, challenger_column
from infra.exposure import PORTS
from model.labels import LABELS


def test_reviewer_runs_on_its_own_port_not_the_user_ui_port():
    assert REVIEWER_PORT == PORTS["reviewer_ui"].number
    assert REVIEWER_PORT != PORTS["user_ui"].number
    assert REVIEWER_PORT != PORTS["monitoring"].number


def test_label_payload_is_complete_and_binary():
    payload = build_label_payload({"toxic": True, "insult": True})
    assert set(payload) == set(LABELS)
    assert payload["toxic"] == 1
    assert payload["threat"] == 0
    assert all(value in (0, 1) for value in payload.values())


def test_challenger_column_degrades_when_the_rescorer_is_cut():
    """C8: the re-scorer sits behind the cut-line. The reviewer must still work."""
    assert challenger_column(None) == {label: None for label in LABELS}
    assert challenger_column({label: 0.5 for label in LABELS})["toxic"] == 0.5


def test_challenger_column_tolerates_a_partial_payload():
    assert challenger_column({"toxic": 0.9})["threat"] is None


def test_reviewer_module_holds_no_database_import():
    source = Path("frontend/reviewer.py").read_text()
    for forbidden in ("sqlalchemy", "psycopg", "create_engine", "DATABASE_URL"):
        assert forbidden not in source, (
            "The reviewer UI must reach Postgres only through the backend API (H12/H16)."
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_reviewer_ui.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'frontend.reviewer'`.

- [ ] **Step 3: Write minimal implementation**

`frontend/reviewer.py`:
```python
"""Reviewer console. Streamlit, port 8503 -- deliberately not 8501.

The premortem (H12) found that opening ingress for the demo also exposed this console, on
the same host and port as the user UI, behind one shared secret, with direct database write
access to the graded metric. Two changes answer that: its own port with its own security
group (infra/terraform/app_ingress.tf), and no database credential at all -- every write
goes through the backend, which derives `reviewer_id` from the session it issued.

The comment is rendered verbatim through frontend.render, so the string being labelled is
byte-identical to the string the classifier scored.
"""

import os

import pandas as pd
import streamlit as st

from frontend.api_client import BackendClient, BackendError, new_session_fp
from frontend.render import render_comment
from infra.exposure import PORTS
from model.labels import LABELS

REVIEWER_PORT = PORTS["reviewer_ui"].number

st.set_page_config(page_title="Moderation review queue", layout="wide")


def build_label_payload(checked: dict[str, bool]) -> dict[str, int]:
    return {label: int(bool(checked.get(label, False))) for label in LABELS}


def challenger_column(distilbert_probs: dict[str, float] | None) -> dict[str, float | None]:
    probs = distilbert_probs or {}
    return {label: probs.get(label) for label in LABELS}


def get_client() -> BackendClient:
    if "session_fp" not in st.session_state:
        st.session_state["session_fp"] = new_session_fp()
    return BackendClient(
        base_url=os.environ["BACKEND_URL"],
        api_key=os.environ.get("DEMO_API_KEY", ""),
        session_fp=st.session_state["session_fp"],
    )


def _login_form() -> None:
    st.title("Moderation review queue")
    secret = st.text_input("Reviewer secret", type="password")
    if st.button("Sign in", type="primary", disabled=not secret):
        try:
            st.session_state["token"] = get_client().login(secret)
            st.rerun()
        except BackendError:
            st.error("Invalid reviewer secret.")


def main() -> None:
    if "token" not in st.session_state:
        _login_form()
        return

    client = get_client()
    token = st.session_state["token"]
    st.title("Moderation review queue")

    try:
        items = client.pending(token, limit=20)
    except BackendError as exc:
        st.error(f"Could not load the queue: {exc}")
        st.session_state.pop("token", None)
        return

    if not items:
        st.info("The queue is empty.")
        return

    item = items[0]
    st.caption(
        f"{len(items)} item(s) waiting. Showing {item['request_id']} "
        f"(stratum: {item['source']}, status: {item['status']})"
    )

    st.subheader("Comment, exactly as scored")
    render_comment(item["input_text_snapshot"])

    challenger = challenger_column(item.get("distilbert_probs"))
    scores = pd.DataFrame(
        [
            {
                "label": label,
                "production model": item["model_probs"][label],
                "challenger (DistilBERT)": challenger[label],
            }
            for label in LABELS
        ]
    )
    st.dataframe(scores, hide_index=True, use_container_width=True)
    if all(value is None for value in challenger.values()):
        st.caption("Challenger scores are not available for this item.")

    st.subheader("Your labels")
    checked = {
        label: st.checkbox(label, value=item["model_probs"][label] >= 0.5, key=f"cb_{label}")
        for label in LABELS
    }

    if st.button("Submit review", type="primary"):
        try:
            client.submit(token, item["request_id"], build_label_payload(checked))
            for label in LABELS:
                st.session_state.pop(f"cb_{label}", None)
            st.success(f"Recorded review for {item['request_id']}.")
            st.rerun()
        except BackendError as exc:
            st.error(f"Review was not recorded: {exc}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_reviewer_ui.py -v`
Expected: 5 PASS (after Task 12 lands `infra/exposure.py`; run Task 12 Step 3 first if the import fails, then return here — the two tasks share one contract by design).

- [ ] **Step 5: Commit**

```bash
git add frontend/reviewer.py tests/unit/test_reviewer_ui.py
git commit -m "Add reviewer console on its own port with no database credential"
```

---

### Task 12: Port exposure contract and app-tier security groups (premortem H12, C6)

> **SCOPE CORRECTION — read before starting this task.** As originally written, this task creates `infra/terraform/app_ingress.tf` declaring `aws_security_group.backend | frontend | reviewer | monitoring | db` and `variable "operator_cidrs"` in the root module `infra/terraform/`. **Phase A2 Task 5 declares four of those same security groups in `network.tf` and `variable "operator_cidrs"` in `variables.tf`, in the same root module.** Terraform refuses to plan a root module with duplicate resource or variable addresses, so `terraform validate` — a required CI job in Phase 4 Task 8 — fails the moment both files land, main goes red, and no deploy is possible. Each plan's tests parse only its own file, so neither suite can see the collision.
>
> **Phase A2 is the Terraform scope of record.** A2 Task 5a now owns the single declaration of every security group, the single `operator_cidrs` / `demo_cidrs` variable pair, and the reviewer group. **This task is reduced to the Python exposure contract only:** `infra/__init__.py` and `infra/exposure.py`, plus the tests that assert the Python contract and assert that A2's Terraform matches it.
>
> Concretely, when executing this task:
> - Do **not** create `infra/terraform/app_ingress.tf`. Do not create the `variable "operator_cidrs"` or `variable "demo_ingress_cidrs"` blocks; the canonical toggle name is A2's `demo_cidrs`, singular.
> - Keep every test in Step 1 that reads only `infra/exposure.py` (`test_reviewer_ui_is_operator_only`, `test_the_demo_toggle_covers_exactly_the_graded_surface`, `test_every_port_is_unique`).
> - **Replace** every test in Step 1 that reads `TF = Path("infra/terraform/app_ingress.tf")` with the two cases below, which read A2's file instead and hold A2 to this contract:
>
> ```python
> def test_the_exposure_contract_declares_no_terraform_of_its_own():
>     assert not Path("infra/terraform/app_ingress.tf").exists(), (
>         "security groups are declared once, in Phase A2's network.tf; two declarations of "
>         "one address is a `terraform validate` failure and CI is a required check"
>     )
>
>
> def test_the_python_contract_matches_the_terraform_locals():
>     source = Path("infra/terraform/network.tf").read_text(encoding="utf-8")
>     block = re.search(r"locals\s*\{(.*?)\n\}", source, re.S).group(1)
>     declared = {n: int(v) for n, v in re.findall(r"(\w+)\s*=\s*(\d+)", block)}
>     assert declared == {name: port.number for name, port in PORTS.items()}
> ```
>
> The reviewer-UI property this task exists to protect (H12: 8503 is never carried by the demo toggle) is **strengthened**, not weakened, by the move: A2 Task 5a gives the reviewer group egress and **no ingress at all**, and adds a repo-wide scan for any rule of any type touching 8503. Under the original `app_ingress.tf`, 8503 was open to `var.operator_cidrs` — a public-internet CIDR — which silently falsified `docs/tls-decision.md`'s entire justification for accepting cleartext (premortem H15).

The reviewer UI must not be exposed by the demo ingress toggle. That is a Terraform property, so it needs a Terraform artifact and a test that reads it. This task also adds the explicit egress that C6 found missing, for the three groups it owns.

**Files:**
- Create: `infra/__init__.py`, `infra/exposure.py` (~~`infra/terraform/app_ingress.tf`~~ — see the scope correction above; A2 Task 5a owns it)
- Test: `tests/unit/test_exposure_contract.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_exposure_contract.py`:
```python
import re
from pathlib import Path

from infra.exposure import DEMO_EXPOSED_PORTS, OPERATOR_ONLY_PORTS, PORTS

TF = Path("infra/terraform/app_ingress.tf")


def test_reviewer_ui_is_operator_only():
    assert PORTS["reviewer_ui"].number == 8503
    assert PORTS["reviewer_ui"].demo_exposed is False
    assert 8503 in OPERATOR_ONLY_PORTS
    assert 8503 not in DEMO_EXPOSED_PORTS


def test_the_demo_toggle_covers_exactly_the_graded_surface():
    assert DEMO_EXPOSED_PORTS == {8000, 8501, 8502}


def test_every_port_is_unique():
    numbers = [port.number for port in PORTS.values()]
    assert len(numbers) == len(set(numbers))


def _blocks(kind: str) -> list[str]:
    source = TF.read_text()
    return re.findall(rf'resource\s+"{kind}"\s+"[^"]+"\s*\{{(.*?)\n\}}', source, re.S)


def test_terraform_declares_a_demo_toggle_variable():
    assert "var.demo_ingress_cidrs" in TF.read_text()


def test_no_demo_exposed_rule_reaches_an_operator_only_port():
    """H12: opening 8501 for the grader must not also open the console that writes the
    graded metric."""
    for block in _blocks("aws_vpc_security_group_ingress_rule"):
        if "demo_ingress_cidrs" not in block:
            continue
        ports = {int(value) for value in re.findall(r"(?:from|to)_port\s*=\s*(\d+)", block)}
        ports |= {
            PORTS[name].number
            for name in re.findall(r"local\.ports\.(\w+)", block)
            if name in PORTS
        }
        assert not (ports & OPERATOR_ONLY_PORTS), (
            f"a demo-exposed ingress rule reaches {sorted(ports & OPERATOR_ONLY_PORTS)}"
        )


def test_reviewer_rule_is_restricted_to_the_operator_cidrs():
    reviewer_blocks = [
        block for block in _blocks("aws_vpc_security_group_ingress_rule")
        if "local.ports.reviewer_ui" in block
    ]
    assert reviewer_blocks, "no ingress rule declares the reviewer UI port"
    for block in reviewer_blocks:
        assert "var.operator_cidrs" in block
        assert "demo_ingress_cidrs" not in block


def test_terraform_port_map_matches_the_python_contract():
    source = TF.read_text()
    locals_block = re.search(r"locals\s*\{(.*?)\n\}", source, re.S).group(1)
    declared = {
        name: int(value)
        for name, value in re.findall(r"(\w+)\s*=\s*(\d+)", locals_block)
    }
    assert declared == {name: port.number for name, port in PORTS.items()}


def test_database_ingress_admits_only_the_backend_and_the_monitoring_tier():
    db_blocks = [
        block for block in _blocks("aws_vpc_security_group_ingress_rule")
        if "5432" in block
    ]
    assert db_blocks
    referenced = set()
    for block in db_blocks:
        referenced |= set(re.findall(r"aws_security_group\.(\w+)\.id", block))
    assert referenced == {"db", "backend", "monitoring"}


def test_every_app_group_declares_explicit_egress():
    """C6: aws_security_group without an egress block removes the default allow-all, and
    an instance that cannot reach 443 never registers with SSM -- with no SSH to fall back
    on."""
    egress = _blocks("aws_vpc_security_group_egress_rule")
    assert egress, "no explicit egress rule; SSM registration would fail"
    groups = set()
    for block in egress:
        groups |= set(re.findall(r"aws_security_group\.(\w+)\.id", block))
    assert {"backend", "frontend", "reviewer", "monitoring"} <= groups
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_exposure_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'infra'`.

- [ ] **Step 3: Write minimal implementation**

`infra/__init__.py`: empty.

`infra/exposure.py`:
```python
"""Port assignments and their exposure class. The single Python source of truth.

The demo ingress toggle (`var.demo_ingress_cidrs`) exists so a grader can reach the live
system for a window. The premortem (H12) found that the same toggle would have exposed the
reviewer console -- a direct write path to the graded metric behind one shared secret. The
reviewer UI therefore gets a port that no demo-exposed rule may ever carry, and
tests/unit/test_exposure_contract.py enforces that against the Terraform.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Port:
    number: int
    service: str
    demo_exposed: bool
    note: str


PORTS: dict[str, Port] = {
    "backend_api": Port(8000, "fastapi", True, "Graded: /predict and /health, README curl examples"),
    "user_ui": Port(8501, "streamlit-user", True, "Graded: rubric 3.1 frontend"),
    "monitoring": Port(8502, "streamlit-monitoring", True, "Graded: rubric 3.2 dashboard"),
    "reviewer_ui": Port(8503, "streamlit-reviewer", False, "Operator only. Premortem H12"),
}

DEMO_EXPOSED_PORTS: frozenset[int] = frozenset(
    port.number for port in PORTS.values() if port.demo_exposed
)
OPERATOR_ONLY_PORTS: frozenset[int] = frozenset(
    port.number for port in PORTS.values() if not port.demo_exposed
)
```

`infra/terraform/app_ingress.tf`:
```hcl
# App-tier security groups. Phase A2 consumes these; it must not redeclare them.
#
# Two properties are load-bearing and are enforced by tests/unit/test_exposure_contract.py:
#   1. The reviewer UI (8503) is never carried by a rule sourced from var.demo_ingress_cidrs.
#   2. Every group declares explicit egress, because aws_security_group without an egress
#      block removes the default allow-all and the instance never registers with SSM.

locals {
  ports = {
    backend_api = 8000
    user_ui     = 8501
    monitoring  = 8502
    reviewer_ui = 8503
  }
}

variable "operator_cidrs" {
  description = "Operator source addresses. Always allowed."
  type        = list(string)
}

variable "demo_ingress_cidrs" {
  description = "Demo window toggle. Set to [\"0.0.0.0/0\"] to open the graded surface, then empty it again."
  type        = list(string)
  default     = []
}

resource "aws_security_group" "backend" {
  name        = "mlops-toxic-backend"
  description = "FastAPI backend"
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "frontend" {
  name        = "mlops-toxic-frontend"
  description = "Streamlit user UI"
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "reviewer" {
  name        = "mlops-toxic-reviewer"
  description = "Streamlit reviewer console. Operator only, never demo exposed."
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "monitoring" {
  name        = "mlops-toxic-monitoring"
  description = "Streamlit monitoring dashboard"
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "db" {
  name        = "mlops-toxic-db"
  description = "RDS Postgres. Reachable from the backend and the dashboard only."
  vpc_id      = var.vpc_id
}

# --- operator ingress: every tier ---
resource "aws_vpc_security_group_ingress_rule" "backend_operator" {
  for_each          = toset(var.operator_cidrs)
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = each.value
  from_port         = local.ports.backend_api
  to_port           = local.ports.backend_api
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "frontend_operator" {
  for_each          = toset(var.operator_cidrs)
  security_group_id = aws_security_group.frontend.id
  cidr_ipv4         = each.value
  from_port         = local.ports.user_ui
  to_port           = local.ports.user_ui
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "monitoring_operator" {
  for_each          = toset(var.operator_cidrs)
  security_group_id = aws_security_group.monitoring.id
  cidr_ipv4         = each.value
  from_port         = local.ports.monitoring
  to_port           = local.ports.monitoring
  ip_protocol       = "tcp"
}

# --- reviewer console: operator only. This rule must never reference the demo toggle. ---
resource "aws_vpc_security_group_ingress_rule" "reviewer_operator_only" {
  for_each          = toset(var.operator_cidrs)
  security_group_id = aws_security_group.reviewer.id
  cidr_ipv4         = each.value
  from_port         = local.ports.reviewer_ui
  to_port           = local.ports.reviewer_ui
  ip_protocol       = "tcp"
  description       = "Reviewer console. Premortem H12: never exposed by var.demo_ingress_cidrs."
}

# --- demo window toggle: the graded surface only ---
resource "aws_vpc_security_group_ingress_rule" "backend_demo" {
  for_each          = toset(var.demo_ingress_cidrs)
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = each.value
  from_port         = local.ports.backend_api
  to_port           = local.ports.backend_api
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "frontend_demo" {
  for_each          = toset(var.demo_ingress_cidrs)
  security_group_id = aws_security_group.frontend.id
  cidr_ipv4         = each.value
  from_port         = local.ports.user_ui
  to_port           = local.ports.user_ui
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "monitoring_demo" {
  for_each          = toset(var.demo_ingress_cidrs)
  security_group_id = aws_security_group.monitoring.id
  cidr_ipv4         = each.value
  from_port         = local.ports.monitoring
  to_port           = local.ports.monitoring
  ip_protocol       = "tcp"
}

# --- UI tiers reach the backend directly ---
resource "aws_vpc_security_group_ingress_rule" "backend_from_frontend" {
  security_group_id            = aws_security_group.backend.id
  referenced_security_group_id = aws_security_group.frontend.id
  from_port                    = local.ports.backend_api
  to_port                      = local.ports.backend_api
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "backend_from_reviewer" {
  security_group_id            = aws_security_group.backend.id
  referenced_security_group_id = aws_security_group.reviewer.id
  from_port                    = local.ports.backend_api
  to_port                      = local.ports.backend_api
  ip_protocol                  = "tcp"
}

# --- database: backend (read/write) and monitoring (read-only role) only ---
resource "aws_vpc_security_group_ingress_rule" "db_from_backend" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.backend.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_monitoring" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.monitoring.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# --- explicit egress (premortem C6): without this, SSM never registers and there is no SSH ---
resource "aws_vpc_security_group_egress_rule" "backend_https" {
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "frontend_https" {
  security_group_id = aws_security_group.frontend.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "reviewer_https" {
  security_group_id = aws_security_group.reviewer.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "monitoring_https" {
  security_group_id = aws_security_group.monitoring.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "backend_to_db" {
  security_group_id            = aws_security_group.backend.id
  referenced_security_group_id = aws_security_group.db.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "monitoring_to_db" {
  security_group_id            = aws_security_group.monitoring.id
  referenced_security_group_id = aws_security_group.db.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "ui_to_backend" {
  for_each                     = toset(["frontend", "reviewer"])
  security_group_id            = each.key == "frontend" ? aws_security_group.frontend.id : aws_security_group.reviewer.id
  referenced_security_group_id = aws_security_group.backend.id
  from_port                    = local.ports.backend_api
  to_port                      = local.ports.backend_api
  ip_protocol                  = "tcp"
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_exposure_contract.py tests/unit/test_reviewer_ui.py -v`
Expected: 8 PASS in `test_exposure_contract.py`, 5 PASS in `test_reviewer_ui.py`.

- [ ] **Step 5: Commit**

```bash
git add infra/__init__.py infra/exposure.py infra/terraform/app_ingress.tf tests/unit/test_exposure_contract.py
git commit -m "Isolate the reviewer console on its own port and security group"
```

---
### Task 13: Latency over time, as percentiles (rubric 3.2, premortem H28)

Rubric 3.2 names "prediction latency over time". A scatter of raw points across four minutes is not that. This returns per-day buckets with n, p50, and p95, so the chart says something about the tail.

**Files:**
- Create: `monitoring/queries.py` (first function)
- Test: `tests/integration/test_queries.py` (first tests)

- [ ] **Step 1: Write the failing test**

`tests/integration/test_queries.py`:
```python
import datetime as dt

import pytest
from sqlalchemy import text

from model.labels import LABELS
from monitoring.queries import latency_over_time

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 14, 12, 0, tzinfo=dt.timezone.utc)


def insert_prediction(conn, request_id, ts, probs=None, latency_ms=20, is_seed=False):
    probs = probs or {label: 0.05 for label in LABELS}
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms, is_seed) VALUES (:rid, :ts, 'text', 'm', {binds}, "
            "'allow', :mx, :lat, :seed)"
        ),
        {
            "rid": request_id,
            "ts": ts,
            "mx": max(probs.values()),
            "lat": latency_ms,
            "seed": is_seed,
            **{f"p_{label}": probs[label] for label in LABELS},
        },
    )


def test_latency_buckets_by_day_with_percentiles(conn):
    for day in range(8):
        ts = NOW - dt.timedelta(days=day)
        for i in range(10):
            insert_prediction(conn, f"p{day}_{i}", ts, latency_ms=10 + i * 10)
    conn.commit()

    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=14))
    assert len(buckets) == 8
    assert [b.n for b in buckets] == [10] * 8
    assert buckets[0].bucket < buckets[-1].bucket
    assert buckets[0].p50 == pytest.approx(55.0)
    assert buckets[0].p95 == pytest.approx(95.5)
    assert buckets[0].p95 >= buckets[0].p50


def test_latency_on_an_empty_table_returns_an_empty_list_not_a_crash(conn):
    assert latency_over_time(conn, since=NOW - dt.timedelta(days=14)) == []


def test_latency_respects_the_window(conn):
    insert_prediction(conn, "old", NOW - dt.timedelta(days=40))
    insert_prediction(conn, "new", NOW - dt.timedelta(days=1))
    conn.commit()
    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=14))
    assert len(buckets) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration`
Expected: FAIL with `ModuleNotFoundError: No module named 'monitoring.queries'`.

- [ ] **Step 3: Write minimal implementation**

`monitoring/queries.py`:
```python
"""Read-only aggregations for the monitoring dashboard.

Every statement here is a SELECT. The dashboard connects with a read-only database role
(premortem H16) and never selects `input_text` or `input_text_snapshot`, because the
dashboard screenshot is a public deliverable and must not carry raw user text
(delivery spec section 6.4).

Flags are recomputed as `prob_<label> >= thresholds[<label>]` rather than read from a
stored column, so the production series and the Phase 1 baseline always share one decision
rule. That is what makes the PSI comparison in `drift_report` mean anything.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import text

from model.labels import LABELS


@dataclass(frozen=True)
class LatencyBucket:
    bucket: dt.datetime
    n: int
    p50: float
    p95: float


def latency_over_time(conn, since: dt.datetime) -> list[LatencyBucket]:
    rows = conn.execute(
        text(
            "SELECT date_trunc('day', ts) AS bucket, count(*) AS n, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
            "FROM predictions WHERE ts >= :since GROUP BY 1 ORDER BY 1"
        ),
        {"since": since},
    ).all()
    return [
        LatencyBucket(bucket=row.bucket, n=int(row.n), p50=float(row.p50), p95=float(row.p95))
        for row in rows
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/queries.py tests/integration/test_queries.py
git commit -m "Add latency percentile aggregation for the monitoring dashboard"
```

---

### Task 14: Target drift against the stored baseline (rubric 3.2, delivery spec §11 drift row)

A chart of production flag rates alone cannot answer whether anything changed. This plots production against the Phase 1 reference, with a per-label PSI and a stated alert threshold.

**Files:**
- Modify: `monitoring/queries.py`
- Test: `tests/integration/test_queries.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_queries.py`:
```python
from pathlib import Path

from monitoring.baseline import Baseline, load_thresholds
from monitoring.queries import DriftRow, drift_report, flag_rate_series

THRESHOLDS = load_thresholds(Path("tests/fixtures/thresholds.json"))
BASELINE = Baseline(
    schema_version=1,
    data_version="d",
    model_version="toxic-clf:v3",
    n=1000,
    flag_rates={
        "toxic": 0.10, "severe_toxic": 0.01, "obscene": 0.05,
        "threat": 0.003, "insult": 0.05, "identity_hate": 0.009,
    },
)


def _toxic_probs(value: float) -> dict[str, float]:
    probs = {label: 0.01 for label in LABELS}
    probs["toxic"] = value
    return probs


def test_drift_report_returns_one_row_per_label_with_a_reference(conn):
    for i in range(100):
        # 30 of 100 above the 0.45 toxic threshold -> production rate 0.30 vs baseline 0.10
        insert_prediction(conn, f"d{i}", NOW - dt.timedelta(hours=i),
                          probs=_toxic_probs(0.9 if i < 30 else 0.1))
    conn.commit()

    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert [row.label for row in rows] == list(LABELS)
    toxic = next(row for row in rows if row.label == "toxic")
    assert isinstance(toxic, DriftRow)
    assert toxic.baseline_rate == pytest.approx(0.10)
    assert toxic.production_rate == pytest.approx(0.30)
    assert toxic.psi == pytest.approx(0.26999, abs=1e-4)
    assert toxic.js == pytest.approx(0.04678, abs=1e-4)
    assert toxic.alert is True


def test_a_stable_label_does_not_alert(conn):
    for i in range(100):
        insert_prediction(conn, f"s{i}", NOW - dt.timedelta(hours=i),
                          probs=_toxic_probs(0.9 if i < 10 else 0.1))
    conn.commit()
    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    toxic = next(row for row in rows if row.label == "toxic")
    assert toxic.production_rate == pytest.approx(0.10)
    assert toxic.psi == pytest.approx(0.0, abs=1e-9)
    assert toxic.alert is False


def test_drift_on_an_empty_window_reports_zero_rates_without_dividing_by_zero(conn):
    rows = drift_report(conn, since=NOW - dt.timedelta(days=14),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert len(rows) == len(LABELS)
    assert all(row.production_rate == 0.0 for row in rows)
    assert all(row.psi >= 0.0 for row in rows)


def test_flag_rate_series_has_one_row_per_bucket_and_one_column_per_label(conn):
    for day in range(7):
        for i in range(5):
            insert_prediction(conn, f"t{day}_{i}", NOW - dt.timedelta(days=day),
                              probs=_toxic_probs(0.9 if i < 2 else 0.1))
    conn.commit()
    frame = flag_rate_series(conn, since=NOW - dt.timedelta(days=14), thresholds=THRESHOLDS)
    assert len(frame) == 7
    assert list(frame.columns) == ["bucket", *LABELS]
    assert frame["toxic"].iloc[0] == pytest.approx(0.4)
    assert frame["threat"].iloc[0] == pytest.approx(0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration -k drift`
Expected: FAIL with `ImportError: cannot import name 'drift_report' from 'monitoring.queries'`.

- [ ] **Step 3: Write minimal implementation**

Append to `monitoring/queries.py`:
```python
import pandas as pd

from monitoring.baseline import Baseline
from monitoring.stats import js_divergence, psi

# Standard PSI reading: < 0.1 no meaningful shift, 0.1-0.2 moderate, >= 0.2 major.
DEFAULT_ALERT_PSI = 0.2


@dataclass(frozen=True)
class DriftRow:
    label: str
    baseline_rate: float
    production_rate: float
    psi: float
    js: float
    alert: bool


def _flag_sum_sql() -> str:
    return ", ".join(
        f"sum(CASE WHEN prob_{label} >= :thr_{label} THEN 1 ELSE 0 END) AS flag_{label}"
        for label in LABELS
    )


def _threshold_binds(thresholds: dict[str, float]) -> dict[str, float]:
    return {f"thr_{label}": float(thresholds[label]) for label in LABELS}


def production_flag_rates(conn, since: dt.datetime, thresholds: dict[str, float]) -> tuple[int, dict[str, float]]:
    row = conn.execute(
        text(f"SELECT count(*) AS n, {_flag_sum_sql()} FROM predictions WHERE ts >= :since"),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().one()
    n = int(row["n"])
    if n == 0:
        return 0, {label: 0.0 for label in LABELS}
    return n, {label: float(row[f"flag_{label}"] or 0) / n for label in LABELS}


def drift_report(
    conn,
    since: dt.datetime,
    thresholds: dict[str, float],
    baseline: Baseline,
    alert_psi: float = DEFAULT_ALERT_PSI,
) -> list[DriftRow]:
    _, production = production_flag_rates(conn, since, thresholds)
    rows = []
    for label in LABELS:
        reference = baseline.flag_rates[label]
        observed = production[label]
        score = psi(reference, observed)
        rows.append(
            DriftRow(
                label=label,
                baseline_rate=reference,
                production_rate=observed,
                psi=score,
                js=js_divergence(reference, observed),
                alert=score >= alert_psi,
            )
        )
    return rows


def flag_rate_series(conn, since: dt.datetime, thresholds: dict[str, float]) -> pd.DataFrame:
    rows = conn.execute(
        text(
            f"SELECT date_trunc('day', ts) AS bucket, count(*) AS n, {_flag_sum_sql()} "
            "FROM predictions WHERE ts >= :since GROUP BY 1 ORDER BY 1"
        ),
        {"since": since, **_threshold_binds(thresholds)},
    ).mappings().all()
    records = []
    for row in rows:
        n = int(row["n"]) or 1
        record = {"bucket": row["bucket"]}
        for label in LABELS:
            record[label] = float(row[f"flag_{label}"] or 0) / n
        records.append(record)
    return pd.DataFrame(records, columns=["bucket", *LABELS])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/queries.py tests/integration/test_queries.py
git commit -m "Plot target drift against the stored baseline with per-label PSI"
```

---

### Task 15: Live accuracy, design-weighted (premortem H8, H9)

The graded number. Stratified collection without stratified estimation is still biased, so this weights by the inclusion probability stored at enqueue time, reports per-stratum n, and never lets a self-selected user click into the estimate.

**Files:**
- Modify: `monitoring/queries.py`
- Test: `tests/integration/test_queries.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_queries.py`:
```python
import json

from monitoring.queries import live_accuracy, user_feedback_panel


def _reviewed(conn, request_id, stratum, sample_rate, correct, ts=None):
    ts = ts or NOW - dt.timedelta(days=1)
    insert_prediction(conn, request_id, ts)
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot, reviewer_id, reviewed_ts) VALUES (:rid, :ts, 'reviewed', "
            ":src, :rate, 'text', 'rock', :ts)"
        ),
        {"rid": request_id, "ts": ts, "src": stratum, "rate": sample_rate},
    )
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES (:rid, :ts, 'reviewer', 'rock', CAST(:agree AS jsonb), :exact)"
        ),
        {
            "rid": request_id,
            "ts": ts,
            "agree": json.dumps({label: correct for label in LABELS}),
            "exact": correct,
        },
    )


def test_live_accuracy_is_design_weighted_not_pooled(conn):
    for i in range(200):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=i < 120)
    for i in range(20):
        _reviewed(conn, f"ra{i}", "random-audit", 0.05, correct=i < 19)
    conn.commit()

    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 220
    assert report.point == pytest.approx(0.83333, abs=1e-4)   # pooled would be 0.63182
    assert {s.stratum for s in report.strata} == {"flagged", "random-audit"}
    assert next(s for s in report.strata if s.stratum == "flagged").n == 200
    assert report.lo < report.point < report.hi


def test_live_accuracy_on_an_empty_table_is_none_not_a_zero_division(conn):
    """C5: this is the panel that renders NaN or a traceback in the graded screenshot when
    nothing has ever been reviewed."""
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 0
    assert report.point is None
    assert report.strata == []


def test_user_feedback_cannot_move_the_graded_estimate(conn):
    """H9 composed with H8: an anonymous write path must not be an anonymous write path
    INTO THE GRADED METRIC."""
    for i in range(200):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=i < 120)
    conn.commit()
    before = live_accuracy(conn, since=NOW - dt.timedelta(days=14))

    for i in range(200):
        conn.execute(
            text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                 "VALUES (:rid, :ts, 'user', '{}'::jsonb, false)"),
            {"rid": f"fl{i}", "ts": NOW},
        )
    conn.commit()
    after = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert after.point == pytest.approx(before.point)
    assert after.n == before.n


def test_user_report_stratum_is_excluded_from_the_estimate(conn):
    for i in range(10):
        _reviewed(conn, f"fl{i}", "flagged", 1.0, correct=True)
    insert_prediction(conn, "ur1", NOW - dt.timedelta(days=1))
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot, reviewer_id, reviewed_ts) VALUES ('ur1', :ts, 'reviewed', "
            "'user-report', NULL, 'text', 'rock', :ts)"
        ),
        {"ts": NOW - dt.timedelta(days=1)},
    )
    conn.execute(
        text(
            "INSERT INTO feedback (request_id, ts, source, reviewer_id, agreement, exact_match) "
            "VALUES ('ur1', :ts, 'reviewer', 'rock', CAST(:agree AS jsonb), false)"
        ),
        {"ts": NOW - dt.timedelta(days=1),
         "agree": json.dumps({label: False for label in LABELS})},
    )
    conn.commit()
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=14))
    assert report.n == 10
    assert report.point == pytest.approx(1.0)


def test_user_panel_reports_its_own_n_and_interval(conn):
    for i in range(10):
        insert_prediction(conn, f"u{i}", NOW - dt.timedelta(hours=1))
        conn.execute(
            text("INSERT INTO feedback (request_id, ts, source, agreement, exact_match) "
                 "VALUES (:rid, :ts, 'user', '{}'::jsonb, :ok)"),
            {"rid": f"u{i}", "ts": NOW, "ok": i < 8},
        )
    conn.commit()
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=14))
    assert panel.n == 10 and panel.agree == 8
    assert panel.rate == pytest.approx(0.8)
    assert panel.lo == pytest.approx(0.4901, abs=1e-3)
    assert panel.hi == pytest.approx(0.9433, abs=1e-3)


def test_user_panel_on_empty_data_is_none_not_nan(conn):
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=14))
    assert panel.n == 0 and panel.rate is None and panel.lo is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration -k accuracy`
Expected: FAIL with `ImportError: cannot import name 'live_accuracy' from 'monitoring.queries'`.

- [ ] **Step 3: Write minimal implementation**

Append to `monitoring/queries.py`:
```python
from monitoring.stats import AccuracyReport, StratumStat, horvitz_thompson_accuracy, wilson_interval


@dataclass(frozen=True)
class UserPanel:
    n: int
    agree: int
    rate: float | None
    lo: float | None
    hi: float | None


def live_accuracy(conn, since: dt.datetime) -> AccuracyReport:
    """Design-weighted accuracy over the two probability-sampled strata.

    `sample_rate IS NOT NULL` is the filter that keeps self-selected rows out. A
    `user-report` row has no known inclusion probability, and a `source='user'` feedback row
    is not a label at all -- both would bias the estimate and both would make the graded
    number writable by an anonymous visitor.
    """
    rows = conn.execute(
        text(
            "SELECT q.source AS stratum, q.sample_rate, f.exact_match "
            "FROM feedback f "
            "JOIN review_queue q ON q.request_id = f.request_id "
            "JOIN predictions p ON p.request_id = f.request_id "
            "WHERE f.source = 'reviewer' AND q.sample_rate IS NOT NULL AND p.ts >= :since"
        ),
        {"since": since},
    ).all()
    return horvitz_thompson_accuracy(
        (row.stratum, float(row.sample_rate), bool(row.exact_match)) for row in rows
    )


def review_counts(conn, since: dt.datetime) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT q.status, count(*) AS n FROM review_queue q "
            "JOIN predictions p ON p.request_id = q.request_id "
            "WHERE p.ts >= :since GROUP BY 1"
        ),
        {"since": since},
    ).all()
    return {row.status: int(row.n) for row in rows}


def seeded_share(conn, since: dt.datetime) -> tuple[int, int]:
    row = conn.execute(
        text(
            "SELECT count(*) AS total, "
            "sum(CASE WHEN is_seed THEN 1 ELSE 0 END) AS seeded "
            "FROM predictions WHERE ts >= :since"
        ),
        {"since": since},
    ).one()
    return int(row.total), int(row.seeded or 0)


def user_feedback_panel(conn, since: dt.datetime) -> UserPanel:
    row = conn.execute(
        text(
            "SELECT count(*) AS n, sum(CASE WHEN f.exact_match THEN 1 ELSE 0 END) AS agree "
            "FROM feedback f JOIN predictions p ON p.request_id = f.request_id "
            "WHERE f.source = 'user' AND p.ts >= :since"
        ),
        {"since": since},
    ).one()
    n = int(row.n)
    if n == 0:
        return UserPanel(n=0, agree=0, rate=None, lo=None, hi=None)
    agree = int(row.agree or 0)
    lo, hi = wilson_interval(agree, n)
    return UserPanel(n=n, agree=agree, rate=agree / n, lo=lo, hi=hi)
```

Note for the implementer: `StratumStat` is imported for re-export to `monitoring/dashboard.py`; keep the import even though this module does not construct one directly, and add `__all__` if ruff flags it.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_queries.py -v -m integration`
Expected: 13 PASS.

- [ ] **Step 5: Commit**

```bash
git add monitoring/queries.py tests/integration/test_queries.py
git commit -m "Estimate live accuracy with design weights and per-stratum reporting"
```

---

### Task 16: Dashboard assembly, read-only and degenerate-safe (premortem C5, H16, delivery spec §6.4)

The Streamlit app on EC2 #3. Three graded panels, a read-only connection, no raw user text, and an explicit "not enough data" caption everywhere a `NaN` could otherwise reach the screenshot.

**Files:**
- Create: `monitoring/dashboard.py`
- Test: `tests/unit/test_dashboard_guards.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_dashboard_guards.py`:
```python
import re
from pathlib import Path

from monitoring.dashboard import (
    accuracy_caption,
    drift_caption,
    latency_caption,
    user_caption,
)
from monitoring.queries import UserPanel
from monitoring.stats import AccuracyReport, StratumStat

DML = re.compile(
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+TABLE"
    r"|CREATE\s+TABLE|TRUNCATE|GRANT)\b",
    re.IGNORECASE,
)
RAW_TEXT = re.compile(r"\binput_text(_snapshot)?\b")


def test_monitoring_issues_no_write_statements():
    """H16: the dashboard holds a read-only role, and the code must match the grant."""
    for path in sorted(Path("monitoring").rglob("*.py")):
        assert not DML.search(path.read_text()), f"{path} contains a write statement"


def test_monitoring_never_selects_raw_user_text():
    """Delivery spec section 6.4: the dashboard screenshot is a public deliverable."""
    for path in sorted(Path("monitoring").rglob("*.py")):
        assert not RAW_TEXT.search(path.read_text()), f"{path} references raw comment text"


def test_dashboard_uses_a_dedicated_read_only_dsn():
    source = Path("monitoring/dashboard.py").read_text()
    assert "MONITORING_DB_DSN" in source
    assert "DATABASE_URL" not in source


def test_accuracy_caption_on_empty_data_says_so_instead_of_rendering_nan():
    caption = accuracy_caption(
        AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[])
    )
    assert "not enough" in caption.lower()
    assert "nan" not in caption.lower()


def test_accuracy_caption_reports_the_point_the_interval_and_every_stratum_n():
    report = AccuracyReport(
        n=220,
        point=0.8333,
        lo=0.6975,
        hi=0.9156,
        effective_n=43.9,
        strata=[
            StratumStat("flagged", 200, 120, 1.0, 0.60, 0.5308, 0.6654),
            StratumStat("random-audit", 20, 19, 0.05, 0.95, 0.7639, 0.9911),
        ],
    )
    caption = accuracy_caption(report)
    assert "83.3%" in caption
    assert "69.7%" in caption and "91.6%" in caption
    assert "flagged n=200" in caption
    assert "random-audit n=20" in caption
    assert "pi=0.05" in caption


def test_accuracy_caption_warns_when_the_audit_stratum_is_empty():
    report = AccuracyReport(
        n=10, point=1.0, lo=0.7, hi=1.0, effective_n=10.0,
        strata=[StratumStat("flagged", 10, 10, 1.0, 1.0, 0.7, 1.0)],
    )
    assert "audit stratum is empty" in accuracy_caption(report).lower()


def test_latency_caption_requires_seven_buckets_before_claiming_a_trend():
    assert "not enough" in latency_caption(3).lower()
    assert "7" in latency_caption(3)
    assert "not enough" not in latency_caption(14).lower()


def test_drift_caption_names_the_threshold_and_the_alerting_labels():
    caption = drift_caption(["toxic", "threat"], alert_psi=0.2)
    assert "0.2" in caption
    assert "toxic" in caption and "threat" in caption
    assert "no label" in drift_caption([], alert_psi=0.2).lower()


def test_user_caption_says_it_is_self_selected_and_not_the_graded_estimate():
    caption = user_caption(UserPanel(n=40, agree=30, rate=0.75, lo=0.60, hi=0.86))
    assert "self-selected" in caption.lower()
    assert "n=40" in caption
    assert "not" in caption.lower()
    assert "not enough" in user_caption(UserPanel(0, 0, None, None, None)).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard_guards.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'monitoring.dashboard'`.

- [ ] **Step 3: Write minimal implementation**

`monitoring/dashboard.py`:
```python
"""Monitoring dashboard. Streamlit, port 8502, EC2 #3.

Rubric 3.2 asks for three things and this renders exactly those three, plus the honesty
captions that keep a thin dataset from looking like a rich one:

  1. prediction latency over time (p50 and p95 per day),
  2. distribution of predicted classes as target drift, plotted against the Phase 1
     baseline with a per-label PSI and a stated alert threshold,
  3. live accuracy from human feedback, design-weighted, with per-stratum n and a Wilson
     interval -- never a bare point estimate.

The connection uses MONITORING_DB_DSN, which is a read-only role. No panel selects raw
comment text, because this screenshot is a public deliverable.
"""

import datetime as dt
import hashlib
import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import create_engine

from model.labels import LABELS
from monitoring.baseline import BaselineContractError, BaselineMissingError, load_baseline, load_thresholds
from monitoring.queries import (
    DEFAULT_ALERT_PSI,
    UserPanel,
    drift_report,
    flag_rate_series,
    latency_over_time,
    live_accuracy,
    review_counts,
    seeded_share,
    user_feedback_panel,
)
from monitoring.stats import AccuracyReport

WINDOW_DAYS = int(os.environ.get("DASHBOARD_WINDOW_DAYS", "14"))
MIN_BUCKETS = 7
ALERT_PSI = float(os.environ.get("DRIFT_PSI_ALERT", str(DEFAULT_ALERT_PSI)))
BASELINE_PATH = Path(os.environ.get("BASELINE_PATH", "artifacts/baseline_flag_rates.json"))
THRESHOLDS_PATH = Path(os.environ.get("THRESHOLDS_PATH", "artifacts/thresholds.json"))

st.set_page_config(page_title="Toxic moderation monitoring", layout="wide")


def accuracy_caption(report: AccuracyReport) -> str:
    if report.point is None:
        return (
            "Not enough reviewed items to estimate live accuracy. Run `make seed-demo` or "
            "review items in the reviewer console."
        )
    strata = ", ".join(
        f"{s.stratum} n={s.n} (pi={s.sample_rate:g}, {s.accuracy:.1%})" for s in report.strata
    )
    warning = ""
    if not any(s.stratum == "random-audit" for s in report.strata):
        return (
            f"{report.point:.1%} (95% CI {report.lo:.1%}-{report.hi:.1%}); {strata}. "
            "WARNING: the random-audit stratum is empty, so this measures only the model's "
            "own flagged set and is blind to confidently-allowed false negatives."
        )
    return (
        f"{report.point:.1%} (95% CI {report.lo:.1%}-{report.hi:.1%}), Horvitz-Thompson "
        f"weighted by the inclusion probability recorded at enqueue time; {strata}; "
        f"effective n={report.effective_n:.1f}.{warning}"
    )


def latency_caption(n_buckets: int) -> str:
    if n_buckets < MIN_BUCKETS:
        return (
            f"Not enough history: {n_buckets} daily bucket(s), {MIN_BUCKETS} required before "
            "this chart shows a trend. Run `make seed-demo`."
        )
    return f"{n_buckets} daily buckets. Bars are p50, the line is p95."


def drift_caption(alerting: list[str], alert_psi: float = ALERT_PSI) -> str:
    if not alerting:
        return f"No label exceeds the PSI alert threshold of {alert_psi:g}."
    return f"PSI >= {alert_psi:g} on: {', '.join(alerting)}. Investigate before trusting the model."


def user_caption(panel: UserPanel) -> str:
    if panel.rate is None:
        return "Not enough user feedback yet."
    return (
        f"{panel.rate:.1%} agreement (95% CI {panel.lo:.1%}-{panel.hi:.1%}), n={panel.n}. "
        "This is self-selected and is NOT an unbiased accuracy estimate, so it is reported "
        "separately from live accuracy. A disagreement sends the comment to a human reviewer."
    )


def _digest(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main() -> None:
    st.title("Toxic comment moderation: production monitoring")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)
    engine = create_engine(os.environ["MONITORING_DB_DSN"], future=True, pool_pre_ping=True)

    try:
        thresholds = load_thresholds(THRESHOLDS_PATH)
        baseline = load_baseline(BASELINE_PATH)
    except (BaselineMissingError, BaselineContractError) as exc:
        st.error(f"Drift reference unavailable, so drift is not shown: {exc}")
        thresholds, baseline = None, None

    with engine.connect() as conn:
        buckets = latency_over_time(conn, since)
        accuracy = live_accuracy(conn, since)
        panel = user_feedback_panel(conn, since)
        statuses = review_counts(conn, since)
        total, seeded = seeded_share(conn, since)
        drift = flags = None
        if thresholds and baseline:
            drift = drift_report(conn, since, thresholds, baseline, alert_psi=ALERT_PSI)
            flags = flag_rate_series(conn, since, thresholds)

    st.caption(
        f"Window: last {WINDOW_DAYS} days. {total} predictions, of which {seeded} are "
        f"replayed held-out Jigsaw comments from `make seed-demo`. Queue: "
        f"{statuses.get('pending', 0)} pending, {statuses.get('rescored', 0)} rescored, "
        f"{statuses.get('reviewed', 0)} reviewed. thresholds.json "
        f"sha256:{_digest(THRESHOLDS_PATH)}."
    )

    st.header("1. Prediction latency over time")
    if buckets:
        frame = pd.DataFrame(
            [{"day": b.bucket, "p50 (ms)": b.p50, "p95 (ms)": b.p95, "n": b.n} for b in buckets]
        )
        st.line_chart(frame, x="day", y=["p50 (ms)", "p95 (ms)"])
    st.caption(latency_caption(len(buckets)))

    st.header("2. Predicted class distribution (target drift)")
    if drift is not None:
        long = pd.DataFrame(
            [{"label": row.label, "series": "baseline", "rate": row.baseline_rate} for row in drift]
            + [{"label": row.label, "series": "production", "rate": row.production_rate} for row in drift]
        )
        chart = (
            alt.Chart(long)
            .mark_bar()
            .encode(
                x=alt.X("label:N", sort=list(LABELS)),
                xOffset="series:N",
                y=alt.Y("rate:Q", title="flag rate"),
                color=alt.Color("series:N", title=""),
            )
        )
        st.altair_chart(chart, use_container_width=True)
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "label": row.label,
                        "baseline": row.baseline_rate,
                        "production": row.production_rate,
                        "PSI": row.psi,
                        "JS": row.js,
                        "alert": row.alert,
                    }
                    for row in drift
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(drift_caption([row.label for row in drift if row.alert]))
        if flags is not None and len(flags) > 1:
            st.line_chart(flags, x="bucket", y=list(LABELS))

    st.header("3. Live accuracy from human feedback")
    if accuracy.point is not None:
        st.metric("Live accuracy (design-weighted)", f"{accuracy.point:.1%}")
    st.caption(accuracy_caption(accuracy))
    if accuracy.strata:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "stratum": s.stratum,
                        "n": s.n,
                        "correct": s.correct,
                        "inclusion probability": s.sample_rate,
                        "accuracy": s.accuracy,
                        "95% CI low": s.lo,
                        "95% CI high": s.hi,
                    }
                    for s in accuracy.strata
                ]
            ),
            hide_index=True,
            use_container_width=True,
        )

    st.subheader("User feedback")
    st.caption(user_caption(panel))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard_guards.py tests/unit/test_no_unsafe_html.py -v`
Expected: 9 PASS in `test_dashboard_guards.py`, HTML scan still green.

- [ ] **Step 5: Commit**

```bash
git add monitoring/dashboard.py tests/unit/test_dashboard_guards.py
git commit -m "Add the monitoring dashboard with read-only access and honest captions"
```

---
### Task 16a: The dashboard exchanges data through the database, not through JSON files [gap `RUBRIC-3.2-JSON`]

Rubric 3.2's parenthetical — "A separate frontend app on a different EC2 server (data exchanged **via the database, not JSON files**)" — appears in no plan, no spec and no test. `grep -rn "not JSON files"` across all eight plans and both specs returns nothing, and Phase 5 Task 25's `rubric_clauses()` parses only the `- **3.2 …` heading, so the parenthetical folds into a parent row that never asserts the constraint.

Meanwhile the deployed dashboard *does* open two JSON files off a mounted volume: `BASELINE_PATH=/artifacts/baseline_flag_rates.json` and `THRESHOLDS_PATH=/artifacts/thresholds.json`. Those are **model artifacts** — the pinned decision boundary and the training-time reference distribution — not observations, and every prediction, review and feedback row comes from RDS. That is a defensible reading of the clause. It is not a reading anybody wrote down, and a grader reading the clause literally sees JSON files feeding the monitoring app.

This task writes the distinction down and pins it, so nobody later "optimises" a metrics cache into a file and quietly falsifies the clause.

**Files:**
- Test: `tests/unit/test_dashboard.py` (append)
- Modify: `monitoring/dashboard.py` (docstring only, if the two constants are not already read through `os.environ`)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_dashboard.py`:
```python
import ast
import pathlib
import re

DASHBOARD = pathlib.Path("monitoring/dashboard.py")


def test_the_dashboard_reads_no_prediction_data_from_a_file():
    """Rubric 3.2: 'data exchanged via the database, not JSON files'. The two JSON files
    the dashboard does open are MODEL artifacts (the pinned decision boundary and the
    training-time reference distribution), not observations. Every observation comes from
    RDS. This test pins that distinction so nobody 'optimises' a metrics cache into a file."""
    body = DASHBOARD.read_text(encoding="utf-8")
    tree = ast.parse(body)
    opened = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "read_csv", "read_json", "open", "load"}
    }
    assert opened <= {"read_text"}, f"dashboard reads data from files: {sorted(opened)}"
    assert "load_baseline" in body and "load_thresholds" in body
    for forbidden in ("predictions.json", "feedback.json", "metrics.json", "latency.json"):
        assert forbidden not in body


def test_the_only_two_files_the_dashboard_opens_are_model_artifacts():
    body = DASHBOARD.read_text(encoding="utf-8")
    paths = set(re.findall(r'os\.environ\[\"([A-Z_]*PATH)\"\]', body))
    assert paths == {"BASELINE_PATH", "THRESHOLDS_PATH"}


def test_every_panel_sources_its_observations_from_the_query_layer():
    """The four graded panels each take a live connection. A panel that took a path instead
    would be the JSON-file exchange the rubric forbids."""
    body = DASHBOARD.read_text(encoding="utf-8")
    for query in ("latency_over_time", "drift_report", "live_accuracy", "flag_rate_series"):
        assert query in body, f"{query} is not called; the panel has another data source"


def test_the_rubric_reading_is_written_down_where_a_grader_will_find_it():
    doc = DASHBOARD.read_text(encoding="utf-8")
    assert "not JSON files" in doc, (
        "rubric 3.2's parenthetical must be quoted and answered in the module docstring"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dashboard.py -v`
Expected: FAIL with `AssertionError: rubric 3.2's parenthetical must be quoted and answered in the module docstring`.

- [ ] **Step 3: Write minimal implementation**

Extend the `monitoring/dashboard.py` module docstring:
```python
"""Read-only monitoring dashboard (rubric 3.1, 3.2).

Rubric 3.2 requires "a separate frontend app on a different EC2 server (data exchanged via
the database, not JSON files)". Every observation this dashboard renders — predictions,
latencies, flag rates, reviews, feedback — is read from RDS through `monitoring/queries.py`
under a read-only role, on EC2 #3, which is a different host from the user UI on EC2 #2.

The two JSON files this module opens are neither predictions nor feedback. They are model
artifacts fetched with the model and digest-verified alongside it:

  THRESHOLDS_PATH  the pinned per-label decision boundary the backend also serves with
  BASELINE_PATH    the training-time reference flag rates drift is measured against

Reading them from the database would mean the dashboard's notion of "the decision boundary"
could drift from the backend's, which is the failure the pinning exists to prevent.
"""
```
Ensure both paths are read through `os.environ["…"]` and nowhere else, and that no panel calls `pandas.read_json`, `pandas.read_csv`, or `json.load` on anything but those two.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dashboard.py -v`
Expected: 4 PASS in addition to Task 16's existing cases.

- [ ] **Step 5: Commit**

```bash
git add monitoring/dashboard.py tests/unit/test_dashboard.py
git commit -m "Pin rubric 3.2's database-not-JSON-files constraint with a test"
```

**Amendment to Phase 5 Task 25.** Add a dedicated row to `docs/rubric-conformance.md`:

`| 3.2 (data via DB, not JSON) | monitoring/queries.py | tests/unit/test_dashboard.py::test_the_dashboard_reads_no_prediction_data_from_a_file | PASS | The two JSON files read are the pinned thresholds and the training-time baseline — model artifacts, not observations; every prediction, review and feedback row is read from RDS |`

and extend `rubric_clauses()` to parse the nested bullets under 2.2 and 3.2 so a sub-clause cannot hide inside a parent row.

---

### Task 17: `make seed-demo` — the dashboard's data source (premortem C5)

C5 is the highest-emphasis graded requirement failing for the most boring reason: no task anywhere creates prediction volume. This replays roughly 2,000 held-out Jigsaw comments through `/predict` with back-dated timestamps. Their labels are known, so reviewer ground truth is free.

**Files:**
- Create: `scripts/__init__.py`, `scripts/export_heldout.py`, `scripts/seed_demo.py`
- Modify: `Makefile`
- Test: `tests/unit/test_seed_selection.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_seed_selection.py`:
```python
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from model.labels import LABELS
from scripts.seed_demo import (
    MIN_BUCKETS,
    MIN_REVIEWED,
    SeedConfig,
    backdated_timestamps,
    check_exit_criteria,
    load_seed_rows,
    SeedReport,
)

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")
END = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)


def test_backdating_spreads_across_every_day_of_the_window():
    stamps = backdated_timestamps(2000, days=14, end=END, seed=42)
    assert len(stamps) == 2000
    days = sorted({stamp.date() for stamp in stamps})
    assert len(days) == 14
    assert min(stamps) >= END - dt.timedelta(days=14)
    assert max(stamps) <= END


def test_every_day_bucket_has_enough_points_for_a_percentile():
    stamps = backdated_timestamps(2000, days=14, end=END, seed=42)
    counts = pd.Series([stamp.date() for stamp in stamps]).value_counts()
    assert counts.min() >= 50
    assert counts.max() != counts.min(), "a perfectly flat series looks synthetic"


def test_backdating_is_deterministic():
    assert backdated_timestamps(500, 14, END, 42) == backdated_timestamps(500, 14, END, 42)
    assert backdated_timestamps(500, 14, END, 42) != backdated_timestamps(500, 14, END, 7)


def test_small_windows_still_produce_seven_buckets():
    stamps = backdated_timestamps(120, days=7, end=END, seed=42)
    assert len({stamp.date() for stamp in stamps}) == 7


def test_selection_is_deterministic_and_covers_every_label():
    rows_a = load_seed_rows(FIXTURE, n=25, seed=42)
    rows_b = load_seed_rows(FIXTURE, n=25, seed=42)
    assert [row.id for row in rows_a] == [row.id for row in rows_b]
    for label in LABELS:
        assert any(row.labels[label] == 1 for row in rows_a), f"{label} has no positive"


def test_selection_carries_the_known_ground_truth():
    rows = load_seed_rows(FIXTURE, n=25, seed=42)
    assert all(set(row.labels) == set(LABELS) for row in rows)
    assert all(value in (0, 1) for row in rows for value in row.labels.values())
    assert all(row.text for row in rows)


def test_selection_never_returns_more_than_the_corpus():
    rows = load_seed_rows(FIXTURE, n=10_000, seed=42)
    assert len(rows) == len(pd.read_csv(FIXTURE))


def test_exit_criteria_reject_a_thin_dataset():
    thin = SeedReport(predictions=30, buckets=2, flagged=5, audited=0, reviewed=5,
                      user_feedback=1, labels_with_flags=2)
    failures = check_exit_criteria(thin)
    assert any(str(MIN_BUCKETS) in message for message in failures)
    assert any(str(MIN_REVIEWED) in message for message in failures)
    assert any("audit" in message for message in failures)


def test_exit_criteria_accept_a_populated_dataset():
    full = SeedReport(predictions=2000, buckets=14, flagged=210, audited=180, reviewed=390,
                      user_feedback=160, labels_with_flags=6)
    assert check_exit_criteria(full) == []


def test_seed_config_defaults_match_the_exit_criteria():
    config = SeedConfig()
    assert config.n >= 2000
    assert config.days >= MIN_BUCKETS
    assert 0.0 < config.audit_rate <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_seed_selection.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts'`.

- [ ] **Step 3: Write minimal implementation**

`scripts/__init__.py`: empty.

`scripts/export_heldout.py`:
```python
"""Export the locked held-out split so `make seed-demo` has known-label comments.

The seeded dataset must be held out. Replaying training rows through /predict would make
the dashboard's live accuracy a measurement of memorisation.
"""

import argparse
from pathlib import Path

from model.data.load import REQUIRED_COLUMNS
from model.data.prepare import SplitConfig, prepare_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path, help="raw Jigsaw CSV")
    parser.add_argument("--out", default=Path("data/heldout.csv"), type=Path)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    bundle = prepare_dataset(args.csv, SplitConfig(seed=args.seed))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    bundle.test_df[list(REQUIRED_COLUMNS)].to_csv(args.out, index=False)
    print(f"wrote {len(bundle.test_df)} held-out rows to {args.out}")
    print(f"data_version={bundle.data_version}")


if __name__ == "__main__":
    main()
```

`scripts/seed_demo.py`:
```python
"""Replay held-out Jigsaw comments through /predict so the dashboard has a data source.

The premortem's C5: no task anywhere created prediction volume, so "latency over time" was
a scatter across four minutes, "target drift" was a single bar, and live accuracy divided by
zero on the rare labels -- rendering NaN or a traceback in the screenshot of the
highest-weighted requirement.

Predictions are made for real, so latency is measured rather than invented. Only the
timestamp is back-dated, and only by this operator tool writing directly to the database:
no production code path accepts a client-supplied timestamp, because that would be an
injection into the graded metric.

Every seeded row is marked `predictions.is_seed = true` and every seeded review carries
`reviewer_id='seed-replay'`, so the dashboard and the README can say exactly what the
dataset is.
"""

import argparse
import bisect
import datetime as dt
import json
import math
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from backend.feedback import derive_feedback, insert_feedback, user_feedback
from model.labels import LABELS

MIN_BUCKETS = 7
MIN_REVIEWED = 200
MIN_PREDICTIONS = 1500
SEED_REVIEWER_ID = "seed-replay"


class SeedError(RuntimeError):
    """The replay cannot produce a defensible dataset and must not pretend otherwise."""


@dataclass(frozen=True)
class SeedRow:
    id: str
    text: str
    labels: dict[str, int]


@dataclass(frozen=True)
class SeedConfig:
    n: int = 2000
    days: int = 14
    seed: int = 42
    audit_rate: float = 0.10
    user_feedback_fraction: float = 0.08


@dataclass(frozen=True)
class SeedReport:
    predictions: int
    buckets: int
    flagged: int
    audited: int
    reviewed: int
    user_feedback: int
    labels_with_flags: int


def backdated_timestamps(n: int, days: int, end: dt.datetime, seed: int) -> list[dt.datetime]:
    """Deterministic, uneven, and never leaves a day empty.

    The weekly sine gives a realistic weekday/weekend shape with a minimum weight of 0.4, so
    with n=2000 over 14 days the thinnest day still holds ~59 points -- enough for a p95.
    """
    weights = [1.0 + 0.6 * math.sin(2.0 * math.pi * day / 7.0) for day in range(days)]
    total = sum(weights)
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)

    start = end - dt.timedelta(days=days)
    rng = random.Random(seed)
    stamps: list[dt.datetime] = []
    for i in range(n):
        day = min(bisect.bisect_left(cumulative, (i + 0.5) / n), days - 1)
        second = rng.randrange(0, 86400)
        stamps.append(start + dt.timedelta(days=day, seconds=second))
    return stamps


def load_seed_rows(csv_path: Path, n: int, seed: int) -> list[SeedRow]:
    frame = pd.read_csv(csv_path)
    rng = random.Random(seed)
    chosen: list[int] = []
    taken: set[int] = set()

    # Rare labels first, so `threat` is never absent from the seeded window.
    per_label = max(1, n // (len(LABELS) * 8))
    for label in LABELS:
        positives = [int(i) for i in frame.index[frame[label] == 1]]
        rng.shuffle(positives)
        for index in positives[:per_label]:
            if index not in taken:
                taken.add(index)
                chosen.append(index)

    remaining = [int(i) for i in frame.index if int(i) not in taken]
    rng.shuffle(remaining)
    for index in remaining:
        if len(chosen) >= n:
            break
        chosen.append(index)

    chosen = sorted(chosen[:n])
    return [
        SeedRow(
            id=str(frame.at[index, "id"]),
            text=str(frame.at[index, "comment_text"]),
            labels={label: int(frame.at[index, label]) for label in LABELS},
        )
        for index in chosen
    ]


def check_exit_criteria(report: SeedReport) -> list[str]:
    failures: list[str] = []
    if report.buckets < MIN_BUCKETS:
        failures.append(f"only {report.buckets} time buckets, need {MIN_BUCKETS}")
    if report.reviewed < MIN_REVIEWED:
        failures.append(f"only {report.reviewed} reviewed items, need {MIN_REVIEWED}")
    if report.predictions < MIN_PREDICTIONS:
        failures.append(f"only {report.predictions} predictions, need {MIN_PREDICTIONS}")
    if report.audited == 0:
        failures.append("the random-audit stratum is empty, so live accuracy stays biased")
    if report.labels_with_flags < len(LABELS):
        failures.append(
            f"only {report.labels_with_flags} of {len(LABELS)} labels were ever flagged"
        )
    return failures


def replay(
    conn,
    rows: list[SeedRow],
    predict: Callable[[str], dict],
    config: SeedConfig,
    now: dt.datetime,
) -> SeedReport:
    stamps = backdated_timestamps(len(rows), config.days, now, config.seed)
    rng = random.Random(config.seed + 1)
    flagged = audited = reviewed = user_rows = 0
    labels_with_flags: set[str] = set()

    for row, ts in zip(rows, stamps, strict=True):
        response = predict(row.text)
        request_id = response["request_id"]
        model_flags = {label: bool(response["labels"][label]["flag"]) for label in LABELS}
        labels_with_flags |= {label for label, flag in model_flags.items() if flag}

        updated = conn.execute(
            text(
                "UPDATE predictions SET ts = :ts, is_seed = TRUE WHERE request_id = :rid"
            ),
            {"ts": ts, "rid": request_id},
        ).rowcount
        if updated != 1:
            raise SeedError(
                f"predict() did not persist {request_id}; the backend must log every "
                "prediction (rubric 2.2) before the dashboard can show anything"
            )

        existing = conn.execute(
            text("SELECT source FROM review_queue WHERE request_id = :rid"),
            {"rid": request_id},
        ).first()

        if existing is not None:
            stratum = existing.source
            conn.execute(
                text("UPDATE review_queue SET enqueued_ts = :ts WHERE request_id = :rid"),
                {"ts": ts, "rid": request_id},
            )
        elif rng.random() < config.audit_rate:
            stratum = "random-audit"
            conn.execute(
                text(
                    "INSERT INTO review_queue (request_id, enqueued_ts, status, source, "
                    "sample_rate, input_text_snapshot) VALUES (:rid, :ts, 'pending', "
                    "'random-audit', :rate, :snap)"
                ),
                {"rid": request_id, "ts": ts, "rate": config.audit_rate, "snap": row.text},
            )
        else:
            stratum = None

        if stratum is not None:
            if stratum == "flagged":
                flagged += 1
            elif stratum == "random-audit":
                audited += 1
            reviewed += 1
            reviewed_ts = ts + dt.timedelta(minutes=17)
            conn.execute(
                text(
                    "UPDATE review_queue SET status = 'reviewed', reviewer_labels = "
                    "CAST(:labels AS jsonb), reviewer_id = :who, reviewed_ts = :ts "
                    "WHERE request_id = :rid"
                ),
                {
                    "labels": json.dumps(row.labels),
                    "who": SEED_REVIEWER_ID,
                    "ts": reviewed_ts,
                    "rid": request_id,
                },
            )
            insert_feedback(
                conn,
                derive_feedback(request_id, row.labels, model_flags, SEED_REVIEWER_ID),
                ts=reviewed_ts,
            )

        if rng.random() < config.user_feedback_fraction:
            verdict = "agree" if rng.random() < 0.85 else "disagree"
            insert_feedback(
                conn, user_feedback(request_id, verdict), ts=ts + dt.timedelta(minutes=2)
            )
            user_rows += 1

    conn.commit()
    buckets = len({stamp.date() for stamp in stamps})
    return SeedReport(
        predictions=len(rows),
        buckets=buckets,
        flagged=flagged,
        audited=audited,
        reviewed=reviewed,
        user_feedback=user_rows,
        labels_with_flags=len(labels_with_flags),
    )


def _http_predict(base_url: str, api_key: str) -> Callable[[str], dict]:
    import httpx

    client = httpx.Client(base_url=base_url, timeout=30.0)

    def predict(text_value: str) -> dict:
        response = client.post(
            "/predict", json={"text": text_value}, headers={"X-API-Key": api_key}
        )
        response.raise_for_status()
        return response.json()

    return predict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, default=Path("data/heldout.csv"))
    parser.add_argument("--n", type=int, default=SeedConfig.n)
    parser.add_argument("--days", type=int, default=SeedConfig.days)
    parser.add_argument("--seed", type=int, default=SeedConfig.seed)
    parser.add_argument("--audit-rate", type=float, default=SeedConfig.audit_rate)
    parser.add_argument("--purge", action="store_true", help="delete previously seeded rows")
    args = parser.parse_args()

    config = SeedConfig(n=args.n, days=args.days, seed=args.seed, audit_rate=args.audit_rate)
    engine = create_engine(os.environ["DATABASE_URL"], future=True)

    with engine.connect() as conn:
        if args.purge:
            conn.execute(
                text(
                    "DELETE FROM feedback WHERE request_id IN "
                    "(SELECT request_id FROM predictions WHERE is_seed)"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM review_queue WHERE request_id IN "
                    "(SELECT request_id FROM predictions WHERE is_seed)"
                )
            )
            conn.execute(text("DELETE FROM predictions WHERE is_seed"))
            conn.commit()
            print("purged previously seeded rows")

        rows = load_seed_rows(args.csv, config.n, config.seed)
        predict = _http_predict(os.environ["BACKEND_URL"], os.environ.get("DEMO_API_KEY", ""))
        report = replay(conn, rows, predict, config, dt.datetime.now(dt.timezone.utc))

    print(json.dumps(report.__dict__, indent=2))
    failures = check_exit_criteria(report)
    if failures:
        for failure in failures:
            print(f"EXIT CRITERION FAILED: {failure}")
        raise SystemExit(1)
    print("all seed-demo exit criteria met")


if __name__ == "__main__":
    main()
```

Add to `Makefile`:
```makefile
SEED_CSV ?= data/heldout.csv
SEED_N ?= 2000
SEED_DAYS ?= 14

.PHONY: heldout seed-demo seed-demo-purge
heldout:
	$(BIN)/python -m scripts.export_heldout --csv $(RAW_CSV) --out $(SEED_CSV)
seed-demo:
	$(BIN)/python -m scripts.seed_demo --csv $(SEED_CSV) --n $(SEED_N) --days $(SEED_DAYS)
seed-demo-purge:
	$(BIN)/python -m scripts.seed_demo --csv $(SEED_CSV) --purge --n 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_seed_selection.py -v`
Expected: 10 PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts tests/unit/test_seed_selection.py Makefile
git commit -m "Add seed-demo replay of held-out comments with back-dated timestamps"
```

---

### Task 18: Seed-demo exit criteria, measured against the dashboard (premortem C5)

The seeder is only useful if the panels it feeds come out non-degenerate. This test runs the whole replay and then asserts on the three graded aggregations — shapes **and** values.

**Files:**
- Test: `tests/integration/test_seed_demo.py`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_seed_demo.py`:
```python
import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from model.labels import LABELS
from monitoring.baseline import Baseline, load_thresholds
from monitoring.queries import (
    drift_report,
    flag_rate_series,
    latency_over_time,
    live_accuracy,
    seeded_share,
    user_feedback_panel,
)
from scripts.seed_demo import (
    MIN_BUCKETS,
    MIN_REVIEWED,
    SeedConfig,
    check_exit_criteria,
    load_seed_rows,
    replay,
)

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.timezone.utc)
THRESHOLDS = load_thresholds(Path("tests/fixtures/thresholds.json"))
BASELINE = Baseline(
    schema_version=1, data_version="d", model_version="toxic-clf:v3", n=1000,
    flag_rates={"toxic": 0.10, "severe_toxic": 0.01, "obscene": 0.05,
                "threat": 0.003, "insult": 0.05, "identity_hate": 0.009},
)


def _fake_backend(conn):
    """Stands in for Phase 2's /predict: scores, persists, and enqueues on review.

    Probabilities are a deterministic function of the known labels plus jitter, so the
    seeded corpus produces a realistic mix of agreements and disagreements.
    """
    state = {"i": 0}

    def predict(comment: str) -> dict:
        state["i"] += 1
        i = state["i"]
        request_id = str(uuid.uuid4())
        row = lookup[comment]
        probs = {}
        for offset, label in enumerate(LABELS):
            truth = row.labels[label]
            jitter = ((i * 7 + offset * 13) % 20) / 100.0
            probs[label] = min(0.98, 0.55 + jitter) if truth else max(0.01, 0.12 - jitter / 4)
        flags = {label: probs[label] >= THRESHOLDS[label] for label in LABELS}
        decision = "review" if any(flags.values()) else "allow"
        cols = ", ".join(f"prob_{label}" for label in LABELS)
        binds = ", ".join(f":p_{label}" for label in LABELS)
        conn.execute(
            text(
                f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
                f"decision, max_prob, latency_ms) VALUES (:rid, :ts, :txt, 'toxic-clf:v3', "
                f"{binds}, :dec, :mx, :lat)"
            ),
            {
                "rid": request_id, "ts": NOW, "txt": comment, "dec": decision,
                "mx": max(probs.values()), "lat": 15 + (i % 60),
                **{f"p_{label}": probs[label] for label in LABELS},
            },
        )
        if decision == "review":
            conn.execute(
                text(
                    "INSERT INTO review_queue (request_id, enqueued_ts, status, source, "
                    "sample_rate, input_text_snapshot) VALUES (:rid, :ts, 'pending', "
                    "'flagged', 1.0, :snap)"
                ),
                {"rid": request_id, "ts": NOW, "snap": comment},
            )
        return {
            "request_id": request_id,
            "model_version": "toxic-clf:v3",
            "labels": {label: {"prob": probs[label], "flag": flags[label]} for label in LABELS},
            "decision": decision,
            "max_prob": max(probs.values()),
            "latency_ms": 15 + (i % 60),
        }

    return predict


HELDOUT = Path("tests/fixtures/mini_jigsaw.csv")
lookup: dict = {}


@pytest.fixture()
def seeded(conn):
    rows = load_seed_rows(HELDOUT, n=32, seed=42)
    # 2000 rows out of a 32-row fixture: repeat with distinct request ids, which is exactly
    # what the real corpus does at volume without needing the real corpus in CI.
    corpus = [rows[i % len(rows)] for i in range(2000)]
    lookup.clear()
    lookup.update({row.text: row for row in rows})
    report = replay(conn, corpus, _fake_backend(conn), SeedConfig(), NOW)
    return report


def test_seed_demo_meets_every_exit_criterion(seeded):
    assert check_exit_criteria(seeded) == [], seeded
    assert seeded.buckets >= MIN_BUCKETS
    assert seeded.reviewed >= MIN_REVIEWED
    assert seeded.audited > 0
    assert seeded.labels_with_flags == len(LABELS)


def test_latency_chart_is_not_a_scatter_across_four_minutes(seeded, conn):
    buckets = latency_over_time(conn, since=NOW - dt.timedelta(days=20))
    assert len(buckets) >= MIN_BUCKETS
    assert all(bucket.n >= 50 for bucket in buckets)
    assert all(bucket.p95 >= bucket.p50 for bucket in buckets)


def test_drift_chart_has_a_reference_and_more_than_one_bucket(seeded, conn):
    rows = drift_report(conn, since=NOW - dt.timedelta(days=20),
                        thresholds=THRESHOLDS, baseline=BASELINE)
    assert len(rows) == len(LABELS)
    assert any(row.production_rate > 0 for row in rows)
    assert all(row.baseline_rate > 0 for row in rows)
    series = flag_rate_series(conn, since=NOW - dt.timedelta(days=20), thresholds=THRESHOLDS)
    assert len(series) >= MIN_BUCKETS


def test_live_accuracy_is_a_real_number_over_both_strata(seeded, conn):
    report = live_accuracy(conn, since=NOW - dt.timedelta(days=20))
    assert report.n >= MIN_REVIEWED
    assert report.point is not None
    assert 0.0 <= report.point <= 1.0
    assert report.lo < report.point < report.hi
    strata = {stratum.stratum for stratum in report.strata}
    assert strata == {"flagged", "random-audit"}
    for stratum in report.strata:
        assert stratum.n > 0


def test_user_panel_is_populated_and_separate(seeded, conn):
    panel = user_feedback_panel(conn, since=NOW - dt.timedelta(days=20))
    assert panel.n > 0
    assert panel.rate is not None


def test_every_seeded_row_is_marked_as_seeded(seeded, conn):
    total, marked = seeded_share(conn, since=NOW - dt.timedelta(days=20))
    assert total == marked == seeded.predictions
    reviewers = conn.execute(
        text("SELECT DISTINCT reviewer_id FROM review_queue WHERE reviewer_id IS NOT NULL")
    ).scalars().all()
    assert reviewers == ["seed-replay"]


def test_replay_is_deterministic_in_its_timestamps(seeded, conn):
    days = conn.execute(
        text("SELECT count(DISTINCT date_trunc('day', ts)) FROM predictions")
    ).scalar_one()
    assert days == SeedConfig().days
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_seed_demo.py -v -m integration`
Expected: FAIL — before Task 17 lands, `ImportError`; with Task 17 landed but the exit criteria unmet, `test_seed_demo_meets_every_exit_criterion` fails listing the unmet criteria.

- [ ] **Step 3: Write minimal implementation**

No new production code. If any exit criterion fails, adjust `SeedConfig` defaults in `scripts/seed_demo.py` until it passes and record why in the docstring — the defaults are the deliverable, not the test.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_seed_demo.py -v -m integration`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_seed_demo.py
git commit -m "Assert seed-demo produces non-degenerate values in every graded panel"
```

---
### Task 18b (C8): The day-8 checkpoint is evaluated and recorded before any cut-line work starts [gap `C8-CHECKPOINT-LOG`]

The cut-line trigger was correctly rewritten into two leading indicators in delivery-spec §8 ("End of day 8 …", "End of day 11 …"), and the day-11 indicator has a real evidence artifact in `docs/evidence/a2-smoke-deploy.md` (A2 Task 2). The day-8 checkpoint has none. No task in any plan records that the day-8 evaluation happened or what it decided — this phase only says "If the day-8 checkpoint fires, stop after Task 18 and go to Task 22", and Phase 1 Task 18 only says "Run this task **only** if the end-of-day-8 checkpoint…".

Worse, Phase 4 Task 7 ships `test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded`, whose escape hatch is `docs/cut-log.md` — a file **no task anywhere creates**. Its own self-review concedes "the strongest cost control in the project can be removed by writing one line in a markdown file." This task creates that file, gives it a schema, and makes the schema a test.

A checkpoint nobody evaluates is the same memo C8 diagnosed: the day arrives, the developer is mid-Phase-3, and nothing forces the decision. Task 19 below is the first cut-line task, so this gate sits immediately before it.

**Files:**
- Create: `docs/cut-log.md`
- Test: `tests/unit/test_cut_log.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_cut_log.py`:
```python
"""C8. A checkpoint nobody evaluated recovers zero days, which is the original defect."""

import re
from pathlib import Path

LOG = Path("docs/cut-log.md")
CHECKPOINTS = ("day-8", "day-11")
ITEMS = ("AIBOM and SBOM", "RunPod sweep", "DistilBERT", "second-opinion column")


def _rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 5 and cells[0].lower() != "checkpoint":
                rows[cells[0]] = cells
    return rows


def test_the_checklist_file_the_ci_escape_hatch_reads_actually_exists():
    """Phase 4's test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded reads this
    path. A missing file is an escape hatch that opens on a FileNotFoundError."""
    assert LOG.exists(), "docs/cut-log.md is read by the Phase 4 reaper gate"


def test_every_checkpoint_in_the_delivery_spec_has_a_row():
    assert set(CHECKPOINTS) <= set(_rows()), "an unevaluated checkpoint recovers zero days"


def test_every_checkpoint_row_records_a_date_a_condition_and_a_decision():
    for name, cells in _rows().items():
        assert re.match(r"20\d\d-\d\d-\d\d", cells[1]), f"{name}: no evaluation date"
        assert cells[2] in {"MET", "NOT MET"}, f"{name}: condition not adjudicated"
        assert cells[3] in {"no cut", "cut"}, f"{name}: no pre-committed action taken"


def test_a_cut_names_the_items_and_the_ordered_list_position():
    for name, cells in _rows().items():
        if cells[3] == "cut":
            assert any(item.lower() in cells[4].lower() for item in ITEMS), (
                f"{name}: 'cut' with no named item from the spec's ordered cut list"
            )


def test_the_never_cut_list_was_not_touched():
    body = LOG.read_text(encoding="utf-8").lower()
    for protected in ("readme", "rollback", "leakage firewall", "ci gate", "safe model loading"):
        assert f"cut: {protected}" not in body, f"{protected} is on the never-cut list"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_cut_log.py -v`
Expected: FAIL — `FileNotFoundError: docs/cut-log.md`, five errors.

- [ ] **Step 3: Write minimal implementation**

`docs/cut-log.md`:
```markdown
# Cut-line log

Delivery spec §8 defines two leading indicators. Each is evaluated **on the day**, before any
work on the item below it starts, and the decision is recorded here whether or not anything is
cut. A checkpoint that is not evaluated recovers zero days (premortem C8).

The ordered cut list, weakest first: AIBOM and SBOM; RunPod sweep; DistilBERT challenger;
second-opinion column.

Never cut, at any checkpoint: the README, the rollback path, the leakage firewall, the CI gate,
and safe model loading.

| Checkpoint | Evaluated on | Condition | Decision | Items cut and why | Evidence |
|---|---|---|---|---|---|
| day-8 | `<YYYY-MM-DD>` | `MET` / `NOT MET` | `no cut` / `cut` | | Phase 1 Task 17 gate output |
| day-11 | `<YYYY-MM-DD>` | `MET` / `NOT MET` | `no cut` / `cut` | | `docs/evidence/a2-smoke-deploy.md` |
```

Fill the day-8 row **now**, at this point in execution, from the delivery-spec condition. Do not proceed to Task 19 with a placeholder in it: the test's date regex is what stops that.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_cut_log.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/cut-log.md tests/unit/test_cut_log.py
git commit -m "Record the day-8 cut-line checkpoint decision in a schema'd log"
```

**Amendment to Phase 5 Task 25.** Add `docs/cut-log.md` to the evidence column of the schedule rows in `docs/rubric-conformance.md`, so the rubric self-grade cannot pass with an unevaluated checkpoint.

---

### Task 19: Challenger contract — objective, label order, and ONNX logit parity (delivery spec §6.2, premortem C8)

**This task and the next two are behind the cut-line.** If the day-8 checkpoint fires, stop after Task 18 and go to Task 22; nothing before this point depends on anything after it.

Two failure modes this gate catches. HF Trainer defaults to softmax cross-entropy on a six-column target unless `problem_type="multi_label_classification"` is set, which trains the wrong objective and produces a model that looks fine and is not. And int8 dynamic quantization silently changes outputs, so the artifact needs a parity fixture checked at load, not only at export.

**Files:**
- Create: `rescorer/__init__.py`, `rescorer/challenger.py`, `tests/fixtures/challenger_ok/*`, `tests/fixtures/challenger_bad_objective/config.json`
- Test: `tests/unit/test_challenger.py`

- [ ] **Step 1: Write the failing test**

`tests/fixtures/challenger_ok/config.json`:
```json
{
  "architectures": ["DistilBertForSequenceClassification"],
  "model_type": "distilbert",
  "problem_type": "multi_label_classification",
  "id2label": {
    "0": "toxic",
    "1": "severe_toxic",
    "2": "obscene",
    "3": "threat",
    "4": "insult",
    "5": "identity_hate"
  }
}
```

`tests/fixtures/challenger_ok/parity.json`:
```json
{
  "atol": 0.05,
  "texts": ["you are an idiot", "have a nice day friend"],
  "logits": [
    [2.10, -1.40, 0.80, -3.20, 1.90, -2.60],
    [-3.50, -4.80, -3.90, -5.10, -3.70, -4.40]
  ]
}
```

`tests/fixtures/challenger_ok/model.onnx`: any 32 bytes; create with
`python -c "from pathlib import Path; Path('tests/fixtures/challenger_ok/model.onnx').write_bytes(b'not-a-real-onnx-graph-fixture-32')"`.

`tests/fixtures/challenger_bad_objective/config.json`: the same as `challenger_ok/config.json` with `"problem_type": "single_label_classification"`.

`tests/unit/test_challenger.py`:
```python
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from model.labels import LABELS
from rescorer.challenger import ChallengerContractError, load_challenger

OK = Path("tests/fixtures/challenger_ok")
BAD_OBJECTIVE = Path("tests/fixtures/challenger_bad_objective")
REFERENCE = np.array(json.loads((OK / "parity.json").read_text())["logits"], dtype=np.float32)
DIGEST = hashlib.sha256((OK / "model.onnx").read_bytes()).hexdigest()


class FakeSession:
    def __init__(self, logits: np.ndarray):
        self.logits = logits
        self.calls = 0

    def run(self, input_ids, attention_mask):
        self.calls += 1
        rows = len(input_ids)
        return np.resize(self.logits, (rows, len(LABELS))).astype(np.float32)


class FakeTokenizer:
    def encode(self, texts):
        ids = [[1] * 8 for _ in texts]
        mask = [[1] * 8 for _ in texts]
        return ids, mask


def _load(directory=OK, digest=DIGEST, logits=REFERENCE):
    return load_challenger(
        directory, digest, session=FakeSession(logits), tokenizer=FakeTokenizer()
    )


def test_a_conforming_artifact_loads():
    challenger = _load()
    probs = challenger.predict_proba(["a", "b", "c"])
    assert probs.shape == (3, len(LABELS))
    assert probs.min() >= 0.0 and probs.max() <= 1.0


def test_wrong_digest_fails_closed(tmp_path):
    with pytest.raises(ChallengerContractError, match="sha256"):
        _load(digest="0" * 64)


def test_softmax_objective_is_refused():
    """Delivery spec section 6.2: HF Trainer defaults to softmax cross-entropy on a
    six-column target, which trains the wrong objective and still produces an artifact."""
    staged = Path("tests/fixtures/challenger_bad_objective")
    shutil.copy(OK / "model.onnx", staged / "model.onnx")
    shutil.copy(OK / "parity.json", staged / "parity.json")
    with pytest.raises(ChallengerContractError, match="multi_label_classification"):
        _load(directory=staged)


def test_missing_problem_type_is_refused(tmp_path):
    staged = tmp_path / "artifact"
    shutil.copytree(OK, staged)
    config = json.loads((staged / "config.json").read_text())
    config.pop("problem_type")
    (staged / "config.json").write_text(json.dumps(config))
    with pytest.raises(ChallengerContractError, match="problem_type"):
        _load(directory=staged)


def test_label_order_mismatch_is_refused(tmp_path):
    staged = tmp_path / "artifact"
    shutil.copytree(OK, staged)
    config = json.loads((staged / "config.json").read_text())
    config["id2label"]["0"], config["id2label"]["1"] = (
        config["id2label"]["1"],
        config["id2label"]["0"],
    )
    (staged / "config.json").write_text(json.dumps(config))
    with pytest.raises(ChallengerContractError, match="id2label"):
        _load(directory=staged)


def test_int8_logit_drift_is_caught_at_load():
    """The parity fixture is checked when the worker starts, not only at export time, so a
    re-quantized or corrupted artifact cannot silently change the challenger's opinion."""
    drifted = REFERENCE + 0.6
    with pytest.raises(ChallengerContractError, match="parity"):
        _load(logits=drifted)


def test_parity_tolerance_admits_ordinary_quantization_noise():
    noise = REFERENCE + np.float32(0.02)
    challenger = _load(logits=noise)
    assert challenger.predict_proba(["a"]).shape == (1, len(LABELS))


def test_missing_parity_fixture_is_refused(tmp_path):
    staged = tmp_path / "artifact"
    shutil.copytree(OK, staged)
    (staged / "parity.json").unlink()
    with pytest.raises(ChallengerContractError, match="parity.json"):
        _load(directory=staged)


def test_module_does_not_import_onnxruntime_at_import_time():
    """C8 severability: the rest of Phase 3 must run on a box with no onnxruntime."""
    import sys

    assert "onnxruntime" not in sys.modules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_challenger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rescorer.challenger'`.

- [ ] **Step 3: Write minimal implementation**

`rescorer/__init__.py`: empty.

`rescorer/challenger.py`:
```python
"""Load the DistilBERT ONNX int8 challenger, and refuse it unless it is what it claims.

Three gates, all fail-closed:

1. SHA-256 against the digest recorded independently in the model card and the W&B version
   alias, never derived from the artifact being loaded.
2. `problem_type == "multi_label_classification"` and `id2label` in exactly LABELS order.
   HF Trainer silently defaults to softmax cross-entropy on a six-column target, which
   trains the wrong objective; and a permuted `id2label` mislabels every probability while
   producing a perfectly valid-looking (n, 6) array.
3. Logit parity against a fixture shipped with the artifact. int8 dynamic quantization
   changes outputs, so parity is verified where the model is used, not only where it was
   exported.

onnxruntime and tokenizers are imported lazily inside the concrete adapters, so importing
this module costs nothing on a machine where the re-scorer has been cut.
"""

import hashlib
import hmac
import json
from pathlib import Path

import numpy as np
from scipy.special import expit

from model.labels import LABELS

EXPECTED_PROBLEM_TYPE = "multi_label_classification"
DEFAULT_PARITY_ATOL = 0.05


class ChallengerContractError(RuntimeError):
    """The artifact is not the model this system agreed to run."""


class Challenger:
    def __init__(self, session, tokenizer):
        self._session = session
        self._tokenizer = tokenizer

    def logits(self, texts: list[str]) -> np.ndarray:
        input_ids, attention_mask = self._tokenizer.encode(texts)
        raw = np.asarray(self._session.run(input_ids, attention_mask), dtype=np.float32)
        if raw.ndim != 2 or raw.shape[1] != len(LABELS):
            raise ChallengerContractError(
                f"challenger returned shape {raw.shape}, expected (n, {len(LABELS)})"
            )
        return raw

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        # Sigmoid, not softmax: the labels are independent, which is the same fact that
        # problem_type encodes at training time.
        return expit(self.logits(texts)).astype(np.float32)


def _verify_digest(model_path: Path, expected_sha256: str) -> None:
    if not model_path.is_file():
        raise ChallengerContractError(f"{model_path} not found")
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise ChallengerContractError(
            f"sha256 mismatch for {model_path}: expected {expected_sha256}, got {actual}"
        )


def _verify_config(artifact_dir: Path) -> None:
    config_path = artifact_dir / "config.json"
    if not config_path.is_file():
        raise ChallengerContractError(f"{config_path} not found")
    config = json.loads(config_path.read_text())

    problem_type = config.get("problem_type")
    if problem_type != EXPECTED_PROBLEM_TYPE:
        raise ChallengerContractError(
            f"config.json declares problem_type={problem_type!r}; this system requires "
            f"{EXPECTED_PROBLEM_TYPE!r}. A model trained without it optimised softmax "
            "cross-entropy over six mutually exclusive classes, which is the wrong objective."
        )

    id2label = config.get("id2label") or {}
    ordered = tuple(id2label.get(str(index)) for index in range(len(LABELS)))
    if ordered != LABELS:
        raise ChallengerContractError(
            f"config.json id2label is {ordered}, expected {LABELS} in that exact order"
        )


def load_challenger(
    artifact_dir: Path,
    expected_sha256: str,
    *,
    session=None,
    tokenizer=None,
) -> Challenger:
    artifact_dir = Path(artifact_dir)
    _verify_digest(artifact_dir / "model.onnx", expected_sha256)
    _verify_config(artifact_dir)

    parity_path = artifact_dir / "parity.json"
    if not parity_path.is_file():
        raise ChallengerContractError(
            f"{parity_path} not found; the int8 export must ship reference logits"
        )
    parity = json.loads(parity_path.read_text())
    reference = np.asarray(parity["logits"], dtype=np.float32)
    atol = float(parity.get("atol", DEFAULT_PARITY_ATOL))

    if session is None or tokenizer is None:
        from rescorer.onnx_session import build_session, build_tokenizer

        session = session or build_session(artifact_dir / "model.onnx")
        tokenizer = tokenizer or build_tokenizer(artifact_dir / "tokenizer.json")

    challenger = Challenger(session, tokenizer)
    observed = challenger.logits(list(parity["texts"]))
    if observed.shape != reference.shape:
        raise ChallengerContractError(
            f"parity fixture shape {reference.shape} != observed {observed.shape}"
        )
    worst = float(np.max(np.abs(observed - reference)))
    if worst > atol:
        raise ChallengerContractError(
            f"int8 logit parity failed: max |diff| = {worst:.4f} > atol {atol}"
        )
    return challenger
```

`rescorer/onnx_session.py`:
```python
"""Concrete onnxruntime and tokenizers adapters.

Every heavy import lives inside a function, so `import rescorer.challenger` costs nothing
on a machine where the re-scorer has been cut and neither package is installed.
"""

from pathlib import Path

import numpy as np

MAX_TOKENS = 256


class OnnxSession:
    def __init__(self, session):
        self._session = session

    def run(self, input_ids, attention_mask) -> np.ndarray:
        feeds = {
            "input_ids": np.asarray(input_ids, dtype=np.int64),
            "attention_mask": np.asarray(attention_mask, dtype=np.int64),
        }
        return self._session.run(None, feeds)[0]


class HfTokenizer:
    def __init__(self, tokenizer):
        self._tokenizer = tokenizer

    def encode(self, texts: list[str]):
        encodings = self._tokenizer.encode_batch(list(texts))
        return (
            [encoding.ids for encoding in encodings],
            [encoding.attention_mask for encoding in encodings],
        )


def build_session(model_path: Path) -> OnnxSession:
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 1
    return OnnxSession(
        onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
    )


def build_tokenizer(tokenizer_path: Path) -> HfTokenizer:
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)
    tokenizer.enable_padding(length=MAX_TOKENS)
    return HfTokenizer(tokenizer)
```

Create `requirements/rescorer.txt`:
```
-r base.txt
onnxruntime==1.19.2
tokenizers==0.20.3
SQLAlchemy==2.0.36
psycopg[binary]==3.2.3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_challenger.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add rescorer tests/fixtures/challenger_ok tests/fixtures/challenger_bad_objective tests/unit/test_challenger.py requirements/rescorer.txt
git commit -m "Gate the challenger artifact on objective, label order, and logit parity"
```

---

### Task 20: The re-scorer worker, through the shared adapter (premortem H23, C8)

Idempotent, batched, backing off, and using the one authoritative array-to-dict adapter rather than a fourth independent `zip(LABELS, row)` that an order-blind validator would never catch.

**Files:**
- Create: `rescorer/worker.py`
- Test: `tests/integration/test_rescorer_drain.py`, `tests/unit/test_adapter_usage.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_adapter_usage.py`:
```python
from pathlib import Path

import numpy as np

from model.contract import probs_to_dict
from model.labels import LABELS


def test_the_shared_adapter_exists_and_orders_by_labels():
    """Premortem H23 / Tier-1 item 1.8. If this import fails, add exactly this function to
    model/contract.py -- it belongs to the contract module and must not be re-derived here:

        def probs_to_dict(row: np.ndarray) -> dict[str, float]:
            values = np.asarray(row, dtype=float).ravel()
            if values.shape != (len(LABELS),):
                raise ValueError(f"expected {len(LABELS)} probabilities, got {values.shape}")
            return {label: float(value) for label, value in zip(LABELS, values, strict=True)}
    """
    result = probs_to_dict(np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]))
    assert list(result) == list(LABELS)
    assert result["toxic"] == 0.1
    assert result["identity_hate"] == 0.6


def test_no_module_re_derives_the_label_mapping():
    for directory in ("rescorer", "frontend", "monitoring", "scripts"):
        for path in sorted(Path(directory).rglob("*.py")):
            source = path.read_text()
            assert "zip(LABELS" not in source, (
                f"{path} re-derives the array-to-dict mapping; use "
                "model.contract.probs_to_dict (premortem H23)"
            )
```

`tests/integration/test_rescorer_drain.py`:
```python
import datetime as dt

import numpy as np
import pytest
from sqlalchemy import text

from model.labels import LABELS
from rescorer.worker import drain_once

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 16, 9, 0, tzinfo=dt.timezone.utc)


class StubChallenger:
    def __init__(self):
        self.batches: list[int] = []

    def predict_proba(self, texts):
        self.batches.append(len(texts))
        return np.tile(
            np.array([0.9, 0.2, 0.3, 0.05, 0.7, 0.1], dtype=np.float32), (len(texts), 1)
        )


def _pending(conn, request_id: str) -> None:
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    vals = ", ".join("0.5" for _ in LABELS)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms) VALUES (:rid, :ts, 'text here', 'm', {vals}, "
            "'review', 0.5, 11)"
        ),
        {"rid": request_id, "ts": NOW},
    )
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', 'flagged', 1.0, 'text here')"
        ),
        {"rid": request_id, "ts": NOW},
    )


def test_drain_writes_probs_and_advances_status(conn):
    _pending(conn, "q1")
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 1
    row = conn.execute(
        text("SELECT status, distilbert_probs FROM review_queue WHERE request_id = 'q1'")
    ).one()
    assert row.status == "rescored"
    assert list(row.distilbert_probs) == list(LABELS)
    assert row.distilbert_probs["toxic"] == pytest.approx(0.9, abs=1e-6)


def test_drain_is_idempotent(conn):
    _pending(conn, "q2")
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 1
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0
    count = conn.execute(
        text("SELECT count(*) FROM review_queue WHERE request_id = 'q2' AND status = 'rescored'")
    ).scalar_one()
    assert count == 1


def test_drain_batches_rather_than_looping_one_row_at_a_time(conn):
    for i in range(20):
        _pending(conn, f"b{i}")
    conn.commit()
    challenger = StubChallenger()
    assert drain_once(conn, challenger, batch_size=8) == 8
    assert challenger.batches == [8]


def test_drain_on_an_empty_queue_returns_zero(conn):
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0


def test_drain_never_touches_a_reviewed_row(conn):
    _pending(conn, "q3")
    conn.execute(
        text("UPDATE review_queue SET status = 'reviewed' WHERE request_id = 'q3'")
    )
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 0
    probs = conn.execute(
        text("SELECT distilbert_probs FROM review_queue WHERE request_id = 'q3'")
    ).scalar_one()
    assert probs is None


def test_drain_uses_the_snapshot_not_the_purgeable_input_text(conn):
    _pending(conn, "q4")
    conn.execute(text("UPDATE predictions SET input_text = NULL WHERE request_id = 'q4'"))
    conn.commit()
    assert drain_once(conn, StubChallenger(), batch_size=16) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_adapter_usage.py tests/integration/test_rescorer_drain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rescorer.worker'` (and, if Phase 0 has not landed the adapter, `ImportError: cannot import name 'probs_to_dict'` — in which case add the function shown in that test's docstring to `model/contract.py` and re-run).

- [ ] **Step 3: Write minimal implementation**

`rescorer/worker.py`:
```python
"""Async DistilBERT re-scorer. EC2 #3, CPU, no ingress.

Reads `input_text_snapshot` rather than `predictions.input_text`, because the 30-day
retention purge nulls the latter and re-scoring must not depend on data that is designed to
disappear.

Idempotent by construction: rows are claimed with FOR UPDATE SKIP LOCKED and the update is
guarded on `status = 'pending'`, so a second pass, a crashed pass, or a second worker
cannot double-advance a row.
"""

import json
import os
import time

from sqlalchemy import create_engine, text

from model.contract import probs_to_dict

BATCH_SIZE = int(os.environ.get("RESCORER_BATCH_SIZE", "16"))
IDLE_SLEEP_SECONDS = float(os.environ.get("RESCORER_IDLE_SLEEP", "5"))
MAX_SLEEP_SECONDS = float(os.environ.get("RESCORER_MAX_SLEEP", "120"))


def drain_once(conn, challenger, batch_size: int = BATCH_SIZE) -> int:
    rows = conn.execute(
        text(
            "SELECT request_id, input_text_snapshot FROM review_queue "
            "WHERE status = 'pending' AND distilbert_probs IS NULL "
            "ORDER BY enqueued_ts LIMIT :n FOR UPDATE SKIP LOCKED"
        ),
        {"n": batch_size},
    ).all()
    if not rows:
        return 0

    probabilities = challenger.predict_proba([row.input_text_snapshot or "" for row in rows])
    for row, values in zip(rows, probabilities, strict=True):
        conn.execute(
            text(
                "UPDATE review_queue SET distilbert_probs = CAST(:probs AS jsonb), "
                "status = 'rescored' WHERE request_id = :rid AND status = 'pending'"
            ),
            {"probs": json.dumps(probs_to_dict(values)), "rid": row.request_id},
        )
    conn.commit()
    return len(rows)


def run_forever() -> None:  # pragma: no cover - exercised by the container smoke test
    from pathlib import Path

    from rescorer.challenger import load_challenger

    challenger = load_challenger(
        Path(os.environ["CHALLENGER_DIR"]), os.environ["CHALLENGER_SHA256"]
    )
    engine = create_engine(os.environ["DATABASE_URL"], future=True, pool_pre_ping=True)
    sleep_for = IDLE_SLEEP_SECONDS
    while True:
        with engine.connect() as conn:
            processed = drain_once(conn, challenger)
        if processed:
            sleep_for = IDLE_SLEEP_SECONDS
        else:
            sleep_for = min(sleep_for * 2, MAX_SLEEP_SECONDS)
        time.sleep(sleep_for)


if __name__ == "__main__":  # pragma: no cover
    run_forever()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_adapter_usage.py tests/integration/test_rescorer_drain.py -v`
Expected: 2 PASS unit, 6 PASS integration.

- [ ] **Step 5: Commit**

```bash
git add rescorer/worker.py tests/unit/test_adapter_usage.py tests/integration/test_rescorer_drain.py
git commit -m "Add idempotent batched re-scorer drain using the shared probability adapter"
```

---

### Task 21: Severability, containers, and the local stack (premortem C8)

The re-scorer is item 3 on the cut list. This task makes cutting it a one-line change with no Terraform edit and no failing test, and packages the four Phase 3 processes.

**Files:**
- Create: `frontend/Dockerfile`, `frontend/Dockerfile.reviewer`, `monitoring/Dockerfile`, `rescorer/Dockerfile`, `infra/docker-compose.yml`
- Test: `tests/unit/test_severability.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_severability.py`:
```python
import subprocess
import sys
from pathlib import Path

import yaml

COMPOSE = Path("infra/docker-compose.yml")


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_the_default_stack_is_complete_without_the_rescorer():
    """C8: cutting the challenger must not remove a graded component."""
    services = _compose()["services"]
    default = {name for name, spec in services.items() if not spec.get("profiles")}
    assert {"postgres", "backend", "frontend", "reviewer", "monitoring"} <= default
    assert "rescorer" not in default
    assert services["rescorer"]["profiles"] == ["challenger"]


def test_each_ui_publishes_exactly_its_own_port():
    services = _compose()["services"]
    assert services["frontend"]["ports"] == ["8501:8501"]
    assert services["reviewer"]["ports"] == ["127.0.0.1:8503:8503"]
    assert services["monitoring"]["ports"] == ["8502:8502"]
    assert "ports" not in services["rescorer"]


def test_no_ui_service_receives_a_database_url():
    services = _compose()["services"]
    for name in ("frontend", "reviewer"):
        env = services[name].get("environment", {})
        assert not any("DATABASE" in key or "DSN" in key for key in env), (
            f"{name} must reach Postgres only through the backend (H12/H16)"
        )
    assert "MONITORING_DB_DSN" in services["monitoring"]["environment"]


def test_importing_the_uis_and_dashboard_pulls_in_no_inference_runtime():
    code = (
        "import sys; import frontend.render, frontend.api_client, monitoring.queries, "
        "monitoring.stats, monitoring.baseline; "
        "assert 'onnxruntime' not in sys.modules; assert 'tokenizers' not in sys.modules; "
        "assert 'torch' not in sys.modules; print('ok')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_reviewer_ui_degrades_to_a_caption_when_the_challenger_is_absent():
    source = Path("frontend/reviewer.py").read_text()
    assert "Challenger scores are not available" in source


def test_the_cut_procedure_is_documented_in_one_place():
    readme = Path("infra/docker-compose.yml").read_text()
    assert "--profile challenger" in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_severability.py -v`
Expected: FAIL with `FileNotFoundError: infra/docker-compose.yml`.

- [ ] **Step 3: Write minimal implementation**

`infra/docker-compose.yml`:
```yaml
# Local full stack. `docker compose up` brings up every GRADED component.
# The DistilBERT re-scorer is behind a profile, because it sits below the cut-line:
#   with challenger:    docker compose --profile challenger up
#   without (default):  docker compose up
# Cutting it changes nothing else -- no Terraform edit, no instance resize, no failing test.

name: mlops-toxic-moderation

services:
  postgres:
    image: postgres:16.4-alpine
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: toxic
    ports:
      - "5433:5432"
    volumes:
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 10

  backend:
    build:
      context: ..
      dockerfile: backend/Dockerfile
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:postgres@postgres:5432/toxic
      THRESHOLDS_PATH: /artifacts/thresholds.json
      REVIEWER_SHARED_SECRET: ${REVIEWER_SHARED_SECRET:?set REVIEWER_SHARED_SECRET}
      REVIEWER_ID: ${REVIEWER_ID:-rock}
      DEMO_API_KEY: ${DEMO_API_KEY:?set DEMO_API_KEY}
      SUBMITTER_FP_KEY: ${SUBMITTER_FP_KEY:?set SUBMITTER_FP_KEY}
      RANDOM_AUDIT_RATE: ${RANDOM_AUDIT_RATE:-0.05}
    volumes:
      - ../artifacts:/artifacts:ro
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy

  frontend:
    build:
      context: ..
      dockerfile: frontend/Dockerfile
    environment:
      BACKEND_URL: http://backend:8000
      DEMO_API_KEY: ${DEMO_API_KEY:?set DEMO_API_KEY}
    ports:
      - "8501:8501"
    depends_on:
      - backend

  reviewer:
    build:
      context: ..
      dockerfile: frontend/Dockerfile.reviewer
    environment:
      BACKEND_URL: http://backend:8000
      DEMO_API_KEY: ${DEMO_API_KEY:?set DEMO_API_KEY}
    ports:
      # Loopback only, locally. In AWS this port lives on its own security group,
      # restricted to the operator, and is never carried by var.demo_ingress_cidrs.
      - "127.0.0.1:8503:8503"
    depends_on:
      - backend

  monitoring:
    build:
      context: ..
      dockerfile: monitoring/Dockerfile
    environment:
      MONITORING_DB_DSN: postgresql+psycopg://monitoring_ro:monitoring_ro@postgres:5432/toxic
      BASELINE_PATH: /artifacts/baseline_flag_rates.json
      THRESHOLDS_PATH: /artifacts/thresholds.json
      DASHBOARD_WINDOW_DAYS: ${DASHBOARD_WINDOW_DAYS:-14}
    volumes:
      - ../artifacts:/artifacts:ro
    ports:
      - "8502:8502"
    depends_on:
      postgres:
        condition: service_healthy

  rescorer:
    profiles: ["challenger"]
    build:
      context: ..
      dockerfile: rescorer/Dockerfile
    environment:
      DATABASE_URL: postgresql+psycopg://postgres:postgres@postgres:5432/toxic
      CHALLENGER_DIR: /artifacts/challenger
      CHALLENGER_SHA256: ${CHALLENGER_SHA256:?set CHALLENGER_SHA256}
    volumes:
      - ../artifacts:/artifacts:ro
    depends_on:
      postgres:
        condition: service_healthy
```

`infra/postgres-init/02-monitoring-role.sql` (the read-only role H16 asks for; the same two statements run against RDS in Phase A2):
```sql
CREATE ROLE monitoring_ro LOGIN PASSWORD 'monitoring_ro';
\c toxic
GRANT CONNECT ON DATABASE toxic TO monitoring_ro;
GRANT USAGE ON SCHEMA public TO monitoring_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO monitoring_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO monitoring_ro;
```

`frontend/Dockerfile`:
```dockerfile
FROM python:3.11-slim-bookworm@sha256:5b3b3b3b0000000000000000000000000000000000000000000000000000dead
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/ui.txt
COPY model/ model/
COPY backend/feedback.py backend/__init__.py backend/
COPY frontend/ frontend/
COPY infra/__init__.py infra/exposure.py infra/
EXPOSE 8501
USER 1000:1000
CMD ["streamlit", "run", "frontend/ui.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
```

`frontend/Dockerfile.reviewer`: identical to `frontend/Dockerfile` except the last two lines:
```dockerfile
EXPOSE 8503
CMD ["streamlit", "run", "frontend/reviewer.py", "--server.port=8503", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
```

`monitoring/Dockerfile`:
```dockerfile
FROM python:3.11-slim-bookworm@sha256:5b3b3b3b0000000000000000000000000000000000000000000000000000dead
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/monitor.txt
COPY model/ model/
COPY monitoring/ monitoring/
EXPOSE 8502
USER 1000:1000
CMD ["streamlit", "run", "monitoring/dashboard.py", "--server.port=8502", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
```

`rescorer/Dockerfile`:
```dockerfile
FROM python:3.11-slim-bookworm@sha256:5b3b3b3b0000000000000000000000000000000000000000000000000000dead
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --no-cache-dir --require-hashes -r requirements/rescorer.txt
COPY model/ model/
COPY rescorer/ rescorer/
USER 1000:1000
CMD ["python", "-m", "rescorer.worker"]
```

Replace the placeholder digest in all four `FROM` lines with the real one before building:
```bash
docker pull python:3.11-slim-bookworm
docker inspect --format='{{index .RepoDigests 0}}' python:3.11-slim-bookworm
```
Pinning by digest is what makes an image traceable to an exact base (premortem H35); an unpinned tag defeats the traceability the git-SHA image tags are supposed to give.

Add `pyyaml==6.0.2` to `requirements/dev.txt` for the compose-contract test.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_severability.py -v && docker compose -f infra/docker-compose.yml config --quiet`
Expected: 6 PASS, and compose validates.

- [ ] **Step 5: Commit**

```bash
git add infra/docker-compose.yml infra/postgres-init frontend/Dockerfile frontend/Dockerfile.reviewer monitoring/Dockerfile rescorer/Dockerfile tests/unit/test_severability.py requirements/dev.txt
git commit -m "Package the Phase 3 services and keep the challenger behind a compose profile"
```

---
### Task 22: Phase gate — the full traversal, contract corrections, and the PR

Delivery spec §3.3: no phase is complete until every route and integration it introduces is proven working against a real dependency, not a mock. For Phase 3 that traversal is submit → predict → log → enqueue → review → feedback → dashboard.

**Files:**
- Create: `tests/integration/test_end_to_end.py`
- Modify: `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, `README.md`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_end_to_end.py`:
```python
import datetime as dt
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.review_api import router
from model.labels import LABELS
from monitoring.queries import live_accuracy, review_counts

pytestmark = pytest.mark.integration

SECRET = "reviewer-shared-secret"


@pytest.fixture()
def client(conn, engine, monkeypatch):
    monkeypatch.setenv("REVIEWER_SHARED_SECRET", SECRET)
    monkeypatch.setenv("REVIEWER_ID", "rock")
    monkeypatch.setenv("THRESHOLDS_PATH", "tests/fixtures/thresholds.json")
    app = FastAPI()
    app.include_router(router)
    app.state.engine = engine
    return TestClient(app)


def test_full_traversal_submit_to_dashboard(client, conn):
    """One comment, all the way through, against a real Postgres."""
    request_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc)
    probs = {label: 0.02 for label in LABELS}
    probs["toxic"] = 0.91
    probs["insult"] = 0.88
    cols = ", ".join(f"prob_{label}" for label in LABELS)
    binds = ", ".join(f":p_{label}" for label in LABELS)

    # 1. predict + log (Phase 2's job; performed here so the traversal is end to end)
    conn.execute(
        text(
            f"INSERT INTO predictions (request_id, ts, input_text, model_version, {cols}, "
            f"decision, max_prob, latency_ms, submitter_fp) VALUES (:rid, :ts, "
            f"'**you** are an idiot', 'toxic-clf:v3', {binds}, 'review', 0.91, 23, "
            "'aaaabbbbccccdddd')"
        ),
        {"rid": request_id, "ts": now, **{f"p_{label}": probs[label] for label in LABELS}},
    )
    # 2. enqueue with its stratum and inclusion probability
    conn.execute(
        text(
            "INSERT INTO review_queue (request_id, enqueued_ts, status, source, sample_rate, "
            "input_text_snapshot) VALUES (:rid, :ts, 'pending', 'flagged', 1.0, "
            "'**you** are an idiot')"
        ),
        {"rid": request_id, "ts": now},
    )
    conn.commit()

    # 3. the reviewer sees the comment byte-identical to what was scored
    token = client.post("/review/login", json={"secret": SECRET}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    item = client.get("/review/pending", headers=headers).json()["items"][0]
    assert item["input_text_snapshot"] == "**you** are an idiot"

    from frontend.render import render_comment

    captured: list[str] = []
    render_comment(item["input_text_snapshot"], renderer=captured.append)
    assert captured == ["**you** are an idiot"]

    # 4. review, which derives feedback
    labels = {label: 0 for label in LABELS}
    labels["toxic"] = 1
    labels["insult"] = 1
    assert client.post(
        "/review/submit", headers=headers, json={"request_id": request_id, "labels": labels}
    ).status_code == 200

    # 5. user feedback, on the same request, from an anonymous visitor
    assert client.post(
        "/feedback/user", json={"request_id": request_id, "verdict": "agree"}
    ).status_code == 200

    # 6. the dashboard sees it
    since = now - dt.timedelta(days=1)
    assert review_counts(conn, since)["reviewed"] == 1
    report = live_accuracy(conn, since)
    assert report.n == 1
    assert report.point == pytest.approx(1.0)
    assert report.strata[0].stratum == "flagged"
    assert report.strata[0].sample_rate == pytest.approx(1.0)


def test_the_reviewer_endpoints_are_the_only_write_path_from_a_ui(client):
    """No UI module opens a database connection; the API is the whole surface."""
    import pathlib

    for path in sorted(pathlib.Path("frontend").rglob("*.py")):
        source = path.read_text()
        assert "create_engine" not in source
        assert "psycopg" not in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_end_to_end.py -v -m integration`
Expected: FAIL until every prior task has landed; the first failure is `ModuleNotFoundError` or a missing column, depending on how far the phase has got.

- [ ] **Step 3: Apply the interface contract corrections and the README section**

In `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, replace the "Database writes (Phase 2 defines; Phase 3 consumes)" block with:

```python
# backend/db.py
def init_db(engine) -> None: ...                     # idempotent create of the three tables
def insert_prediction(session, response: PredictionResponse, input_text: str,
                      submitter_fp: str | None) -> None: ...

# backend/queue_guard.py  (Phase 3)
def admit_review(conn, *, request_id: str, source: str, submitter_fp: str | None,
                 now: datetime, config: AdmissionConfig) -> Admission: ...
def admit_user_feedback(conn, *, request_id: str, submitter_fp: str | None,
                        now: datetime, config: AdmissionConfig) -> Admission: ...

# backend/review_api.py  (Phase 3) -- the UI's entire write surface
# POST /review/login   {secret}                  -> {token}
# GET  /review/pending ?limit=                   -> {items: [...]}      (Bearer token)
# POST /review/submit  {request_id, labels}      -> {request_id, exact_match}  (Bearer token)
#      no reviewer_id field: the identity is derived from the verified session
# POST /feedback/user  {request_id, verdict}     -> {request_id, verdict}

# rescorer/worker.py  (Phase 3)
def drain_once(conn, challenger, batch_size: int = 16) -> int: ...
```

Add to `README.md`, under a "Demo dataset and monitoring" heading:

```markdown
### Demo dataset and monitoring

The monitoring dashboard is populated by `make seed-demo`, which replays roughly 2,000
comments from the **locked held-out split** through `/predict` and back-dates their
timestamps across 14 days. Predictions are real and their latency is measured; only the
timestamp is written by the operator tool. Every seeded row carries
`predictions.is_seed = true` and every seeded review carries `reviewer_id = 'seed-replay'`,
and the dashboard states how many of the displayed rows are seeded.

Live accuracy is a Horvitz-Thompson estimate over two probability-sampled strata: every
flagged item is reviewed (inclusion probability 1.0) and a `RANDOM_AUDIT_RATE` fraction of
the rest is audited. The inclusion probability is stored on each `review_queue` row at
enqueue time, so the estimate stays sound when the rate is changed. Per-stratum counts and a
95% Wilson interval are shown alongside the point estimate.

User feedback (the agree/disagree control on the user UI) is collected, stored with
`feedback.source = 'user'`, and displayed with its own n and interval. It is deliberately
**excluded** from the live-accuracy estimate, because a self-selected click has no known
inclusion probability and because a graded metric must not be writable by an anonymous
visitor. A disagreement instead refers the comment to a human reviewer.

The reviewer console runs on port 8503 with its own security group, restricted to the
operator. The demo ingress toggle opens 8000, 8501, and 8502 only.
```

- [ ] **Step 4: Run the full gate**

```bash
make lint
make test
make test-integration
docker compose -f infra/docker-compose.yml up -d --build
make seed-demo
docker compose -f infra/docker-compose.yml ps
```
Expected: ruff clean; every unit test passes; every integration test passes; the stack comes up; `make seed-demo` prints `all seed-demo exit criteria met` and exits 0; five services running (six with `--profile challenger`).

Then open each surface and confirm by eye:
- `http://localhost:8501` — submit a comment, see the decision and six probabilities, click Agree.
- `http://localhost:8503` — sign in, see the comment rendered verbatim, submit labels.
- `http://localhost:8502` — latency chart spans ≥7 buckets; the drift chart shows baseline beside production with a PSI column; live accuracy shows a point estimate, an interval, and both strata.

- [ ] **Step 5: Open the PR**

```bash
git add tests/integration/test_end_to_end.py docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md README.md
git commit -m "Prove the Phase 3 traversal end to end and reconcile the interface contracts"
git push -u origin feat/phase-3-ui-monitoring-rescorer
gh pr create --base main --title "Phase 3: user UI, reviewer UI, monitoring dashboard, feedback, re-scorer" \
  --body "User UI with a two-click feedback control, reviewer console on its own port and security group, monitoring dashboard with latency percentiles, baseline-referenced drift, and a design-weighted live-accuracy estimate, plus make seed-demo and a severable DistilBERT re-scorer. Unit and integration suites green, ruff clean, local compose traversal verified."
```

---

## Self-Review

**Premortem coverage.** Every finding assigned to this phase has an owning task whose test fails if the finding is unfixed.

| Finding | Owning task | The test that fails if unfixed |
|---|---|---|
| **C5** — dashboard has no data source | 17, 18 | `test_seed_demo_meets_every_exit_criterion`, `test_latency_chart_is_not_a_scatter_across_four_minutes`, `test_live_accuracy_is_a_real_number_over_both_strata` |
| **C5** — live accuracy divides by zero | 2, 15, 16 | `test_empty_input_returns_none_not_nan_and_not_zero_division`, `test_live_accuracy_on_an_empty_table_is_none_not_a_zero_division`, `test_accuracy_caption_on_empty_data_says_so_instead_of_rendering_nan` |
| **H8** — unweighted pooling of two strata | 1, 2, 15 | `test_horvitz_thompson_differs_from_the_unweighted_pool`, `test_design_stratum_without_sample_rate_is_rejected`, `test_live_accuracy_is_design_weighted_not_pooled` |
| **H8** — no pinned `RANDOM_AUDIT_RATE`, not stored per row | 1, 5 | `test_random_audit_records_the_configured_rate`, `test_missing_inclusion_probability_is_an_error_not_a_default` |
| **H8** — bare point estimate | 2, 16 | `test_report_carries_per_stratum_n_and_intervals_not_a_bare_point`, `test_accuracy_caption_reports_the_point_the_interval_and_every_stratum_n`, `test_accuracy_caption_warns_when_the_audit_stratum_is_empty` |
| **H9** — no user feedback control | 6, 8, 10 | `test_user_feedback_writes_a_user_sourced_row`, `test_user_feedback_is_a_single_bit_with_no_free_text` |
| **H9** — anonymous write path into the graded metric | 5, 8, 15 | `test_user_feedback_cannot_move_the_graded_estimate`, `test_user_feedback_needs_no_reviewer_token_but_is_rate_limited`, `test_user_feedback_rejects_free_text` |
| **H12** — reviewer UI exposed by the demo toggle | 11, 12 | `test_no_demo_exposed_rule_reaches_an_operator_only_port`, `test_reviewer_rule_is_restricted_to_the_operator_cidrs`, `test_reviewer_runs_on_its_own_port_not_the_user_ui_port` |
| **H12/H16** — frontend holds direct RDS write access | 8, 11, 21, 22 | `test_reviewer_module_holds_no_database_import`, `test_no_ui_service_receives_a_database_url`, `test_the_reviewer_endpoints_are_the_only_write_path_from_a_ui` |
| **H16** — no read-only role for the dashboard | 16, 21 | `test_monitoring_issues_no_write_statements`, `test_dashboard_uses_a_dedicated_read_only_dsn` |
| **H23** — no named array→dict adapter | 20 | `test_the_shared_adapter_exists_and_orders_by_labels`, `test_no_module_re_derives_the_label_mapping` |
| **H24** — drifted interface contracts | 22 | The contract corrections are applied at source in Task 22 Step 3 |
| **H28** — no latency percentile | 13 | `test_latency_buckets_by_day_with_percentiles` |
| **C6** — no egress rule anywhere | 12 | `test_every_app_group_declares_explicit_egress` |
| **C8** — cut-line cannot fire cleanly | 19, 21 | `test_the_default_stack_is_complete_without_the_rescorer`, `test_importing_the_uis_and_dashboard_pulls_in_no_inference_runtime` |
| **Rubric 3.2 drift needs a reference** | 3, 14, 16 | `test_missing_baseline_fails_closed`, `test_drift_report_returns_one_row_per_label_with_a_reference`, `test_a_stable_label_does_not_alert` |
| **§6.3** — never `unsafe_allow_html`; render verbatim | 9 | `test_rendered_payload_is_byte_identical_to_the_input`, `test_no_html_or_markdown_rendering_primitives` |
| **§6.3** — `reviewer_id` server-side | 7, 8 | `test_identity_is_never_taken_from_the_token_alone`, `test_submit_body_rejects_a_client_supplied_reviewer_id` |
| **§6.4** — queue depth cap and per-source rate limit | 4, 5 | `test_depth_cap_rejects_once_the_queue_is_full`, `test_per_source_quota_rejects_a_flood_from_one_fingerprint`, `test_identity_ignores_a_session_header_without_the_frontend_api_key` |
| **§6.4** — `review_queue.source` distinguishes strata | 1, 5, 15 | `test_user_report_stratum_must_have_null_sample_rate`, `test_user_report_stratum_is_excluded_from_the_estimate` |
| **§6.4** — `input_text_snapshot` at enqueue | 1, 5, 20 | `test_admits_a_flagged_row_and_records_its_inclusion_probability`, `test_drain_uses_the_snapshot_not_the_purgeable_input_text` |
| **§6.4** — screenshots carry no raw user text | 16 | `test_monitoring_never_selects_raw_user_text` |
| **§6.2** — DistilBERT objective, ONNX parity | 19 | `test_softmax_objective_is_refused`, `test_int8_logit_drift_is_caught_at_load` |
| **H35** — unpinned base images | 21 | Base images pinned by digest; the pull-and-inspect command is in Task 21 Step 3 |

**Rubric coverage.**

| Rubric clause | Owning task | Evidence artifact |
|---|---|---|
| 3.1 frontend calls backend, displays prediction | 10 | Live UI screenshot; `tests/unit/test_api_client.py` |
| 3.2 dashboard on a different EC2 server | 12, 21 | `sg-monitoring` + the monitoring container on EC2 #3 |
| 3.2 latency over time | 13, 17 | Chart with ≥7 daily buckets, p50 and p95 |
| 3.2 predicted-class distribution (target drift) | 14 | Grouped bar against `baseline_flag_rates.json`, PSI table, stated 0.2 threshold |
| 3.2 mechanism to collect **user** feedback | 6, 8, 10 | Agree/disagree control; a `feedback` row with `source='user'`; the user panel |
| 3.2 live accuracy from feedback | 15, 16 | Design-weighted estimate with per-stratum n and a Wilson interval |
| 2.2 every prediction logged | 17 | `replay` raises `SeedError` if `/predict` did not persist the row |
| 5.1 containerize components | 21 | Four Dockerfiles, digest-pinned bases |
| 4.1 unit + integration tests | all | `make test` and `make test-integration` |

**Cut-line position.** Tasks 1–18 are above the line and produce every graded component. Tasks 19–21 are the DistilBERT branch. If the end-of-day-8 checkpoint fires, stop after Task 18 and go straight to Task 22: `test_the_default_stack_is_complete_without_the_rescorer` and `test_importing_the_uis_and_dashboard_pulls_in_no_inference_runtime` are the only two tests that reference the cut work, and both are in Task 21, which is cut with it. No Terraform change, no instance resize, and no other test moves.

**Placeholder scan.** Every step carries real code and an exact command. No TODO, no "handle edge cases", no "similar to". Three values are deliberately left to be filled from the environment rather than guessed, each with the exact command that produces it: the base-image digest (Task 21 Step 3, `docker inspect --format='{{index .RepoDigests 0}}'`), `CHALLENGER_SHA256` (from the Phase 1 model card), and the operator CIDR (a Terraform variable). The statistical constants asserted in Tasks 2, 3, 14, 15, and 17 were computed before being written down, not recalled.

**Type consistency.** `LABELS` is a `tuple[str, ...]` used identically in every module; no module re-derives its order (`test_no_module_re_derives_the_label_mapping`). `AccuracyReport` and `StratumStat` are produced by `monitoring/stats.py`, consumed unchanged by `monitoring/queries.live_accuracy` and `monitoring/dashboard.accuracy_caption`. `FeedbackRecord` is produced by both `derive_feedback` and `user_feedback` and consumed by one writer, `insert_feedback`. `Admission` is returned by both admission functions and mapped to HTTP status codes in exactly one place. `probs_to_dict(row: np.ndarray) -> dict[str, float]` is the Phase 0 contract signature, unchanged. `Challenger.predict_proba(texts) -> np.ndarray` of shape `(n, 6)` matches the master plan's model interface, so the re-scorer and the production model satisfy the same shape. `sample_rate` is `float | None` in Python and `DOUBLE PRECISION NULL` in Postgres, with the null-ness constrained by `review_queue_sample_rate_ck` rather than by convention.

**Known seams that depend on another phase landing.** Each is guarded by a test that fails loudly rather than skipping: `backend.db.init_db` and the `prob_<label>` column names (Task 1), `model.contract.probs_to_dict` (Task 20, with the exact function body in the test docstring if it is absent), `backend.app.app` (Task 8), `artifacts/thresholds.json` and `artifacts/baseline_flag_rates.json` (Task 3, fixtures committed so the suite runs before Phase 1 finishes), and the challenger artifact (Task 19, fixtures committed so the contract is testable with no real model).

## Execution Handoff

Two options:
1. **Subagent-Driven (recommended):** fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.

Tasks 1–3 have no dependency on each other and can run in parallel. Tasks 4–8 are sequential (each consumes the previous). Tasks 9–12 are independent of 13–16. Task 17 needs 6; Task 18 needs 13, 14, 15, and 17. Tasks 19–21 are the severable branch.
