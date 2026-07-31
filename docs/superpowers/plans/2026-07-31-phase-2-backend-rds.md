# Phase 2: FastAPI Backend, Safe Model Loading, RDS Postgres, Prediction Logging

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/predict` and `/health` serving the promoted classical model on CPU, behind an authenticated, rate-limited, size-capped listener; loading through a fail-closed skops loader whose trusted-type allowlist is a static literal and whose expected digest is read from the git-committed model card; applying the moderation policy to produce hierarchically coherent flags; and logging **every** prediction to Postgres through a persistence path that is durable and bounded rather than an availability switch.

**Architecture:** One process, three trust boundaries. The **listener boundary** (body-size cap, API key, rate limit) runs in a single HTTP middleware before FastAPI parses the body, so unauthenticated traffic never reaches validation or the model. The **artifact boundary** (`backend/model_loader.py`) verifies a digest taken from the repository against an artifact taken from the registry, then deserializes under a static allowlist. The **persistence boundary** (`backend/persistence.py`) writes to Postgres when it can and to a bounded, fsync'd local spool when it cannot, so RDS pressure degrades the log path instead of the moderation endpoint. Nothing in the request path calls Weights & Biases, AWS, or any other network service besides Postgres.

```
POST /predict
  |
  [_gate middleware]  Content-Length -> API key -> token bucket      (411 / 413 / 401 / 429)
  |
  [pydantic]          PredictRequest.text, 1..4000 chars              (422)
  |
  t0 = perf_counter()
  prepare_input -> model.data.dedup.normalize (the SAME function)
  LoadedModel.predict_proba -> (1, 6) ndarray
  probs_to_dict -> {label: float}
  decide(probs, thresholds) -> flags (severe_toxic implies toxic), decision, max_prob
  |
  persist_prediction:  direct -> INSERT + optional review row, THEN stamp latency_ms
                       degraded -> fsync'd spool row, HTTP 200
                       spool full -> HTTP 503 + Retry-After
  |
  PredictionResponse(model_version = OPAQUE public label, latency_ms = through persistence)
```

**Tech Stack:** Python 3.11, FastAPI 0.115.5, uvicorn 0.32.1, pydantic 2.9.2, SQLAlchemy 2.0.36, psycopg 3.2.3 (binary), skops 0.11.0, scikit-learn 1.5.2, numpy 2.1.3, pytest 8.3.3, httpx 0.27.2 (FastAPI `TestClient`), testcontainers 4.8.2 (integration), ruff 0.7.4. Postgres 16 (RDS `db.t4g.micro` in `us-west-2`; `postgres:16-alpine` for tests).

## Global Constraints

Inherited from the master roadmap and `docs/superpowers/specs/2026-07-30-delivery-plan-design.md`, which governs on conflict. The ones that bind Phase 2:

- **Labels ordered exactly:** `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`. Every array→dict conversion goes through `probs_to_dict`. No `zip(LABELS, ...)` anywhere in `backend/`.
- **Safe loading only.** `skops.io.load` with an **explicit static** trusted-type allowlist. Never `get_untrusted_types()`-then-trust-all, never `trusted=True`. Digest mismatch refuses to load. The loader is the trust boundary and it does not degrade.
- **Rubric 2.2 is unconditional:** every prediction request, its output, and a timestamp reach the database. Phase 2 satisfies that with durability, not with a 503.
- **Three EC2 instances** (backend, frontend, monitoring), each separate. The backend instance runs this container and nothing else.
- **No secret is written to the repository, to an image layer, or to a log line.** `DATABASE_URL` and `DEMO_API_KEY` arrive as environment variables sourced from Secrets Manager at container start.
- **Never log raw `input_text`.** Only the access-restricted RDS row holds it, for `INPUT_TEXT_RETENTION_DAYS` (30).
- **Integration gate (delivery spec §3.3):** Phase 2 is not done until `/predict` and `/health` answer against a **real Postgres** and a row lands in `predictions`. Mocks are for unit tests; the phase gate does not accept them.
- **Feature-branch + PR, human author** (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or docs.
- **Schedule:** days 5–6 of 19 (delivery spec §7). Day-8 checkpoint requires Slice 1 serving end-to-end locally.

**Branch:** `feat/phase-2-backend-rds` off `main`.

## File Structure

- `requirements/serve.in`, `requirements/serve.txt` — serving deps; `serve.txt` is a hashed lock.
- `backend/__init__.py` — empty, no heavy imports at package load.
- `backend/config.py` — `MAX_INPUT_CHARS`, `Settings`, `load_settings`.
- `backend/schemas.py` — `PredictRequest`.
- `backend/preprocess.py` — `prepare_input`, re-exported `normalize`.
- `backend/model_card.py` — `read_expected_digest`.
- `backend/model_loader.py` — `TRUSTED_TYPES`, `LoadedModel`, `load_model`, `load_from_settings`, `sha256_file`, `ModelIntegrityError`.
- `backend/policy.py` — `DecisionResult`, `decide`, `load_thresholds`.
- `backend/audit.py` — `should_random_audit`, `FLAGGED_INCLUSION_PROBABILITY`.
- `backend/db.py` — ORM models, `PredictionRow`, `ReviewIntent`, `PendingWrite`, `make_engine`, `init_schema`, `insert_prediction`, `enqueue_review`, `fetch_pending_reviews`, `write_pending`.
- `backend/spool.py` — `Spool`, `SpoolFull`.
- `backend/persistence.py` — `PersistResult`, `persist_prediction`, `drain_spool`.
- `backend/ratelimit.py` — `RateLimiter`.
- `backend/auth.py` — `API_KEY_HEADER`, `check_api_key`, `client_fingerprint`.
- `backend/retention.py` — `PurgeReport`, `purge`, CLI `main`.
- `backend/app.py` — `create_app`, `_gate` middleware, `POST /predict`, `GET /health`.
- `backend/Dockerfile` — digest-pinned, non-root CPU serve image.
- `model/contract.py` — **amended** with `probs_to_dict` (Phase 0 file, one addition).
- `MODEL_CARD.md` — **amended** with the machine-readable digest-of-record block.
- `tests/fixtures/make_model.py` — deterministic tiny skops artifact builder.
- `tests/unit/test_*.py`, `tests/integration/conftest.py`, `tests/integration/test_*.py`, `tests/perf/test_latency_budget.py`.
- `docs/latency-baseline.md` — measured p50/p95/p99, written by the load pass.

## Interfaces Produced (consumed by Phase 3+)

```python
# model/contract.py  (Phase 0 file, amended here)
probs_to_dict(row) -> dict[str, float]                 # ordered by LABELS, length-checked

# backend/config.py
MAX_INPUT_CHARS: int = 4000                            # hard literal, NOT environment-tunable
@dataclass(frozen=True) class Settings: ...
load_settings(env: Mapping[str, str] | None = None) -> Settings

# backend/preprocess.py
prepare_input(text: str) -> str                        # raises ValueError above the cap

# backend/model_card.py
read_expected_digest(card_path: Path) -> str           # 64-hex, from the git-committed card

# backend/model_loader.py
TRUSTED_TYPES: tuple[str, ...]                         # static literal allowlist
class ModelIntegrityError(RuntimeError): ...
@dataclass class LoadedModel:
    model_version: str        # FULL, internal:  "toxic-clf:v3@sha256:<64 hex>"
    public_version: str       # OPAQUE, public:  "toxic-clf:v3"
    def predict_proba(self, texts: list[str]) -> np.ndarray   # (len(texts), 6)
load_model(artifact_path, expected_sha256, artifact_name, registry_version) -> LoadedModel
load_from_settings(settings: Settings) -> LoadedModel
sha256_file(path: Path) -> str

# backend/policy.py
@dataclass(frozen=True) class DecisionResult:
    flags: dict[str, bool]; decision: str; max_prob: float
decide(probs: dict[str, float], thresholds: dict[str, float]) -> DecisionResult
load_thresholds(path: Path) -> dict[str, float]

# backend/audit.py
FLAGGED_INCLUSION_PROBABILITY: float = 1.0
should_random_audit(rate: float, rng: random.Random) -> bool

# backend/db.py
@dataclass(frozen=True) class PredictionRow: ...       # see Task 10
@dataclass(frozen=True) class ReviewIntent: ...
@dataclass(frozen=True) class PendingWrite: prediction: PredictionRow; review: ReviewIntent | None
make_engine(settings) -> Engine                        # bounded pool, 2s checkout timeout
init_schema(engine) -> None
insert_prediction(session, row: PredictionRow) -> None            # idempotent on request_id
enqueue_review(session, intent: ReviewIntent) -> None             # idempotent on request_id
fetch_pending_reviews(session, limit: int) -> list[ReviewQueue]
write_pending(session, pending: PendingWrite, stamp) -> int       # returns latency_ms

# backend/spool.py
class SpoolFull(RuntimeError): ...
class Spool:
    def depth(self) -> int
    def append(self, pending: PendingWrite) -> None
    def read_all(self) -> list[PendingWrite]
    def truncate(self) -> None

# backend/persistence.py
@dataclass(frozen=True) class PersistResult: persist_status: str; latency_ms: int; error: str | None
persist_prediction(session_factory, spool, pending, t0, retries=1) -> PersistResult
drain_spool(session_factory, spool) -> int

# backend/ratelimit.py
class RateLimiter:
    def __init__(self, per_minute: int, burst: int, clock=time.monotonic)
    def allow(self, key: str) -> bool

# backend/auth.py
API_KEY_HEADER = "X-API-Key"
check_api_key(presented: str | None, expected: str) -> bool        # constant time
client_fingerprint(api_key: str) -> str                            # sha256[:16]

# backend/retention.py
@dataclass(frozen=True) class PurgeReport: expired_reviews: int; purged_input_text: int; purged_snapshots: int
purge(session, now, *, input_text_retention_days, pending_review_ttl_days, snapshot_retention_days) -> PurgeReport

# backend/app.py
create_app(settings: Settings | None = None) -> FastAPI            # uvicorn runs it with --factory
```

**Tables produced:** `predictions`, `review_queue`, `feedback`. Phase 3 reads all three and writes `review_queue.reviewer_labels` / `distilbert_probs` and `feedback`.

## Interface Contract corrections (premortem H24)

The master plan's Interface Contracts block is declared authoritative but has drifted. These five corrections apply to the seams Phase 2 touches, and the master plan must be edited to match in Task 22.

| Master plan says | Corrected to | Why |
|---|---|---|
| `LoadedModel.model_version` is the only version field, e.g. `"toxic-clf:v3@sha256:abcd..."` | `LoadedModel` carries **both** `model_version` (full, with digest, internal only) and `public_version` (opaque, returned to clients) | H14. The response was handing out the digest that `/health` goes out of its way to strip |
| `load_model(artifact_path, expected_sha256) -> LoadedModel` | `load_model(artifact_path, expected_sha256, artifact_name, registry_version)` | The public label needs the registry version, and deriving it from the filename is guesswork |
| `PredictionResponse.model_version` example is `"toxic-clf:v3@<wandb-digest>"` | The response carries `public_version`; the **full** version goes to `predictions.model_version` and the request log | H14 |
| `insert_prediction(session, response: PredictionResponse, input_text: str) -> None` | `write_pending(session, pending: PendingWrite, stamp) -> int`, built on `PredictionRow` / `ReviewIntent` | H28 and H30. A failed request has no `PredictionResponse` but must still write a row, and the spool must be able to replay a prediction *and* its review row through one code path |
| `submit_review(...)` and `write_distilbert_probs(...)` are Phase 2 | Phase 3 | Both need reviewer session identity and re-scorer status semantics, neither of which exists in Phase 2. The tables they write are defined here |
| "A single authoritative array→dict adapter" — unnamed, no file | `model/contract.py::probs_to_dict(row) -> dict[str, float]` | H23. Phase 0 owns the file; Phase 2 is the first consumer and adds it if absent |

## Premortem coverage map

Every row has an owning task whose test **fails if the finding is unfixed**. Ids beginning `REG-` are the unnumbered normative items in delivery-spec §6.3 that the premortem found had no schedule row, no acceptance criterion, and no test; §13 names three of them as the compensating controls that make the public-registry decision defensible, so they are load-bearing rather than optional. `TAIL-1` is the parked tail risk "poisoned W&B artifact achieving RCE on EC2" and its named cheap pre-mitigation.

| Id | Finding | Owning task | Test that fails if unfixed |
|---|---|---|---|
| H30 | 503 on persistence failure is an attacker-operated off switch | 11, 12, 16 | `test_predict_stays_available_when_the_database_is_down` |
| H28 | `latency_ms` stamped before persistence; 503s write no row | 12, 15, 16, 20 | `test_latency_includes_the_persistence_component`, `test_failed_prediction_still_writes_a_row`, `test_p95_latency_under_budget` |
| H14 | `/predict` returns the digest that `/health` strips | 7, 15, 17, 18 | `test_no_response_ever_carries_the_artifact_digest` |
| H25 | Serving normalizer vs dedup normalizer; train/serve skew | 4, **4a** | `test_the_serving_path_uses_the_declared_serving_normalizer`, `test_model_card_folding_claim_matches_the_serving_path`, `test_the_input_cap_has_one_source_of_truth` |
| IFACE-DB-SCHEMA | Phase 2 and Phase 3 declare the same three tables incompatibly; `NotNullViolation` on every enqueue, `ck_review_source` rejects the H9 remedy, two `feedback` column sets, `init_schema` vs `init_db` | **10a** | `test_the_review_queue_sampling_column_has_exactly_one_name`, `test_the_review_source_vocabulary_admits_user_report`, `test_the_schema_entry_point_has_the_name_phase_3_imports` |
| H23 | Unnamed array→dict adapter; independent `zip()` re-derivations | 1, 15 | `test_backend_never_re_derives_the_label_zip` |
| H24 | Interface Contracts drift | 1, 7, 10, 22 | `test_master_plan_interface_block_matches_phase_2` (Task 22 checklist) |
| H22 | Output contract accepts incoherent flags | 8 | `test_severe_toxic_forces_toxic_before_the_response_is_built` |
| H8 | Live accuracy pools strata without weights; no rate stored | 9, 10, 10a | `test_review_row_records_its_sample_rate`, `test_a_design_stratum_row_cannot_omit_its_sample_rate` |
| REG-6.3a | `/predict` input-size cap | 3 | `test_oversize_text_is_rejected`, `test_input_cap_is_not_environment_tunable` |
| REG-6.3b | `/predict` rate limit | 13, 16 | `test_rate_limited_after_the_burst_is_exhausted` |
| REG-6.3c | Demo API key or source allowlist | 14, 16 | `test_predict_requires_a_valid_api_key` |
| REG-6.3d | `skops.io.load` with an explicit static allowlist | 6 | `test_trusted_types_is_a_literal_tuple_of_strings`, `test_type_outside_the_allowlist_is_rejected` |
| TAIL-1 | Digest and artifact share one credential | 5, 6 | `test_digest_of_record_comes_from_the_committed_model_card` |
| Retention (remediation 3.13) | Unbounded pending-review exemption | 19 | `test_pending_exemption_expires_at_the_hard_ttl` |

**Explicitly not owned by Phase 2**, listed so the gap is visible rather than assumed: H15 (no TLS — Phase A2 or an accepted documented decision), H16 (per-tier security groups and the read-only dashboard DB role — Phase A2 Terraform; Phase 2 only guarantees the backend needs no `SELECT` grant beyond its own tables), H12 (reviewer UI on its own port — Phase 3), H27 (`awslogs` log driver and the health alarm — Phase 5; Phase 2 emits the structured line those consume), C5 (`make seed-demo` — Phase 3/day 15; Phase 2 makes it possible, see below), H9 (`feedback.source='user'` — the column is defined here, the control is Phase 3), H6/H29 (RDS snapshot and 7-day restart — Phase A2).

## Design decisions this phase must make explicitly

**D1 — the persistence path (H30).** Delivery spec §10 says `/predict` returns 503 when the prediction cannot be persisted, and accepts "the moderation endpoint is unavailable while the database is unavailable". On a `db.t4g.micro` with no rate limit that is an off switch: modest concurrent traffic exhausts connections and moderation is *down*, not degraded, for as long as the pressure lasts. Rubric 2.2's complete-logging requirement does not actually demand that trade. It demands durability.

Phase 2 replaces the rule with three ordered paths:

1. **Direct.** Insert with a bounded connection checkout (2 s) and one retry. `persist_status='direct'`.
2. **Spooled.** On `SQLAlchemyError`, append the row — and its review row, if any — to an fsync'd append-only JSONL file on the instance volume, return **200**, and let the drainer replay it into `predictions` with `persist_status='spooled'` when Postgres recovers. Completeness is preserved because the spool is durable and the drain is idempotent on `request_id`.
3. **Fail closed.** When the spool reaches `SPOOL_MAX_ROWS` (10 000), return **503** with `Retry-After`. The availability switch still exists, but pulling it now costs an attacker ten thousand *successful* requests through a 30/minute-per-key rate limit — roughly five and a half hours per key — instead of a handful of concurrent connections.

Both paths are tested. The bound moved from "database connections", which an attacker controls, to "local disk", which the operator controls.

**D2 — train/serve skew (H25).** Delivery spec §6.2 describes the serving normalizer as the dedup normalizer *plus* confusable/homoglyph folding. They cannot both hold. Adding folding only at serving time means the model scores text it was never fitted on. Adding it to `model/data/dedup.normalize` changes dedup's output, therefore `data_version`, therefore the locked test set — after Phase 1 has already registered models against it. Phase 2 resolves it by making the two **the same function object**: `backend/preprocess.py` imports `normalize` from `model.data.dedup` and adds only a length cap. A test asserts function identity, so a future "improvement" to one side breaks the build. The residual cross-script and homoglyph evasion is disclosed in the model card, which delivery spec §13 already commits to, and the review queue is explicitly *not* a mitigation because a successful evasion is never flagged.

**D3 — where the digest of record lives (TAIL-1).** SHA-256 proves integrity in transit, not provenance. Today the artifact and the digest both arrive from W&B under one API key that is deliberately shared with RunPod pods, so an attacker who holds that key can serve a poisoned artifact *and* the matching digest. Phase 2 breaks the co-location for free: the expected digest is read from `MODEL_CARD.md`, which is committed to git and protected by the branch-protection rule, and the `MODEL_DIGEST` environment variable is cross-checked against it at startup. Forging both now requires compromising the registry **and** the repository.

**D4 — who may call `/predict`.** A public endpoint on a public repository with no auth is free denial-of-service capacity, and it is one of the three controls delivery spec §13 names as compensating for the public-registry decision. The control is a demo API key in an `X-API-Key` header, compared with `hmac.compare_digest`. The key is **not** published in the repository: `README.md` shows `curl -H "X-API-Key: $DEMO_API_KEY"` and the value goes in the Canvas submission text entry, which is not public. It is rotated after grading. `/health` is unauthenticated so a grader, the deploy gate, and the container `HEALTHCHECK` can all reach it.

**D5 — what `latency_ms` measures (H28).** It is stamped after the `INSERT` statement and any review-row insert, inside the same transaction, so the graded latency chart includes the persistence component instead of omitting the slowest one. The only piece still excluded is the `COMMIT` round trip, which is measured separately and emitted on the request log line as `commit_ms`, so the omission is visible rather than silent. Failed requests write a row with `status='error'` and their measured latency, so the slow tail is present in the series rather than structurally absent. Target: **p95 under 500 ms** for a single request against a warm process and a reachable database, measured once by the Task 20 load pass and recorded in `docs/latency-baseline.md`.

**D6 — downstream contract for `make seed-demo` (unblocks C5).** `PredictionRow.ts` is honoured when supplied, so Phase 3's demo seeding can back-date timestamps across 7–14 days rather than piling every point into one four-minute bucket. Seeding ~2 000 comments through `/predict` will trip the rate limit, so `make seed-demo` raises `RATE_LIMIT_PER_MINUTE` for the seeding window and restores it afterwards; that is a documented operator step, not a bypass in the code.

---

### Task 1: The authoritative array→dict adapter (H23, H24)

**Files:**
- Amend: `model/contract.py`
- Test: `tests/unit/test_probs_to_dict.py`

**Interfaces produced:** `probs_to_dict(row) -> dict[str, float]`

Phase 0 owns `model/contract.py`. Phase 2 is the first consumer, so if Phase 0 shipped the adapter this task is a no-op on the implementation and the tests still ship. If Phase 0 shipped it elsewhere, move it here — `model/contract.py` is the file every consumer already imports.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_probs_to_dict.py`:
```python
import ast
from pathlib import Path

import numpy as np
import pytest

from model.contract import probs_to_dict
from model.labels import LABELS


def test_probs_to_dict_maps_positionally_in_label_order():
    row = np.array([0.9, 0.1, 0.4, 0.03, 0.7, 0.05])
    out = probs_to_dict(row)
    assert list(out.keys()) == list(LABELS)
    assert out["toxic"] == pytest.approx(0.9)
    assert out["identity_hate"] == pytest.approx(0.05)


def test_probs_to_dict_returns_plain_floats():
    out = probs_to_dict(np.array([0.1] * 6, dtype=np.float32))
    assert all(type(value) is float for value in out.values())


def test_probs_to_dict_rejects_a_wrong_length_row():
    with pytest.raises(ValueError, match="expected 6 probabilities"):
        probs_to_dict(np.array([0.1, 0.2, 0.3]))


def test_backend_never_re_derives_the_label_zip():
    """H23: three call sites zipping LABELS independently mislabel probabilities silently
    if column order ever drifts, and the order-blind contract validator cannot see it."""
    offenders = []
    for path in sorted(Path("backend").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "zip":
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if "LABELS" in names:
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"use model.contract.probs_to_dict instead of zip(LABELS, ...): {offenders}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_probs_to_dict.py -v`
Expected: FAIL with `ImportError: cannot import name 'probs_to_dict' from 'model.contract'` (collection error on all four tests).

- [ ] **Step 3: Write minimal implementation**

> **Correction, 2026-07-31 (H23 recurring).** Phase 0 v2 Task 12 **already ships this function**, and Phase 1 Task 1 shipped a second copy; all three said "Append to `model/contract.py`" with three different bodies and three different error messages. Python keeps the last `def`, so the phase that lands last silently redefines the adapter for the two that landed earlier and their `pytest.raises(match=...)` cases go red untouched. **Phase 0 owns this function. Do not redefine it — import it, and delete any local copy.** Phase 4 Task 11's `test_probs_to_dict_is_defined_exactly_once` is the guard.
>
> This phase's tests must be written against the canonical messages: `"probs_to_dict takes a 1-D row, got shape …"` and `"expected 6 probabilities, got …"`. In particular `test_adapter_rejects_wrong_length` must use `match="expected 6 probabilities"` (unchanged) and any 2-D case must use `match="1-D"`. The canonical body does **not** call `ravel()`, so `np.zeros((2, 6))` is a dimensionality error rather than a 12-element length error.

Verify `model/contract.py` contains (do not append a second definition):
```python
def probs_to_dict(row: "np.ndarray") -> dict[str, float]:
    """THE array->dict converter. Owned by Phase 0 v2 Task 12.

    Independent `zip(LABELS, row)` re-derivations are the failure this exists to prevent: a
    column-order drift mislabels every probability, and the output contract validates label
    key membership rather than order, so no test would see it.
    """
    arr = np.asarray(row, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"probs_to_dict takes a 1-D row, got shape {arr.shape}")
    if arr.shape[0] != len(LABELS):
        raise ValueError(f"expected {len(LABELS)} probabilities, got {arr.shape[0]}")
    return {label: float(arr[i]) for i, label in enumerate(LABELS)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_probs_to_dict.py -v`
Expected: 4 PASS. `test_backend_never_re_derives_the_label_zip` would pass **vacuously** while `backend/` is empty, which is a green test that measures nothing — add `assert list(Path("backend").rglob("*.py")), "the scan found no files to scan"` as its first line so an empty package cannot be mistaken for a clean scan. It becomes load-bearing from Task 15.

- [ ] **Step 5: Commit**

```bash
git add model/contract.py tests/unit/test_probs_to_dict.py
git commit -m "Add the authoritative probability array to dict adapter"
```

---

### Task 2: Serving dependencies, settings, and secret hygiene

**Files:**
- Create: `requirements/serve.in`, `requirements/serve.txt`, `backend/__init__.py`, `backend/config.py`
- Amend: `Makefile`
- Test: `tests/unit/test_config.py`

**Interfaces produced:** `MAX_INPUT_CHARS`, `Settings`, `load_settings`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_config.py`:
```python
import pytest

from backend.config import MAX_INPUT_CHARS, load_settings

BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/toxic",
    "DEMO_API_KEY": "demo-key-value",
    "MODEL_ARTIFACT_PATH": "/srv/artifacts/toxic-clf.skops",
    "MODEL_CARD_PATH": "MODEL_CARD.md",
    "MODEL_DIGEST": "a" * 64,
    "MODEL_REGISTRY_VERSION": "3",
    "THRESHOLDS_PATH": "/srv/artifacts/thresholds.json",
}


def test_defaults_are_the_documented_ones():
    settings = load_settings(BASE_ENV)
    assert settings.rate_limit_per_minute == 30
    assert settings.rate_limit_burst == 10
    assert settings.max_body_bytes == 16384
    assert settings.spool_max_rows == 10000
    assert settings.db_pool_size == 5
    assert settings.db_timeout_seconds == 2.0
    assert settings.random_audit_rate == 0.05
    assert settings.input_text_retention_days == 30
    assert settings.pending_review_ttl_days == 7
    assert settings.snapshot_retention_days == 30
    assert settings.artifact_name == "toxic-clf"


def test_environment_overrides_are_applied():
    settings = load_settings({**BASE_ENV, "RATE_LIMIT_PER_MINUTE": "5", "SPOOL_MAX_ROWS": "42"})
    assert settings.rate_limit_per_minute == 5
    assert settings.spool_max_rows == 42


@pytest.mark.parametrize("missing", sorted(BASE_ENV))
def test_every_required_variable_is_required(missing):
    env = {key: value for key, value in BASE_ENV.items() if key != missing}
    with pytest.raises(RuntimeError, match=missing):
        load_settings(env)


def test_secrets_never_appear_in_the_repr():
    """A Settings object reaches tracebacks, `uvicorn --log-level debug`, and any crash
    reporter. The DSN carries the RDS master password and the API key is the abuse control."""
    settings = load_settings(BASE_ENV)
    rendered = repr(settings)
    assert "demo-key-value" not in rendered
    assert "u:p@localhost" not in rendered


def test_input_cap_is_not_environment_tunable():
    """REG-6.3a: a control that a deploy-time environment variable can widen is not a control.
    The size cap is a literal, and no Settings field shadows it."""
    settings = load_settings({**BASE_ENV, "MAX_INPUT_CHARS": "1000000"})
    assert MAX_INPUT_CHARS == 4000
    assert not hasattr(settings, "max_input_chars")


def test_random_audit_rate_must_be_a_probability():
    with pytest.raises(RuntimeError, match="RANDOM_AUDIT_RATE"):
        load_settings({**BASE_ENV, "RANDOM_AUDIT_RATE": "1.5"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend'`

- [ ] **Step 3: Write minimal implementation**

`requirements/serve.in` (Phase 0 pinned `base.txt`; `pip-compile` resolves the `-r` relative to this file's directory):
```
-r base.txt
fastapi==0.115.5
uvicorn==0.32.1
sqlalchemy==2.0.36
psycopg[binary]==3.2.3
```

`backend/__init__.py`: empty file, no imports.

`backend/config.py`:
```python
"""Environment-driven settings for the serving backend.

Two rules this module enforces rather than documents. Secrets are `repr=False`, because a
Settings object reaches every traceback. And MAX_INPUT_CHARS is a module literal with no
Settings field and no environment key, because an abuse control that a deploy-time variable
can widen is not a control (delivery spec section 6.3).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

MAX_INPUT_CHARS: int = 4000

REQUIRED = (
    "DATABASE_URL",
    "DEMO_API_KEY",
    "MODEL_ARTIFACT_PATH",
    "MODEL_CARD_PATH",
    "MODEL_DIGEST",
    "MODEL_REGISTRY_VERSION",
    "THRESHOLDS_PATH",
)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    demo_api_key: str = field(repr=False)
    model_artifact_path: Path
    model_card_path: Path
    model_digest: str
    model_registry_version: int
    thresholds_path: Path
    artifact_name: str = "toxic-clf"
    max_body_bytes: int = 16384
    rate_limit_per_minute: int = 30
    rate_limit_burst: int = 10
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_timeout_seconds: float = 2.0
    spool_path: Path = Path("/var/lib/toxic/predictions.spool")
    spool_max_rows: int = 10000
    random_audit_rate: float = 0.05
    input_text_retention_days: int = 30
    pending_review_ttl_days: int = 7
    snapshot_retention_days: int = 30
    latency_budget_p95_ms: int = 500


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    import os

    source: Mapping[str, str] = os.environ if env is None else env
    missing = [name for name in REQUIRED if not source.get(name)]
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")

    def integer(name: str, default: int) -> int:
        try:
            return int(source.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer") from exc

    def number(name: str, default: float) -> float:
        try:
            return float(source.get(name, default))
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a number") from exc

    rate = number("RANDOM_AUDIT_RATE", 0.05)
    if not 0.0 <= rate <= 1.0:
        raise RuntimeError("RANDOM_AUDIT_RATE must be between 0 and 1 inclusive")

    return Settings(
        database_url=source["DATABASE_URL"],
        demo_api_key=source["DEMO_API_KEY"],
        model_artifact_path=Path(source["MODEL_ARTIFACT_PATH"]),
        model_card_path=Path(source["MODEL_CARD_PATH"]),
        model_digest=source["MODEL_DIGEST"],
        model_registry_version=integer("MODEL_REGISTRY_VERSION", 1),
        thresholds_path=Path(source["THRESHOLDS_PATH"]),
        artifact_name=source.get("ARTIFACT_NAME", "toxic-clf"),
        max_body_bytes=integer("MAX_BODY_BYTES", 16384),
        rate_limit_per_minute=integer("RATE_LIMIT_PER_MINUTE", 30),
        rate_limit_burst=integer("RATE_LIMIT_BURST", 10),
        db_pool_size=integer("DB_POOL_SIZE", 5),
        db_max_overflow=integer("DB_MAX_OVERFLOW", 5),
        db_timeout_seconds=number("DB_TIMEOUT_SECONDS", 2.0),
        spool_path=Path(source.get("SPOOL_PATH", "/var/lib/toxic/predictions.spool")),
        spool_max_rows=integer("SPOOL_MAX_ROWS", 10000),
        random_audit_rate=rate,
        input_text_retention_days=integer("INPUT_TEXT_RETENTION_DAYS", 30),
        pending_review_ttl_days=integer("PENDING_REVIEW_TTL_DAYS", 7),
        snapshot_retention_days=integer("SNAPSHOT_RETENTION_DAYS", 30),
        latency_budget_p95_ms=integer("LATENCY_BUDGET_P95_MS", 500),
    )
```

Amend the `Makefile` (append these targets; keep the existing `venv`, `lint`, `test`, `data`):
```makefile
.PHONY: serve-deps test-integration serve purge
serve-deps:
	$(BIN)/pip install pip-tools==7.4.1
	$(BIN)/pip-compile --generate-hashes --output-file requirements/serve.txt requirements/serve.in
	$(BIN)/pip install --require-hashes -r requirements/serve.txt
test-integration:
	PYTHONHASHSEED=0 $(BIN)/pytest -m integration
serve:
	$(BIN)/uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000
purge:
	$(BIN)/python -m backend.retention
```

- [ ] **Step 4: Generate the hashed lock and run the tests**

Run: `make serve-deps && PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_config.py -v && .venv/bin/ruff check backend`
Expected: `requirements/serve.txt` written with `--hash=sha256:` lines for every package; 11 PASS (six named tests, of which one is parametrized over five variables); ruff clean.

- [ ] **Step 5: Commit**

```bash
git add requirements/serve.in requirements/serve.txt backend/__init__.py backend/config.py Makefile tests/unit/test_config.py
git commit -m "Add serving dependencies and backend settings with secret-safe repr"
```

---

### Task 3: Request schema with a hard input-size cap (REG-6.3a)

**Files:**
- Create: `backend/schemas.py`
- Test: `tests/unit/test_schemas.py`

**Interfaces produced:** `PredictRequest`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_schemas.py`:
```python
import pytest
from pydantic import ValidationError

from backend.config import MAX_INPUT_CHARS
from backend.schemas import PredictRequest


def test_valid_text_parses():
    request = PredictRequest(text="you are wrong about this")
    assert request.text == "you are wrong about this"


def test_text_at_the_cap_is_accepted():
    assert len(PredictRequest(text="a" * MAX_INPUT_CHARS).text) == MAX_INPUT_CHARS


def test_oversize_text_is_rejected():
    """REG-6.3a. Jigsaw comments top out around 5k characters; a moderation endpoint that
    accepts a megabyte of text per request is free CPU for anyone who asks."""
    with pytest.raises(ValidationError, match="at most 4000"):
        PredictRequest(text="a" * (MAX_INPUT_CHARS + 1))


def test_empty_text_is_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(text="")


def test_whitespace_only_text_is_rejected():
    with pytest.raises(ValidationError, match="must not be blank"):
        PredictRequest(text="   \n\t  ")


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        PredictRequest(text="hello", reviewer_id="not-yours")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.schemas'`

- [ ] **Step 3: Write minimal implementation**

`backend/schemas.py`:
```python
"""Request models for the serving backend. The response model is model/contract.py."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.config import MAX_INPUT_CHARS


class PredictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)

    @field_validator("text")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_schemas.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/schemas.py tests/unit/test_schemas.py
git commit -m "Add predict request schema with a hard input-size cap"
```

---

### Task 4: One normalizer for training and serving (H25)

**Files:**
- Create: `backend/preprocess.py`
- Test: `tests/unit/test_preprocess.py`

**Interfaces produced:** `prepare_input(text) -> str`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_preprocess.py`:
```python
import pytest

import backend.preprocess as preprocess
import model.data.dedup as dedup
from backend.config import MAX_INPUT_CHARS
from backend.preprocess import prepare_input

SKEW_CORPUS = [
    "You  are an   IDIOT",
    "ＦＵＬＬＷＩＤＴＨ text",
    "  leading and trailing  ",
    "line\nbreaks\tand\ttabs",
    "Ünicode combining áccent",
    "f*ck this garbage",
    "MiXeD CaSe WoRdS",
]


def test_serving_normalizer_is_the_dedup_normalizer_itself():
    """H25. The delivery spec described the serving normalizer as dedup's plus homoglyph
    folding; that is train/serve skew by construction. One function object, asserted, so an
    'improvement' to either side breaks the build instead of silently shifting the input
    distribution the model was fitted on."""
    assert preprocess.normalize is dedup.normalize


def test_no_serving_side_normalization_diverges_from_training():
    for text in SKEW_CORPUS:
        assert prepare_input(text) == dedup.normalize(text)


def test_prepare_input_normalizes_case_and_whitespace():
    assert prepare_input("You  are an   IDIOT") == "you are an idiot"


def test_prepare_input_rejects_text_above_the_cap():
    with pytest.raises(ValueError, match="exceeds 4000 characters"):
        prepare_input("a" * (MAX_INPUT_CHARS + 1))


def test_prepare_input_accepts_text_at_the_cap():
    assert prepare_input("a" * MAX_INPUT_CHARS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_preprocess.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.preprocess'`

- [ ] **Step 3: Write minimal implementation**

`backend/preprocess.py`:
```python
"""Serving-path input preparation.

Train/serve skew resolution (premortem H25). The serving normalizer IS
`model.data.dedup.normalize` - the same function object, not a superset. Delivery spec
section 6.2 described it as dedup's normalizer plus confusable/homoglyph folding, which
cannot hold: folding only here means the model scores text it was never fitted on, and
folding in `dedup` changes dedup's output, therefore `data_version`, therefore the locked
test set - after Phase 1 registered models against it. The gap is closed by making the two
identical. Residual cross-script and homoglyph evasion is a model-card limitation, and the
review queue does not mitigate it because a successful evasion is never flagged.
"""

from backend.config import MAX_INPUT_CHARS
from model.data.dedup import normalize

__all__ = ["MAX_INPUT_CHARS", "normalize", "prepare_input"]


def prepare_input(text: str) -> str:
    """Normalize one comment for scoring. Raises on oversize input.

    The pydantic layer already rejects oversize text with 422; this is the second gate, for
    internal callers such as the spool drainer and the Phase 3 re-scorer.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise ValueError(f"input exceeds {MAX_INPUT_CHARS} characters")
    return normalize(text)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_preprocess.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/preprocess.py tests/unit/test_preprocess.py
git commit -m "Share one normalizer between the training and serving paths"
```

---

### Task 4a: The serving path uses the declared serving normalizer, and the cap has one source [H25 — resolves a mutually exclusive contradiction with Phase 0 Task 3]

Phase 0 Task 3 and Task 4 above resolve H25 in **mutually exclusive** ways, and today nothing fails when the wrong one wins.

- Phase 0 ships two functions in `model/normalize.py`: a **frozen** corpus `normalize` (NFKC + casefold + whitespace, golden-digest pinned) and `normalize_for_serving` (corpus normalizer **plus** confusable folding, combining-mark stripping, and a `MAX_INPUT_CHARS = 5000` cap). Its test asserts `normalize_for_serving("уou are an idiot") == "you are an idiot"` while `normalize` does not fold it.
- Task 4 above then makes the serving path `model.data.dedup.normalize` — **no folding at all** — with a cap of 4000 in `backend/config.py`.

Under Task 4's resolution, `normalize_for_serving` becomes dead code no consumer imports, and Phase 1 Task 16's `MODEL_CARD.md` claim — "Serving-path normalization (NFKC, homoglyph folding, lowercase, whitespace collapse) defeats simple tricks" — repeated by Phase 5 Task 21, is **false**, with no test comparing the card's claim to the serving code.

**Phase 0's resolution is the one that binds**, for the reason Phase 0 stated and Task 4's docstring got backwards: folding at serving maps an evasion *onto* the training distribution rather than away from it (`уou` → `you`, a token the model was fitted on), and it is applied *after* the corpus normalizer, so `model/data/dedup.py` never imports it, dedup output never moves, and `split_version` never moves. Train/serve skew is bounded to inputs that contain confusables or combining marks — which is the entire point.

The named limitation stands and belongs in the card: `normalize_for_serving` strips combining marks, so `händbuch` serves as `handbuch` while the corpus keeps `händbuch`.

**Files:**
- Modify: `backend/preprocess.py`, `backend/config.py`
- Test: `tests/unit/test_preprocess.py` (delete two cases, append four)

- [ ] **Step 1: Write the failing test**

**Delete** these two cases from Task 4's `tests/unit/test_preprocess.py`; they encode the rejected resolution:
`test_serving_normalizer_is_the_dedup_normalizer_itself` and `test_no_serving_side_normalization_diverges_from_training`.

Change `test_prepare_input_rejects_text_above_the_cap` to match the cap it is given rather than a hard-coded 4000:
```python
def test_prepare_input_rejects_text_above_the_cap():
    with pytest.raises(ValueError, match=f"exceeds {MAX_INPUT_CHARS} characters"):
        prepare_input("a" * (MAX_INPUT_CHARS + 1))
```

Append:
```python
import model.data.dedup as dedup
import model.normalize as mnorm
from pathlib import Path


def test_the_serving_path_uses_the_declared_serving_normalizer():
    """H25. Phase 0 shipped `normalize_for_serving` and the model card claims it. Binding
    the serving path to the corpus normalizer instead makes that claim false and leaves the
    serving normalizer as dead code no consumer imports."""
    assert preprocess.normalize is mnorm.normalize_for_serving


def test_the_corpus_normalizer_is_still_the_one_dedup_uses():
    """The other half of H25: folding must NEVER reach dedup, because that moves
    split_version and therefore the locked 15% test set, after models were registered."""
    assert dedup.normalize is mnorm.normalize
    assert "normalize_for_serving" not in Path("model/data/dedup.py").read_text(encoding="utf-8")


def test_the_serving_path_defeats_the_trick_the_model_card_claims_it_defeats():
    assert prepare_input("уou are an idiot") == "you are an idiot"     # Cyrillic у
    assert prepare_input("You  are an   IDIOT") == "you are an idiot"
    assert dedup.normalize("уou are an idiot") != "you are an idiot"


def test_model_card_folding_claim_matches_the_serving_path():
    """The card is a graded artifact and a public one. If it claims folding, folding runs."""
    card = Path("MODEL_CARD.md").read_text(encoding="utf-8")
    if "homoglyph folding" in card:
        assert prepare_input("уou are an idiot") == "you are an idiot"


def test_the_input_cap_has_one_source_of_truth():
    """Phase 0 says 5000 in model/normalize.py; this phase said 4000 in backend/config.py,
    and both described themselves as authoritative. A cap with two values is a cap that is
    enforced twice at different places and reported wrongly at least once."""
    import backend.config
    assert backend.config.MAX_INPUT_CHARS is mnorm.MAX_INPUT_CHARS
    assert "MAX_INPUT_CHARS: int = 4000" not in Path("backend/config.py").read_text(encoding="utf-8")
```

Also change Task 2's `tests/unit/test_config.py` assertion `assert MAX_INPUT_CHARS == 4000` to `assert MAX_INPUT_CHARS == mnorm.MAX_INPUT_CHARS == 5000`. The property that test is protecting — the cap is **not** environment-tunable — is unchanged and still asserted by the surrounding case.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_preprocess.py tests/unit/test_config.py -v`
Expected: FAIL — `assert <function normalize at 0x…> is <function normalize_for_serving at 0x…>`, and `assert 4000 is 5000` on the cap.

- [ ] **Step 3: Write minimal implementation**

In `backend/config.py`, replace the literal with the single source:
```python
from model.normalize import MAX_INPUT_CHARS  # single source of truth; see Phase 0 Task 3

__all__ = [..., "MAX_INPUT_CHARS"]
```
`MAX_INPUT_CHARS` remains un-tunable by environment: it is not read from `os.environ` and it is not a `Settings` field. It is now un-tunable in *one* place instead of two.

Replace the import and docstring in `backend/preprocess.py`:
```python
"""Serving-path input preparation.

Train/serve skew resolution (premortem H25). The serving normalizer is
`model.normalize.normalize_for_serving`: the FROZEN corpus normalizer plus confusable
folding, combining-mark stripping, and the length cap. It is a strict superset applied
AFTER the corpus normalizer, so `model/data/dedup.py` never imports it, dedup output
never moves, `split_version` never moves, and the locked 15% test set stays locked.

Folding at serving time maps an evasion ONTO the training distribution rather than away
from it: `уou` becomes `you`, a token the model was fitted on. The residual skew is
bounded to inputs containing confusables or combining marks, which is the population this
exists to canonicalise.

Named limitation for MODEL_CARD.md: combining marks are stripped, so `händbuch` serves as
`handbuch` while the corpus keeps `händbuch`. Residual cross-script and paraphrase evasion
remains a model-card limitation, and the review queue does not mitigate it because a
successful evasion is never flagged.
"""

from model.normalize import MAX_INPUT_CHARS, normalize_for_serving as normalize

__all__ = ["MAX_INPUT_CHARS", "normalize", "prepare_input"]
```
`prepare_input` is unchanged: it raises above the cap **before** calling `normalize`, so the serving normalizer's internal truncation is never reached on this path and no oversize input is ever silently shortened.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_preprocess.py tests/unit/test_config.py tests/unit/test_normalize.py -v`
Expected: 7 PASS in `test_preprocess.py`, and Phase 0's `test_dedup_does_not_use_the_serving_normalizer` still green.

- [ ] **Step 5: Commit**

```bash
git add backend/preprocess.py backend/config.py tests/unit/test_preprocess.py tests/unit/test_config.py
git commit -m "Bind the serving path to normalize_for_serving and give the input cap one source"
```

---

### Task 5: The digest of record lives in the git-committed model card (TAIL-1)

**Files:**
- Create: `backend/model_card.py`
- Amend: `MODEL_CARD.md` (drafted in Phase 1)
- Test: `tests/unit/test_model_card.py`

**Interfaces produced:** `read_expected_digest(card_path) -> str`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_model_card.py`:
```python
from pathlib import Path

import pytest

from backend.model_card import DIGEST_LINE, read_expected_digest

DIGEST = "3f" * 32
CARD = f"""# Model Card: toxic-clf

## Artifact digest of record

- MODEL_ARTIFACT: toxic-clf
- MODEL_REGISTRY_VERSION: 3
- MODEL_DIGEST: sha256:{DIGEST}
"""


def test_reads_the_digest_from_a_well_formed_card(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(CARD, encoding="utf-8")
    assert read_expected_digest(card) == DIGEST


def test_missing_digest_line_raises(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text("# Model Card\n\nNo digest here.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL_DIGEST"):
        read_expected_digest(card)


def test_conflicting_digests_raise(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(CARD + f"- MODEL_DIGEST: sha256:{'ab' * 32}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting"):
        read_expected_digest(card)


def test_truncated_digest_is_not_accepted(tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text("- MODEL_DIGEST: sha256:abc123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="MODEL_DIGEST"):
        read_expected_digest(card)


def test_the_repositorys_own_model_card_declares_a_digest():
    """TAIL-1. The digest of record must be in the repository, under branch protection, so
    that forging it requires compromising git as well as the registry credential."""
    assert DIGEST_LINE.search(Path("MODEL_CARD.md").read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_card.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.model_card'`

- [ ] **Step 3: Write minimal implementation**

`backend/model_card.py`:
```python
"""Read the expected artifact digest from the git-committed model card.

Provenance, not merely integrity (premortem tail risk 1). SHA-256 proves an artifact arrived
unaltered; it proves nothing about who produced it. Today the artifact and its digest both
come from Weights & Biases under one API key that is deliberately shared with RunPod pods,
so whoever holds that key can publish a poisoned artifact and a matching digest. Reading the
expected digest from MODEL_CARD.md - committed to git, protected by branch protection -
splits the two trust domains, and the cost of doing so is one regex.
"""

import re
from pathlib import Path

DIGEST_LINE = re.compile(r"^-\s*MODEL_DIGEST:\s*sha256:([0-9a-f]{64})\s*$", re.MULTILINE)


def read_expected_digest(card_path: Path) -> str:
    """Return the 64-character hex digest declared by the model card.

    Raises ValueError when the card declares none, or declares more than one distinct value.
    """
    text = Path(card_path).read_text(encoding="utf-8")
    found = DIGEST_LINE.findall(text)
    if not found:
        raise ValueError(
            f"{card_path} carries no `- MODEL_DIGEST: sha256:<64 lowercase hex>` line"
        )
    distinct = sorted(set(found))
    if len(distinct) > 1:
        raise ValueError(f"{card_path} declares {len(distinct)} conflicting MODEL_DIGEST values")
    return distinct[0]
```

Append the digest-of-record block to `MODEL_CARD.md`, computing the value from the artifact in hand rather than transcribing it from the W&B UI — a transcribed digest is a digest the registry supplied, which is the co-location this control exists to break:

```bash
DIGEST=$(sha256sum artifacts/toxic-clf.skops | cut -d' ' -f1)
VERSION=$(python -c "import json,sys; print(json.load(open('artifacts/registry.json'))['version'])")
cat >> MODEL_CARD.md <<CARD

## Artifact digest of record

The serving backend refuses to load any artifact whose SHA-256 differs from this value, and
refuses to start if the MODEL_DIGEST environment variable differs from it. This line is the
provenance anchor: it lives in git rather than in the registry, so an attacker holding the
registry credential cannot supply both the artifact and its expected digest.

- MODEL_ARTIFACT: toxic-clf
- MODEL_REGISTRY_VERSION: ${VERSION}
- MODEL_DIGEST: sha256:${DIGEST}
CARD
grep -E '^- MODEL_' MODEL_CARD.md
```

If Phase 1 did not emit `artifacts/registry.json`, substitute the promoted version number directly; the format the parser requires is the `MODEL_DIGEST` line alone.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_card.py -v`
Expected: 5 PASS. If `test_the_repositorys_own_model_card_declares_a_digest` fails, the placeholder was committed instead of the real digest — that is the test doing its job.

- [ ] **Step 5: Commit**

```bash
git add backend/model_card.py MODEL_CARD.md tests/unit/test_model_card.py
git commit -m "Record the artifact digest of record in the committed model card"
```

---

### Task 6: Fail-closed safe loader with a static trusted-type allowlist (REG-6.3d, TAIL-1)

**Files:**
- Create: `backend/model_loader.py`, `tests/fixtures/make_model.py`
- Test: `tests/unit/test_model_loader.py`

**Interfaces produced:** `TRUSTED_TYPES`, `ModelIntegrityError`, `LoadedModel`, `sha256_file`, `load_model`, `load_from_settings`

- [ ] **Step 1: Write the failing test**

`tests/fixtures/make_model.py`:
```python
"""Deterministic tiny classical artifact, shaped exactly like the Phase 1 Production model.

Every label carries both classes so OneVsRestClassifier fits a real LogisticRegression per
label rather than a _ConstantPredictor; that keeps the fixture's trusted-type set equal to
the production one.
"""

import hashlib
from pathlib import Path

import numpy as np
import skops.io as sio
from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline

TEXTS = [
    "have a nice day friend",
    "thanks for the thoughtful edit",
    "you are an idiot",
    "what a moron you are",
    "f*ck this garbage",
    "i will kill you",
    "people of that group are subhuman",
    "you vile disgusting worthless scum",
]

# columns: toxic, severe_toxic, obscene, threat, insult, identity_hate
Y = np.array(
    [
        [0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0],
        [1, 0, 0, 0, 1, 0],
        [1, 0, 0, 0, 1, 0],
        [1, 0, 1, 0, 0, 0],
        [1, 1, 0, 1, 0, 0],
        [1, 0, 0, 0, 0, 1],
        [1, 1, 1, 1, 1, 1],
    ]
)


def build_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=1)),
            (
                "clf",
                OneVsRestClassifier(
                    CalibratedClassifierCV(
                        LogisticRegression(class_weight="balanced", solver="liblinear"),
                        cv=2,
                        method="sigmoid",
                    )
                ),
            ),
        ]
    )


def build_demo_artifact(path: Path) -> tuple[Path, str]:
    """Fit, dump with skops, and return (path, sha256 hex digest)."""
    pipeline = build_pipeline().fit(TEXTS, Y)
    sio.dump(pipeline, path)
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return Path(path), digest
```

`tests/unit/test_model_loader.py`:
```python
import ast
from pathlib import Path

import pytest
import skops.io as sio
from sklearn.preprocessing import FunctionTransformer

from backend.config import load_settings
from backend.model_loader import (
    TRUSTED_TYPES,
    LoadedModel,
    ModelIntegrityError,
    load_from_settings,
    load_model,
    sha256_file,
)
from tests.fixtures.make_model import build_demo_artifact

SOURCE = Path("backend/model_loader.py")


def _shout(text):  # a plain function: exactly the payload the allowlist exists to refuse
    return [t.upper() for t in text]


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    return build_demo_artifact(tmp_path_factory.mktemp("artifact") / "toxic-clf.skops")


def test_trusted_types_is_a_literal_tuple_of_strings():
    """REG-6.3d. The control is only real if it cannot be widened at deploy time. A tuple of
    string literals is auditable in a diff; a computed list is not."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assigns = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "TRUSTED_TYPES"
    ]
    assert len(assigns) == 1, "TRUSTED_TYPES must be assigned exactly once at module level"
    literal = assigns[0].value
    assert isinstance(literal, ast.Tuple), "TRUSTED_TYPES must be a tuple literal"
    assert literal.elts, "TRUSTED_TYPES must not be empty"
    for element in literal.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
            "every TRUSTED_TYPES entry must be a string literal, not a call, name, or splat"
        )


def test_loader_never_trusts_whatever_the_artifact_contains():
    source = SOURCE.read_text(encoding="utf-8")
    assert "get_untrusted_types" not in source, (
        "get_untrusted_types()-then-trust-all silently voids the control"
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "trusted":
            assert not (
                isinstance(node.value, ast.Constant) and node.value.value is True
            ), "trusted=True disables the allowlist"


def test_type_outside_the_allowlist_is_rejected(tmp_path):
    """The poisoned-artifact path: an arbitrary callable inside the artifact is remote code
    execution in a process that holds the instance role."""
    payload = tmp_path / "payload.skops"
    sio.dump(FunctionTransformer(func=_shout), payload)
    with pytest.raises(ModelIntegrityError, match="untrusted"):
        load_model(payload, sha256_file(payload), artifact_name="toxic-clf", registry_version=1)


def test_digest_mismatch_fails_closed(artifact):
    path, _ = artifact
    with pytest.raises(ModelIntegrityError, match="digest mismatch"):
        load_model(path, "0" * 64, artifact_name="toxic-clf", registry_version=3)


def test_malformed_expected_digest_fails_closed(artifact):
    path, _ = artifact
    with pytest.raises(ModelIntegrityError, match="64-character"):
        load_model(path, "not-a-digest", artifact_name="toxic-clf", registry_version=3)


def test_tampered_artifact_fails_closed(artifact, tmp_path):
    path, digest = artifact
    tampered = tmp_path / "tampered.skops"
    tampered.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(ModelIntegrityError, match="digest mismatch"):
        load_model(tampered, digest, artifact_name="toxic-clf", registry_version=3)


def test_valid_artifact_loads_and_scores(artifact):
    path, digest = artifact
    model = load_model(path, digest, artifact_name="toxic-clf", registry_version=3)
    assert isinstance(model, LoadedModel)
    probabilities = model.predict_proba(["you are an idiot", "have a nice day"])
    assert probabilities.shape == (2, 6)
    assert ((probabilities >= 0.0) & (probabilities <= 1.0)).all()


def test_digest_of_record_comes_from_the_committed_model_card(artifact, tmp_path):
    """TAIL-1. A MODEL_DIGEST that disagrees with the card means the two trust domains
    disagree, and the only safe response is to refuse to start."""
    path, digest = artifact
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(f"- MODEL_DIGEST: sha256:{digest}\n", encoding="utf-8")
    env = {
        "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/toxic",
        "DEMO_API_KEY": "k",
        "MODEL_ARTIFACT_PATH": str(path),
        "MODEL_CARD_PATH": str(card),
        "MODEL_DIGEST": digest,
        "MODEL_REGISTRY_VERSION": "3",
        "THRESHOLDS_PATH": "unused.json",
    }
    assert load_from_settings(load_settings(env)).public_version == "toxic-clf:v3"

    disagreeing = {**env, "MODEL_DIGEST": "b" * 64}
    with pytest.raises(ModelIntegrityError, match="model card"):
        load_from_settings(load_settings(disagreeing))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_loader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.model_loader'`

- [ ] **Step 3: Write minimal implementation**

`backend/model_loader.py`:
```python
"""Safe model loading. This is the trust boundary between the registry and the instance.

The registry hands an artifact over the network into a process holding the EC2 instance
profile, so a poisoned artifact is remote code execution against the account. Two independent
controls close that path, and both fail closed:

1. Provenance and integrity. The expected digest is read from the git-committed model card
   and cross-checked against the MODEL_DIGEST environment variable before anything is
   deserialized, so the artifact and its expected digest do not share one trust domain.
2. Deserialization under an explicit static allowlist. `get_untrusted_types()` followed by
   trusting the result is not a control - it trusts whatever the attacker put in the file.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from skops.io import load as skops_load
from skops.io.exceptions import UntrustedTypesFoundException

from backend.config import Settings
from backend.model_card import read_expected_digest

# EXPLICIT STATIC ALLOWLIST. Every entry is a string literal. Widening it is a reviewable
# diff on this line, never a runtime decision. numpy and scipy containers are handled by
# skops' own persistence protocols and deliberately do not appear here.
TRUSTED_TYPES: tuple[str, ...] = (
    "sklearn.calibration.CalibratedClassifierCV",
    "sklearn.calibration._CalibratedClassifier",
    "sklearn.calibration._SigmoidCalibration",
    "sklearn.feature_extraction.text.TfidfTransformer",
    "sklearn.feature_extraction.text.TfidfVectorizer",
    "sklearn.isotonic.IsotonicRegression",
    "sklearn.linear_model._logistic.LogisticRegression",
    "sklearn.multiclass.OneVsRestClassifier",
    "sklearn.multiclass._ConstantPredictor",
    "sklearn.pipeline.FeatureUnion",
    "sklearn.pipeline.Pipeline",
    "sklearn.preprocessing._data.Normalizer",
    "sklearn.preprocessing._label.LabelBinarizer",
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ModelIntegrityError(RuntimeError):
    """Provenance or integrity could not be established. Never recoverable at runtime."""


@dataclass
class LoadedModel:
    model_version: str
    public_version: str
    estimator: object = field(repr=False)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.estimator.predict_proba(texts), dtype=float)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def load_model(
    artifact_path: Path,
    expected_sha256: str,
    artifact_name: str,
    registry_version: int,
) -> LoadedModel:
    if not _HEX64.match(expected_sha256 or ""):
        raise ModelIntegrityError(
            "expected_sha256 must be 64-character lowercase hex; refusing to load"
        )
    actual = sha256_file(artifact_path)
    if not hmac.compare_digest(actual, expected_sha256):
        raise ModelIntegrityError(
            f"artifact digest mismatch: expected {expected_sha256}, computed {actual}"
        )
    try:
        estimator = skops_load(artifact_path, trusted=list(TRUSTED_TYPES))
    except UntrustedTypesFoundException as exc:
        raise ModelIntegrityError(f"artifact contains untrusted types: {exc}") from exc
    return LoadedModel(
        model_version=f"{artifact_name}:v{registry_version}@sha256:{expected_sha256}",
        public_version=f"{artifact_name}:v{registry_version}",
        estimator=estimator,
    )


def load_from_settings(settings: Settings) -> LoadedModel:
    card_digest = read_expected_digest(settings.model_card_path)
    if not hmac.compare_digest(card_digest, settings.model_digest):
        raise ModelIntegrityError(
            "MODEL_DIGEST does not match the digest of record in the model card; "
            "the registry and the repository disagree about which artifact is Production"
        )
    return load_model(
        settings.model_artifact_path,
        card_digest,
        artifact_name=settings.artifact_name,
        registry_version=settings.model_registry_version,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_loader.py -v`
Expected: 8 PASS.

If `test_valid_artifact_loads_and_scores` raises `ModelIntegrityError: artifact contains untrusted types: [...]`, skops needs a type this allowlist does not name. Add the **exact** type it printed as one more **string literal** in `TRUSTED_TYPES`, in alphabetical order, and re-run. That is the only sanctioned way to widen the list: never by calling `get_untrusted_types()`, never by passing `trusted=True`. The AST test enforces the distinction.

- [ ] **Step 5: Commit**

```bash
git add backend/model_loader.py tests/fixtures/make_model.py tests/unit/test_model_loader.py
git commit -m "Add fail-closed model loader with a static trusted-type allowlist"
```

---

### Task 7: Opaque public version label (H14)

**Files:**
- Amend: none (the fields land in Task 6; this task is their contract test)
- Test: `tests/unit/test_public_version.py`

**Interfaces produced:** the `model_version` / `public_version` split, asserted

- [ ] **Step 1: Write the failing test**

`tests/unit/test_public_version.py`:
```python
import re

import pytest

from backend.model_loader import load_model, sha256_file
from tests.fixtures.make_model import build_demo_artifact

HEX64 = re.compile(r"[0-9a-f]{64}")


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    path, digest = build_demo_artifact(tmp_path_factory.mktemp("artifact") / "toxic-clf.skops")
    assert digest == sha256_file(path)
    return load_model(path, digest, artifact_name="toxic-clf", registry_version=3)


def test_public_version_carries_no_digest(model):
    """H14. Delivery spec section 6.3 strips the digest from /health specifically so the
    exact model cannot be fingerprinted by an attacker crafting evasions. Returning it on
    every /predict response makes that control inert."""
    assert model.public_version == "toxic-clf:v3"
    assert "sha256" not in model.public_version
    assert not HEX64.search(model.public_version)


def test_full_version_is_retained_for_logs_and_the_database(model):
    assert model.model_version.startswith("toxic-clf:v3@sha256:")
    assert HEX64.search(model.model_version)


def test_the_two_labels_are_distinct(model):
    assert model.public_version != model.model_version
    assert model.model_version.startswith(model.public_version)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_public_version.py -v`
Expected: FAIL with `AttributeError: 'LoadedModel' object has no attribute 'public_version'` if Task 6 was implemented from the pre-correction master-plan interface; PASS immediately if Task 6 was implemented as written. Either outcome is informative — the test exists so the split cannot be quietly collapsed back into one field later.

- [ ] **Step 3: Write minimal implementation**

No new code if Task 6 shipped `LoadedModel` as written. If the split is missing, add it exactly as in Task 6 and update `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`'s Interface Contracts block:

```python
# backend/model_loader.py
@dataclass
class LoadedModel:
    model_version: str                # full, internal:  "toxic-clf:v3@sha256:abcd..."
    public_version: str               # opaque, public:  "toxic-clf:v3"
    def predict_proba(self, texts: list[str]) -> "np.ndarray": ...

def load_model(artifact_path: "Path", expected_sha256: str,
               artifact_name: str, registry_version: int) -> LoadedModel: ...
# verifies SHA-256 against the digest of record in MODEL_CARD.md, skops.io.load with a
# static trusted allowlist, fails closed on mismatch. The API returns public_version;
# predictions.model_version and the request log carry the full model_version.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_public_version.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_public_version.py docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md
git commit -m "Split the internal model version from the opaque public label"
```

---

### Task 8: Moderation policy with hierarchical coherence (H22)

**Files:**
- Create: `backend/policy.py`
- Test: `tests/unit/test_policy.py`

**Interfaces produced:** `DecisionResult`, `decide`, `load_thresholds`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_policy.py`:
```python
import json

import pytest

from backend.policy import BLOCK_MARGIN, REVIEW_MARGIN, decide, load_thresholds
from model.labels import LABELS

THRESHOLDS = {label: 0.50 for label in LABELS}


def probs(**overrides) -> dict[str, float]:
    values = {label: 0.01 for label in LABELS}
    values.update(overrides)
    return values


def test_probability_exactly_at_the_threshold_flags():
    result = decide(probs(insult=0.50), THRESHOLDS)
    assert result.flags["insult"] is True


def test_probability_just_below_the_threshold_does_not_flag():
    result = decide(probs(insult=0.4999), THRESHOLDS)
    assert result.flags["insult"] is False


def test_severe_toxic_forces_toxic_before_the_response_is_built():
    """H22 and delivery spec section 6.2. The contract must never carry 'severe but not
    toxic'. Enforcing it in the policy means the contract validator is a backstop that never
    fires in production rather than the only thing standing between the model and the UI."""
    result = decide(probs(toxic=0.02, severe_toxic=0.91), THRESHOLDS)
    assert result.flags["severe_toxic"] is True
    assert result.flags["toxic"] is True


def test_coherence_does_not_invent_probabilities():
    result = decide(probs(toxic=0.02, severe_toxic=0.91), THRESHOLDS)
    assert result.max_prob == pytest.approx(0.91)


def test_high_confidence_severe_label_blocks():
    result = decide(probs(threat=0.50 + BLOCK_MARGIN), THRESHOLDS)
    assert result.decision == "block"


def test_severe_label_just_over_the_threshold_reviews_rather_than_blocks():
    result = decide(probs(threat=0.51), THRESHOLDS)
    assert result.decision == "review"


def test_high_confidence_non_severe_label_reviews_rather_than_blocks():
    result = decide(probs(obscene=0.99), THRESHOLDS)
    assert result.decision == "review"


def test_near_threshold_reviews_even_without_a_flag():
    result = decide(probs(toxic=0.50 - REVIEW_MARGIN), THRESHOLDS)
    assert all(flag is False for flag in result.flags.values())
    assert result.decision == "review"


def test_clearly_benign_is_allowed():
    result = decide(probs(), THRESHOLDS)
    assert result.decision == "allow"
    assert result.max_prob == pytest.approx(0.01)


def test_flags_are_returned_in_label_order():
    assert list(decide(probs(), THRESHOLDS).flags) == list(LABELS)


def test_missing_label_is_a_hard_error():
    incomplete = {label: 0.1 for label in LABELS if label != "threat"}
    with pytest.raises(ValueError, match="threat"):
        decide(incomplete, THRESHOLDS)


def test_load_thresholds_accepts_a_complete_file(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps(THRESHOLDS), encoding="utf-8")
    assert load_thresholds(path) == THRESHOLDS


def test_load_thresholds_rejects_an_incomplete_file(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({"toxic": 0.5}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        load_thresholds(path)


def test_load_thresholds_rejects_an_out_of_range_value(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text(json.dumps({**THRESHOLDS, "threat": 1.4}), encoding="utf-8")
    with pytest.raises(ValueError, match="threat"):
        load_thresholds(path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.policy'`

- [ ] **Step 3: Write minimal implementation**

`backend/policy.py`:
```python
"""Moderation policy: calibrated probabilities plus tuned thresholds -> flags and a decision.

Toxicity is asymmetric-cost, so the thresholds are per label and tuned on validation only
(Phase 1). This module makes two guarantees the rest of the system relies on. Flags are
hierarchically coherent - severe_toxic implies toxic - and coherence is applied here, before
the response is built, so the contract validator is a backstop rather than the only control.
And `decide` is pure: same inputs, same outputs, no clock, no I/O.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from model.labels import LABELS

# Severe labels block outright, but only well clear of their threshold: a near-threshold
# severe score is exactly the case a human should see rather than one the machine should
# silently suppress.
BLOCK_LABELS: tuple[str, ...] = ("severe_toxic", "threat", "identity_hate")
BLOCK_MARGIN: float = 0.15
REVIEW_MARGIN: float = 0.10


@dataclass(frozen=True)
class DecisionResult:
    flags: dict[str, bool]
    decision: str
    max_prob: float


def decide(probs: dict[str, float], thresholds: dict[str, float]) -> DecisionResult:
    missing = [
        label for label in LABELS if label not in probs or label not in thresholds
    ]
    if missing:
        raise ValueError(f"probs/thresholds missing labels: {missing}")

    flags = {label: probs[label] >= thresholds[label] for label in LABELS}
    if flags["severe_toxic"]:
        flags["toxic"] = True

    max_prob = max(probs[label] for label in LABELS)

    if any(probs[label] >= thresholds[label] + BLOCK_MARGIN for label in BLOCK_LABELS):
        decision = "block"
    elif any(flags.values()):
        decision = "review"
    elif any(probs[label] >= thresholds[label] - REVIEW_MARGIN for label in LABELS):
        decision = "review"
    else:
        decision = "allow"

    return DecisionResult(flags=flags, decision=decision, max_prob=max_prob)


def load_thresholds(path: Path) -> dict[str, float]:
    """Load the Phase 1 `thresholds.json` artifact, rejecting anything unusable at startup."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [label for label in LABELS if label not in raw]
    if missing:
        raise ValueError(f"{path} is missing thresholds for: {missing}")
    thresholds: dict[str, float] = {}
    for label in LABELS:
        value = float(raw[label])
        if not 0.0 < value < 1.0:
            raise ValueError(f"{path}: threshold for {label} must be in (0, 1), got {value}")
        thresholds[label] = value
    return thresholds
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_policy.py -v`
Expected: 14 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/policy.py tests/unit/test_policy.py
git commit -m "Add moderation policy with hierarchically coherent flags"
```

---

### Task 9: Random-audit sampling with a recorded inclusion probability (H8)

**Files:**
- Create: `backend/audit.py`
- Test: `tests/unit/test_audit.py`

**Interfaces produced:** `FLAGGED_INCLUSION_PROBABILITY`, `should_random_audit`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_audit.py`:
```python
import random

import pytest

from backend.audit import FLAGGED_INCLUSION_PROBABILITY, should_random_audit


def test_flagged_rows_are_sampled_with_certainty():
    assert FLAGGED_INCLUSION_PROBABILITY == 1.0


def test_rate_zero_never_audits():
    rng = random.Random(0)
    assert not any(should_random_audit(0.0, rng) for _ in range(500))


def test_rate_one_always_audits():
    rng = random.Random(0)
    assert all(should_random_audit(1.0, rng) for _ in range(500))


def test_sampling_is_deterministic_for_a_seeded_generator():
    first = [should_random_audit(0.05, random.Random(7)) for _ in range(1)]
    second = [should_random_audit(0.05, random.Random(7)) for _ in range(1)]
    assert first == second


def test_observed_rate_tracks_the_requested_rate():
    rng = random.Random(1234)
    hits = sum(should_random_audit(0.05, rng) for _ in range(20000))
    assert 0.04 < hits / 20000 < 0.06


@pytest.mark.parametrize("rate", [-0.01, 1.01])
def test_rate_outside_zero_to_one_is_rejected(rate):
    with pytest.raises(ValueError, match="between 0 and 1"):
        should_random_audit(rate, random.Random(0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.audit'`

- [ ] **Step 3: Write minimal implementation**

`backend/audit.py`:
```python
"""Random-audit sampling for the review queue.

Live accuracy is the graded metric (rubric 3.2), and computing it over the model's own
flagged set is structurally blind to confidently-allowed false negatives - the costly missed
`threat`. A random-audit stratum fixes that only if the two strata are weighted, which
requires each row to carry the probability with which it was selected (premortem H8). The
sampler therefore returns a decision, and the caller writes the corresponding inclusion
probability onto the review row.

The generator is injected. Production uses `random.SystemRandom()`: with a public repository
and a seeded PRNG, an attacker could compute which requests will be audited and time
submissions to miss the sample.
"""

import random

FLAGGED_INCLUSION_PROBABILITY: float = 1.0


def should_random_audit(rate: float, rng: random.Random) -> bool:
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"random audit rate must be between 0 and 1 inclusive, got {rate}")
    return rng.random() < rate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_audit.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/audit.py tests/unit/test_audit.py
git commit -m "Add random-audit sampling with an explicit inclusion probability"
```

---

### Task 10: Database schema and bounded engine (rubric 2.2, H8, H24)

**Files:**
- Create: `backend/db.py`, `tests/integration/__init__.py`, `tests/integration/conftest.py`
- Test: `tests/integration/test_db_schema.py`

**Interfaces produced:** `Prediction`, `ReviewQueue`, `Feedback`, `PredictionRow`, `ReviewIntent`, `PendingWrite`, `make_engine`, `init_schema`, `insert_prediction`, `enqueue_review`, `fetch_pending_reviews`, `write_pending`

- [ ] **Step 1: Write the failing test**

`tests/integration/conftest.py`:
```python
"""Integration fixtures. These run against a REAL Postgres, never SQLite: the schema uses
JSONB and ON CONFLICT, and delivery spec section 3.3 makes a real dependency the phase gate.

TEST_DATABASE_URL wins when set (that is how CI's `services: postgres` is wired). Otherwise a
throwaway container is started, which is how it runs on the build box.
"""

import os

import pytest
from sqlalchemy.orm import sessionmaker

from backend.db import Base, init_schema


@pytest.fixture(scope="session")
def postgres_url():
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def engine(postgres_url):
    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, future=True)
    init_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
```

`tests/integration/test_db_schema.py`:
```python
import datetime as dt
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.db import (
    Feedback,
    PendingWrite,
    Prediction,
    PredictionRow,
    ReviewIntent,
    ReviewQueue,
    enqueue_review,
    fetch_pending_reviews,
    insert_prediction,
    write_pending,
)
from model.labels import LABELS

pytestmark = pytest.mark.integration


def make_row(request_id="r1", **overrides) -> PredictionRow:
    row = PredictionRow(
        request_id=request_id,
        input_text="you are an idiot",
        input_chars=16,
        model_version="toxic-clf:v3@sha256:" + "a" * 64,
        probs={label: 0.1 for label in LABELS},
        decision="review",
        max_prob=0.1,
        latency_ms=12,
        status="ok",
        persist_status="direct",
    )
    return replace(row, **overrides) if overrides else row


def test_prediction_round_trip_preserves_every_probability(session):
    probs = {label: round(0.1 * index, 3) for index, label in enumerate(LABELS, start=1)}
    insert_prediction(session, make_row(probs=probs))
    session.commit()
    stored = session.get(Prediction, "r1")
    assert stored.prob_toxic == pytest.approx(0.1)
    assert stored.prob_identity_hate == pytest.approx(0.6)
    assert stored.ts is not None


def test_insert_is_idempotent_on_request_id(session):
    """The spool drain is at-least-once by design: rows are committed before the spool is
    truncated, so a crash mid-drain must duplicate nothing."""
    insert_prediction(session, make_row())
    insert_prediction(session, make_row())
    session.commit()
    assert session.scalars(select(Prediction)).all().__len__() == 1


def test_explicit_timestamp_is_honoured(session):
    backdated = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
    insert_prediction(session, make_row(ts=backdated))
    session.commit()
    assert session.get(Prediction, "r1").ts == backdated


def test_review_row_records_its_inclusion_probability(session):
    """H8. Stratified collection without stratified estimation is still biased. The weight
    has to be stored at enqueue time; it cannot be reconstructed later, because the audit
    rate is a deploy-time setting that may change between rows."""
    insert_prediction(session, make_row())
    enqueue_review(
        session,
        ReviewIntent(
            request_id="r1",
            source="random-audit",
            inclusion_probability=0.05,
            input_text_snapshot="you are an idiot",
        ),
    )
    session.commit()
    stored = session.get(ReviewQueue, "r1")
    assert stored.source == "random-audit"
    assert stored.inclusion_probability == pytest.approx(0.05)
    assert stored.input_text_snapshot == "you are an idiot"
    assert stored.status == "pending"


def test_review_source_is_constrained_to_the_three_documented_values(session):
    insert_prediction(session, make_row())
    session.commit()
    with pytest.raises(IntegrityError, match="ck_review_source"):
        enqueue_review(
            session,
            ReviewIntent(
                request_id="r1",
                source="whatever",
                inclusion_probability=1.0,
                input_text_snapshot="x",
            ),
        )
        session.commit()


def test_feedback_source_is_constrained_to_reviewer_or_user(session):
    insert_prediction(session, make_row())
    session.add(Feedback(request_id="r1", source="reviewer", actor_id="rock", agree=True))
    session.commit()
    session.add(Feedback(request_id="r1", source="bot", actor_id="x"))
    with pytest.raises(IntegrityError, match="ck_feedback_source"):
        session.commit()


def test_write_pending_stamps_latency_after_the_insert(session):
    stamped = write_pending(
        session,
        PendingWrite(
            prediction=make_row(latency_ms=0),
            review=ReviewIntent(
                request_id="r1",
                source="flagged",
                inclusion_probability=1.0,
                input_text_snapshot="you are an idiot",
            ),
        ),
        stamp=lambda: 77,
    )
    session.commit()
    assert stamped == 77
    assert session.get(Prediction, "r1").latency_ms == 77
    assert session.get(ReviewQueue, "r1") is not None


def test_fetch_pending_reviews_returns_oldest_first(session):
    older = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 7, 22, tzinfo=dt.timezone.utc)
    for request_id, enqueued in (("r1", newer), ("r2", older)):
        insert_prediction(session, make_row(request_id=request_id))
        enqueue_review(
            session,
            ReviewIntent(
                request_id=request_id,
                source="flagged",
                inclusion_probability=1.0,
                input_text_snapshot="x",
                enqueued_ts=enqueued,
            ),
        )
    session.commit()
    assert [row.request_id for row in fetch_pending_reviews(session, limit=10)] == ["r2", "r1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_db_schema.py -v -m integration`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'backend.db'`

- [ ] **Step 3: Write minimal implementation**

`backend/db.py`:
```python
"""SQLAlchemy models and write paths for the three RDS tables.

Rubric 2.2 requires every prediction request, its output, and a timestamp to be logged, and
rubric 3.2's dashboard is built on these tables, so the schema carries the columns the
monitoring queries need rather than the columns the request happens to have. Three of them
exist because of specific premortem findings: `review_queue.source` and
`review_queue.inclusion_probability` (H8), `review_queue.input_text_snapshot` (the retention
purge nulls `predictions.input_text` and review must not depend on it), and
`predictions.status` / `persist_status` (H28 and H30 - failed and degraded requests write
rows so the latency tail is present in the series).
"""

import datetime as dt
from dataclasses import dataclass, replace

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from model.labels import LABELS

REVIEW_SOURCES = ("flagged", "random-audit", "user")
REVIEW_STATUSES = ("pending", "rescored", "reviewed", "expired")


class Base(DeclarativeBase):
    pass


class Prediction(Base):
    __tablename__ = "predictions"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    input_text: Mapped[str | None] = mapped_column(Text)
    input_chars: Mapped[int] = mapped_column(Integer)
    model_version: Mapped[str] = mapped_column(String(200))
    prob_toxic: Mapped[float | None] = mapped_column(Float)
    prob_severe_toxic: Mapped[float | None] = mapped_column(Float)
    prob_obscene: Mapped[float | None] = mapped_column(Float)
    prob_threat: Mapped[float | None] = mapped_column(Float)
    prob_insult: Mapped[float | None] = mapped_column(Float)
    prob_identity_hate: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str | None] = mapped_column(String(10))
    max_prob: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))
    persist_status: Mapped[str] = mapped_column(String(10))
    error_kind: Mapped[str | None] = mapped_column(String(60))
    client_fp: Mapped[str | None] = mapped_column(String(16))

    __table_args__ = (
        CheckConstraint("status in ('ok','error')", name="ck_predictions_status"),
        CheckConstraint(
            "persist_status in ('direct','spooled')", name="ck_predictions_persist_status"
        ),
        CheckConstraint("latency_ms >= 0", name="ck_predictions_latency_nonneg"),
        CheckConstraint(
            "decision is null or decision in ('allow','review','block')",
            name="ck_predictions_decision",
        ),
    )


class ReviewQueue(Base):
    __tablename__ = "review_queue"

    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("predictions.request_id"), primary_key=True
    )
    enqueued_ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source: Mapped[str] = mapped_column(String(16))
    inclusion_probability: Mapped[float] = mapped_column(Float)
    input_text_snapshot: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    distilbert_probs: Mapped[dict | None] = mapped_column(JSONB)
    reviewer_labels: Mapped[dict | None] = mapped_column(JSONB)
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    reviewed_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source in ('flagged','random-audit','user')", name="ck_review_source"
        ),
        CheckConstraint(
            "status in ('pending','rescored','reviewed','expired')", name="ck_review_status"
        ),
        CheckConstraint(
            "inclusion_probability > 0 and inclusion_probability <= 1",
            name="ck_review_inclusion_probability",
        ),
    )


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("predictions.request_id"), index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    source: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    agree: Mapped[bool | None] = mapped_column(Boolean)
    true_labels: Mapped[dict | None] = mapped_column(JSONB)
    model_labels: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint("source in ('reviewer','user')", name="ck_feedback_source"),
    )


@dataclass(frozen=True)
class PredictionRow:
    request_id: str
    input_text: str | None
    input_chars: int
    model_version: str
    probs: dict[str, float] | None
    decision: str | None
    max_prob: float | None
    latency_ms: int
    status: str
    persist_status: str
    error_kind: str | None = None
    client_fp: str | None = None
    ts: dt.datetime | None = None


@dataclass(frozen=True)
class ReviewIntent:
    request_id: str
    source: str
    inclusion_probability: float
    input_text_snapshot: str | None
    enqueued_ts: dt.datetime | None = None


@dataclass(frozen=True)
class PendingWrite:
    prediction: PredictionRow
    review: ReviewIntent | None = None


def make_engine(settings) -> Engine:
    """Bounded pool with a short checkout timeout.

    Under database pressure the endpoint must fail over to the spool in a couple of seconds
    rather than pile up connections until the instance runs out of workers. That is the
    difference between degraded and down (premortem H30).
    """
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"connect_timeout": max(1, int(settings.db_timeout_seconds))},
        future=True,
    )


def init_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def insert_prediction(session, row: PredictionRow) -> None:
    values = {
        "request_id": row.request_id,
        "input_text": row.input_text,
        "input_chars": row.input_chars,
        "model_version": row.model_version,
        "decision": row.decision,
        "max_prob": row.max_prob,
        "latency_ms": row.latency_ms,
        "status": row.status,
        "persist_status": row.persist_status,
        "error_kind": row.error_kind,
        "client_fp": row.client_fp,
    }
    if row.ts is not None:
        values["ts"] = row.ts
    probs = row.probs or {}
    for label in LABELS:
        values[f"prob_{label}"] = probs.get(label)
    session.execute(
        pg_insert(Prediction).values(**values).on_conflict_do_nothing(
            index_elements=["request_id"]
        )
    )


def enqueue_review(session, intent: ReviewIntent) -> None:
    values = {
        "request_id": intent.request_id,
        "source": intent.source,
        "inclusion_probability": intent.inclusion_probability,
        "input_text_snapshot": intent.input_text_snapshot,
        "status": "pending",
    }
    if intent.enqueued_ts is not None:
        values["enqueued_ts"] = intent.enqueued_ts
    session.execute(
        pg_insert(ReviewQueue).values(**values).on_conflict_do_nothing(
            index_elements=["request_id"]
        )
    )


def fetch_pending_reviews(session, limit: int) -> list[ReviewQueue]:
    return list(
        session.scalars(
            select(ReviewQueue)
            .where(ReviewQueue.status == "pending")
            .order_by(ReviewQueue.enqueued_ts)
            .limit(limit)
        )
    )


def write_pending(session, pending: PendingWrite, stamp) -> int:
    """Insert the prediction row and any review row, then stamp `latency_ms`.

    `stamp` is either an int (replay of an already-measured row) or a zero-argument callable
    evaluated AFTER the insert statements (the live path). Premortem H28: stamping before
    persistence omits the slowest component from the graded latency chart. What remains
    outside the measurement is the COMMIT round trip, which the caller measures separately
    and emits as `commit_ms` on the request log line.
    """
    insert_prediction(session, pending.prediction)
    if pending.review is not None:
        enqueue_review(session, pending.review)
    latency_ms = int(stamp()) if callable(stamp) else int(stamp)
    session.execute(
        update(Prediction)
        .where(Prediction.request_id == pending.prediction.request_id)
        .values(latency_ms=latency_ms)
    )
    return latency_ms


def with_persist_status(pending: PendingWrite, persist_status: str) -> PendingWrite:
    return PendingWrite(
        prediction=replace(pending.prediction, persist_status=persist_status),
        review=pending.review,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_db_schema.py -v -m integration`
Expected: 8 PASS. First run pulls `postgres:16-alpine` (about 15 s on the build box); subsequent runs reuse the layer.

- [ ] **Step 5: Commit**

```bash
git add backend/db.py tests/integration/__init__.py tests/integration/conftest.py tests/integration/test_db_schema.py
git commit -m "Add prediction, review queue, and feedback tables with a bounded engine"
```

---

### Task 10a: One schema, not two — reconcile the ORM with the shape Phase 3 consumes [gap `IFACE-DB-SCHEMA`, H8, H9]

Task 10 above and Phase 3 Task 1 describe **the same three tables** with divergent, mutually incompatible DDL, and no task in any plan reconciles them. Phase 3's "Interface Contract Corrections" table already declares Phase 3's shape authoritative, so this phase moves. Four concrete breakages, each of which is a runtime failure on the real database rather than a style disagreement:

| # | Task 10 as written | Phase 3 as written | Failure on the deployed stack |
|---|---|---|---|
| a | `review_queue.inclusion_probability: Mapped[float]`, NOT NULL, `CHECK (> 0 and <= 1)` | adds a separate nullable `sample_rate DOUBLE PRECISION`; `admit_review` and `scripts/seed_demo.replay` write `sample_rate` and never mention `inclusion_probability` | every enqueue raises `NotNullViolation` — a NOT NULL column with no server default that nothing writes |
| b | `ck_review_source` allows `('flagged','random-audit','user')` and is never dropped | writes `source='user-report'` on the user-disagreement referral path, which **is** the H9 remedy | the surviving Phase 2 constraint rejects the H9 remedy's only write |
| c | `feedback(actor_id, agree, true_labels, model_labels)` | `feedback(reviewer_id, agreement jsonb, exact_match bool)` | two column sets for one table: the dashboard reads columns nothing writes |
| d | produces `init_schema` | `tests/integration/conftest.py` does `from backend.db import init_db`, and Phase 3's Interfaces-Consumed table names `backend.db.init_db(engine)` | Phase 3's entire integration suite fails at **collection** with `ImportError` |

The estimator's semantics are why (a) and (b) must go Phase 3's way rather than the reverse. Horvitz-Thompson weights by `1/π`. A `user-report` row has **no known inclusion probability** — it arrived because a user objected, not because a design sampled it — so its rate must be `NULL` and the estimator must skip it until a human reviews it under a known design. A NOT NULL `CHECK (> 0 and <= 1)` column cannot express that, which is why the two designs cannot both be right.

**Files:**
- Modify: `backend/db.py`, and every reference to `inclusion_probability` produced by Task 10
- Test: `tests/integration/test_db_schema.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_db_schema.py`:
```python
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import IntegrityError

from model.labels import LABELS


def test_the_review_queue_sampling_column_has_exactly_one_name(session):
    """Phase 3's admit_review and seed_demo write sample_rate. A surviving NOT NULL
    inclusion_probability makes every enqueue a NotNullViolation on the real database."""
    cols = {c.name for c in ReviewQueue.__table__.columns}
    assert "sample_rate" in cols
    assert "inclusion_probability" not in cols


def test_a_user_report_row_may_carry_a_null_sample_rate(session):
    """H9's referral path. Horvitz-Thompson must ignore rows of unknown inclusion."""
    session.add(Prediction(request_id="r1", input_text="x", model_version="m",
                           decision="allow", max_prob=0.1, latency_ms=5,
                           persist_status="direct", **{f"prob_{l}": 0.1 for l in LABELS}))
    session.add(ReviewQueue(request_id="r1", source="user-report", sample_rate=None,
                            input_text_snapshot="x"))
    session.commit()


def test_a_design_stratum_row_cannot_omit_its_sample_rate(session):
    with pytest.raises(IntegrityError):
        session.add(ReviewQueue(request_id="r2", source="flagged", sample_rate=None))
        session.commit()
    session.rollback()


def test_the_review_source_vocabulary_admits_user_report(session):
    checks = [c for c in ReviewQueue.__table__.constraints
              if isinstance(c, CheckConstraint) and "source" in str(c.sqltext)]
    assert any("user-report" in str(c.sqltext) for c in checks), (
        "the H9 user-referral path writes source='user-report' and this constraint rejects it"
    )


def test_the_feedback_table_matches_the_phase3_contract(session):
    cols = {c.name for c in Feedback.__table__.columns}
    assert {"request_id", "ts", "source", "reviewer_id", "agreement", "exact_match"} <= cols
    assert not ({"actor_id", "agree", "true_labels", "model_labels"} & cols), (
        "two column sets for one table is how the dashboard reads a column nothing writes"
    )


def test_the_schema_entry_point_has_the_name_phase_3_imports():
    from backend import db
    assert hasattr(db, "init_db"), "Phase 3's conftest does `from backend.db import init_db`"


def test_no_module_in_the_repo_still_says_inclusion_probability():
    """A rename that misses one call site is a NotNullViolation on day 13, not a lint nit."""
    import pathlib
    offenders = [
        str(p) for p in pathlib.Path(".").rglob("*.py")
        if ".venv" not in str(p) and "inclusion_probability" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_db_schema.py -v -m integration`
Expected: FAIL — `assert 'inclusion_probability' not in {...}`, `IntegrityError: ck_review_source` on the `user-report` insert, `AssertionError` on the feedback column set, and `AssertionError: Phase 3's conftest does 'from backend.db import init_db'`.

- [ ] **Step 3: Write minimal implementation**

Four changes in `backend/db.py`, and one rename across the phase.

1. **Rename the column.** `inclusion_probability` becomes `sample_rate`, nullable, and the CHECK becomes the conditional one Phase 3 needs:
```python
class ReviewQueue(Base):
    ...
    source: Mapped[str] = mapped_column(String(16))
    sample_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_text_snapshot: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "source in ('flagged','random-audit','user-report')", name="ck_review_source"
        ),
        # A design stratum must carry the π it was drawn with; a user report has no known
        # inclusion probability and must carry NULL, so the estimator skips it until a human
        # reviews it under a known design (premortem H8, H9).
        CheckConstraint(
            "(source in ('flagged','random-audit')"
            " AND sample_rate IS NOT NULL AND sample_rate > 0 AND sample_rate <= 1)"
            " OR (source = 'user-report' AND sample_rate IS NULL)",
            name="review_queue_sample_rate_ck",
        ),
    )
```
   `ReviewIntent.inclusion_probability` becomes `ReviewIntent.sample_rate: float | None`, and `enqueue_review` writes `"sample_rate": intent.sample_rate`.

2. **The vocabulary.** `'user'` is gone from the source vocabulary; it was never written by anything. `'user-report'` replaces it. Note the two are different concepts and both survive: `feedback.source` takes `('user','reviewer')`, `review_queue.source` takes `('flagged','random-audit','user-report')`.

3. **The feedback table** takes Phase 3's columns:
```python
class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source: Mapped[str] = mapped_column(String(16))
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    agreement: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    exact_match: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))

    __table_args__ = (
        CheckConstraint("source in ('user','reviewer')", name="ck_feedback_source"),
        CheckConstraint(
            "source <> 'reviewer' OR agreement <> '{}'::jsonb",
            name="feedback_reviewer_agreement_ck",
        ),
    )
```

4. **The entry point.** Rename `init_schema` to `init_db` and keep a one-line alias so no already-written call site in this phase breaks:
```python
def init_db(engine: Engine) -> None:
    """Idempotent create of the three tables. Named for the seam Phase 3 imports."""
    Base.metadata.create_all(engine)


init_schema = init_db  # historical name used inside Phase 2; both are the same function
```

**Rename discipline for the rest of this phase.** After this task, every occurrence of `inclusion_probability` in Task 10's own tests, Task 12's persistence path, Task 15's `/predict` tests, and Task 19's purge tests means `sample_rate`, and `FLAGGED_INCLUSION_PROBABILITY` becomes `FLAGGED_SAMPLE_RATE`. `test_no_module_in_the_repo_still_says_inclusion_probability` is what makes that mechanical rather than a matter of memory. Task 9's interface produces `sample_rate` too.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration -v -m integration`
Expected: the appended 7 PASS, and Task 10's own suite green after the rename.

- [ ] **Step 5: Commit**

```bash
git add backend/db.py tests/integration/test_db_schema.py
git commit -m "Reconcile the ORM with the schema Phase 3 consumes: sample_rate, user-report, init_db"
```

**Amendment to Phase 3 Task 1.** Add a step: `- [ ] Run this phase's schema tests and Phase 3's migration against ONE database and assert the resulting column set is identical to backend/schema_phase3.py's expectation, so a future divergence fails on day 7 rather than on day 13.` Phase 3's `apply_phase3_schema` remains an idempotent no-op-safe `ALTER` set; after this task most of its `ADD COLUMN IF NOT EXISTS` statements find the column already present, which is the intended behaviour and is what its `test_migration_is_idempotent` already asserts.

**Amendment to the Interface Contracts block (Phase 4 Task 11, Edit 4).** `# backend/db.py` gains `def init_db(session_or_engine) -> None: ...` and the `enqueue_review` comment names `sample_rate`.

---

### Task 11: Bounded durable spool (H30)

**Files:**
- Create: `backend/spool.py`
- Test: `tests/unit/test_spool.py`

**Interfaces produced:** `Spool`, `SpoolFull`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_spool.py`:
```python
import datetime as dt

import pytest

from backend.db import PendingWrite, PredictionRow, ReviewIntent
from backend.spool import Spool, SpoolFull
from model.labels import LABELS


def pending(request_id="r1") -> PendingWrite:
    return PendingWrite(
        prediction=PredictionRow(
            request_id=request_id,
            input_text="you are an idiot",
            input_chars=16,
            model_version="toxic-clf:v3@sha256:" + "a" * 64,
            probs={label: 0.25 for label in LABELS},
            decision="review",
            max_prob=0.25,
            latency_ms=31,
            status="ok",
            persist_status="spooled",
            ts=dt.datetime(2026, 8, 4, 9, 30, tzinfo=dt.timezone.utc),
        ),
        review=ReviewIntent(
            request_id=request_id,
            source="flagged",
            inclusion_probability=1.0,
            input_text_snapshot="you are an idiot",
        ),
    )


def test_append_then_read_round_trips_every_field(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending())
    restored = spool.read_all()
    assert len(restored) == 1
    assert restored[0] == pending()


def test_depth_tracks_appends_and_survives_a_restart(tmp_path):
    path = tmp_path / "s.jsonl"
    spool = Spool(path, max_rows=10)
    spool.append(pending("r1"))
    spool.append(pending("r2"))
    assert spool.depth() == 2
    assert Spool(path, max_rows=10).depth() == 2


def test_spool_refuses_to_grow_past_its_bound(tmp_path):
    """H30. The bound is what keeps the degraded path from becoming an unbounded disk write
    primitive, and it is deliberately large enough that reaching it costs an attacker
    SPOOL_MAX_ROWS successful requests through the rate limiter."""
    spool = Spool(tmp_path / "s.jsonl", max_rows=2)
    spool.append(pending("r1"))
    spool.append(pending("r2"))
    with pytest.raises(SpoolFull, match="2 rows"):
        spool.append(pending("r3"))
    assert spool.depth() == 2


def test_truncate_empties_the_spool(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending())
    spool.truncate()
    assert spool.depth() == 0
    assert spool.read_all() == []


def test_a_row_without_a_review_round_trips(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    entry = PendingWrite(prediction=pending().prediction, review=None)
    spool.append(entry)
    assert spool.read_all() == [entry]


def test_a_corrupt_line_does_not_lose_the_rest(tmp_path):
    path = tmp_path / "s.jsonl"
    spool = Spool(path, max_rows=10)
    spool.append(pending("r1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ this is not json\n")
    spool.append(pending("r2"))
    restored = spool.read_all()
    assert [entry.prediction.request_id for entry in restored] == ["r1", "r2"]


def test_the_directory_is_created_on_demand(tmp_path):
    spool = Spool(tmp_path / "nested" / "dir" / "s.jsonl", max_rows=10)
    spool.append(pending())
    assert spool.depth() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_spool.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.spool'`

- [ ] **Step 3: Write minimal implementation**

`backend/spool.py`:
```python
"""Bounded durable spool for prediction rows that could not reach Postgres.

Premortem H30. Delivery spec section 10 returned 503 whenever a prediction could not be
persisted, and accepted that the moderation endpoint is unavailable while the database is.
On a db.t4g.micro with no rate limit that is an off switch an attacker operates: modest
concurrent traffic exhausts connections and moderation is down for as long as the pressure
lasts. Rubric 2.2 requires complete logging, which is a durability requirement, not an
availability trade.

So the failure path is durable and bounded. Rows land in an fsync'd append-only file on the
instance volume and the drainer replays them with persist_status='spooled'. The bound moved
from database connections, which the attacker controls, to local disk, which the operator
controls; and reaching the bound costs SPOOL_MAX_ROWS successful requests through the rate
limiter rather than a handful of concurrent connections.

Durability caveat, stated rather than assumed: fsync on the file guarantees the row survives
a process crash and an instance reboot. It does not survive destruction of the EBS volume,
which is what `terraform destroy` does. A drain therefore runs before teardown.
"""

import datetime as dt
import json
import os
from dataclasses import asdict
from pathlib import Path

from backend.db import PendingWrite, PredictionRow, ReviewIntent


class SpoolFull(RuntimeError):
    """The spool reached its bound. The caller fails closed."""


def _encode(pending: PendingWrite) -> str:
    prediction = asdict(pending.prediction)
    prediction["ts"] = (
        pending.prediction.ts or dt.datetime.now(dt.timezone.utc)
    ).isoformat()
    review = None
    if pending.review is not None:
        review = asdict(pending.review)
        review["enqueued_ts"] = (
            pending.review.enqueued_ts.isoformat()
            if pending.review.enqueued_ts is not None
            else None
        )
    return json.dumps({"prediction": prediction, "review": review}, sort_keys=True)


def _decode(line: str) -> PendingWrite:
    payload = json.loads(line)
    prediction = payload["prediction"]
    prediction["ts"] = dt.datetime.fromisoformat(prediction["ts"])
    review = payload.get("review")
    if review is not None:
        raw = review.get("enqueued_ts")
        review["enqueued_ts"] = dt.datetime.fromisoformat(raw) if raw else None
        review = ReviewIntent(**review)
    return PendingWrite(prediction=PredictionRow(**prediction), review=review)


class Spool:
    def __init__(self, path: Path, max_rows: int) -> None:
        self.path = Path(path)
        self.max_rows = int(max_rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._count = sum(1 for line in self._lines())

    def _lines(self) -> list[str]:
        with self.path.open("r", encoding="utf-8") as handle:
            return [line for line in handle.read().splitlines() if line.strip()]

    def depth(self) -> int:
        return self._count

    def append(self, pending: PendingWrite) -> None:
        if self._count >= self.max_rows:
            raise SpoolFull(f"spool already holds {self.max_rows} rows; refusing more")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(_encode(pending) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._count += 1

    def read_all(self) -> list[PendingWrite]:
        restored: list[PendingWrite] = []
        for line in self._lines():
            try:
                restored.append(_decode(line))
            except (ValueError, TypeError, KeyError):
                # A partially written tail line after a hard kill. Losing one row is
                # preferable to refusing to drain the rest.
                continue
        return restored

    def truncate(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self._count = 0
```

Note: `test_a_corrupt_line_does_not_lose_the_rest` also proves `depth()` counts lines rather than decodable rows, which is the conservative direction — a corrupt line consumes budget.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_spool.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/spool.py tests/unit/test_spool.py
git commit -m "Add bounded durable spool for degraded prediction logging"
```

---

### Task 12: Persistence path — direct, spooled, fail closed (H30, H28)

**Files:**
- Create: `backend/persistence.py`
- Test: `tests/unit/test_persistence.py`, `tests/integration/test_persistence_drain.py`

**Interfaces produced:** `PersistResult`, `persist_prediction`, `drain_spool`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_persistence.py`:
```python
import time
from contextlib import contextmanager

import pytest
from sqlalchemy.exc import OperationalError

from backend.persistence import persist_prediction
from backend.spool import Spool, SpoolFull
from tests.unit.test_spool import pending


class FakeSession:
    def __init__(self, fail: bool, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.written = []

    def execute(self, statement):
        if self.fail:
            raise OperationalError("insert", {}, Exception("connection refused"))
        time.sleep(self.delay)
        self.written.append(statement)

    def commit(self):
        if self.fail:
            raise OperationalError("commit", {}, Exception("connection refused"))


def factory_for(session):
    @contextmanager
    def factory():
        yield session

    return factory


def test_healthy_database_takes_the_direct_path(tmp_path):
    session = FakeSession(fail=False)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.persist_status == "direct"
    assert spool.depth() == 0


def test_latency_includes_the_persistence_component(tmp_path):
    """H28. Stamping latency before persistence omits the slowest component from the graded
    chart. A 50 ms insert must show up in the stamped value."""
    session = FakeSession(fail=False, delay=0.05)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.latency_ms >= 50


def test_unreachable_database_spools_instead_of_failing(tmp_path):
    """H30. This is the test that fails under the original 'return 503' design."""
    session = FakeSession(fail=True)
    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(factory_for(session), spool, pending(), t0=time.perf_counter())
    assert result.persist_status == "spooled"
    assert result.error == "OperationalError"
    assert spool.depth() == 1
    assert spool.read_all()[0].prediction.persist_status == "spooled"


def test_the_direct_path_is_retried_once_before_spooling(tmp_path):
    attempts = {"count": 0}

    @contextmanager
    def flaky():
        attempts["count"] += 1
        yield FakeSession(fail=attempts["count"] == 1)

    spool = Spool(tmp_path / "s.jsonl", max_rows=5)
    result = persist_prediction(flaky, spool, pending(), t0=time.perf_counter())
    assert attempts["count"] == 2
    assert result.persist_status == "direct"
    assert spool.depth() == 0


def test_a_full_spool_fails_closed(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=1)
    spool.append(pending("r0"))
    with pytest.raises(SpoolFull):
        persist_prediction(
            factory_for(FakeSession(fail=True)), spool, pending(), t0=time.perf_counter()
        )
```

`tests/integration/test_persistence_drain.py`:
```python
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.db import Prediction, ReviewQueue
from backend.persistence import drain_spool, persist_prediction
from backend.spool import Spool
from tests.unit.test_persistence import FakeSession, factory_for
from tests.unit.test_spool import pending

pytestmark = pytest.mark.integration


def test_spooled_rows_reach_postgres_when_it_recovers(engine, session, tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    persist_prediction(
        factory_for(FakeSession(fail=True)), spool, pending("r1"), t0=time.perf_counter()
    )
    assert spool.depth() == 1

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert drain_spool(factory, spool) == 1
    assert spool.depth() == 0

    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].persist_status == "spooled"
    assert stored[0].latency_ms == 31          # the value measured at request time, not now
    assert session.get(ReviewQueue, "r1") is not None


def test_draining_twice_writes_one_row(engine, session, tmp_path):
    spool = Spool(tmp_path / "s.jsonl", max_rows=10)
    spool.append(pending("r1"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    drain_spool(factory, spool)
    spool.append(pending("r1"))
    drain_spool(factory, spool)
    assert len(session.scalars(select(Prediction)).all()) == 1


def test_draining_an_empty_spool_is_a_no_op(engine, tmp_path):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    assert drain_spool(factory, Spool(tmp_path / "s.jsonl", max_rows=10)) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_persistence.py tests/integration/test_persistence_drain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.persistence'`

- [ ] **Step 3: Write minimal implementation**

`backend/persistence.py`:
```python
"""The /predict persistence path. One module decides direct, spooled, or fail closed.

Ordering matters and is deliberate (premortem H30):

  direct   insert with a bounded checkout and one retry            -> HTTP 200
  spooled  fsync'd local row, replayed when Postgres recovers      -> HTTP 200
  full     the spool reached its bound                             -> HTTP 503

Only the third path returns 503, and reaching it costs an attacker SPOOL_MAX_ROWS successful
requests through the rate limiter.
"""

import time
from dataclasses import dataclass, replace

from sqlalchemy.exc import SQLAlchemyError

from backend.db import PendingWrite, with_persist_status, write_pending
from backend.spool import Spool


@dataclass(frozen=True)
class PersistResult:
    persist_status: str
    latency_ms: int
    error: str | None = None
    commit_ms: float = 0.0


def persist_prediction(
    session_factory,
    spool: Spool,
    pending: PendingWrite,
    t0: float,
    retries: int = 1,
) -> PersistResult:
    """Persist one prediction. Raises SpoolFull only when the degraded path is exhausted."""
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            with session_factory() as session:
                latency_ms = write_pending(
                    session,
                    pending,
                    stamp=lambda: int(round((time.perf_counter() - t0) * 1000)),
                )
                commit_started = time.perf_counter()
                session.commit()
                commit_ms = (time.perf_counter() - commit_started) * 1000
            return PersistResult(
                persist_status="direct", latency_ms=latency_ms, commit_ms=commit_ms
            )
        except SQLAlchemyError as exc:
            last = exc

    latency_ms = int(round((time.perf_counter() - t0) * 1000))
    degraded = with_persist_status(pending, "spooled")
    spool.append(
        PendingWrite(
            prediction=replace(degraded.prediction, latency_ms=latency_ms),
            review=degraded.review,
        )
    )
    return PersistResult(
        persist_status="spooled",
        latency_ms=latency_ms,
        error=type(last).__name__ if last else None,
    )


def drain_spool(session_factory, spool: Spool) -> int:
    """Replay spooled rows into Postgres. At-least-once by construction.

    Rows are committed BEFORE the spool is truncated, so a crash between the two duplicates
    rather than loses - and `insert_prediction` is idempotent on `request_id`, so a duplicate
    is a no-op. The stored `latency_ms` is the value measured at request time, never the
    drain time, or the graded latency series would be corrupted by an unrelated outage.
    """
    entries = spool.read_all()
    if not entries:
        return 0
    with session_factory() as session:
        for entry in entries:
            write_pending(session, entry, stamp=entry.prediction.latency_ms)
        session.commit()
    spool.truncate()
    return len(entries)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_persistence.py tests/integration/test_persistence_drain.py -v`
Expected: 8 PASS (5 unit, 3 integration)

- [ ] **Step 5: Commit**

```bash
git add backend/persistence.py tests/unit/test_persistence.py tests/integration/test_persistence_drain.py
git commit -m "Add durable persistence path with spool fallback and idempotent drain"
```

---

### Task 13: Rate limiter (REG-6.3b)

**Files:**
- Create: `backend/ratelimit.py`
- Test: `tests/unit/test_ratelimit.py`

**Interfaces produced:** `RateLimiter`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ratelimit.py`:
```python
import pytest

from backend.ratelimit import MAX_TRACKED_KEYS, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_burst_is_allowed_then_the_bucket_is_empty():
    """REG-6.3b. A public /predict on a public repo with no rate limit is free denial-of-
    service capacity, and delivery spec section 13 names this control as one of three that
    make the public-registry decision defensible."""
    limiter = RateLimiter(per_minute=60, burst=5, clock=FakeClock())
    assert [limiter.allow("k") for _ in range(5)] == [True] * 5
    assert limiter.allow("k") is False


def test_tokens_refill_at_the_configured_rate():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=1, clock=clock)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    clock.advance(1.0)                      # 60/minute == one token per second
    assert limiter.allow("k") is True


def test_refill_is_capped_at_the_burst():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=3, clock=clock)
    clock.advance(3600.0)
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]


def test_keys_are_isolated():
    limiter = RateLimiter(per_minute=60, burst=1, clock=FakeClock())
    assert limiter.allow("first") is True
    assert limiter.allow("first") is False
    assert limiter.allow("second") is True


def test_the_key_table_is_bounded():
    """The limiter is itself a memory-growth primitive if it tracks unbounded keys."""
    limiter = RateLimiter(per_minute=60, burst=1, clock=FakeClock())
    for index in range(MAX_TRACKED_KEYS + 500):
        limiter.allow(f"key-{index}")
    assert len(limiter._buckets) <= MAX_TRACKED_KEYS


@pytest.mark.parametrize("kwargs", [{"per_minute": 0, "burst": 1}, {"per_minute": 5, "burst": 0}])
def test_nonsense_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(**kwargs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ratelimit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.ratelimit'`

- [ ] **Step 3: Write minimal implementation**

`backend/ratelimit.py`:
```python
"""In-process token bucket.

Deliberately not Redis and not a database table. The control protects the request path, so
adding a network dependency to it would make the endpoint fail exactly when the thing it
defends against is happening. One backend instance serves /predict (delivery spec section 4),
so per-process state is per-service state.

The clock is injected so the tests are deterministic rather than slept-through.
"""

import threading
import time
from dataclasses import dataclass

MAX_TRACKED_KEYS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int, burst: int, clock=time.monotonic) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must both be positive")
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._buckets) >= MAX_TRACKED_KEYS and key not in self._buckets:
                self._evict(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, updated=now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                self.burst, bucket.tokens + (now - bucket.updated) * self.rate
            )
            bucket.updated = now
            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True

    def _evict(self, now: float) -> None:
        """Drop the least recently seen half. Full buckets are indistinguishable from absent
        ones, so evicting a full bucket grants nothing an attacker did not already have."""
        ordered = sorted(self._buckets.items(), key=lambda item: item[1].updated)
        for key, _ in ordered[: len(ordered) // 2]:
            del self._buckets[key]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ratelimit.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/ratelimit.py tests/unit/test_ratelimit.py
git commit -m "Add bounded in-process token bucket rate limiter"
```

---

### Task 14: Demo API key authentication (REG-6.3c)

**Files:**
- Create: `backend/auth.py`
- Test: `tests/unit/test_auth.py`

**Interfaces produced:** `API_KEY_HEADER`, `check_api_key`, `client_fingerprint`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_auth.py`:
```python
import re

from backend.auth import API_KEY_HEADER, check_api_key, client_fingerprint


def test_header_name_is_the_documented_one():
    assert API_KEY_HEADER == "X-API-Key"


def test_the_correct_key_is_accepted():
    assert check_api_key("s3cret-demo-key", "s3cret-demo-key") is True


def test_a_wrong_key_is_rejected():
    assert check_api_key("wrong", "s3cret-demo-key") is False


def test_a_missing_key_is_rejected():
    assert check_api_key(None, "s3cret-demo-key") is False
    assert check_api_key("", "s3cret-demo-key") is False


def test_a_prefix_of_the_key_is_rejected():
    assert check_api_key("s3cret", "s3cret-demo-key") is False


def test_comparison_is_constant_time():
    """A byte-by-byte `==` on a secret is a timing oracle. This asserts the implementation
    uses hmac.compare_digest rather than trying to measure nanoseconds in CI."""
    source = __import__("inspect").getsource(check_api_key)
    assert "compare_digest" in source
    assert re.search(r"presented\s*==\s*expected", source) is None


def test_fingerprint_is_stable_short_and_not_the_key():
    fingerprint = client_fingerprint("s3cret-demo-key")
    assert fingerprint == client_fingerprint("s3cret-demo-key")
    assert len(fingerprint) == 16
    assert "s3cret" not in fingerprint
    assert client_fingerprint("another-key") != fingerprint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.auth'`

- [ ] **Step 3: Write minimal implementation**

`backend/auth.py`:
```python
"""Demo API key check for /predict.

This is not an identity system and the model card says so. It is the control that stops a
public moderation endpoint on a public repository from being free denial-of-service capacity,
and it is one of the three compensating controls delivery spec section 13 names for the
decision to make the W&B registry publicly visible.

Operational note that belongs with the code: the key is NOT published in the repository. The
README shows `curl -H "X-API-Key: $DEMO_API_KEY" ...` and the value travels in the Canvas
submission text entry, which is not public. It is rotated after grading. /health carries no
key requirement, so the grader, the deploy gate, and the container HEALTHCHECK all work.
"""

import hashlib
import hmac

API_KEY_HEADER = "X-API-Key"


def check_api_key(presented: str | None, expected: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def client_fingerprint(api_key: str) -> str:
    """Stable per-key identifier for rate limiting and abuse forensics.

    The fingerprint, never the key, is what reaches the rate limiter, the log line, and
    `predictions.client_fp`, so a leaked log or a screenshot cannot replay traffic.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_auth.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/auth.py tests/unit/test_auth.py
git commit -m "Add constant-time demo API key check and client fingerprint"
```

---

### Task 15: `/predict` — contract, persistence, enqueue (rubric 2.1, 2.2; H14, H23, H28)

**Files:**
- Create: `backend/app.py`, `tests/integration/test_predict_api.py`
- Amend: `tests/integration/conftest.py` (add the app fixtures)
- Test: `tests/integration/test_predict_api.py`

**Interfaces produced:** `create_app`, `POST /predict`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/conftest.py`:
```python
import json

import pytest
from fastapi.testclient import TestClient

from backend.config import load_settings
from model.labels import LABELS
from tests.fixtures.make_model import build_demo_artifact

DEMO_KEY = "test-demo-key"
AUTH = {"X-API-Key": DEMO_KEY}


@pytest.fixture(scope="session")
def artifact_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("artifacts")
    path, digest = build_demo_artifact(directory / "toxic-clf.skops")
    card = directory / "MODEL_CARD.md"
    card.write_text(f"- MODEL_DIGEST: sha256:{digest}\n", encoding="utf-8")
    thresholds = directory / "thresholds.json"
    thresholds.write_text(json.dumps({label: 0.5 for label in LABELS}), encoding="utf-8")
    return {"artifact": path, "digest": digest, "card": card, "thresholds": thresholds}


@pytest.fixture()
def app_settings(artifact_bundle, postgres_url, tmp_path):
    return load_settings(
        {
            "DATABASE_URL": postgres_url,
            "DEMO_API_KEY": DEMO_KEY,
            "MODEL_ARTIFACT_PATH": str(artifact_bundle["artifact"]),
            "MODEL_CARD_PATH": str(artifact_bundle["card"]),
            "MODEL_DIGEST": artifact_bundle["digest"],
            "MODEL_REGISTRY_VERSION": "3",
            "THRESHOLDS_PATH": str(artifact_bundle["thresholds"]),
            "SPOOL_PATH": str(tmp_path / "spool.jsonl"),
            "RATE_LIMIT_PER_MINUTE": "600",
            "RATE_LIMIT_BURST": "300",
            "RANDOM_AUDIT_RATE": "0.0",
        }
    )


@pytest.fixture()
def client(app_settings, session):
    from backend.app import create_app

    with TestClient(create_app(app_settings)) as client:
        yield client
```

`tests/integration/test_predict_api.py`:
```python
import re

import pytest
from sqlalchemy import select

from backend.db import Prediction, ReviewQueue
from model.labels import LABELS
from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration

HEX64 = re.compile(r"[0-9a-f]{64}")


def test_predict_returns_a_contract_valid_response(client):
    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "request_id",
        "model_version",
        "labels",
        "decision",
        "max_prob",
        "latency_ms",
    }
    assert set(body["labels"]) == set(LABELS)
    assert body["decision"] in {"allow", "review", "block"}
    assert 0.0 <= body["max_prob"] <= 1.0
    assert body["latency_ms"] >= 0
    for score in body["labels"].values():
        assert 0.0 <= score["prob"] <= 1.0
        assert isinstance(score["flag"], bool)


def test_every_prediction_writes_exactly_one_row(client, session):
    """Rubric 2.2: the service must log every prediction request, its output, and a
    timestamp."""
    response = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH)
    request_id = response.json()["request_id"]
    stored = session.get(Prediction, request_id)
    assert stored is not None
    assert stored.ts is not None
    assert stored.input_text == "have a nice day friend"
    assert stored.input_chars == len("have a nice day friend")
    assert stored.status == "ok"
    assert stored.persist_status == "direct"
    assert stored.prob_toxic is not None
    assert stored.latency_ms == response.json()["latency_ms"]


def test_the_database_row_carries_the_full_version_and_the_response_does_not(client, session):
    """H14. The digest belongs in the row and the log, where it supports incident response,
    and nowhere a client can read it."""
    body = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH).json()
    stored = session.get(Prediction, body["request_id"])
    assert stored.model_version.startswith("toxic-clf:v3@sha256:")
    assert body["model_version"] == "toxic-clf:v3"


def test_no_response_ever_carries_the_artifact_digest(client, artifact_bundle):
    digest = artifact_bundle["digest"]
    responses = [
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH),
        client.get("/health"),
    ]
    for response in responses:
        assert digest not in response.text
        assert not HEX64.search(response.text)


def test_a_reviewable_prediction_enqueues_a_flagged_review_row(client, session, monkeypatch):
    monkeypatch.setattr(
        "backend.app.decide",
        lambda probs, thresholds: __import__("backend.policy", fromlist=["DecisionResult"])
        .DecisionResult(
            flags={label: label == "toxic" for label in LABELS},
            decision="review",
            max_prob=0.8,
        ),
    )
    body = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH).json()
    assert body["decision"] == "review"
    queued = session.get(ReviewQueue, body["request_id"])
    assert queued.source == "flagged"
    assert queued.inclusion_probability == 1.0
    assert queued.input_text_snapshot == "you are an idiot"
    assert queued.status == "pending"


def test_an_allowed_prediction_does_not_enqueue_when_the_audit_rate_is_zero(client, session):
    body = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH).json()
    if body["decision"] == "allow":
        assert session.get(ReviewQueue, body["request_id"]) is None


def test_random_audit_enqueues_with_its_inclusion_probability(client, session, monkeypatch):
    """H8. The weight has to be on the row, or Phase 3 cannot correct the pooled estimate."""
    from dataclasses import replace

    monkeypatch.setattr("backend.app.should_random_audit", lambda rate, rng: True)
    client.app.state.settings = replace(client.app.state.settings, random_audit_rate=0.05)
    body = client.post("/predict", json={"text": "have a nice day friend"}, headers=AUTH).json()
    queued = session.get(ReviewQueue, body["request_id"])
    assert queued.source == "random-audit"
    assert queued.inclusion_probability == pytest.approx(0.05)


def test_request_ids_are_unique_per_request(client):
    seen = {
        client.post("/predict", json={"text": f"comment {index}"}, headers=AUTH).json()[
            "request_id"
        ]
        for index in range(20)
    }
    assert len(seen) == 20


def test_severe_toxic_never_appears_without_toxic(client, monkeypatch):
    """H22, end to end. The policy enforces coherence; this asserts nothing downstream
    reintroduces the incoherent pair."""
    monkeypatch.setattr(
        "backend.app.probs_to_dict",
        lambda row: {
            "toxic": 0.02,
            "severe_toxic": 0.95,
            "obscene": 0.01,
            "threat": 0.01,
            "insult": 0.01,
            "identity_hate": 0.01,
        },
    )
    body = client.post("/predict", json={"text": "anything"}, headers=AUTH).json()
    assert body["labels"]["severe_toxic"]["flag"] is True
    assert body["labels"]["toxic"]["flag"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_predict_api.py -v -m integration`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'backend.app'`

- [ ] **Step 3: Write minimal implementation**

`backend/app.py`:
```python
"""FastAPI moderation backend: POST /predict and GET /health.

Ordering in this module is load-bearing. The `_gate` middleware runs the three abuse controls
(delivery spec section 6.3) before FastAPI parses the body, so unauthenticated or
rate-limited traffic never reaches validation, the model, or the database. Inside the
handler, `latency_ms` is stamped through persistence rather than before it (premortem H28),
and a failure writes a row rather than vanishing.
"""

import json
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.audit import FLAGGED_INCLUSION_PROBABILITY, should_random_audit
from backend.auth import API_KEY_HEADER, check_api_key, client_fingerprint
from backend.config import Settings, load_settings
from backend.db import PendingWrite, PredictionRow, ReviewIntent, init_schema, make_engine
from backend.model_loader import load_from_settings
from backend.persistence import persist_prediction
from backend.policy import decide, load_thresholds
from backend.preprocess import prepare_input
from backend.ratelimit import RateLimiter
from backend.schemas import PredictRequest
from backend.spool import Spool, SpoolFull
from model.contract import LabelScore, PredictionResponse, probs_to_dict
from model.labels import LABELS

log = logging.getLogger("backend.request")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings)
        app.state.session_factory = sessionmaker(
            bind=app.state.engine, expire_on_commit=False
        )
        init_schema(app.state.engine)
        # Startup fails closed: a digest or allowlist violation must stop the container from
        # ever accepting traffic, not surface as a 500 on the first request.
        app.state.model = load_from_settings(settings)
        app.state.thresholds = load_thresholds(settings.thresholds_path)
        app.state.spool = Spool(settings.spool_path, settings.spool_max_rows)
        app.state.limiter = RateLimiter(
            settings.rate_limit_per_minute, settings.rate_limit_burst
        )
        # SystemRandom, not a seeded PRNG: the repository is public, and a predictable audit
        # sample lets an attacker time submissions to miss it.
        app.state.rng = random.SystemRandom()
        app.state.rejected = {"unauthenticated": 0, "rate_limited": 0, "oversize": 0}
        yield
        app.state.engine.dispose()

    app = FastAPI(title="Toxic Comment Moderation API", version="2.0", lifespan=lifespan)
    app.state.settings = settings

    def _reject(kind: str, status_code: int, detail: str, headers=None) -> JSONResponse:
        app.state.rejected[kind] += 1
        return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        if request.url.path != "/predict":
            return await call_next(request)
        raw_length = request.headers.get("content-length")
        if raw_length is None:
            return _reject("oversize", 411, "Content-Length header is required")
        if not raw_length.isdigit() or int(raw_length) > settings.max_body_bytes:
            return _reject("oversize", 413, "request body too large")
        presented = request.headers.get(API_KEY_HEADER)
        if not check_api_key(presented, settings.demo_api_key):
            return _reject("unauthenticated", 401, f"a valid {API_KEY_HEADER} header is required")
        fingerprint = client_fingerprint(presented)
        if not app.state.limiter.allow(fingerprint):
            return _reject(
                "rate_limited", 429, "rate limit exceeded", {"Retry-After": "60"}
            )
        request.state.client_fp = fingerprint
        return await call_next(request)

    @app.post("/predict", response_model=PredictionResponse)
    def predict(payload: PredictRequest, request: Request) -> PredictionResponse:
        started = time.perf_counter()
        state = request.app.state
        request_id = str(uuid.uuid4())
        client_fp = getattr(request.state, "client_fp", None)
        model = state.model

        try:
            normalized = prepare_input(payload.text)
            probs = probs_to_dict(model.predict_proba([normalized])[0])
            result = decide(probs, state.thresholds)
        except Exception as exc:  # noqa: BLE001 - every failure must leave a row behind
            failed = PredictionRow(
                request_id=request_id,
                input_text=payload.text,
                input_chars=len(payload.text),
                model_version=model.model_version,
                probs=None,
                decision=None,
                max_prob=None,
                latency_ms=0,
                status="error",
                persist_status="direct",
                error_kind=type(exc).__name__,
                client_fp=client_fp,
            )
            outcome = _persist(state, PendingWrite(prediction=failed), started)
            _log(model, failed, outcome, started)
            raise HTTPException(status_code=500, detail="prediction failed") from exc

        review = None
        if result.decision in ("review", "block"):
            review = ReviewIntent(
                request_id=request_id,
                source="flagged",
                inclusion_probability=FLAGGED_INCLUSION_PROBABILITY,
                input_text_snapshot=payload.text,
            )
        elif should_random_audit(state.settings.random_audit_rate, state.rng):
            review = ReviewIntent(
                request_id=request_id,
                source="random-audit",
                inclusion_probability=state.settings.random_audit_rate,
                input_text_snapshot=payload.text,
            )

        row = PredictionRow(
            request_id=request_id,
            input_text=payload.text,
            input_chars=len(payload.text),
            model_version=model.model_version,
            probs=probs,
            decision=result.decision,
            max_prob=result.max_prob,
            latency_ms=0,
            status="ok",
            persist_status="direct",
            client_fp=client_fp,
        )
        outcome = _persist(state, PendingWrite(prediction=row, review=review), started)
        _log(model, row, outcome, started)

        return PredictionResponse(
            request_id=request_id,
            model_version=model.public_version,
            labels={
                label: LabelScore(prob=probs[label], flag=result.flags[label])
                for label in LABELS
            },
            decision=result.decision,
            max_prob=result.max_prob,
            latency_ms=outcome.latency_ms,
        )

    @app.get("/health")
    def health(request: Request) -> dict:
        state = request.app.state
        database = "ok"
        try:
            with state.session_factory() as session:
                session.execute(sql_text("select 1"))
        except SQLAlchemyError:
            database = "degraded"
        spool_depth = state.spool.depth()
        return {
            "status": "ok" if database == "ok" and spool_depth == 0 else "degraded",
            "model_version": state.model.public_version,
            "database": database,
            "spool_depth": spool_depth,
            "rejected": dict(state.rejected),
        }

    return app


def _persist(state, pending: PendingWrite, started: float):
    try:
        return persist_prediction(state.session_factory, state.spool, pending, started)
    except SpoolFull as exc:
        raise HTTPException(
            status_code=503,
            detail="prediction log is saturated; retry later",
            headers={"Retry-After": "30"},
        ) from exc


def _log(model, row: PredictionRow, outcome, started: float) -> None:
    """One structured line per request. Carries the FULL model version, because incident
    response needs to know exactly which artifact produced a score (H14), and never carries
    input_text, because only the access-restricted RDS row holds user comments."""
    log.info(
        json.dumps(
            {
                "event": "predict",
                "request_id": row.request_id,
                "model_version": model.model_version,
                "status": row.status,
                "decision": row.decision,
                "error_kind": row.error_kind,
                "latency_ms": outcome.latency_ms,
                "handler_ms": round((time.perf_counter() - started) * 1000, 1),
                "commit_ms": round(outcome.commit_ms, 1),
                "persist_status": outcome.persist_status,
                "client_fp": row.client_fp,
                "input_chars": row.input_chars,
            },
            sort_keys=True,
        )
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_predict_api.py tests/unit/test_probs_to_dict.py -v`
Expected: 9 integration PASS plus 4 unit PASS. `test_backend_never_re_derives_the_label_zip` is now load-bearing: `backend/app.py` exists and must go through `probs_to_dict`.

- [ ] **Step 5: Commit**

```bash
git add backend/app.py tests/integration/conftest.py tests/integration/test_predict_api.py
git commit -m "Serve /predict with contract-valid responses and complete prediction logging"
```

---

### Task 16: `/predict` failure and abuse paths (H30, H28, REG-6.3a/b/c)

**Files:**
- Test: `tests/integration/test_predict_failure_paths.py`, `tests/integration/test_predict_abuse_controls.py`

**Interfaces produced:** none; this task proves the behaviour the previous two built

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_predict_failure_paths.py`:
```python
from contextlib import contextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.db import Prediction
from backend.persistence import drain_spool
from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration


def break_the_database(client):
    @contextmanager
    def broken():
        raise OperationalError("connect", {}, Exception("connection refused"))
        yield  # pragma: no cover

    client.app.state.session_factory = broken


def test_predict_stays_available_when_the_database_is_down(client, engine, session):
    """H30, the finding this phase exists to close. Under the original design this returns
    503, which hands an attacker an off switch: exhaust a db.t4g.micro's connections and
    moderation is down, not degraded, for as long as the pressure lasts."""
    healthy = client.app.state.session_factory
    break_the_database(client)

    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["decision"] in {"allow", "review", "block"}
    assert client.app.state.spool.depth() == 1

    client.app.state.session_factory = healthy
    assert drain_spool(sessionmaker(bind=engine, expire_on_commit=False), client.app.state.spool) == 1
    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].persist_status == "spooled"
    assert stored[0].input_text == "you are an idiot"


def test_health_reports_the_degradation_rather_than_hiding_it(client):
    break_the_database(client)
    client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    body = client.get("/health").json()
    assert body["database"] == "degraded"
    assert body["spool_depth"] == 1
    assert body["status"] == "degraded"


def test_a_saturated_spool_fails_closed_with_retry_after(client):
    """The one remaining 503. Reaching it costs SPOOL_MAX_ROWS successful requests through
    the rate limiter rather than a handful of concurrent connections."""
    break_the_database(client)
    client.app.state.spool.max_rows = 1
    assert client.post("/predict", json={"text": "first"}, headers=AUTH).status_code == 200
    saturated = client.post("/predict", json={"text": "second"}, headers=AUTH)
    assert saturated.status_code == 503
    assert saturated.headers["Retry-After"] == "30"


def test_failed_prediction_still_writes_a_row(client, session, monkeypatch):
    """H28's second half. If the failure path writes no row, the slowest requests are
    structurally absent from the graded latency series."""
    def explode(texts):
        raise RuntimeError("estimator blew up")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert response.status_code == 500

    stored = session.scalars(select(Prediction)).all()
    assert len(stored) == 1
    assert stored[0].status == "error"
    assert stored[0].error_kind == "RuntimeError"
    assert stored[0].decision is None
    assert stored[0].prob_toxic is None
    assert stored[0].latency_ms >= 0
    assert stored[0].input_text == "you are an idiot"


def test_the_error_response_leaks_no_internals(client, monkeypatch):
    def explode(texts):
        raise RuntimeError("/srv/artifacts/toxic-clf.skops is corrupt")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    response = client.post("/predict", json={"text": "x"}, headers=AUTH)
    assert response.json() == {"detail": "prediction failed"}
    assert "skops" not in response.text
```

`tests/integration/test_predict_abuse_controls.py`:
```python
import pytest

from backend.config import MAX_INPUT_CHARS
from backend.ratelimit import RateLimiter
from tests.integration.conftest import AUTH, DEMO_KEY

pytestmark = pytest.mark.integration


def test_predict_requires_a_valid_api_key(client):
    """REG-6.3c."""
    assert client.post("/predict", json={"text": "hello"}).status_code == 401
    assert (
        client.post("/predict", json={"text": "hello"}, headers={"X-API-Key": "wrong"}).status_code
        == 401
    )
    assert client.post("/predict", json={"text": "hello"}, headers=AUTH).status_code == 200


def test_the_key_never_appears_in_a_response(client):
    response = client.post("/predict", json={"text": "hello"}, headers={"X-API-Key": "wrong"})
    assert DEMO_KEY not in response.text


def test_health_is_reachable_without_a_key(client):
    assert client.get("/health").status_code == 200


def test_authentication_precedes_body_validation(client):
    """An unauthenticated caller must not be able to make the server parse and validate a
    16 KB body; the gate runs before pydantic."""
    response = client.post("/predict", json={"text": "a" * (MAX_INPUT_CHARS + 1)})
    assert response.status_code == 401


def test_oversize_text_is_rejected_with_422(client):
    """REG-6.3a."""
    response = client.post(
        "/predict", json={"text": "a" * (MAX_INPUT_CHARS + 1)}, headers=AUTH
    )
    assert response.status_code == 422


def test_oversize_body_is_rejected_before_parsing(client):
    response = client.post(
        "/predict", content=b'{"text": "' + b"a" * 20000 + b'"}',
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_body_without_content_length_is_refused(client):
    def chunks():
        yield b'{"text": "hello"}'

    response = client.post(
        "/predict", content=chunks(), headers={**AUTH, "Content-Type": "application/json"}
    )
    assert response.status_code == 411


def test_rate_limited_after_the_burst_is_exhausted(client):
    """REG-6.3b. Without this the endpoint is free denial-of-service capacity, and the
    durable spool it protects fills SPOOL_MAX_ROWS times faster."""
    client.app.state.limiter = RateLimiter(per_minute=60, burst=3)
    codes = [
        client.post("/predict", json={"text": f"comment {index}"}, headers=AUTH).status_code
        for index in range(5)
    ]
    assert codes[:3] == [200, 200, 200]
    assert codes[3:] == [429, 429]


def test_rate_limited_responses_carry_retry_after_and_are_counted(client):
    client.app.state.limiter = RateLimiter(per_minute=60, burst=1)
    client.post("/predict", json={"text": "one"}, headers=AUTH)
    limited = client.post("/predict", json={"text": "two"}, headers=AUTH)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert client.get("/health").json()["rejected"]["rate_limited"] == 1


def test_rejected_requests_do_not_write_rows(client, session):
    from sqlalchemy import select

    from backend.db import Prediction

    client.post("/predict", json={"text": "hello"})                      # 401
    client.post("/predict", json={"text": ""}, headers=AUTH)             # 422
    assert session.scalars(select(Prediction)).all() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_predict_failure_paths.py tests/integration/test_predict_abuse_controls.py -v -m integration`
Expected: the abuse-control tests PASS immediately (Task 15 wired the gate) and the failure-path tests FAIL — `test_predict_stays_available_when_the_database_is_down` with `assert 503 == 200` if the persistence path was written the way delivery spec §10 described, and `test_failed_prediction_still_writes_a_row` with `assert 0 == 1` if the error path re-raises without persisting. If both pass on the first run, verify by reverting `_persist` to `raise HTTPException(503)` and confirming the failure, then restore.

- [ ] **Step 3: Write minimal implementation**

No new modules. If Step 2 showed real failures, the corrections are in `backend/app.py` exactly as written in Task 15: `_persist` converts only `SpoolFull` into a 503, and the `except Exception` branch persists a `status='error'` row **before** raising `HTTPException(500)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration -v -m integration`
Expected: 10 abuse-control PASS, 5 failure-path PASS, plus the earlier integration suites green.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_predict_failure_paths.py tests/integration/test_predict_abuse_controls.py
git commit -m "Prove the degraded, failed, and abuse paths of the predict endpoint"
```

---

### Task 17: Structured request log — digest in, raw text out (H14, retention)

**Files:**
- Test: `tests/integration/test_request_log.py`

**Interfaces produced:** the `backend.request` JSON log line, consumed by Phase 5's `awslogs` driver

- [ ] **Step 1: Write the failing test**

`tests/integration/test_request_log.py`:
```python
import json
import logging

import pytest

from tests.integration.conftest import AUTH

pytestmark = pytest.mark.integration


def emitted(caplog) -> list[dict]:
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "backend.request"
    ]


def test_one_structured_line_per_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    lines = emitted(caplog)
    assert len(lines) == 1
    assert lines[0]["event"] == "predict"
    assert lines[0]["status"] == "ok"
    assert lines[0]["persist_status"] == "direct"
    assert lines[0]["input_chars"] == 16
    assert lines[0]["latency_ms"] >= 0
    assert lines[0]["handler_ms"] >= lines[0]["latency_ms"]


def test_the_log_carries_the_full_digest(client, caplog, artifact_bundle):
    """H14's other half. The digest is stripped from the public listener precisely so that
    it can live where incident response needs it: the log and the database row."""
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
    assert artifact_bundle["digest"] in emitted(caplog)[0]["model_version"]


def test_the_log_never_carries_raw_user_text(client, caplog):
    """Only the access-restricted RDS row holds user comments, and it holds them for 30 days.
    A log line copies them into CloudWatch, where the retention purge cannot reach."""
    secret = "my home address is 221b baker street"
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": secret}, headers=AUTH)
    rendered = json.dumps(emitted(caplog))
    assert secret not in rendered
    assert "221b" not in rendered


def test_the_log_never_carries_the_api_key(client, caplog):
    from tests.integration.conftest import DEMO_KEY

    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "hello"}, headers=AUTH)
    assert DEMO_KEY not in json.dumps(emitted(caplog))
    assert emitted(caplog)[0]["client_fp"] is not None


def test_a_failed_request_is_logged_with_its_error_kind(client, caplog, monkeypatch):
    def explode(texts):
        raise RuntimeError("estimator blew up")

    monkeypatch.setattr(client.app.state.model, "predict_proba", explode)
    with caplog.at_level(logging.INFO, logger="backend.request"):
        client.post("/predict", json={"text": "hello"}, headers=AUTH)
    line = emitted(caplog)[0]
    assert line["status"] == "error"
    assert line["error_kind"] == "RuntimeError"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_request_log.py -v -m integration`
Expected: FAIL. If Task 15 shipped `_log` as written the first four pass; the run is still meaningful because `test_the_log_never_carries_raw_user_text` is the regression guard for the one change every future debugging session wants to make. Verify it bites by temporarily adding `"input_text": row.input_text` to `_log` and observing `AssertionError`, then removing it.

- [ ] **Step 3: Write minimal implementation**

`_log` in `backend/app.py` as written in Task 15 already satisfies this. Add the logging configuration to `create_app` so the line is emitted under uvicorn as well as under pytest:

```python
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",           # the record IS the JSON document
        force=False,
    )
```

Place it as the first statement inside `create_app`, after `settings` is resolved.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_request_log.py -v -m integration`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app.py tests/integration/test_request_log.py
git commit -m "Emit one structured request log line carrying the digest and no user text"
```

---

### Task 18: `/health` (rubric 2.1, H14)

**Files:**
- Test: `tests/integration/test_health.py`

**Interfaces produced:** `GET /health`, the contract the Phase 5 deploy gate and the container HEALTHCHECK assert against

- [ ] **Step 1: Write the failing test**

`tests/integration/test_health.py`:
```python
import re

import pytest

pytestmark = pytest.mark.integration

HEX64 = re.compile(r"[0-9a-f]{64}")


def test_health_reports_model_version_and_database_readiness(client):
    """Rubric 2.1 requires a health check; delivery spec section 3.3 makes it the deploy
    gate, so it has to distinguish 'loaded and connected' from 'process is up'."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["model_version"] == "toxic-clf:v3"
    assert body["database"] == "ok"
    assert body["spool_depth"] == 0


def test_health_never_fingerprints_the_model(client):
    """H14. Delivery spec section 6.3 strips the digest here specifically so an attacker
    cannot confirm which artifact is deployed while crafting evasions."""
    response = client.get("/health")
    assert "sha256" not in response.text
    assert not HEX64.search(response.text)


def test_health_exposes_the_rejection_counters(client):
    client.post("/predict", json={"text": "hello"})            # 401
    counters = client.get("/health").json()["rejected"]
    assert counters["unauthenticated"] == 1
    assert set(counters) == {"unauthenticated", "rate_limited", "oversize"}


def test_health_answers_200_even_while_degraded(client):
    """A 5xx here would take the instance out of the deploy gate and out of any future load
    balancer at the exact moment the operator needs to see why."""
    from tests.integration.test_predict_failure_paths import break_the_database

    break_the_database(client)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "degraded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_health.py -v -m integration`
Expected: PASS if Task 15 shipped `/health` as written; otherwise FAIL with `KeyError: 'spool_depth'` or an assertion on `status`. Confirm `test_health_never_fingerprints_the_model` bites by temporarily returning `state.model.model_version` and observing the failure, then restoring `public_version`.

- [ ] **Step 3: Write minimal implementation**

`/health` in `backend/app.py` as written in Task 15. Record the deploy-gate contract in the Phase 5 handoff by adding this line to `README.md` under the endpoints section:

```markdown
`GET /health` returns 200 with `{"status": "ok" | "degraded", "model_version", "database",
"spool_depth", "rejected"}`. The deploy gate asserts `status == "ok"` per instance; a
`degraded` status means the process is serving but the database or the spool needs attention.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_health.py -v -m integration`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_health.py README.md
git commit -m "Pin the health endpoint contract used by the deploy gate"
```

---

### Task 19: Retention purge with a bounded pending exemption (remediation 3.13)

**Files:**
- Create: `backend/retention.py`
- Test: `tests/integration/test_retention.py`

**Interfaces produced:** `PurgeReport`, `purge`, `python -m backend.retention`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_retention.py`:
```python
import datetime as dt

import pytest

from backend.db import Prediction, ReviewQueue, ReviewIntent, enqueue_review, insert_prediction
from backend.retention import purge
from tests.integration.test_db_schema import make_row

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
POLICY = {
    "input_text_retention_days": 30,
    "pending_review_ttl_days": 7,
    "snapshot_retention_days": 30,
}


def seed(session, request_id, *, predicted_days_ago, enqueued_days_ago=None, status="pending"):
    insert_prediction(
        session,
        make_row(request_id=request_id, ts=NOW - dt.timedelta(days=predicted_days_ago)),
    )
    if enqueued_days_ago is not None:
        enqueue_review(
            session,
            ReviewIntent(
                request_id=request_id,
                source="flagged",
                inclusion_probability=1.0,
                input_text_snapshot="you are an idiot",
                enqueued_ts=NOW - dt.timedelta(days=enqueued_days_ago),
            ),
        )
        session.execute(
            ReviewQueue.__table__.update()
            .where(ReviewQueue.request_id == request_id)
            .values(status=status)
        )
    session.commit()


def test_recent_predictions_are_untouched(session):
    seed(session, "r1", predicted_days_ago=10)
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_old_prediction_without_a_pending_review_is_purged(session):
    seed(session, "r1", predicted_days_ago=31)
    report = purge(session, NOW, **POLICY)
    assert report.purged_input_text == 1
    assert session.get(Prediction, "r1").input_text is None
    assert session.get(Prediction, "r1").decision is not None      # the row survives


def test_a_live_pending_review_exempts_its_prediction(session):
    """Delivery spec section 6.4: the purge must not destroy the reviewer's evidence
    mid-workflow."""
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=2)
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_pending_exemption_expires_at_the_hard_ttl(session):
    """Remediation 3.13, and the reason this task exists. An unbounded pending exemption is
    attacker-controlled retention: anything that lands in the queue and is never reviewed is
    kept forever, which defeats the 30-day policy for exactly the content an attacker chose
    to submit. The exemption is capped, so the queue cannot be used as a storage primitive."""
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=8)
    report = purge(session, NOW, **POLICY)
    assert report.expired_reviews == 1
    assert session.get(ReviewQueue, "r1").status == "expired"
    assert session.get(Prediction, "r1").input_text is None


def test_a_rescored_review_still_exempts_within_the_ttl(session):
    seed(session, "r1", predicted_days_ago=31, enqueued_days_ago=3, status="rescored")
    purge(session, NOW, **POLICY)
    assert session.get(Prediction, "r1").input_text == "you are an idiot"


def test_snapshots_are_nulled_at_their_own_ttl_regardless_of_status(session):
    seed(session, "r1", predicted_days_ago=45, enqueued_days_ago=31, status="reviewed")
    report = purge(session, NOW, **POLICY)
    assert report.purged_snapshots == 1
    assert session.get(ReviewQueue, "r1").input_text_snapshot is None
    assert session.get(ReviewQueue, "r1").status == "reviewed"     # the row survives


def test_purge_is_idempotent(session):
    seed(session, "r1", predicted_days_ago=31)
    purge(session, NOW, **POLICY)
    second = purge(session, NOW, **POLICY)
    assert second.purged_input_text == 0
    assert second.expired_reviews == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_retention.py -v -m integration`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.retention'`

- [ ] **Step 3: Write minimal implementation**

`backend/retention.py`:
```python
"""Retention purge with a BOUNDED pending-review exemption.

Three rules, applied in this order, and the order is the design:

1. Expire pending reviews older than PENDING_REVIEW_TTL_DAYS. Delivery spec section 6.4
   exempts pending rows from the purge so it cannot destroy a reviewer's evidence
   mid-workflow. Unbounded, that exemption is attacker-controlled retention: anything that
   lands in the queue and is never reviewed is kept forever, which defeats the 30-day policy
   for exactly the content an attacker chose to submit (premortem remediation 3.13).
   Expiring first is what makes rule 2's exemption finite.
2. Null `predictions.input_text` older than INPUT_TEXT_RETENTION_DAYS, except where a review
   is still pending or rescored.
3. Null `review_queue.input_text_snapshot` older than SNAPSHOT_RETENTION_DAYS regardless of
   status, so no path retains user text past the stated policy.

Every other column survives: probabilities, decision, flags, timestamps, and latency are what
the monitoring dashboard reads, and they are not personal data.
"""

import argparse
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select, update

from backend.db import Prediction, ReviewQueue


@dataclass(frozen=True)
class PurgeReport:
    expired_reviews: int
    purged_input_text: int
    purged_snapshots: int


def purge(
    session,
    now: dt.datetime,
    *,
    input_text_retention_days: int,
    pending_review_ttl_days: int,
    snapshot_retention_days: int,
) -> PurgeReport:
    expired = session.execute(
        update(ReviewQueue)
        .where(
            ReviewQueue.status.in_(("pending", "rescored")),
            ReviewQueue.enqueued_ts < now - dt.timedelta(days=pending_review_ttl_days),
        )
        .values(status="expired")
    ).rowcount

    still_open = select(ReviewQueue.request_id).where(
        ReviewQueue.status.in_(("pending", "rescored"))
    )
    purged_text = session.execute(
        update(Prediction)
        .where(
            Prediction.ts < now - dt.timedelta(days=input_text_retention_days),
            Prediction.input_text.is_not(None),
            Prediction.request_id.not_in(still_open),
        )
        .values(input_text=None)
    ).rowcount

    purged_snapshots = session.execute(
        update(ReviewQueue)
        .where(
            ReviewQueue.enqueued_ts < now - dt.timedelta(days=snapshot_retention_days),
            ReviewQueue.input_text_snapshot.is_not(None),
        )
        .values(input_text_snapshot=None)
    ).rowcount

    session.commit()
    return PurgeReport(expired, purged_text, purged_snapshots)


def main() -> None:
    from sqlalchemy.orm import sessionmaker

    from backend.config import load_settings
    from backend.db import make_engine

    parser = argparse.ArgumentParser(description="Run the input-text retention purge")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    engine = make_engine(settings)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        if args.dry_run:
            print("dry run: no rows modified")
            return
        report = purge(
            session,
            dt.datetime.now(dt.timezone.utc),
            input_text_retention_days=settings.input_text_retention_days,
            pending_review_ttl_days=settings.pending_review_ttl_days,
            snapshot_retention_days=settings.snapshot_retention_days,
        )
    print(
        f"expired_reviews={report.expired_reviews} "
        f"purged_input_text={report.purged_input_text} "
        f"purged_snapshots={report.purged_snapshots}"
    )
    engine.dispose()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_retention.py -v -m integration`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/retention.py tests/integration/test_retention.py
git commit -m "Add retention purge with a hard cap on the pending-review exemption"
```

---

### Task 20: Latency budget and one load pass (H28)

**Files:**
- Create: `tests/perf/__init__.py`, `tests/perf/test_latency_budget.py`, `docs/latency-baseline.md` (generated)
- Amend: `pyproject.toml` (a `perf` marker), `Makefile` (a `loadtest` target)

**Interfaces produced:** `docs/latency-baseline.md`, the stated p95 budget

- [ ] **Step 1: Write the failing test**

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:
```toml
markers = [
    "integration: needs external services (deselect with -m 'not integration')",
    "perf: measures latency against a real database (deselect with -m 'not perf')",
]
```

Add to the `Makefile`:
```makefile
.PHONY: loadtest
loadtest:
	PYTHONHASHSEED=0 $(BIN)/pytest -m perf -s
```

`tests/perf/test_latency_budget.py`:
```python
"""One load pass against a real Postgres, so the graded latency chart has a stated budget
rather than an anecdote.

Premortem H28: the design had no latency budget, no percentile, and no load test, while
`latency_ms` is a graded artifact (rubric 3.2, latency over time). This measures what the
system actually stamps - the whole handler including persistence - and fails if p95 exceeds
the budget in `Settings.latency_budget_p95_ms`.
"""

import statistics
import time
from pathlib import Path

import pytest

from backend.ratelimit import RateLimiter
from tests.integration.conftest import AUTH

pytestmark = [pytest.mark.perf, pytest.mark.integration]

SAMPLES = 200
BASELINE = Path("docs/latency-baseline.md")

CORPUS = [
    "have a nice day friend",
    "i disagree but respect your point",
    "you are an idiot",
    "what the hell is this crap",
    "watch your back i am coming",
    "your kind does not belong here",
    "thanks for the thoughtful edit " * 40,
]


def test_p95_latency_under_budget(client):
    client.app.state.limiter = RateLimiter(per_minute=600_000, burst=SAMPLES + 10)

    client.post("/predict", json={"text": "warm up"}, headers=AUTH)      # JIT + pool warm

    stamped: list[int] = []
    observed: list[float] = []
    for index in range(SAMPLES):
        text = CORPUS[index % len(CORPUS)]
        started = time.perf_counter()
        response = client.post("/predict", json={"text": text}, headers=AUTH)
        observed.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
        stamped.append(response.json()["latency_ms"])

    percentiles = statistics.quantiles(stamped, n=100)
    p50, p95, p99 = statistics.median(stamped), percentiles[94], percentiles[98]
    round_trip_p95 = statistics.quantiles(observed, n=100)[94]

    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        "# Latency baseline\n\n"
        f"Measured by `make loadtest`: {SAMPLES} sequential single-comment requests against a\n"
        "real Postgres, warm process, no concurrency. `latency_ms` is the value the service\n"
        "stamps and stores, measured from handler entry through the prediction and review\n"
        "inserts; the client round trip additionally includes serialization and the commit.\n\n"
        "| Measure | p50 | p95 | p99 |\n|---|---|---|---|\n"
        f"| stamped `latency_ms` | {p50:.0f} ms | {p95:.0f} ms | {p99:.0f} ms |\n"
        f"| client round trip | - | {round_trip_p95:.0f} ms | - |\n\n"
        f"Budget: p95 under {client.app.state.settings.latency_budget_p95_ms} ms. "
        f"Result: {'PASS' if p95 < client.app.state.settings.latency_budget_p95_ms else 'FAIL'}.\n",
        encoding="utf-8",
    )

    print(f"\nlatency p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms")
    assert p95 < client.app.state.settings.latency_budget_p95_ms


def test_the_stamped_latency_is_not_systematically_smaller_than_the_round_trip(client):
    """A stamped value far below the observed round trip would mean the stamp is being taken
    before the work it is supposed to cover - the H28 failure, in numbers."""
    client.app.state.limiter = RateLimiter(per_minute=600_000, burst=100)
    stamped, observed = [], []
    for _ in range(30):
        started = time.perf_counter()
        response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
        observed.append((time.perf_counter() - started) * 1000)
        stamped.append(response.json()["latency_ms"])
    assert statistics.median(stamped) >= 0.5 * statistics.median(observed)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/perf -v -m perf`
Expected: FAIL with `'perf' not found in `markers` configuration option` before `pyproject.toml` is amended; after the marker is added, the test runs and either passes or fails on the budget assertion.

- [ ] **Step 3: Write minimal implementation**

The marker and Makefile target above are the implementation. If p95 exceeds 500 ms on the build box, the ordered remedies are, cheapest first: confirm `pool_pre_ping` is not re-validating on every checkout under a cold pool; confirm the TF-IDF `max_features` caps from delivery spec §6.2 were actually applied to the registered artifact; and only then raise `LATENCY_BUDGET_P95_MS` — with the new number and its justification written into `docs/latency-baseline.md`, never silently.

- [ ] **Step 4: Run test to verify it passes**

Run: `make loadtest`
Expected: prints `latency p50=... p95=... p99=...`, writes `docs/latency-baseline.md`, 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml Makefile tests/perf docs/latency-baseline.md
git commit -m "Measure the predict latency budget and record the baseline"
```

---

### Task 21: Backend serve image (rubric 5.1; H35 partial, REG-6.3 build hygiene)

**Files:**
- Create: `backend/Dockerfile`, `.dockerignore`
- Test: `tests/unit/test_dockerfile_hygiene.py`

**Interfaces produced:** the `backend` image, finalized in Phase 5

- [ ] **Step 1: Write the failing test**

`tests/unit/test_dockerfile_hygiene.py`:
```python
import re
from pathlib import Path

DOCKERFILE = Path("backend/Dockerfile")
SOURCE = DOCKERFILE.read_text(encoding="utf-8") if DOCKERFILE.exists() else ""


def test_the_base_image_is_pinned_by_digest():
    """H35. A floating tag defeats the SHA traceability the whole deploy pipeline is built
    on: the same git SHA would produce different images on different days."""
    assert re.search(
        r"^FROM python:3\.11-slim-bookworm@sha256:[0-9a-f]{64}", SOURCE, re.MULTILINE
    ), "pin the base image by digest"


def test_dependencies_install_from_a_hashed_lock():
    assert "--require-hashes" in SOURCE
    assert "requirements/serve.txt" in SOURCE


def test_the_container_does_not_run_as_root():
    assert re.search(r"^USER appuser", SOURCE, re.MULTILINE)


def test_no_secret_is_baked_into_a_layer():
    """Delivery spec section 6.3: a build-arg or ENV bakes a credential into an image layer
    permanently, and this image is pushed to a registry."""
    for forbidden in (
        "WANDB_API_KEY",
        "DEMO_API_KEY",
        "DATABASE_URL",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    ):
        assert forbidden not in SOURCE, f"{forbidden} must never appear in the Dockerfile"


def test_the_model_card_is_copied_into_the_image():
    """The digest of record has to travel with the code, or the loader's provenance check
    degrades to reading a value the deploy environment supplied."""
    assert "MODEL_CARD.md" in SOURCE


def test_the_image_declares_a_healthcheck():
    assert "HEALTHCHECK" in SOURCE


def test_the_dockerignore_excludes_local_state():
    ignored = Path(".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".venv", ".git", "tests", "docs", "*.spool"):
        assert entry in ignored
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dockerfile_hygiene.py -v`
Expected: FAIL — `test_the_base_image_is_pinned_by_digest` with `AssertionError: pin the base image by digest` (the file does not exist, so `SOURCE` is empty), and `test_the_dockerignore_excludes_local_state` with `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Resolve the base digest and write the Dockerfile in one step, so no placeholder digest is ever committed:

```bash
DIGEST=$(docker buildx imagetools inspect python:3.11-slim-bookworm --format '{{ .Manifest.Digest }}')
echo "pinning python:3.11-slim-bookworm at ${DIGEST}"
cat > backend/Dockerfile <<DOCKERFILE
# syntax=docker/dockerfile:1.10
# CPU serve image for the FastAPI moderation backend. arm64 (Graviton) and the aarch64
# build box use the same manifest list, so this digest is correct on both.
FROM python:3.11-slim-bookworm@${DIGEST} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 appuser \\
 && install -d -o appuser -g appuser /var/lib/toxic

WORKDIR /app

COPY requirements/base.txt requirements/serve.txt requirements/
RUN pip install --require-hashes --no-deps -r requirements/serve.txt

COPY model/ model/
COPY backend/ backend/
COPY MODEL_CARD.md MODEL_CARD.md

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \\
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

CMD ["uvicorn", "backend.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
DOCKERFILE
```

`.dockerignore`:
```
.git
.github
.venv
tests
docs
infra
*.spool
*.pyc
__pycache__
.pytest_cache
.ruff_cache
```

Note what is deliberately absent: no `ARG`, no `ENV` carrying a credential, and no `wandb` in `serve.txt`. The artifact is fetched at deploy time by `infra/aws/fetch_artifacts.sh` (Phase 5) and mounted at `MODEL_ARTIFACT_PATH`; the image never holds a registry credential. `--no-deps` is safe and deliberate because `pip-compile` already resolved the full transitive closure into the hashed lock.

- [ ] **Step 4: Run the test, then build and run the image**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dockerfile_hygiene.py -v`
Expected: 7 PASS

Then prove the image actually serves:
```bash
docker build -f backend/Dockerfile -t toxic-backend:dev .
docker run --rm -d --name toxic-backend-smoke -p 8000:8000 \
  -e DATABASE_URL="postgresql+psycopg://postgres:postgres@host.docker.internal:5432/postgres" \
  -e DEMO_API_KEY="local-dev-key" \
  -e MODEL_ARTIFACT_PATH=/srv/toxic-clf.skops \
  -e MODEL_CARD_PATH=/app/MODEL_CARD.md \
  -e MODEL_DIGEST="$(grep -oE '[0-9a-f]{64}' MODEL_CARD.md | head -1)" \
  -e MODEL_REGISTRY_VERSION=3 \
  -e THRESHOLDS_PATH=/srv/thresholds.json \
  -e SPOOL_PATH=/var/lib/toxic/predictions.spool \
  -v "$PWD/artifacts:/srv:ro" toxic-backend:dev
sleep 5 && curl -sS localhost:8000/health && docker rm -f toxic-backend-smoke
```
Expected: `{"status":"ok","model_version":"toxic-clf:v3","database":"ok","spool_depth":0,...}`, with no 64-hex string anywhere in the output.

- [ ] **Step 5: Commit**

```bash
git add backend/Dockerfile .dockerignore tests/unit/test_dockerfile_hygiene.py
git commit -m "Add digest-pinned non-root serve image for the backend"
```

---

### Task 22: Phase 2 gate, interface reconciliation, and PR

**Files:**
- Amend: `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md` (Interface Contracts and Phase 2 task list), `README.md`

- [ ] **Step 1: Full suite, lint, and the real-dependency gate**

Run:
```bash
make lint && make test && make test-integration && make loadtest
```
Expected: ruff clean; unit suite green; every integration test green against a real Postgres; `docs/latency-baseline.md` written with p95 under 500 ms.

- [ ] **Step 2: Reconcile the master plan's Interface Contracts block (H24)**

Apply the six corrections from the *Interface Contract corrections* table at the top of this file directly to `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`. A supersession note is not enough — the premortem established that this mechanism has already failed twice in this project, because a phase implementer reads one narrow slice and never opens the document that contains the correction.

Replace the master plan's **Safe loader (Phase 2)** and **Database writes (Phase 2 defines; Phase 3 consumes)** blocks with:

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

# backend/db.py  (Phase 2 defines; Phase 3 consumes)
@dataclass(frozen=True) class PredictionRow: ...     # includes status, persist_status, ts
@dataclass(frozen=True) class ReviewIntent: ...      # source, inclusion_probability, snapshot
@dataclass(frozen=True) class PendingWrite: prediction: PredictionRow; review: ReviewIntent | None
def write_pending(session, pending: PendingWrite, stamp) -> int: ...   # returns latency_ms
def insert_prediction(session, row: PredictionRow) -> None: ...        # idempotent
def enqueue_review(session, intent: ReviewIntent) -> None: ...         # idempotent
def fetch_pending_reviews(session, limit: int) -> list["ReviewQueue"]: ...
# Phase 3 owns submit_review(...) and write_distilbert_probs(...): both need reviewer session
# identity and re-scorer status semantics that do not exist in Phase 2.

# model/contract.py
def probs_to_dict(row) -> dict[str, float]: ...   # the single authoritative array->dict adapter
# PredictionResponse.model_version carries LoadedModel.public_version, never the digest.
```

Also amend the master plan's Phase 2 task list to name the abuse controls, the spool, and the retention purge, so the phase's scope matches what was built.

- [ ] **Step 3: Verify the exit criteria one by one**

```bash
# rubric 2.1 - both endpoints answer
curl -sS localhost:8000/health
curl -sS -X POST localhost:8000/predict -H 'Content-Type: application/json' \
  -H "X-API-Key: $DEMO_API_KEY" -d '{"text":"you are an idiot"}'

# rubric 2.2 - the row is there, with a timestamp
psql "$DATABASE_URL" -c "select request_id, ts, decision, latency_ms, status, persist_status from predictions order by ts desc limit 5;"

# H14 - no digest on the public listener, digest present in the row
curl -sS localhost:8000/health | grep -Eo '[0-9a-f]{64}' && echo "FAIL: digest leaked" || echo "ok: no digest"
psql "$DATABASE_URL" -c "select model_version from predictions limit 1;"
```
Expected: contract-valid JSON from `/predict`; at least one `predictions` row with a non-null `ts`; `ok: no digest`; the stored `model_version` containing `@sha256:`.

- [ ] **Step 4: Update the README's endpoint section (rubric 5.3 groundwork)**

Add the runnable example request to `README.md`, since rubric 5.3 grades "example user requests" and the premortem found that clause had no owning task:

```markdown
### Example request

```bash
curl -sS -X POST "http://$BACKEND_HOST:8000/predict" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d '{"text":"you are an idiot"}'
```

```json
{"request_id":"6f1c...","model_version":"toxic-clf:v3","labels":{"toxic":{"prob":0.87,"flag":true},
"severe_toxic":{"prob":0.12,"flag":false},"obscene":{"prob":0.44,"flag":false},
"threat":{"prob":0.03,"flag":false},"insult":{"prob":0.71,"flag":true},
"identity_hate":{"prob":0.05,"flag":false}},"decision":"review","max_prob":0.87,"latency_ms":42}
```

`DEMO_API_KEY` is not published here. It travels with the assignment submission and is rotated
after grading. Comments are capped at 4000 characters and each key is limited to 30 requests
per minute.
```

- [ ] **Step 5: Commit and open the PR**

```bash
git add docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md README.md
git commit -m "Reconcile the interface contracts with the Phase 2 implementation"
git push -u origin feat/phase-2-backend-rds
gh pr create --base main --title "Phase 2: FastAPI backend, safe model loading, RDS Postgres, prediction logging" \
  --body "$(cat <<'BODY'
/predict and /health serving the promoted classical model on CPU.

- Fail-closed skops loader with an explicit static trusted-type allowlist; the expected digest
  is read from the git-committed model card and cross-checked against MODEL_DIGEST.
- Abuse controls on the public listener: 4000-character input cap, 16 KB body cap, demo API
  key, and a per-key token bucket. Each has a test that fails if the control is removed.
- Complete prediction logging without an availability switch: direct insert, then a bounded
  fsync'd spool with an idempotent drain, and 503 only when the spool is saturated.
- latency_ms is stamped through persistence, failed requests write a row, and p95 is measured
  once and recorded in docs/latency-baseline.md.
- predictions / review_queue / feedback, with review source, per-row inclusion probability,
  input_text_snapshot, and a retention purge whose pending exemption is capped by a hard TTL.

Unit suite green, integration suite green against a real Postgres, ruff clean.
BODY
)"
```

**Exit criteria**

- `/predict` returns contract-valid JSON and persists exactly one `predictions` row per request, including on the degraded and failed paths.
- `/health` reports the opaque model version and database readiness, and no public response carries the artifact digest.
- A tampered artifact, a digest that disagrees with the model card, and an artifact carrying a type outside the allowlist all refuse to load.
- Every abuse control has a test that fails when the control is removed.
- With the database unreachable, `/predict` still answers 200 and the row reaches Postgres after the drain.
- p95 latency under 500 ms, recorded in `docs/latency-baseline.md`.
- The retention purge expires pending reviews at the hard TTL and nulls snapshots regardless of status.
- Master plan Interface Contracts reconciled; merged by PR.

---

## Self-Review

**Spec coverage.**

| Source clause | Where it lands |
|---|---|
| Rubric 2.1 `/predict` + `/health` | Tasks 15, 18; `tests/integration/test_predict_api.py`, `test_health.py` |
| Rubric 2.2 log every request, output, timestamp | Tasks 10, 11, 12, 15, 16 — including the degraded and failed paths, which is where "every" actually gets decided |
| Rubric 4.1 integration tests for FastAPI endpoints with pytest | Tasks 15, 16, 17, 18, 19 — all against a real Postgres, per delivery spec §3.3 |
| Rubric 5.1 containerize the backend | Task 21 |
| Rubric 5.3 example user requests | Task 22 step 4 |
| Delivery spec §6.2 shared serving normalizer plus a max-length cap | Tasks 3, 4 |
| Delivery spec §6.2 hierarchically coherent flags | Task 8 |
| Delivery spec §6.2 single authoritative array→dict adapter | Task 1 |
| Delivery spec §6.3 explicit static skops allowlist | Task 6 |
| Delivery spec §6.3 digest recorded independently | Tasks 5, 6 |
| Delivery spec §6.3 input cap, rate limit, demo API key | Tasks 3, 13, 14, 16 |
| Delivery spec §6.3 `/health` omits the digest | Tasks 7, 18 |
| Delivery spec §6.3 hashed lock | Task 2, Task 21 |
| Delivery spec §6.4 `review_queue.source`, `input_text_snapshot` | Tasks 9, 10 |
| Delivery spec §6.4 retention exempts pending reviews | Task 19, with the exemption capped |
| Delivery spec §10 error-handling decisions | D1 in the front matter; Tasks 11, 12, 16 — the 503 rule is deliberately replaced, and the replacement is tested on both paths |
| Premortem findings H8, H14, H22, H23, H24, H25, H28, H30, REG-6.3a–d, TAIL-1, remediation 3.13 | Coverage map in the front matter; every row names an owning task and a test that fails if unfixed |

**Placeholder scan.** Every step carries real code and an exact command. No TODO, no "handle edge cases", no "similar to". Three values are resolved by a command rather than transcribed, and each has the command inline: the base-image digest (Task 21, resolved into the heredoc so nothing fake is ever committed), the artifact digest in `MODEL_CARD.md` (Task 5, `sha256sum`), and the measured latency numbers (Task 20, written by the test). Two tasks (16, 18) deliberately have no new implementation because they assert behaviour built earlier; each names the exact temporary mutation that proves the test bites, which is the alternative to a test that can pass for the wrong reason.

**Type consistency.** `LABELS` is the only label source, reaching `probs_to_dict`, `decide`, `insert_prediction`, and the response builder unchanged. `probs: dict[str, float]` flows `probs_to_dict → decide → PredictionRow.probs → prob_*` columns with no re-derivation. `PredictionRow` / `ReviewIntent` / `PendingWrite` are the single currency of the persistence path and cross the spool boundary by explicit encode/decode rather than by `asdict` round-tripping datetimes. `LoadedModel.model_version` (full) reaches `predictions.model_version` and the log; `LoadedModel.public_version` (opaque) reaches `PredictionResponse.model_version` and `/health`, and the two are asserted distinct in Task 7 and asserted absent from every public response in Tasks 15 and 18. `DecisionResult.decision` is one of `allow | review | block`, matching both the `PredictionResponse` `Literal` and the `ck_predictions_decision` check constraint. `Settings` is frozen and threaded through `create_app`; no module reads `os.environ` except `load_settings`.

**Known residual, stated rather than hidden.** A chunked request with no `Content-Length` is refused with 411 rather than streamed and measured, which is complete but blunt — a legitimate chunked client cannot call `/predict`. That is acceptable because the only clients are the Streamlit frontend, `make seed-demo`, and `curl`, all of which send `Content-Length`. Closing it gracefully needs the reverse proxy that H15 also wants, and that is Phase A2 or an accepted documented decision. The spool survives process crash and instance reboot but not volume destruction, so a drain runs before `terraform destroy`; that step belongs to Phase 5's teardown path and is named here so it is not discovered there.

## Execution Handoff

Two options:
1. **Subagent-Driven (recommended):** fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.

**Needs before Task 5:** the Phase 1 promoted `toxic-clf` skops artifact and its `thresholds.json`, fetched by digest. Tasks 1–4 and 9–14 have no Phase 1 dependency and can run first if Phase 1 slips.
