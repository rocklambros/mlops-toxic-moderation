# Phase 1: Train, Calibrate, Tune, Evaluate, Register Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One registered, calibrated, threshold-tuned classical model promoted to a **publicly visible** W&B Registry stage, with per-label metrics and stratified bootstrap confidence intervals, a per-identity-term fairness slice, a measured memory footprint, a `baseline_flag_rates.json` drift reference for the Phase 3 dashboard, and a held-out test set touched exactly once under a durable git-tracked guard.

**Architecture:** Build-time only, offline, CPU, on the aarch64 Jetson. Phase 0 hands over a `DatasetBundle` (deduped `train_df`, locked 15% `test_df`, five stratified fold index pairs, and a `data_version`). Phase 1 fits TF-IDF **inside** the sklearn `Pipeline` **inside** each fold, so the vectorizer never sees a validation or test row. Calibration is cross-fitted **inside** the one-vs-rest wrapper on each fold's training rows only; thresholds are tuned on the out-of-fold probabilities those models produce; the held-out test set is evaluated once, at the end, on the single model cross-validation already chose. Nothing in this phase reaches AWS. W&B receives runs, metrics, and artifacts. RunPod is build-time GPU only and sits behind the day-8 cut-line.

**Tech Stack:** Python 3.11, scikit-learn 1.5.2, numpy, pandas, scipy, skops 0.11.0, iterative-stratification, pydantic 2.9.2, wandb 0.25.1, httpx, pytest 8.3.3, ruff 0.7.4.

## Global Constraints

Every task inherits these. They are copied from `docs/superpowers/specs/2026-07-30-delivery-plan-design.md` (governs on conflict), `docs/2026-07-01-toxic-moderation-mlops-design.md`, and `docs/week9_FinalProject.md` (the rubric).

- **Labels, ordered, single source of truth:** `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`. Import `LABELS` from `model/labels.py`; never re-type the tuple.
- **`solver='liblinear'`.** `saga` is forbidden (§6.2, premortem C3). Convergence is asserted in the test suite.
- **Calibration nests inside one-vs-rest:** `OneVsRestClassifier(CalibratedClassifierCV(LogisticRegression(...), cv=5, method='sigmoid'))`. The outer nesting raises `ValueError` (§6.2, premortem C4). `method='sigmoid'`, never `isotonic`, because `threat` has roughly 80 per-fold positives.
- **Calibration folds are disjoint from threshold-tuning folds.** Thresholds are tuned only on out-of-fold probabilities produced by models that never saw those rows.
- **Both vectorizers are capped:** word `max_features=200_000`, char `max_features=100_000`. The resulting memory is **measured**, not assumed, and written to `docs/feature-footprint.md`.
- **The held-out test set evaluates the single model cross-validation already chose.** It never *chooses* between classical and DistilBERT. The once-only guard is a git-tracked ledger at `docs/test-set-touch-log.md`, keyed on `data_version`, that refuses a second entry. It is durable across a fresh interpreter, a fresh clone, and an ephemeral RunPod pod.
- **Headline metrics are macro-F1 and per-label PR-AUC with stratified bootstrap confidence intervals.** `accuracy` **is** logged per run and shown on the dashboard, because rubric 1.2 and 3.2 name it — and is **never** a promotion or comparison metric. That ban is enforced by a function that raises, not by a comment.
- **Confidence intervals use a stratified bootstrap** that preserves the positive count in every resample, so a small slice can never produce a zero-positive resample that silently scores 0.0.
- **Safe serialization only.** `skops.io.dump` for the classical model. Never pickle, never joblib. `trusted=True` was removed from `skops.io.load`; Phase 1 emits an explicit static type allowlist for Phase 2.
- **Raw comment text never reaches W&B or any log.** Only the access-restricted RDS row (Phase 2) holds it.
- **The W&B Registry page is publicly visible showing a promoted stage** (owner decision 2026-07-31, delivery-spec §11 and §13). The white-box evasion this exposes is accepted residual risk, disclosed in the model card. Do not create a private artifact project.
- **Seed hygiene.** `set_all_seeds(seed)` before any fit; `run_metadata()` supplies the git SHA. `PYTHONHASHSEED=0` is set by the Makefile.
- **Git workflow.** Feature branch, PR to `main`, one small commit per task. Human author (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or docs.

**Branch:** `feat/phase-1-train-register` off `main`.

## File Structure

Created by this phase:

- `requirements/train.txt` — pinned Phase 1 dependencies.
- `tests/fixtures/synthetic.py` — deterministic synthetic multi-label corpus builder.
- `model/pipeline.py` — `build_classical_pipeline`, `inner_logistic_regressions`, `assert_converged`, `measure_feature_footprint`, `assert_feature_budget`.
- `model/calibration.py` — `reliability`, `calibration_gain`.
- `model/oof.py` — `OofPredictions`, `cross_val_probabilities`.
- `model/metrics.py` — `compute_metrics`, `stratified_bootstrap_ci`, `proportion_ci`, `select_best_run`.
- `model/thresholds.py` — `tune_thresholds`, `write_thresholds`.
- `model/ledger.py` — the durable once-only held-out guard.
- `model/evaluate.py` — `evaluate_on_test`.
- `model/fairness.py` — `IDENTITY_TERMS`, `term_mask`, `identity_fairness_report`, `render_fairness_markdown`.
- `model/baseline_rates.py` — `BaselineFlagRates`, `compute_baseline_flag_rates`.
- `model/tracking.py` — `build_run_config`, `build_run_summary`, `assert_no_raw_text`, `log_run`.
- `model/registry.py` — `register_and_promote`, `check_public_registry`.
- `model/trusted_types.py` — the explicit static skops allowlist consumed by Phase 2.
- `model/model_card.py` — `render_model_card`.
- `model/train_classical.py` — the `make train` CLI entrypoint.
- `infra/runpod/pods.py`, `infra/runpod/reap.py`, `.github/workflows/runpod-reaper.yml` — cut-line, Task 18 only.
- `docs/test-set-touch-log.md`, `docs/feature-footprint.md`, `docs/fairness-report.md`, `MODEL_CARD.md`.
- `artifacts/` (gitignored) — `toxic-clf.skops`, `thresholds.json`, `baseline_flag_rates.json`, `metrics.json`.
- `tests/unit/test_pipeline.py`, `test_calibration.py`, `test_oof.py`, `test_features.py`, `test_metrics.py`, `test_thresholds.py`, `test_ledger.py`, `test_evaluate.py`, `test_fairness.py`, `test_baseline_rates.py`, `test_tracking.py`, `test_registry.py`, `test_model_card.py`, `test_contract_adapter.py`, `test_runpod_reap.py`.
- `tests/perf/test_fit_budget.py` — deselected from `make test`, run at the Phase 1 gate.

Edited by this phase: `pyproject.toml` (a `perf` marker), `Makefile` (Phase 1 targets), `model/contract.py` (the `probs_to_dict` adapter), `.gitignore` (`artifacts/`).

## Interfaces Produced (consumed by Phase 2+)

```python
# model/contract.py  (added in Phase 1, premortem H23)
def probs_to_dict(row: "np.ndarray") -> dict[str, float]: ...   # the single authoritative adapter

# model/pipeline.py
WORD_MAX_FEATURES: int = 200_000
CHAR_MAX_FEATURES: int = 100_000
def build_classical_pipeline(*, word_max_features=..., char_max_features=..., C=1.0,
                             calibration_folds=5, method="sigmoid", max_iter=1000,
                             seed=42) -> "Pipeline": ...
def assert_converged(fitted: "Pipeline") -> None: ...            # raises ConvergenceError

# model/oof.py
@dataclass(frozen=True)
class OofPredictions:
    y_true: "np.ndarray"      # (n, 6) int
    y_prob: "np.ndarray"      # (n, 6) float, every row scored by a model that never saw it
    row_fold: "np.ndarray"    # (n,) int, which fold scored each row
    data_version: str

# model/thresholds.py
def tune_thresholds(oof: OofPredictions, *, recall_weights=...) -> dict[str, float]: ...
# thresholds.json artifact shape: {label: float} for each label in LABELS, in LABELS order

# model/evaluate.py
def evaluate_on_test(*, bundle, model, thresholds, git_sha, run_id,
                     ledger_path=Path("docs/test-set-touch-log.md")) -> dict: ...
# raises TestSetAlreadyTouched on a second call for the same data_version

# model/baseline_rates.py — the Phase 3 target-drift reference distribution
class BaselineFlagRates(BaseModel):
    data_version: str
    model_version: str
    model_digest: str
    n_test: int
    thresholds: dict[str, float]
    flag_rates: dict[str, float]      # keys == LABELS, per-label flag rate on the held-out test
    generated_at_utc: str

# model/trusted_types.py — consumed by backend/model_loader.py in Phase 2
TRUSTED_TYPES: tuple[str, ...]

# artifacts handed to Phase 2 / Phase 5
#   toxic-clf.skops           W&B artifact "toxic-clf", promoted alias "production"
#   MODEL_DIGEST              sha256 of the .skops file, recorded in git-committed MODEL_CARD.md
#   thresholds.json           {label: float}
#   baseline_flag_rates.json  W&B artifact "baseline-flag-rates"
```

**Corrections to the master plan's Interface Contracts block (premortem H24).** The master plan is authoritative for type seams but has drifted. Where this phase touches it, the following is correct and the master plan is wrong:

| Master plan says | Correct as of the hardened Phase 0 |
|---|---|
| `data_version: str  # sha256 over sorted deduped ids + config` | `data_version` is sha256 over the **realized split** (train/test/fold membership), a **per-id label fingerprint**, the split config, **and the pinned versions of numpy, scikit-learn, and iterative-stratification**. Hashing only surviving ids collides silently when labels change or a split library is bumped |
| `prepare_dataset(raw_csv: Path, config: SplitConfig) -> DatasetBundle` with no default | `prepare_dataset(raw_csv: Path, config: SplitConfig = SplitConfig()) -> DatasetBundle` |
| `make_splits(df, config)` | `make_splits(df, seed: int, test_size: float = 0.15, n_folds: int = 5)` — it takes the fields, not the config object |
| A single authoritative array to dict adapter is mandated but unnamed | `model.contract.probs_to_dict(row: np.ndarray) -> dict[str, float]`, added by Task 1 |

## Premortem finding coverage

Every row below has an owning task whose test fails if the finding is unfixed.

| Finding | Owning task |
|---|---|
| **C3** `solver='saga'` non-convergent and ~220x slower | Task 2 (solver pinned), Task 3 (convergence asserted), Task 17 perf gate |
| **C4** `CalibratedClassifierCV(OneVsRestClassifier(...))` raises `ValueError` | Task 4 (nesting), Task 5 (calibration measurably helps), Task 6 (disjoint folds) |
| **C11 / H11 (cap)** uncapped vectorizers reach ~4.7M features and ~1.7 GB at 135k rows | Task 7 |
| **H11 (registry)** rubric 1.3 needs a *visible* promotion; the checklist verified only the project | Task 15 |
| **H31** zero fairness measurement; `SECURITY.md` cites a `MODEL_CARD.md` that does not exist | Task 12, Task 16 |
| **C5 (drift reference)** the target-drift panel has no baseline distribution to drift *from* | Task 13 |
| **H13** white-box evasion via the public registry (accepted, must be disclosed) | Task 16 |
| **H14** the artifact digest must be recorded independently of the artifact | Task 16 |
| **H23** the array to dict adapter has no name, signature, or file | Task 1 |
| **H24** the Interface Contracts block has drifted in five places | This document's Interfaces section |
| **§6.1 held-out discipline** once-only guard must be durable, not a module-level flag | Task 11 |
| **§6.2 confidence intervals** stratified bootstrap that survives zero-positive resamples | Task 8 |
| **§6.2 accuracy** logged and displayed, never promoted on | Task 9 |
| **§6.3 safe serialization** explicit static skops allowlist, no `get-then-trust-all` | Task 16 |
| **§10 RunPod governance** atomic registry write, `trap EXIT`, name guard, orphan-safe, dry-run default | Task 18 |

**Note on the two id collisions.** The premortem numbers the `max_features` cap as `C11` inside C3's cross-attack, while its Critical list numbers `C11` as the static-credential finding and its High table numbers `H11` as the registry-visibility finding. This plan owns the `max_features` cap (Task 7) and the registry visibility (Task 15) and cites both ids so the mapping is auditable. The static-credential `C11` belongs to Phase A.

---

### Task 1: Phase 1 scaffold and the authoritative array to dict adapter [H23]

**Files:**
- Create: `requirements/train.txt`
- Edit: `pyproject.toml`, `Makefile`, `.gitignore`, `model/contract.py`
- Test: `tests/unit/test_contract_adapter.py`

**Interfaces produced:** `probs_to_dict(row) -> dict[str, float]`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_contract_adapter.py`:
```python
import numpy as np
import pytest

from model.contract import probs_to_dict
from model.labels import LABELS


def test_adapter_maps_positionally_in_labels_order():
    row = np.array([0.9, 0.1, 0.2, 0.3, 0.4, 0.5])
    out = probs_to_dict(row)
    assert list(out.keys()) == list(LABELS)
    assert out["toxic"] == pytest.approx(0.9)
    assert out["identity_hate"] == pytest.approx(0.5)


def test_adapter_rejects_wrong_length():
    with pytest.raises(ValueError, match="expected 6"):
        probs_to_dict(np.array([0.1, 0.2, 0.3]))


def test_adapter_rejects_a_two_dimensional_row():
    with pytest.raises(ValueError, match="1-D"):
        probs_to_dict(np.zeros((2, 6)))


def test_adapter_returns_plain_floats_not_numpy_scalars():
    out = probs_to_dict(np.float32([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]))
    assert all(type(v) is float for v in out.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_contract_adapter.py -v`
Expected: FAIL with `ImportError: cannot import name 'probs_to_dict' from 'model.contract'`

- [ ] **Step 3: Write minimal implementation**

> **Correction, 2026-07-31 (H23 recurring).** Phase 0 v2 Task 12 **already ships this function**, with exactly the body below. So does Phase 2 Task 1, with a third body and a third error message. All three said "Append to `model/contract.py`"; Python keeps the last `def`, so whichever phase lands last silently redefines the adapter for the two that landed earlier and their `pytest.raises(match=...)` cases go red untouched. **Phase 0 owns this function. Do not redefine it — verify it, and delete any local copy.** The body below is the canonical one and matches what Phase 0 now ships. Phase 4 Task 11's `test_probs_to_dict_is_defined_exactly_once` and `test_the_canonical_adapter_raises_both_documented_messages` are the guards.

Verify `model/contract.py` contains (do not append a second definition):
```python
def probs_to_dict(row: "np.ndarray") -> dict[str, float]:
    """The single authoritative (n, 6) row to per-label dict adapter.

    The API, the re-scorer, and the DB layer all call this. Independent ``zip(LABELS, row)``
    re-derivations mislabel probabilities silently if the column order ever drifts, and the
    output contract's key-membership validator is order-blind, so a transposition would be
    invisible to every test (premortem H23).
    """
    arr = np.asarray(row, dtype=float)
    if arr.ndim != 1:
        raise ValueError(f"probs_to_dict takes a 1-D row, got shape {arr.shape}")
    if arr.shape[0] != len(LABELS):
        raise ValueError(f"expected {len(LABELS)} probabilities, got {arr.shape[0]}")
    return {label: float(arr[i]) for i, label in enumerate(LABELS)}
```

Add `import numpy as np` to the imports at the top of `model/contract.py`.

`requirements/train.txt`:
```
-r base.txt
wandb==0.25.1
httpx==0.27.2
```

`pyproject.toml`, replace the `markers` line:
```toml
markers = [
    "integration: needs external services (deselect with -m 'not integration')",
    "perf: wall-clock budget checks (deselect with -m 'not perf')",
]
```

`Makefile`, replace the `test` recipe and append the Phase 1 targets:
```makefile
test:
	PYTHONHASHSEED=0 $(BIN)/pytest -m "not integration and not perf"
perf:
	PYTHONHASHSEED=0 $(BIN)/pytest -m perf -v
train:
	PYTHONHASHSEED=0 $(BIN)/python -m model.train_classical --csv $(JIGSAW_CSV)
footprint:
	PYTHONHASHSEED=0 $(BIN)/python -m model.train_classical --csv $(JIGSAW_CSV) --footprint-only
verify-registry:
	$(BIN)/python -m model.registry --entity $(WANDB_ENTITY)
```

Add `JIGSAW_CSV ?= data/raw/jigsaw-toxic-comment-train.csv` and `WANDB_ENTITY ?= rocklambros` near the top of the `Makefile`, and add `train perf footprint verify-registry` to the `.PHONY` line.

Append to `.gitignore`:
```
artifacts/
data/raw/
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pip install -r requirements/train.txt && PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_contract_adapter.py -v && .venv/bin/ruff check .`
Expected: `4 passed`, ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add requirements/train.txt pyproject.toml Makefile .gitignore model/contract.py tests/unit/test_contract_adapter.py
git commit -m "Add Phase 1 scaffold and the authoritative probability-row adapter"
```

---

### Task 2: Classical pipeline factory with capped vectorizers and liblinear [C3]

**Files:**
- Create: `model/pipeline.py`, `tests/fixtures/synthetic.py`
- Test: `tests/unit/test_pipeline.py`

**Interfaces produced:** `build_classical_pipeline`, `WORD_MAX_FEATURES`, `CHAR_MAX_FEATURES`

- [ ] **Step 1: Write the failing test**

`tests/fixtures/synthetic.py`:
```python
"""Deterministic synthetic multi-label corpus for Phase 1 unit tests.

Real Jigsaw is ~135k rows and lives outside the repo (it is gitignored under data/raw/).
These tests need a corpus that is small, seeded, learnable, and carries the same shape of
imbalance as the real thing, including a rare `threat`-like label at a few percent.
"""

import numpy as np

from model.labels import LABELS

_CLEAN = (
    "thanks for the edit",
    "great work on the article",
    "i disagree politely",
    "nice sourcing here",
    "the weather is lovely",
    "please add a citation",
)
_CUES = {
    "toxic": "idiot",
    "severe_toxic": "vile",
    "obscene": "filth",
    "threat": "killyou",
    "insult": "moron",
    "identity_hate": "yourkind",
}
_RATES = {
    "toxic": 0.30,
    "severe_toxic": 0.10,
    "obscene": 0.20,
    "threat": 0.04,
    "insult": 0.25,
    "identity_hate": 0.08,
}


def make_corpus(n: int = 800, seed: int = 0) -> tuple[list[str], np.ndarray]:
    rng = np.random.default_rng(seed)
    texts: list[str] = []
    y = np.zeros((n, len(LABELS)), dtype=int)
    for i in range(n):
        parts = [_CLEAN[i % len(_CLEAN)], f"comment {i}"]
        for j, label in enumerate(LABELS):
            if rng.random() < _RATES[label]:
                y[i, j] = 1
                parts.append(_CUES[label])
        rng.shuffle(parts)
        texts.append(" ".join(parts))
    return texts, y
```

`tests/unit/test_pipeline.py`:
```python
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from model.labels import LABELS
from model.pipeline import (
    CHAR_MAX_FEATURES,
    WORD_MAX_FEATURES,
    build_classical_pipeline,
)
from tests.fixtures.synthetic import make_corpus


def _vectorizers(pipe):
    return dict(pipe.named_steps["features"].transformer_list)


def test_factory_returns_a_pipeline_with_the_vectorizers_inside_it():
    pipe = build_classical_pipeline()
    assert isinstance(pipe, Pipeline)
    vecs = _vectorizers(pipe)
    assert isinstance(vecs["word"], TfidfVectorizer)
    assert isinstance(vecs["char"], TfidfVectorizer)
    # Not fitted at construction time: the vocabulary and IDF are learned inside each CV fold,
    # which is the classic silent leak this asserts against.
    assert not hasattr(vecs["word"], "vocabulary_")
    assert not hasattr(vecs["char"], "vocabulary_")


def test_both_vectorizers_are_capped_at_the_documented_values():
    vecs = _vectorizers(build_classical_pipeline())
    assert vecs["word"].max_features == WORD_MAX_FEATURES == 200_000
    assert vecs["char"].max_features == CHAR_MAX_FEATURES == 100_000


def test_word_and_char_ngram_ranges_match_the_design():
    vecs = _vectorizers(build_classical_pipeline())
    assert vecs["word"].ngram_range == (1, 2)
    assert vecs["char"].analyzer == "char_wb"
    assert vecs["char"].ngram_range == (3, 5)


def test_solver_is_liblinear_and_never_saga():
    pipe = build_classical_pipeline()
    base = pipe.named_steps["clf"].estimator.estimator
    assert isinstance(base, LogisticRegression)
    assert base.solver == "liblinear", (
        "saga was measured at 493 s for n=15,000 while hitting max_iter without converging, "
        "against 5.7 s for liblinear converging in 6 iterations (premortem C3)"
    )
    assert base.class_weight == "balanced"


def test_fitted_pipeline_emits_six_calibrated_columns():
    texts, y = make_corpus()
    pipe = build_classical_pipeline().fit(texts, y)
    probs = pipe.predict_proba(texts)
    assert probs.shape == (len(texts), len(LABELS))
    assert probs.min() >= 0.0 and probs.max() <= 1.0
    assert np.isfinite(probs).all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.pipeline'`

- [ ] **Step 3: Write minimal implementation**

`model/pipeline.py`:
```python
"""Classical TF-IDF + one-vs-rest calibrated logistic regression pipeline.

Two normative constraints from the delivery spec section 6.2 live here, and both were
established by measurement rather than by preference:

- ``solver='liblinear'``. ``saga`` was measured on this build box at 493 s for n=15,000 while
  hitting ``max_iter=1000`` WITHOUT converging, against 5.7 s for liblinear converging in 6
  iterations. Extrapolated to six labels by five folds at 135k rows that is roughly 37 hours
  against a two-day budget, and the result would still carry a ConvergenceWarning.
- ``max_features`` caps on both vectorizers. Uncapped, the word vectorizer reaches ~4.7M
  features and a ~1.7 GB design matrix at 135k rows, which does not fit the 4 GB EC2 #1.
  The real number is measured by ``measure_feature_footprint``; these caps are the starting
  point, not the answer.

Calibration nests INSIDE the one-vs-rest wrapper. The outer nesting
``CalibratedClassifierCV(OneVsRestClassifier(...))`` raises
``ValueError: y should be a 1d array, got an array of shape (n, 6) instead.`` because
``CalibratedClassifierCV.fit`` calls ``LabelEncoder().fit(y)``.
"""

from sklearn.calibration import CalibratedClassifierCV
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import FeatureUnion, Pipeline

WORD_MAX_FEATURES: int = 200_000
CHAR_MAX_FEATURES: int = 100_000
MAX_ITER: int = 1000
CALIBRATION_FOLDS: int = 5
SOLVER: str = "liblinear"
CALIBRATION_METHOD: str = "sigmoid"


def build_classical_pipeline(
    *,
    word_max_features: int | None = WORD_MAX_FEATURES,
    char_max_features: int | None = CHAR_MAX_FEATURES,
    C: float = 1.0,
    calibration_folds: int = CALIBRATION_FOLDS,
    method: str = CALIBRATION_METHOD,
    max_iter: int = MAX_ITER,
    seed: int = 42,
) -> Pipeline:
    features = FeatureUnion(
        [
            (
                "word",
                TfidfVectorizer(
                    ngram_range=(1, 2),
                    min_df=2,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    max_features=word_max_features,
                ),
            ),
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    min_df=3,
                    sublinear_tf=True,
                    strip_accents="unicode",
                    max_features=char_max_features,
                ),
            ),
        ]
    )
    base = LogisticRegression(
        solver=SOLVER,
        class_weight="balanced",
        C=C,
        max_iter=max_iter,
        random_state=seed,
    )
    calibrated = CalibratedClassifierCV(base, cv=calibration_folds, method=method)
    # n_jobs=1 deliberately: each parallel job copies the sparse design matrix, and the
    # memory budget measured in Task 7 assumes one copy.
    return Pipeline([("features", features), ("clf", OneVsRestClassifier(calibrated, n_jobs=1))])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pipeline.py -v`
Expected: `5 passed` in roughly 3 seconds.

- [ ] **Step 5: Commit**

```bash
git add model/pipeline.py tests/fixtures/synthetic.py tests/unit/test_pipeline.py
git commit -m "Add classical pipeline factory with capped vectorizers and liblinear solver"
```

---

### Task 3: Convergence assertion and the no-ConvergenceWarning gate [C3]

**Files:**
- Edit: `model/pipeline.py`
- Test: `tests/unit/test_pipeline.py` (append), `tests/perf/test_fit_budget.py`

**Interfaces produced:** `assert_converged`, `inner_logistic_regressions`, `ConvergenceError`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_pipeline.py`:
```python
import warnings

import pytest
from sklearn.exceptions import ConvergenceWarning

from model.pipeline import ConvergenceError, assert_converged, inner_logistic_regressions


def test_inner_estimators_are_reachable_and_counted():
    texts, y = make_corpus(n=400)
    pipe = build_classical_pipeline(calibration_folds=3).fit(texts, y)
    inner = inner_logistic_regressions(pipe)
    # six labels x three calibration folds
    assert len(inner) == len(LABELS) * 3
    assert all(isinstance(lr, LogisticRegression) for lr in inner)


def test_fitted_pipeline_converges_and_emits_no_convergence_warning():
    texts, y = make_corpus()
    pipe = build_classical_pipeline()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        pipe.fit(texts, y)  # a saga-configured pipeline raises here instead of passing
    assert_converged(pipe)


def test_assert_converged_raises_when_iterations_hit_the_cap():
    texts, y = make_corpus(n=400)
    pipe = build_classical_pipeline(calibration_folds=3, max_iter=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(texts, y)
    with pytest.raises(ConvergenceError, match="hit max_iter"):
        assert_converged(pipe)
```

`tests/perf/test_fit_budget.py`:
```python
"""Wall-clock budget for one full pipeline fit. Deselected from `make test`, run at the gate.

This is the sharpest possible expression of premortem C3: the saga configuration measured
493 s at n=15,000 on this box, and liblinear measured 5.7 s. A 60 s ceiling at n=15,000
separates them by a factor of eight with room for a slower machine.
"""

import time

import pytest

from model.pipeline import build_classical_pipeline
from tests.fixtures.synthetic import make_corpus

FIT_BUDGET_SECONDS = 60.0
BENCH_ROWS = 15_000


@pytest.mark.perf
def test_single_full_fit_stays_inside_the_wall_clock_budget():
    texts, y = make_corpus(n=BENCH_ROWS, seed=3)
    pipe = build_classical_pipeline()
    started = time.perf_counter()
    pipe.fit(texts, y)
    elapsed = time.perf_counter() - started
    assert elapsed < FIT_BUDGET_SECONDS, (
        f"one fit took {elapsed:.1f}s at n={BENCH_ROWS}; the six-label five-fold run projects to "
        f"{elapsed * 5 * (135_000 / BENCH_ROWS) / 3600:.1f} h against a two-day phase budget"
    )
    print(f"\nfit {BENCH_ROWS} rows in {elapsed:.1f}s")
```

Create `tests/perf/__init__.py` (empty).

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pipeline.py -v`
Expected: FAIL with `ImportError: cannot import name 'ConvergenceError' from 'model.pipeline'`

- [ ] **Step 3: Write minimal implementation**

Append to `model/pipeline.py` (and add `import numpy as np` and `from sklearn.linear_model import LogisticRegression` to the imports if not already present):
```python
class ConvergenceError(RuntimeError):
    """A fitted inner estimator hit max_iter without converging."""


def inner_logistic_regressions(fitted: Pipeline) -> list[LogisticRegression]:
    """Every fitted base estimator inside the OvR-of-calibrated stack.

    Path: Pipeline["clf"] -> OneVsRestClassifier.estimators_ (one CalibratedClassifierCV per
    label) -> .calibrated_classifiers_ (one per calibration fold) -> .estimator. The
    ``.estimator`` attribute name is correct for the pinned scikit-learn 1.5.2; it was
    ``base_estimator`` before 1.2.
    """
    out: list[LogisticRegression] = []
    for calibrated in fitted.named_steps["clf"].estimators_:
        for per_fold in calibrated.calibrated_classifiers_:
            out.append(per_fold.estimator)
    return out


def assert_converged(fitted: Pipeline) -> None:
    """Raise unless every inner estimator converged before max_iter.

    A ConvergenceWarning-tainted Production artifact is a correctness failure, not a style
    complaint: the coefficients are wherever the optimiser happened to stop.
    """
    inner = inner_logistic_regressions(fitted)
    bad = []
    for i, lr in enumerate(inner):
        n_iter = int(np.max(np.atleast_1d(lr.n_iter_)))
        if n_iter >= lr.max_iter:
            bad.append((i, n_iter, lr.max_iter))
    if bad:
        raise ConvergenceError(
            f"{len(bad)} of {len(inner)} inner estimators hit max_iter without converging "
            f"(first: index={bad[0][0]} n_iter={bad[0][1]} max_iter={bad[0][2]}, solver={SOLVER})"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pipeline.py -v && PYTHONHASHSEED=0 .venv/bin/pytest tests/perf -m perf -v`
Expected: `8 passed` for the unit file, `1 passed` for the perf file with a printed fit time under 60 s.

- [ ] **Step 5: Commit**

```bash
git add model/pipeline.py tests/unit/test_pipeline.py tests/perf/__init__.py tests/perf/test_fit_budget.py
git commit -m "Assert solver convergence and add a wall-clock fit budget check"
```

---

### Task 4: Calibration nests inside one-vs-rest, sigmoid for rare labels [C4]

**Files:**
- Test: `tests/unit/test_calibration.py`

No implementation is needed — Task 2 already built the correct nesting. This task exists because
the premortem's central lesson is that a normative item without a failing test is a memo. These
tests pin the nesting so a future "simplify the wrapper" commit fails loudly instead of silently
voiding the output contract's central promise.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_calibration.py`:
```python
import numpy as np
import pytest
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier

from model.labels import LABELS
from model.pipeline import CALIBRATION_FOLDS, build_classical_pipeline
from tests.fixtures.synthetic import make_corpus


def test_the_outer_nesting_is_a_hard_crash_and_stays_documented():
    """CalibratedClassifierCV(OneVsRestClassifier(...)) cannot take a (n, 6) target.

    CalibratedClassifierCV.fit calls LabelEncoder().fit(y). Reproduced on the pinned
    scikit-learn 1.5.2 (premortem C4). This test pins the reason so nobody re-derives the
    'obvious' outer wrap at 2 a.m. and then drops calibration when it crashes.
    """
    rng = np.random.default_rng(0)
    x = rng.random((96, 10))
    y = (rng.random((96, len(LABELS))) > 0.7).astype(int)
    wrong = CalibratedClassifierCV(
        OneVsRestClassifier(LogisticRegression(solver="liblinear")), cv=3, method="sigmoid"
    )
    with pytest.raises(ValueError, match=r"y should be a 1d array, got an array of shape \(96, 6\)"):
        wrong.fit(x, y)


def test_the_shipped_nesting_is_calibration_inside_one_vs_rest():
    clf = build_classical_pipeline().named_steps["clf"]
    assert isinstance(clf, OneVsRestClassifier)
    assert isinstance(clf.estimator, CalibratedClassifierCV)
    assert isinstance(clf.estimator.estimator, LogisticRegression)


def test_calibration_method_is_sigmoid_not_isotonic():
    """`threat` carries roughly 80 per-fold positives; isotonic overfits that badly."""
    clf = build_classical_pipeline().named_steps["clf"]
    assert clf.estimator.method == "sigmoid"
    assert clf.estimator.cv == CALIBRATION_FOLDS == 5


def test_the_shipped_nesting_fits_a_six_column_target_and_returns_six_columns():
    texts, y = make_corpus(n=400)
    pipe = build_classical_pipeline(calibration_folds=3).fit(texts, y)
    probs = pipe.predict_proba(texts)
    assert probs.shape == (400, len(LABELS))
```

- [ ] **Step 2: Run test to verify it fails**

Before Task 2 exists this fails on import. To see it fail *for the right reason* right now,
temporarily change `model/pipeline.py`'s last line to the outer nesting and run the suite:

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_calibration.py -v`
Expected (with the outer nesting temporarily in place): FAIL on
`test_the_shipped_nesting_is_calibration_inside_one_vs_rest` with
`AssertionError: assert False` on `isinstance(clf, OneVsRestClassifier)`, and FAIL on
`test_the_shipped_nesting_fits_a_six_column_target_and_returns_six_columns` with
`ValueError: y should be a 1d array, got an array of shape (400, 6) instead.`

- [ ] **Step 3: Restore the correct nesting**

Revert `model/pipeline.py` to the Task 2 version. No new implementation code.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_calibration.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_calibration.py
git commit -m "Pin the calibration nesting against the multi-label ValueError"
```

---

### Task 5: Calibration must measurably improve reliability [C4]

**Files:**
- Create: `model/calibration.py`
- Test: `tests/unit/test_calibration.py` (append)

**Interfaces produced:** `reliability`, `calibration_gain`, `Reliability`

The premortem's predicted 2 a.m. repair for C4 is "drop calibration". These tests make that
repair fail, because they measure the thing calibration is for.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_calibration.py`:
```python
from sklearn.datasets import make_classification

from model.calibration import calibration_gain, reliability


def _uncalibrated_and_calibrated():
    """A deliberately miscalibrated baseline and its sigmoid-calibrated twin.

    class_weight='balanced' on a 5%-positive problem is exactly the configuration this project
    ships, and it systematically over-predicts the positive class. That is the miscalibration
    the policy layer would then threshold if calibration were dropped.
    """
    x, y = make_classification(
        n_samples=6000, n_features=40, n_informative=8, weights=[0.95, 0.05],
        random_state=0, flip_y=0.02,
    )
    x_tr, y_tr, x_te, y_te = x[:4000], y[:4000], x[4000:], y[4000:]
    kw = dict(solver="liblinear", class_weight="balanced", max_iter=1000, random_state=42)
    uncal = LogisticRegression(**kw).fit(x_tr, y_tr).predict_proba(x_te)[:, 1]
    cal = (
        CalibratedClassifierCV(LogisticRegression(**kw), cv=5, method="sigmoid")
        .fit(x_tr, y_tr)
        .predict_proba(x_te)[:, 1]
    )
    return y_te, uncal, cal


def test_reliability_reports_brier_and_ece_and_the_stratum_sizes():
    y = np.array([0, 0, 1, 1])
    r = reliability(y, np.array([0.0, 0.0, 1.0, 1.0]))
    assert r.brier == pytest.approx(0.0)
    assert r.ece == pytest.approx(0.0)
    assert r.n == 4 and r.n_pos == 2


def test_reliability_is_worst_for_a_confidently_wrong_predictor():
    y = np.array([0, 0, 1, 1])
    r = reliability(y, np.array([1.0, 1.0, 0.0, 0.0]))
    assert r.brier == pytest.approx(1.0)
    assert r.ece == pytest.approx(1.0)


def test_sigmoid_calibration_measurably_improves_brier_and_ece():
    y_te, uncal, cal = _uncalibrated_and_calibrated()
    gain = calibration_gain(y_te, uncal, cal)
    assert gain["brier_improved"] is True
    assert gain["ece_improved"] is True
    # Measured on the pinned stack: Brier 0.0903 -> 0.0284, ECE 0.1686 -> 0.0107.
    # The margins below are deliberately loose so a minor library bump does not go red.
    assert gain["brier_calibrated"] < 0.60 * gain["brier_uncalibrated"]
    assert gain["ece_calibrated"] < 0.25 * gain["ece_uncalibrated"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_calibration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.calibration'`

- [ ] **Step 3: Write minimal implementation**

`model/calibration.py`:
```python
"""Reliability measurement. Calibration must be shown to help, not assumed to.

The output contract promises calibrated probabilities and the moderation policy thresholds
them, so an uncalibrated score makes every threshold meaningless. This module is what turns
"we wrapped it in CalibratedClassifierCV" into evidence.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Reliability:
    brier: float
    ece: float
    n: int
    n_pos: int


def reliability(y_true, y_prob, *, n_bins: int = 10) -> Reliability:
    """Brier score and expected calibration error over equal-width probability bins."""
    y_true = np.asarray(y_true).astype(float).ravel()
    y_prob = np.asarray(y_prob, dtype=float).ravel()
    brier = float(np.mean((y_prob - y_true) ** 2))
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (y_prob > lo) & (y_prob <= hi) if lo > 0 else (y_prob >= lo) & (y_prob <= hi)
        if not mask.any():
            continue
        ece += (mask.sum() / len(y_prob)) * abs(y_true[mask].mean() - y_prob[mask].mean())
    return Reliability(brier=brier, ece=float(ece), n=len(y_true), n_pos=int(y_true.sum()))


def calibration_gain(y_true, p_uncal, p_cal, *, n_bins: int = 10) -> dict:
    """Side-by-side reliability for the uncalibrated and calibrated scores of one label."""
    before = reliability(y_true, p_uncal, n_bins=n_bins)
    after = reliability(y_true, p_cal, n_bins=n_bins)
    return {
        "brier_uncalibrated": before.brier,
        "brier_calibrated": after.brier,
        "ece_uncalibrated": before.ece,
        "ece_calibrated": after.ece,
        "brier_improved": bool(after.brier < before.brier),
        "ece_improved": bool(after.ece < before.ece),
        "n": before.n,
        "n_pos": before.n_pos,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_calibration.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add model/calibration.py tests/unit/test_calibration.py
git commit -m "Measure Brier and ECE so calibration must be shown to help"
```

---

### Task 6: Out-of-fold probabilities with calibration folds disjoint from tuning folds [C4]

**Files:**
- Create: `model/oof.py`
- Test: `tests/unit/test_oof.py`

**Interfaces produced:** `OofPredictions`, `cross_val_probabilities`

`CalibratedClassifierCV(cv=5)` cross-fits on whatever rows it is handed. Hand it the outer
fold's training rows and tune thresholds on the outer fold's validation rows, and the two sets
are disjoint by construction. This task builds that construction and proves it with a spy that
records exactly which rows each estimator saw.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_oof.py`:
```python
import numpy as np
import pytest

from model.labels import LABELS
from model.oof import OofPredictions, cross_val_probabilities


class _Spy:
    """Records the rows it was fit on and the rows it was asked to score."""

    fitted_on: list[set[str]] = []
    scored: list[set[str]] = []

    def fit(self, x, y):
        _Spy.fitted_on.append(set(x))
        return self

    def predict_proba(self, x):
        _Spy.scored.append(set(x))
        return np.tile(np.linspace(0.1, 0.9, len(LABELS)), (len(x), 1))


def _four_folds(n=20):
    idx = np.arange(n)
    return [(idx[idx % 4 != k], idx[idx % 4 == k]) for k in range(4)]


def _reset_spy():
    _Spy.fitted_on, _Spy.scored = [], []


def test_calibration_rows_are_disjoint_from_threshold_tuning_rows():
    _reset_spy()
    texts = [f"row{i}" for i in range(20)]
    y = np.zeros((20, len(LABELS)), dtype=int)
    y[::3] = 1
    cross_val_probabilities(_Spy, texts, y, _four_folds(), "dv")
    assert len(_Spy.fitted_on) == 4
    for fitted, scored in zip(_Spy.fitted_on, _Spy.scored, strict=True):
        assert fitted.isdisjoint(scored), (
            "rows used to fit and calibrate leaked into the rows thresholds are tuned on; "
            "thresholds would be tuned on calibration-optimistic probabilities"
        )


def test_every_row_is_scored_exactly_once_by_a_model_that_never_saw_it():
    _reset_spy()
    texts = [f"row{i}" for i in range(20)]
    y = np.zeros((20, len(LABELS)), dtype=int)
    oof = cross_val_probabilities(_Spy, texts, y, _four_folds(), "dv")
    assert oof.y_prob.shape == (20, len(LABELS))
    assert np.isfinite(oof.y_prob).all()
    assert (oof.row_fold >= 0).all()
    assert sorted(set(oof.row_fold.tolist())) == [0, 1, 2, 3]
    assert oof.data_version == "dv"


def test_overlapping_fold_indices_are_rejected():
    _reset_spy()
    texts = [f"row{i}" for i in range(10)]
    y = np.zeros((10, len(LABELS)), dtype=int)
    bad = [(np.arange(10), np.arange(10))]
    with pytest.raises(ValueError, match="both the fit and the tuning set"):
        cross_val_probabilities(_Spy, texts, y, bad, "dv")


def test_a_row_missing_from_every_validation_fold_is_rejected():
    _reset_spy()
    texts = [f"row{i}" for i in range(10)]
    y = np.zeros((10, len(LABELS)), dtype=int)
    partial = [(np.arange(5, 10), np.arange(0, 5))]  # rows 5..9 never validated
    with pytest.raises(ValueError, match="never appeared in a validation fold"):
        cross_val_probabilities(_Spy, texts, y, partial, "dv")


def test_oof_predictions_is_frozen():
    oof = OofPredictions(
        y_true=np.zeros((2, len(LABELS)), dtype=int),
        y_prob=np.zeros((2, len(LABELS))),
        row_fold=np.zeros(2, dtype=int),
        data_version="dv",
    )
    with pytest.raises(Exception):
        oof.data_version = "tampered"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_oof.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.oof'`

- [ ] **Step 3: Write minimal implementation**

`model/oof.py`:
```python
"""Out-of-fold probabilities: the only sanctioned source of threshold-tuning data.

Every row's probability comes from a model fitted and calibrated on the other folds. That is
what keeps the calibration folds disjoint from the threshold-tuning folds (delivery spec
section 6.2). Passing raw arrays around instead would let the held-out test set be tuned on by
accident, so `tune_thresholds` accepts this type and nothing else.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from model.labels import LABELS


@dataclass(frozen=True)
class OofPredictions:
    y_true: np.ndarray      # (n, 6) int
    y_prob: np.ndarray      # (n, 6) float
    row_fold: np.ndarray    # (n,) int, which fold scored each row
    data_version: str


def cross_val_probabilities(
    factory: Callable[[], Any],
    texts,
    y,
    fold_indices,
    data_version: str,
) -> OofPredictions:
    """Fit a fresh estimator per fold and score only that fold's held-out rows.

    `factory` returns a brand-new unfitted estimator on every call, so no state crosses folds.
    """
    texts = list(texts)
    y = np.asarray(y).astype(int)
    n = len(texts)
    y_prob = np.full((n, len(LABELS)), np.nan, dtype=float)
    row_fold = np.full(n, -1, dtype=int)
    for k, (train_idx, val_idx) in enumerate(fold_indices):
        train_idx = np.asarray(train_idx)
        val_idx = np.asarray(val_idx)
        overlap = np.intersect1d(train_idx, val_idx)
        if overlap.size:
            raise ValueError(
                f"fold {k}: {overlap.size} rows are in both the fit and the tuning set"
            )
        estimator = factory()
        estimator.fit([texts[i] for i in train_idx], y[train_idx])
        y_prob[val_idx] = estimator.predict_proba([texts[i] for i in val_idx])
        row_fold[val_idx] = k
    unfilled = int((row_fold < 0).sum())
    if unfilled:
        raise ValueError(f"{unfilled} rows never appeared in a validation fold")
    return OofPredictions(
        y_true=y, y_prob=y_prob, row_fold=row_fold, data_version=data_version
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_oof.py -v`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add model/oof.py tests/unit/test_oof.py
git commit -m "Add out-of-fold scoring with calibration and tuning rows kept disjoint"
```

---

### Task 7: Measured feature footprint and the 4 GB memory budget [C11 / H11 cap]

**Files:**
- Edit: `model/pipeline.py`
- Test: `tests/unit/test_features.py`

**Interfaces produced:** `FeatureFootprint`, `measure_feature_footprint`, `assert_feature_budget`, `FeatureBudgetError`

The delivery spec says the caps are "both to be re-measured". This task is the measurement.
EC2 #1 is a `t4g.medium` with 4 GB, and the model, the interpreter, and uvicorn also live there,
so the design matrix budget is 2.0 GB, not 4.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_features.py`:
```python
import pytest

from model.pipeline import (
    CHAR_MAX_FEATURES,
    WORD_MAX_FEATURES,
    FeatureBudgetError,
    assert_feature_budget,
    measure_feature_footprint,
)
from tests.fixtures.synthetic import make_corpus


def test_footprint_reports_real_measured_bytes_not_an_estimate():
    texts, _ = make_corpus(n=600)
    fp = measure_feature_footprint(texts, word_max_features=50, char_max_features=30)
    assert fp.n_rows == 600
    assert fp.n_features == fp.n_word_features + fp.n_char_features
    assert fp.nnz > 0
    # CSR bytes are the sum of the three real arrays, so the number is measured, not modelled.
    assert fp.matrix_bytes >= fp.nnz * (8 + 4)
    assert fp.bytes_per_row == pytest.approx(fp.matrix_bytes / fp.n_rows)


def test_the_cap_is_load_bearing_and_binds_both_vectorizers():
    texts, _ = make_corpus(n=600)
    capped = measure_feature_footprint(texts, word_max_features=50, char_max_features=30)
    uncapped = measure_feature_footprint(texts, word_max_features=None, char_max_features=None)
    assert capped.n_word_features == 50
    assert capped.n_char_features == 30
    assert uncapped.n_features > capped.n_features, (
        "the corpus must exceed the cap for this test to prove the cap does anything"
    )


def test_default_caps_are_the_documented_values():
    assert WORD_MAX_FEATURES == 200_000
    assert CHAR_MAX_FEATURES == 100_000


def test_budget_check_raises_with_the_projection_in_the_message():
    texts, _ = make_corpus(n=600)
    fp = measure_feature_footprint(texts, word_max_features=50, char_max_features=30)
    assert assert_feature_budget(fp, n_rows_full=135_000, max_bytes=2_000_000_000) > 0
    with pytest.raises(FeatureBudgetError, match="exceeds the"):
        assert_feature_budget(fp, n_rows_full=135_000, max_bytes=1_000)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_features.py -v`
Expected: FAIL with `ImportError: cannot import name 'FeatureBudgetError' from 'model.pipeline'`

- [ ] **Step 3: Write minimal implementation**

Append to `model/pipeline.py` (add `from dataclasses import dataclass` to the imports):
```python
@dataclass(frozen=True)
class FeatureFootprint:
    n_rows: int
    n_word_features: int
    n_char_features: int
    n_features: int
    nnz: int
    matrix_bytes: int
    bytes_per_row: float


class FeatureBudgetError(RuntimeError):
    """The projected design matrix does not fit the target instance."""


def measure_feature_footprint(
    texts,
    *,
    word_max_features: int | None = WORD_MAX_FEATURES,
    char_max_features: int | None = CHAR_MAX_FEATURES,
) -> FeatureFootprint:
    """Fit only the FeatureUnion and report the real CSR byte counts.

    Uncapped, the word vectorizer reaches ~4.7M features and a ~1.7 GB matrix at 135k rows,
    which does not fit a 4 GB instance. This function is how that claim stops being folklore.
    """
    pipe = build_classical_pipeline(
        word_max_features=word_max_features, char_max_features=char_max_features
    )
    union = pipe.named_steps["features"]
    matrix = union.fit_transform(list(texts))
    word = union.transformer_list[0][1]
    char = union.transformer_list[1][1]
    nbytes = int(matrix.data.nbytes + matrix.indices.nbytes + matrix.indptr.nbytes)
    return FeatureFootprint(
        n_rows=matrix.shape[0],
        n_word_features=len(word.vocabulary_),
        n_char_features=len(char.vocabulary_),
        n_features=matrix.shape[1],
        nnz=int(matrix.nnz),
        matrix_bytes=nbytes,
        bytes_per_row=nbytes / matrix.shape[0],
    )


def assert_feature_budget(
    footprint: FeatureFootprint, *, n_rows_full: int, max_bytes: int
) -> int:
    """Project the measured bytes-per-row to the full corpus and enforce the budget."""
    projected = int(footprint.bytes_per_row * n_rows_full)
    if projected > max_bytes:
        raise FeatureBudgetError(
            f"projected design matrix {projected / 1e9:.2f} GB at {n_rows_full} rows exceeds the "
            f"{max_bytes / 1e9:.2f} GB budget (measured {footprint.bytes_per_row:.0f} B/row over "
            f"{footprint.n_features} features); lower max_features"
        )
    return projected
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_features.py -v`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add model/pipeline.py tests/unit/test_features.py
git commit -m "Measure the TF-IDF memory footprint and enforce an instance budget"
```

---

### Task 8: Stratified bootstrap confidence intervals that survive zero-positive resamples

**Files:**
- Create: `model/metrics.py`
- Test: `tests/unit/test_metrics.py`

**Interfaces produced:** `CIResult`, `stratified_bootstrap_ci`, `proportion_ci`

Measured on this box: at n=120 with 4 positives, **38 of 2000** naive resamples contain zero
positives, and `average_precision_score` on those returns **0.0 with only a `UserWarning`**. It
does not crash — it silently drags the interval's lower bound to the floor, which is worse,
because a promote decision then happens inside noise. That is the exact shape of the small
strata this project has: `threat`, and every identity-term fairness slice in Task 12.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_metrics.py`:
```python
import warnings

import numpy as np
import pytest
from sklearn.metrics import average_precision_score

from model.metrics import proportion_ci, stratified_bootstrap_ci


def _small_slice(seed=0):
    rng = np.random.default_rng(seed)
    n, n_pos = 120, 4
    y = np.zeros(n, dtype=int)
    y[:n_pos] = 1
    scores = rng.random(n)
    scores[:n_pos] += 0.6
    return y, scores


def test_naive_bootstrap_silently_produces_zero_positive_resamples():
    """The failure this task exists to remove. Documented, not fixed here."""
    y, scores = _small_slice()
    rng = np.random.default_rng(1)
    zero_positive = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for _ in range(2000):
            idx = rng.integers(0, len(y), len(y))
            if y[idx].sum() == 0:
                zero_positive += 1
                assert average_precision_score(y[idx], scores[idx]) == 0.0
    assert zero_positive > 0, "expected the naive resampler to lose all positives sometimes"


def test_stratified_resamples_always_retain_every_positive():
    y, scores = _small_slice()
    seen = []

    def spy(y_true, y_score):
        seen.append(int(y_true.sum()))
        return float(average_precision_score(y_true, y_score))

    stratified_bootstrap_ci(y, scores, spy, n_boot=500, seed=7)
    assert min(seen) == 4, "a resample lost positives; the stratification is not doing its job"
    assert len(seen) == 501  # 500 resamples plus the point estimate


def test_interval_is_ordered_and_reports_the_strata():
    y, scores = _small_slice()
    ci = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=500, seed=7)
    assert ci.lo is not None and ci.hi is not None
    assert ci.lo <= ci.hi
    assert ci.lo > 0.0, "a stratified lower bound can never be dragged to zero by an empty stratum"
    assert ci.n_pos == 4 and ci.n_neg == 116
    assert ci.low_power is True and "4 positives" in ci.reason


def test_a_label_with_no_positives_returns_low_power_and_does_not_raise():
    ci = stratified_bootstrap_ci(
        np.zeros(50, dtype=int), np.random.default_rng(0).random(50),
        average_precision_score, n_boot=100,
    )
    assert ci.lo is None and ci.hi is None
    assert ci.low_power is True
    assert ci.n_boot == 0
    assert "0 positives" in ci.reason


def test_intervals_are_deterministic_for_a_fixed_seed():
    y, scores = _small_slice()
    first = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=1)
    second = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=1)
    assert first == second


def test_a_well_powered_label_is_not_flagged_low_power():
    rng = np.random.default_rng(2)
    y = np.zeros(4000, dtype=int)
    y[:400] = 1
    scores = rng.random(4000)
    scores[:400] += 0.5
    ci = stratified_bootstrap_ci(y, scores, average_precision_score, n_boot=200, seed=3)
    assert ci.low_power is False and ci.reason is None


def test_proportion_ci_handles_an_empty_stratum():
    ci = proportion_ci(np.array([]), n_boot=50)
    assert ci.lo is None and ci.low_power is True and ci.reason == "empty stratum"


def test_proportion_ci_brackets_a_known_rate():
    ci = proportion_ci(np.array([1] * 30 + [0] * 70), n_boot=1000, seed=5)
    assert ci.point == pytest.approx(0.30)
    assert ci.lo < 0.30 < ci.hi
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.metrics'`

- [ ] **Step 3: Write minimal implementation**

`model/metrics.py`:
```python
"""Metrics and confidence intervals.

Two bootstrap flavours, because two different things break:

- ``stratified_bootstrap_ci`` resamples the positive and negative strata separately, so every
  resample carries exactly the observed number of positives. Naive resampling of a small slice
  loses all positives roughly 2% of the time at 4 positives in 120 rows, and
  ``average_precision_score`` then returns 0.0 with only a UserWarning — silently, not loudly.
- ``proportion_ci`` is the plain percentile bootstrap for a rate (selection rate, FPR, TPR),
  where the statistic is defined for any non-empty sample.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CIResult:
    point: float
    lo: float | None
    hi: float | None
    n_pos: int
    n_neg: int
    n_boot: int
    low_power: bool
    reason: str | None


def stratified_bootstrap_ci(
    y_true,
    y_score,
    metric_fn: Callable[[np.ndarray, np.ndarray], float],
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    min_positives: int = 1,
    low_power_below: int = 30,
) -> CIResult:
    y_true = np.asarray(y_true).astype(int).ravel()
    y_score = np.asarray(y_score, dtype=float).ravel()
    pos = np.flatnonzero(y_true == 1)
    neg = np.flatnonzero(y_true == 0)
    if len(pos) < min_positives:
        return CIResult(
            float("nan"), None, None, len(pos), len(neg), 0, True,
            f"only {len(pos)} positives; need at least {min_positives}",
        )
    point = float(metric_fn(y_true, y_score))
    rng = np.random.default_rng(seed)
    stats = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        idx = np.concatenate(
            [rng.choice(pos, len(pos), replace=True), rng.choice(neg, len(neg), replace=True)]
        )
        stats[b] = float(metric_fn(y_true[idx], y_score[idx]))
    lo, hi = (float(v) for v in np.quantile(stats, [alpha / 2.0, 1.0 - alpha / 2.0]))
    low_power = len(pos) < low_power_below
    return CIResult(
        point, lo, hi, len(pos), len(neg), n_boot, low_power,
        f"only {len(pos)} positives; the interval is wide" if low_power else None,
    )


def proportion_ci(
    indicator,
    *,
    n_boot: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    low_power_below: int = 30,
) -> CIResult:
    """Percentile bootstrap for a simple rate."""
    x = np.asarray(indicator, dtype=float).ravel()
    n = len(x)
    if n == 0:
        return CIResult(float("nan"), None, None, 0, 0, 0, True, "empty stratum")
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, n, size=(n_boot, n))
    stats = x[draws].mean(axis=1)
    lo, hi = (float(v) for v in np.quantile(stats, [alpha / 2.0, 1.0 - alpha / 2.0]))
    low_power = n < low_power_below
    return CIResult(
        float(x.mean()), lo, hi, int(x.sum()), int(n - x.sum()), n_boot, low_power,
        f"n={n} is below {low_power_below}" if low_power else None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_metrics.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add model/metrics.py tests/unit/test_metrics.py
git commit -m "Add stratified bootstrap intervals that preserve small positive strata"
```

---

### Task 9: Accuracy is logged and displayed, never promoted on

**Files:**
- Edit: `model/metrics.py`
- Test: `tests/unit/test_metrics.py` (append)

**Interfaces produced:** `compute_metrics`, `select_best_run`, `PROMOTION_METRIC`, `ForbiddenPromotionMetric`

Rubric 1.2 names accuracy in the experiment-tracking requirement and rubric 3.2 names live
accuracy on the dashboard, so it must exist. The design bans it as a *headline* metric because
an all-negative predictor scores about 90% on this corpus. Both facts are true at once, and the
ban is enforced by a function that raises rather than a sentence in a document.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_metrics.py`:
```python
from model.labels import LABELS
from model.metrics import (
    PROMOTION_METRIC,
    ForbiddenPromotionMetric,
    compute_metrics,
    select_best_run,
)


def _thresholds(value=0.5):
    return {label: value for label in LABELS}


def test_metrics_include_accuracy_for_rubric_1_2_and_3_2():
    rng = np.random.default_rng(0)
    y_true = (rng.random((400, len(LABELS))) > 0.85).astype(int)
    y_prob = rng.random((400, len(LABELS)))
    out = compute_metrics(y_true, y_prob, _thresholds())
    assert "accuracy" in out and "subset_accuracy" in out
    for label in LABELS:
        assert f"accuracy/{label}" in out
        assert f"f1/{label}" in out
        assert f"pr_auc/{label}" in out
        assert f"precision/{label}" in out
        assert f"recall/{label}" in out
    assert out["macro_f1"] == pytest.approx(
        float(np.mean([out[f"f1/{label}"] for label in LABELS]))
    )


def test_a_perfect_prediction_scores_one_everywhere():
    y_true = np.array([[1, 0, 1, 0, 1, 0], [0, 1, 0, 1, 0, 1]])
    y_prob = y_true.astype(float)
    out = compute_metrics(y_true, y_prob, _thresholds())
    assert out["macro_f1"] == pytest.approx(1.0)
    assert out["accuracy"] == pytest.approx(1.0)
    assert out["subset_accuracy"] == pytest.approx(1.0)


def test_promotion_on_accuracy_is_refused():
    with pytest.raises(ForbiddenPromotionMetric, match="banned as a promotion metric"):
        select_best_run([{"accuracy": 0.99}, {"accuracy": 0.98}], key="accuracy")
    with pytest.raises(ForbiddenPromotionMetric):
        select_best_run([{"accuracy/threat": 0.99}], key="accuracy/threat")
    with pytest.raises(ForbiddenPromotionMetric):
        select_best_run([{"subset_accuracy": 0.9}], key="subset_accuracy")


def test_promotion_on_macro_f1_is_the_default_and_works():
    assert PROMOTION_METRIC == "macro_f1"
    winner = select_best_run([{"macro_f1": 0.10}, {"macro_f1": 0.30}, {"macro_f1": 0.20}])
    assert winner["macro_f1"] == pytest.approx(0.30)


def test_selecting_from_no_runs_is_an_error_not_a_silent_none():
    with pytest.raises(ValueError, match="no runs"):
        select_best_run([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_metrics.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_metrics' from 'model.metrics'`

- [ ] **Step 3: Write minimal implementation**

Append to `model/metrics.py` (add the sklearn imports and the `LABELS` import at the top):
```python
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    f1_score,
    precision_score,
    recall_score,
)

from model.labels import LABELS

PROMOTION_METRIC = "macro_f1"
FORBIDDEN_PROMOTION_KEYS = frozenset({"accuracy", "subset_accuracy", "macro_accuracy"})


class ForbiddenPromotionMetric(ValueError):
    """Run selection was attempted on a metric the design bans for selection."""


def compute_metrics(y_true, y_prob, thresholds: dict[str, float]) -> dict:
    """Per-label and aggregate metrics at the supplied thresholds.

    accuracy is present on purpose: rubric 1.2 lists it among the metrics each run must log and
    rubric 3.2 puts live accuracy on the dashboard. select_best_run is what stops it becoming a
    decision input.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    thr = np.array([thresholds[label] for label in LABELS], dtype=float)
    y_flag = (y_prob >= thr).astype(int)
    out: dict[str, float] = {}
    for j, label in enumerate(LABELS):
        out[f"f1/{label}"] = float(f1_score(y_true[:, j], y_flag[:, j], zero_division=0))
        out[f"precision/{label}"] = float(
            precision_score(y_true[:, j], y_flag[:, j], zero_division=0)
        )
        out[f"recall/{label}"] = float(recall_score(y_true[:, j], y_flag[:, j], zero_division=0))
        out[f"pr_auc/{label}"] = float(average_precision_score(y_true[:, j], y_prob[:, j]))
        out[f"accuracy/{label}"] = float(accuracy_score(y_true[:, j], y_flag[:, j]))
    out["macro_f1"] = float(np.mean([out[f"f1/{label}"] for label in LABELS]))
    out["macro_pr_auc"] = float(np.mean([out[f"pr_auc/{label}"] for label in LABELS]))
    out["accuracy"] = float(np.mean([out[f"accuracy/{label}"] for label in LABELS]))
    out["subset_accuracy"] = float((y_flag == y_true).all(axis=1).mean())
    return out


def select_best_run(runs: list[dict], key: str = PROMOTION_METRIC) -> dict:
    """Pick the run to promote. Refuses to decide on accuracy."""
    if key in FORBIDDEN_PROMOTION_KEYS or key.startswith("accuracy"):
        raise ForbiddenPromotionMetric(
            f"{key!r} is logged for rubric 1.2 and 3.2 but is banned as a promotion metric: an "
            f"all-negative predictor scores about 90% on this corpus. Use {PROMOTION_METRIC!r}"
        )
    if not runs:
        raise ValueError("no runs to select from")
    return max(runs, key=lambda run: run[key])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_metrics.py -v`
Expected: `13 passed`

- [ ] **Step 5: Commit**

```bash
git add model/metrics.py tests/unit/test_metrics.py
git commit -m "Log accuracy for the rubric and refuse to promote on it"
```

---

### Task 10: Per-label thresholds tuned on out-of-fold data only, recall-weighted for rare labels

**Files:**
- Create: `model/thresholds.py`
- Test: `tests/unit/test_thresholds.py`

**Interfaces produced:** `RECALL_WEIGHTS`, `tune_thresholds`, `write_thresholds`

A missed `threat` is worse than a false flag on `toxic`, so the objective is F-beta with a
per-label beta rather than F1 everywhere. `tune_thresholds` accepts `OofPredictions` and nothing
else, which makes "tune on the test set" require a deliberate lie rather than a slip.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_thresholds.py`:
```python
import json

import numpy as np
import pytest

from model.labels import LABELS
from model.oof import OofPredictions
from model.thresholds import RECALL_WEIGHTS, tune_thresholds, write_thresholds


def _overlapping_oof(seed=11, n=6000):
    """Background and positive score distributions that genuinely overlap.

    Without overlap every beta picks the same separating threshold and the test proves nothing.
    """
    rng = np.random.default_rng(seed)
    y_true = np.zeros((n, len(LABELS)), dtype=int)
    y_prob = np.clip(rng.beta(2, 12, size=(n, len(LABELS))), 0.001, 0.999)
    for j, _label in enumerate(LABELS):
        n_pos = 60 if j == 3 else 600          # index 3 is `threat`
        idx = rng.choice(n, n_pos, replace=False)
        y_true[idx, j] = 1
        y_prob[idx, j] = np.clip(rng.beta(6, 5, size=n_pos), 0.001, 0.999)
    return OofPredictions(y_true, y_prob, np.zeros(n, dtype=int), "dv")


def test_thresholds_cover_every_label_in_order_and_are_probabilities():
    thresholds = tune_thresholds(_overlapping_oof())
    assert list(thresholds.keys()) == list(LABELS)
    assert all(0.0 < v < 1.0 for v in thresholds.values())


def test_rare_severe_labels_get_a_lower_threshold_than_symmetric_f1_would_pick():
    oof = _overlapping_oof()
    symmetric = tune_thresholds(oof, recall_weights={label: 1.0 for label in LABELS})
    asymmetric = tune_thresholds(oof)
    for label in ("threat", "severe_toxic", "identity_hate"):
        assert asymmetric[label] < symmetric[label], (
            f"{label} carries recall weight {RECALL_WEIGHTS[label]} and must flag more freely"
        )
    for label in ("toxic", "obscene", "insult"):
        assert asymmetric[label] == symmetric[label], f"{label} has weight 1.0 and must not move"


def test_recall_weights_name_every_label_and_prioritise_threat_most():
    assert set(RECALL_WEIGHTS) == set(LABELS)
    assert RECALL_WEIGHTS["threat"] == max(RECALL_WEIGHTS.values())
    assert RECALL_WEIGHTS["toxic"] == 1.0


def test_raw_arrays_are_refused_so_the_test_set_cannot_be_tuned_on():
    oof = _overlapping_oof()
    with pytest.raises(TypeError, match="only accepts OofPredictions"):
        tune_thresholds(oof.y_prob)


def test_tuning_is_deterministic():
    assert tune_thresholds(_overlapping_oof()) == tune_thresholds(_overlapping_oof())


def test_written_thresholds_round_trip_in_labels_order(tmp_path):
    thresholds = tune_thresholds(_overlapping_oof())
    path = tmp_path / "thresholds.json"
    write_thresholds(path, thresholds, data_version="dv")
    loaded = json.loads(path.read_text())
    assert list(loaded.keys()) == list(LABELS)
    assert loaded == thresholds
    meta = json.loads((tmp_path / "thresholds.meta.json").read_text())
    assert meta["data_version"] == "dv"


def test_writing_an_incomplete_threshold_map_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must equal"):
        write_thresholds(tmp_path / "t.json", {"toxic": 0.5}, data_version="dv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_thresholds.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.thresholds'`

- [ ] **Step 3: Write minimal implementation**

`model/thresholds.py`:
```python
"""Per-label threshold tuning on out-of-fold predictions only.

Toxicity is asymmetric-cost: a missed `threat` is worse than a false flag on `toxic`. The
objective is therefore F-beta with a per-label beta, not F1 everywhere. The held-out test set is
never an input here, and the OofPredictions type is what enforces that.
"""

import json
from pathlib import Path

import numpy as np

from model.labels import LABELS
from model.oof import OofPredictions

RECALL_WEIGHTS: dict[str, float] = {
    "toxic": 1.0,
    "severe_toxic": 3.0,
    "obscene": 1.0,
    "threat": 5.0,
    "insult": 1.0,
    "identity_hate": 3.0,
}
GRID = np.round(np.arange(0.05, 0.96, 0.01), 2)


def _f_beta(y_true: np.ndarray, y_flag: np.ndarray, beta: float) -> float:
    tp = float(((y_true == 1) & (y_flag == 1)).sum())
    fp = float(((y_true == 0) & (y_flag == 1)).sum())
    fn = float(((y_true == 1) & (y_flag == 0)).sum())
    if tp == 0.0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    b2 = beta * beta
    return (1.0 + b2) * precision * recall / (b2 * precision + recall)


def tune_thresholds(
    oof: OofPredictions, *, recall_weights: dict[str, float] = RECALL_WEIGHTS
) -> dict[str, float]:
    if not isinstance(oof, OofPredictions):
        raise TypeError(
            "tune_thresholds only accepts OofPredictions produced by cross_val_probabilities; "
            "accepting raw arrays would let the held-out test set be tuned on"
        )
    out: dict[str, float] = {}
    for j, label in enumerate(LABELS):
        beta = float(recall_weights[label])
        y_true = oof.y_true[:, j]
        y_prob = oof.y_prob[:, j]
        scores = [_f_beta(y_true, (y_prob >= t).astype(int), beta) for t in GRID]
        out[label] = float(GRID[int(np.argmax(scores))])
    return out


def write_thresholds(path: Path, thresholds: dict[str, float], *, data_version: str) -> None:
    if set(thresholds) != set(LABELS):
        raise ValueError(f"thresholds keys must equal {LABELS}")
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {label: float(thresholds[label]) for label in LABELS}
    path.write_text(json.dumps(ordered, indent=2) + "\n")
    (path.parent / "thresholds.meta.json").write_text(
        json.dumps({"data_version": data_version}, indent=2) + "\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_thresholds.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add model/thresholds.py tests/unit/test_thresholds.py
git commit -m "Tune per-label thresholds on out-of-fold data with asymmetric cost"
```

---

### Task 11: Durable git-tracked once-only guard for the held-out test set [§6.1]

**Files:**
- Create: `model/ledger.py`, `docs/test-set-touch-log.md`
- Test: `tests/unit/test_ledger.py`

**Interfaces produced:** `LEDGER_PATH`, `read_touched_versions`, `record_touch`, `assert_ledger_is_git_tracked`, `TestSetAlreadyTouched`, `LedgerNotTracked`

A module-level `_already_evaluated = False` guards nothing, because RunPod pods are ephemeral by
design and a fresh interpreter resets it. The guard is a **file in git**, keyed on `data_version`.
The test that proves it runs the guard twice in **two separate subprocesses**.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ledger.py`:
```python
import subprocess
import sys
import textwrap

import pytest

from model.ledger import (
    LEDGER_PATH,
    LedgerNotTracked,
    TestSetAlreadyTouched,
    assert_ledger_is_git_tracked,
    read_touched_versions,
    record_touch,
)

DV_A = "a" * 64
DV_B = "b" * 64


def test_a_fresh_ledger_has_touched_nothing(tmp_path):
    assert read_touched_versions(tmp_path / "absent.md") == set()


def test_first_touch_is_recorded_with_its_provenance(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", macro_f1=0.7412, path=path)
    assert read_touched_versions(path) == {DV_A}
    body = path.read_text()
    assert "9f1c" in body and "run-1" in body and "0.7412" in body


def test_second_touch_of_the_same_data_version_is_refused(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", macro_f1=0.74, path=path)
    with pytest.raises(TestSetAlreadyTouched, match="evaluated exactly once"):
        record_touch(DV_A, git_sha="9f1c", run_id="run-2", macro_f1=0.99, path=path)
    assert read_touched_versions(path) == {DV_A}


def test_a_new_data_version_is_allowed(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", macro_f1=0.74, path=path)
    record_touch(DV_B, git_sha="9f1c", run_id="run-2", macro_f1=0.75, path=path)
    assert read_touched_versions(path) == {DV_A, DV_B}


def test_the_guard_survives_a_fresh_interpreter(tmp_path):
    """The test a module-level flag cannot pass.

    Two separate Python processes. A boolean in module state resets between them; a git-tracked
    file does not. RunPod pods are ephemeral by design, so this is the realistic case.
    """
    path = tmp_path / "log.md"
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from model.ledger import TestSetAlreadyTouched, record_touch
        try:
            record_touch({DV_A!r}, git_sha="9f1c", run_id=sys.argv[1], macro_f1=0.5,
                         path=Path({str(path)!r}))
            print("WROTE")
        except TestSetAlreadyTouched:
            print("REFUSED")
        """
    )
    first = subprocess.run([sys.executable, "-c", script, "run-1"], capture_output=True, text=True)
    second = subprocess.run([sys.executable, "-c", script, "run-2"], capture_output=True, text=True)
    assert first.stdout.strip() == "WROTE", first.stderr
    assert second.stdout.strip() == "REFUSED", second.stderr


def test_the_header_row_is_not_mistaken_for_a_data_version(tmp_path):
    path = tmp_path / "log.md"
    record_touch(DV_A, git_sha="9f1c", run_id="run-1", macro_f1=0.74, path=path)
    assert read_touched_versions(path) == {DV_A}  # not {"data_version", DV_A}


def test_an_untracked_ledger_is_refused(tmp_path):
    with pytest.raises(LedgerNotTracked, match="not tracked by git"):
        assert_ledger_is_git_tracked(tmp_path / "scratch-copy.md")


def test_the_committed_ledger_is_tracked_by_git():
    assert_ledger_is_git_tracked(LEDGER_PATH)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.ledger'`

- [ ] **Step 3: Write minimal implementation**

`model/ledger.py`:
```python
"""Durable once-only guard for the held-out test set.

The held-out test set is evaluated once per data_version, on the single model cross-validation
already chose. A module-level boolean cannot enforce that, because RunPod pods are ephemeral by
design and every fresh interpreter starts with the flag clear. The guard is therefore a file
tracked in git: it survives a new process, a new pod, and a fresh clone, and a second entry for
the same data_version is refused.
"""

import datetime as dt
import re
import subprocess
from pathlib import Path

LEDGER_PATH = Path("docs/test-set-touch-log.md")

_HEADER = (
    "# Held-out test-set touch log\n"
    "\n"
    "The held-out test set is evaluated **once** per `data_version`, on the single model that\n"
    "cross-validation already chose. It never *chooses* between candidate models: picking the\n"
    "better of two test numbers is selection on the test set and biases the winner upward.\n"
    "\n"
    "This file is the guard. It is tracked in git, so it survives an ephemeral RunPod pod, a\n"
    "fresh interpreter, and a fresh clone. A second entry for the same `data_version` is refused\n"
    "by `model.ledger.record_touch`.\n"
    "\n"
    "| data_version | git_sha | run_id | touched_at_utc | macro_f1 |\n"
    "|---|---|---|---|---|\n"
)
_ROW = re.compile(r"^\|\s*([0-9a-f]{64})\s*\|", re.MULTILINE)


class TestSetAlreadyTouched(RuntimeError):
    """The held-out test set was already evaluated for this data_version."""


class LedgerNotTracked(RuntimeError):
    """The ledger is not tracked by git, so the guard would not be durable."""


def read_touched_versions(path: Path = LEDGER_PATH) -> set[str]:
    if not path.exists():
        return set()
    return set(_ROW.findall(path.read_text()))


def assert_ledger_is_git_tracked(path: Path = LEDGER_PATH) -> None:
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise LedgerNotTracked(
            f"{path} is not tracked by git, so the once-only guard would vanish with the pod. "
            f"Run: git add {path} && git commit"
        ) from exc


def record_touch(
    data_version: str,
    *,
    git_sha: str,
    run_id: str,
    macro_f1: float,
    path: Path = LEDGER_PATH,
) -> None:
    if data_version in read_touched_versions(path):
        raise TestSetAlreadyTouched(
            f"data_version {data_version} already appears in {path}. The held-out test set is "
            f"evaluated exactly once per data_version. Read the recorded numbers, or change the "
            f"data (which changes data_version) if a genuinely new split is intended."
        )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_HEADER)
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    with path.open("a") as handle:
        handle.write(f"| {data_version} | {git_sha} | {run_id} | {stamp} | {macro_f1:.4f} |\n")
```

- [ ] **Step 4: Create and commit the ledger, then run the tests**

The last test asserts the real ledger is tracked, so it must be committed before the suite is
green. Create it with the header only:

```bash
.venv/bin/python -c "from model.ledger import _HEADER, LEDGER_PATH; LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True); LEDGER_PATH.write_text(_HEADER)"
git add docs/test-set-touch-log.md
```

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ledger.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add model/ledger.py docs/test-set-touch-log.md tests/unit/test_ledger.py
git commit -m "Add a git-tracked once-only ledger for the held-out test set"
```

---

### Task 12: Per-identity-term fairness slice of the held-out test set [H31]

**Files:**
- Create: `model/fairness.py`
- Test: `tests/unit/test_fairness.py`

**Interfaces produced:** `IDENTITY_TERMS`, `term_mask`, `identity_fairness_report`, `render_fairness_markdown`

Jigsaw's best-documented failure is that comments **merely mentioning** an identity group are
over-flagged, so the metric that matters is the false-positive rate **within the non-toxic rows
of each term slice**, compared against the background non-toxic false-positive rate. The original
Jigsaw six-label corpus carries no identity annotations, so the slice is built by term match, and
the model card must say so. Following `auditing-model-fairness`: multi-metric, intersection-aware
where the data allows, bootstrap intervals on every gap, small groups flagged low-power rather
than dropped, and no "fair / not fair" verdict from the report itself.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_fairness.py`:
```python
import numpy as np

from model.fairness import (
    IDENTITY_TERMS,
    identity_fairness_report,
    render_fairness_markdown,
    term_mask,
)


def _corpus():
    """400 ordinary non-toxic rows, 60 non-toxic rows that merely MENTION an identity group and
    are over-flagged, 6 genuinely toxic rows mentioning it, 20 low-power rows, 40 toxic rows."""
    rng = np.random.default_rng(0)
    texts, y_true, y_flag, y_prob = [], [], [], []
    for i in range(400):
        texts.append(f"a perfectly ordinary comment number {i}")
        flag = int(rng.random() < 0.05)
        y_true.append(0); y_flag.append(flag); y_prob.append(0.6 if flag else 0.05)
    for i in range(60):
        texts.append(f"i am a muslim and i edited paragraph {i}")
        flag = int(rng.random() < 0.50)
        y_true.append(0); y_flag.append(flag); y_prob.append(0.7 if flag else 0.10)
    for i in range(6):
        texts.append(f"muslim people are scum {i}")
        y_true.append(1); y_flag.append(1); y_prob.append(0.90)
    for i in range(20):
        texts.append(f"my sikh neighbour helped with source {i}")
        y_true.append(0); y_flag.append(0); y_prob.append(0.02)
    for i in range(40):
        texts.append(f"you are an idiot number {i}")
        y_true.append(1); y_flag.append(1); y_prob.append(0.95)
    return texts, y_true, y_flag, y_prob


def test_term_matching_respects_word_boundaries():
    assert term_mask(["a woman spoke"], "man").tolist() == [False]
    assert term_mask(["a man spoke"], "man").tolist() == [True]
    assert term_mask(["MUSLIM readers"], "muslim").tolist() == [True]
    assert term_mask(["anti-muslim graffiti"], "muslim").tolist() == [True]


def test_over_flagged_identity_mentions_are_detected_as_a_material_gap():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=300)
    assert report["worst_term"] == "muslim"
    assert report["max_fpr_gap"] > 0.10
    assert report["material"] is True
    slices = {s["term"]: s for s in report["slices"]}
    assert slices["muslim"]["fpr"] > 4 * report["background_fpr"]
    assert slices["muslim"]["fpr_ratio_to_background"] > 4.0


def test_every_scored_slice_carries_a_bootstrap_interval():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=300)
    muslim = next(s for s in report["slices"] if s["term"] == "muslim")
    assert muslim["fpr_lo"] is not None and muslim["fpr_hi"] is not None
    assert muslim["fpr_lo"] <= muslim["fpr"] <= muslim["fpr_hi"]
    assert muslim["pr_auc"] is not None and muslim["pr_auc_lo"] is not None


def test_small_groups_are_flagged_low_power_and_never_dropped():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=200)
    slices = {s["term"]: s for s in report["slices"]}
    assert "sikh" in slices, "a small group was silently dropped instead of reported"
    assert slices["sikh"]["n"] == 20
    assert slices["sikh"]["low_power"] is True
    assert report["n_terms_low_power"] >= 1


def test_terms_absent_from_the_corpus_are_omitted_not_reported_as_zero():
    report = identity_fairness_report(
        ["nothing to see here"], [0], [0], [0.01], terms=("muslim",), n_boot=50
    )
    assert report["slices"] == []
    assert report["n_terms_present"] == 0


def test_the_term_list_covers_the_documented_bias_axes():
    for term in ("muslim", "jewish", "gay", "transgender", "black", "female", "disabled"):
        assert term in IDENTITY_TERMS
    assert len(IDENTITY_TERMS) >= 40


def test_markdown_renders_the_background_rate_and_every_slice():
    texts, y_true, y_flag, y_prob = _corpus()
    report = identity_fairness_report(texts, y_true, y_flag, y_prob, n_boot=200)
    md = render_fairness_markdown(report)
    assert "background non-toxic flag rate" in md
    assert "| muslim |" in md
    assert "| sikh |" in md
    assert "low power" in md
    assert "no fair / not fair verdict" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_fairness.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.fairness'`

- [ ] **Step 3: Write minimal implementation**

`model/fairness.py`:
```python
"""Per-identity-term fairness slice of the held-out test set.

Jigsaw's documented unintended bias is that comments which merely MENTION an identity group are
over-flagged. The metric that captures it is the false-positive rate among the NON-TOXIC rows of
each term slice, against the background non-toxic false-positive rate.

Method notes that belong in the model card, not just here:

- The original six-label Jigsaw corpus carries no identity annotations, so a slice is a term
  match, not a demographic. A term slice is a proxy and it is a noisy one.
- Groups below `min_group_size` are reported with a low-power flag, never dropped.
- Every rate carries a bootstrap interval, and the per-slice PR-AUC uses the positive-preserving
  stratified bootstrap because a term slice can hold only a handful of toxic rows.
- The report names which metrics fail. It does not return a verdict. The deployer owns that.
"""

import re
from dataclasses import asdict, dataclass

import numpy as np
from sklearn.metrics import average_precision_score

from model.metrics import proportion_ci, stratified_bootstrap_ci

# The identity-term list from Dixon et al. 2018, "Measuring and Mitigating Unintended Bias in
# Text Classification", restricted to descriptors rather than slurs so the repository stays
# publishable. Slur-bearing comments are already covered by the `identity_hate` label itself.
IDENTITY_TERMS: tuple[str, ...] = (
    "atheist", "queer", "gay", "lesbian", "transgender", "trans", "bisexual", "homosexual",
    "heterosexual", "straight", "lgbt", "lgbtq", "nonbinary", "female", "male", "woman",
    "women", "man", "men", "black", "white", "african", "asian", "latino", "latina",
    "hispanic", "mexican", "indian", "chinese", "japanese", "arab", "middle eastern",
    "immigrant", "refugee", "american", "canadian", "european", "irish", "muslim", "islam",
    "jewish", "jew", "christian", "catholic", "protestant", "buddhist", "hindu", "sikh",
    "mormon", "deaf", "blind", "disabled", "paralyzed", "elderly", "older", "younger",
    "teenage",
)
_SEP = "[^a-z0-9]"


@dataclass(frozen=True)
class TermSlice:
    term: str
    n: int
    n_pos: int
    n_neg: int
    base_rate: float
    selection_rate: float
    fpr: float | None
    fpr_lo: float | None
    fpr_hi: float | None
    tpr: float | None
    pr_auc: float | None
    pr_auc_lo: float | None
    pr_auc_hi: float | None
    fpr_ratio_to_background: float | None
    low_power: bool


def term_mask(texts, term: str) -> np.ndarray:
    """Word-boundary-ish match so `man` does not match `woman`."""
    pattern = re.compile(rf"(?:^|{_SEP}){re.escape(term)}(?:{_SEP}|$)")
    return np.array([bool(pattern.search(str(t).lower())) for t in texts], dtype=bool)


def identity_fairness_report(
    texts,
    y_true,
    y_flag,
    y_prob,
    *,
    terms: tuple[str, ...] = IDENTITY_TERMS,
    min_group_size: int = 30,
    seed: int = 42,
    n_boot: int = 1000,
    material_gap: float = 0.10,
) -> dict:
    """`y_true`, `y_flag`, `y_prob` are the 1-D `toxic`-label vectors for the held-out test set."""
    y_true = np.asarray(y_true).astype(int)
    y_flag = np.asarray(y_flag).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    background = y_true == 0
    background_fpr = float(y_flag[background].mean()) if background.any() else float("nan")

    slices: list[TermSlice] = []
    for term in terms:
        mask = term_mask(texts, term)
        n = int(mask.sum())
        if n == 0:
            continue
        neg = mask & (y_true == 0)
        pos = mask & (y_true == 1)
        fpr_ci = proportion_ci(y_flag[neg], n_boot=n_boot, seed=seed) if neg.any() else None
        ap_ci = (
            stratified_bootstrap_ci(
                y_true[mask], y_prob[mask], average_precision_score, n_boot=n_boot, seed=seed
            )
            if pos.any() and neg.any()
            else None
        )
        fpr = float(y_flag[neg].mean()) if neg.any() else None
        slices.append(
            TermSlice(
                term=term,
                n=n,
                n_pos=int(pos.sum()),
                n_neg=int(neg.sum()),
                base_rate=float(y_true[mask].mean()),
                selection_rate=float(y_flag[mask].mean()),
                fpr=fpr,
                fpr_lo=(fpr_ci.lo if fpr_ci else None),
                fpr_hi=(fpr_ci.hi if fpr_ci else None),
                tpr=(float(y_flag[pos].mean()) if pos.any() else None),
                pr_auc=(ap_ci.point if ap_ci else None),
                pr_auc_lo=(ap_ci.lo if ap_ci else None),
                pr_auc_hi=(ap_ci.hi if ap_ci else None),
                fpr_ratio_to_background=(
                    fpr / background_fpr if fpr is not None and background_fpr > 0 else None
                ),
                low_power=n < min_group_size,
            )
        )

    scored = [s for s in slices if s.fpr is not None and not s.low_power]
    max_fpr_gap = max((s.fpr - background_fpr for s in scored), default=0.0)
    worst = max(scored, key=lambda s: s.fpr, default=None)
    rates = [s.selection_rate for s in scored]
    return {
        "background_fpr": background_fpr,
        "n_terms_present": len(slices),
        "n_terms_scored": len(scored),
        "n_terms_low_power": len(slices) - len(scored),
        "max_fpr_gap": float(max_fpr_gap),
        "worst_term": (worst.term if worst else None),
        "four_fifths_ratio": (min(rates) / max(rates) if rates and max(rates) > 0 else None),
        "material": bool(max_fpr_gap > material_gap),
        "min_group_size": min_group_size,
        "slices": [asdict(s) for s in slices],
    }


def _fmt(value: float | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def render_fairness_markdown(report: dict) -> str:
    lines = [
        "# Fairness: per-identity-term slice of the held-out test set",
        "",
        "Jigsaw's documented unintended bias is that comments which merely **mention** an identity",
        "group are over-flagged. The number that captures it is the false-positive rate among the",
        "**non-toxic** rows of each term slice, against the background non-toxic flag rate.",
        "",
        f"- background non-toxic flag rate: **{_fmt(report['background_fpr'])}**",
        f"- terms present in the test set: {report['n_terms_present']}",
        f"- terms with enough rows to score (n >= {report['min_group_size']}): "
        f"{report['n_terms_scored']}",
        f"- terms reported but under-powered: {report['n_terms_low_power']}",
        f"- largest false-positive gap: **{_fmt(report['max_fpr_gap'])}** "
        f"({report['worst_term'] or 'n/a'})",
        f"- four-fifths ratio across scored terms: {_fmt(report['four_fifths_ratio'])}",
        "",
        "| term | n | n_pos | base rate | flag rate | FPR | FPR 95% CI | FPR vs background | "
        "PR-AUC | note |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in sorted(report["slices"], key=lambda s: -(s["fpr"] or 0.0)):
        note = "low power" if s["low_power"] else ""
        ci = f"[{_fmt(s['fpr_lo'], 3)}, {_fmt(s['fpr_hi'], 3)}]"
        lines.append(
            f"| {s['term']} | {s['n']} | {s['n_pos']} | {_fmt(s['base_rate'], 3)} | "
            f"{_fmt(s['selection_rate'], 3)} | {_fmt(s['fpr'], 3)} | {ci} | "
            f"{_fmt(s['fpr_ratio_to_background'], 2)} | {_fmt(s['pr_auc'], 3)} | {note} |"
        )
    lines += [
        "",
        "## Limitations",
        "",
        "- The original six-label Jigsaw corpus carries **no identity annotations**. A term slice is",
        "  a proxy for a demographic, and a noisy one: it captures who is *talked about*, not who is",
        "  speaking, and it misses every mention that uses no listed term.",
        "- Under-powered groups are reported with wide intervals rather than dropped, because",
        "  dropping them is how the worst-affected group disappears from a fairness report.",
        "- This report names which metrics fail and by how much, and issues"
        " **no fair / not fair verdict**.",
        "  Demographic parity and equal opportunity cannot both hold when base rates differ, so",
        "  choosing which to honour is a deployer decision, not a measurement.",
    ]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_fairness.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add model/fairness.py tests/unit/test_fairness.py
git commit -m "Add per-identity-term fairness slice with bootstrap intervals"
```

---

### Task 13: `baseline_flag_rates.json`, the drift reference the dashboard has nowhere else [C5]

**Files:**
- Create: `model/baseline_rates.py`
- Test: `tests/unit/test_baseline_rates.py`

**Interfaces produced:** `BaselineFlagRates`, `compute_baseline_flag_rates`, `write_baseline_flag_rates`

Rubric 3.2 asks for "distribution of predicted classes (target drift)". Drift is a comparison, and
the Phase 3 dashboard currently has nothing to compare against. This file is that reference: the
per-label flag rate on the held-out test set, at the promoted thresholds, with the model version
and `data_version` that produced it. It is written here because this is the only phase that holds
the held-out predictions.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_baseline_rates.py`:
```python
import json

import numpy as np
import pytest
from pydantic import ValidationError

from model.baseline_rates import (
    BaselineFlagRates,
    compute_baseline_flag_rates,
    write_baseline_flag_rates,
)
from model.labels import LABELS


def _args(**over):
    base = dict(
        data_version="d" * 64,
        model_version="toxic-clf:v1",
        model_digest="sha256:" + "e" * 64,
        thresholds={label: 0.5 for label in LABELS},
    )
    base.update(over)
    return base


def test_flag_rates_are_computed_at_the_promoted_thresholds():
    # column 0 (`toxic`) flags on 3 of 4 rows at threshold 0.5; column 3 (`threat`) on 1 of 4
    probs = np.array(
        [
            [0.9, 0.1, 0.1, 0.9, 0.1, 0.1],
            [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.7, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.2, 0.1, 0.1, 0.1, 0.1, 0.1],
        ]
    )
    out = compute_baseline_flag_rates(probs, **_args())
    assert out.flag_rates["toxic"] == pytest.approx(0.75)
    assert out.flag_rates["threat"] == pytest.approx(0.25)
    assert out.flag_rates["insult"] == pytest.approx(0.0)
    assert out.n_test == 4


def test_per_label_thresholds_are_applied_per_label_not_globally():
    probs = np.full((10, len(LABELS)), 0.40)
    thresholds = {label: 0.5 for label in LABELS}
    thresholds["threat"] = 0.30
    out = compute_baseline_flag_rates(probs, **_args(thresholds=thresholds))
    assert out.flag_rates["threat"] == pytest.approx(1.0)
    assert out.flag_rates["toxic"] == pytest.approx(0.0)


def test_keys_are_exactly_labels_in_order():
    probs = np.random.default_rng(0).random((50, len(LABELS)))
    out = compute_baseline_flag_rates(probs, **_args())
    assert list(out.flag_rates.keys()) == list(LABELS)
    assert list(out.thresholds.keys()) == list(LABELS)


def test_the_schema_rejects_a_missing_label():
    with pytest.raises(ValidationError):
        BaselineFlagRates(
            data_version="d" * 64,
            model_version="toxic-clf:v1",
            model_digest="sha256:" + "e" * 64,
            n_test=10,
            thresholds={label: 0.5 for label in LABELS},
            flag_rates={"toxic": 0.1},
            generated_at_utc="2026-08-02T00:00:00+00:00",
        )


def test_the_schema_rejects_a_rate_outside_zero_to_one():
    with pytest.raises(ValidationError):
        BaselineFlagRates(
            data_version="d" * 64,
            model_version="toxic-clf:v1",
            model_digest="sha256:" + "e" * 64,
            n_test=10,
            thresholds={label: 0.5 for label in LABELS},
            flag_rates={label: (1.5 if label == "toxic" else 0.1) for label in LABELS},
            generated_at_utc="2026-08-02T00:00:00+00:00",
        )


def test_the_json_round_trips_for_phase_three(tmp_path):
    probs = np.random.default_rng(0).random((50, len(LABELS)))
    out = compute_baseline_flag_rates(probs, **_args())
    path = tmp_path / "baseline_flag_rates.json"
    write_baseline_flag_rates(path, out)
    reloaded = BaselineFlagRates.model_validate(json.loads(path.read_text()))
    assert reloaded == out
    assert reloaded.model_digest.startswith("sha256:")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_baseline_rates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.baseline_rates'`

- [ ] **Step 3: Write minimal implementation**

`model/baseline_rates.py`:
```python
"""The reference distribution the Phase 3 target-drift panel drifts FROM.

Rubric 3.2 asks for the distribution of predicted classes as target drift. Drift is a
comparison, and without a stored baseline the dashboard can only plot today's bar next to
nothing. This is the per-label flag rate on the held-out test set at the promoted thresholds,
stamped with the model version and data_version that produced it so a later mismatch is visible
rather than silent.
"""

import datetime as dt
import json
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field, model_validator

from model.labels import LABELS


class BaselineFlagRates(BaseModel):
    data_version: str
    model_version: str
    model_digest: str
    n_test: int = Field(ge=1)
    thresholds: dict[str, float]
    flag_rates: dict[str, float]
    generated_at_utc: str

    @model_validator(mode="after")
    def _validate(self) -> "BaselineFlagRates":
        for name, mapping in (("thresholds", self.thresholds), ("flag_rates", self.flag_rates)):
            if set(mapping) != set(LABELS):
                raise ValueError(f"{name} keys must equal {LABELS}")
            for label, value in mapping.items():
                if not 0.0 <= value <= 1.0:
                    raise ValueError(f"{name}[{label}] = {value} is outside [0, 1]")
        return self


def compute_baseline_flag_rates(
    y_prob,
    *,
    data_version: str,
    model_version: str,
    model_digest: str,
    thresholds: dict[str, float],
) -> BaselineFlagRates:
    probs = np.asarray(y_prob, dtype=float)
    if probs.ndim != 2 or probs.shape[1] != len(LABELS):
        raise ValueError(f"expected an (n, {len(LABELS)}) probability matrix, got {probs.shape}")
    thr = np.array([thresholds[label] for label in LABELS], dtype=float)
    flags = (probs >= thr).astype(int)
    return BaselineFlagRates(
        data_version=data_version,
        model_version=model_version,
        model_digest=model_digest,
        n_test=int(probs.shape[0]),
        thresholds={label: float(thresholds[label]) for label in LABELS},
        flag_rates={label: float(flags[:, j].mean()) for j, label in enumerate(LABELS)},
        generated_at_utc=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    )


def write_baseline_flag_rates(path: Path, rates: BaselineFlagRates) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json.loads(rates.model_dump_json()), indent=2) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_baseline_rates.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add model/baseline_rates.py tests/unit/test_baseline_rates.py
git commit -m "Persist per-label baseline flag rates as the drift reference"
```

---

### Task 14: W&B run logging with git SHA, hyperparameters, data version, and no raw text [rubric 1.2]

**Files:**
- Create: `model/tracking.py`
- Test: `tests/unit/test_tracking.py`

**Interfaces produced:** `build_run_config`, `build_run_summary`, `assert_no_raw_text`, `RawTextLeak`, `log_run`

Rubric 1.2 requires each run to log the code version (git commit), hyperparameters, performance
metrics **including accuracy**, and data versions. The W&B project is public, so this is also the
last place a raw user comment could escape into a public artifact — hence a leak check that
scans the payload against the corpus before anything is sent.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_tracking.py`:
```python
import pytest

from model.labels import LABELS
from model.metrics import CIResult
from model.tracking import (
    RawTextLeak,
    assert_no_raw_text,
    build_run_config,
    build_run_summary,
    log_run,
)


def _config():
    return build_run_config(
        git_sha="9f1c2ab",
        seed=42,
        data_version="d" * 64,
        model_name="classical-tfidf-ovr-lr",
        hyperparameters={"C": 1.0, "solver": "liblinear", "word_max_features": 200_000,
                         "char_max_features": 100_000, "calibration_method": "sigmoid",
                         "calibration_folds": 5},
        thresholds={label: 0.5 for label in LABELS},
    )


def test_config_carries_every_field_rubric_1_2_names():
    cfg = _config()
    assert cfg["git_sha"] == "9f1c2ab"
    assert cfg["data_version"] == "d" * 64
    assert cfg["seed"] == 42
    assert cfg["hyperparameters"]["solver"] == "liblinear"
    assert cfg["hyperparameters"]["word_max_features"] == 200_000
    assert set(cfg["thresholds"]) == set(LABELS)


def test_summary_includes_accuracy_and_flattens_confidence_intervals():
    metrics = {"macro_f1": 0.74, "accuracy": 0.91, "pr_auc/threat": 0.31}
    cis = {"pr_auc/threat": CIResult(0.31, 0.18, 0.44, 72, 23_859, 1000, False, None)}
    summary = build_run_summary(metrics, cis)
    assert summary["accuracy"] == 0.91          # rubric 1.2 and 3.2 name it explicitly
    assert summary["macro_f1"] == 0.74
    assert summary["pr_auc/threat"] == 0.31
    assert summary["pr_auc/threat.ci_lo"] == 0.18
    assert summary["pr_auc/threat.ci_hi"] == 0.44
    assert summary["pr_auc/threat.n_pos"] == 72
    assert summary["pr_auc/threat.low_power"] is False


def test_a_corpus_comment_in_the_payload_is_caught_before_it_is_sent():
    corpus = ["you are an idiot", "have a nice day friend"]
    with pytest.raises(RawTextLeak, match="raw comment text"):
        assert_no_raw_text({"worst_example": "you are an idiot"}, corpus)
    with pytest.raises(RawTextLeak):
        assert_no_raw_text({"nested": {"rows": ["have a nice day friend"]}}, corpus)


def test_a_clean_payload_passes_the_leak_check():
    corpus = ["you are an idiot", "have a nice day friend"]
    assert_no_raw_text({"macro_f1": 0.74, "git_sha": "9f1c", "note": "no user text here"}, corpus)


def test_short_corpus_entries_do_not_produce_false_positives():
    """A one-word comment would otherwise match half the metric names."""
    assert_no_raw_text({"decision": "allow"}, ["allow", "hi"])


def test_log_run_sends_config_then_summary_and_refuses_a_leaking_payload():
    sent = {}

    class FakeRun:
        def __init__(self):
            self.config, self.summary = {}, {}
            self.id = "run-abc"

        def log(self, payload):
            sent.update(payload)

        def finish(self):
            sent["finished"] = True

    run = FakeRun()
    log_run(run, config=_config(), summary={"macro_f1": 0.74, "accuracy": 0.91}, corpus=["idiot"])
    assert run.config["git_sha"] == "9f1c2ab"
    assert sent["macro_f1"] == 0.74 and sent["accuracy"] == 0.91

    with pytest.raises(RawTextLeak):
        log_run(run, config=_config(), summary={"sample": "you are an idiot"},
                corpus=["you are an idiot"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_tracking.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.tracking'`

- [ ] **Step 3: Write minimal implementation**

`model/tracking.py`:
```python
"""W&B run payloads.

Rubric 1.2 requires every run to log the code version (git commit), the hyperparameters, the
performance metrics including accuracy, and the data version. The W&B project is public by
deliberate owner decision, which makes this the last place a raw user comment could escape into
a public artifact, so every payload is scanned against the corpus before it is sent.

The W&B client is injected rather than imported at call time so this module is unit-testable
with no network and no API key.
"""

from typing import Any

from model.metrics import CIResult


class RawTextLeak(RuntimeError):
    """A payload bound for a public surface contains a raw corpus comment."""


def build_run_config(
    *,
    git_sha: str,
    seed: int,
    data_version: str,
    model_name: str,
    hyperparameters: dict[str, Any],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    return {
        "git_sha": git_sha,
        "seed": seed,
        "data_version": data_version,
        "model_name": model_name,
        "hyperparameters": dict(hyperparameters),
        "thresholds": dict(thresholds),
    }


def build_run_summary(metrics: dict[str, float], cis: dict[str, CIResult]) -> dict[str, Any]:
    """Flatten metrics and their intervals into scalars W&B can chart."""
    summary: dict[str, Any] = dict(metrics)
    for key, ci in cis.items():
        summary[f"{key}.ci_lo"] = ci.lo
        summary[f"{key}.ci_hi"] = ci.hi
        summary[f"{key}.n_pos"] = ci.n_pos
        summary[f"{key}.low_power"] = ci.low_power
    return summary


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _strings(v)]
    return []


def assert_no_raw_text(payload: Any, corpus, *, min_length: int = 12) -> None:
    """Refuse to send a payload containing any corpus comment.

    Entries shorter than `min_length` are skipped: a one-word comment would match ordinary
    metric names and turn the check into noise.
    """
    haystacks = _strings(payload)
    if not haystacks:
        return
    for comment in corpus:
        text = str(comment)
        if len(text) < min_length:
            continue
        for haystack in haystacks:
            if text in haystack:
                raise RawTextLeak(
                    f"payload contains raw comment text ({len(text)} chars) and the W&B project "
                    f"is public; log ids and aggregates, never the comment"
                )


def log_run(run, *, config: dict, summary: dict, corpus) -> None:
    assert_no_raw_text(config, corpus)
    assert_no_raw_text(summary, corpus)
    run.config.update(config)
    run.log(summary)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_tracking.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add model/tracking.py tests/unit/test_tracking.py
git commit -m "Add W&B run payloads with a raw-text leak check"
```

---

### Task 14a: The three provenance fields reach W&B separately, not as one opaque composite [gap `data_version`, remediation 3.12 / H20, rubric 1.2 "data versions"]

Phase 0 Task 14 split `data_version` into `raw_sha256`, `split_version`, and `env_version` precisely so a moved number can be attributed. Nothing carried them past Phase 0: `grep -n 'raw_sha256\|split_version\|env_version'` across phases 1 through 5 returned zero hits before this task existed, Task 14 above accepts a single opaque `data_version: str`, and Task 17's driver passes `data_version=bundle.data_version` — the derived sha256-of-a-sha256 display string. The one place the split was supposed to pay off, a W&B run page, collapses back to one hash, and no test anywhere fails when it does. Rubric 1.2 says "data version**s**", plural.

This task makes the bundle itself the argument, so a caller cannot silently regress to the composite, and supersedes two assertions written in Task 14.

**Files:**
- Modify: `model/tracking.py`, `model/train_classical.py`
- Test: `tests/unit/test_tracking.py` (amend `_config`, append four cases)

- [ ] **Step 1: Write the failing test**

First **replace** Task 14's `_config()` helper and its first assertion, because they encode the resolution this task rejects:

```python
# REPLACES the module-level _config() written in Task 14.
import hashlib
from pathlib import Path

from model.data.prepare import SplitConfig, prepare_dataset

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def _bundle(seed: int = 42):
    return prepare_dataset(FIXTURE, SplitConfig(seed=seed))


def _config(bundle=None):
    return build_run_config(
        git_sha="9f1c2ab",
        seed=42,
        bundle=bundle if bundle is not None else _bundle(),
        model_name="classical-tfidf-ovr-lr",
        hyperparameters={"C": 1.0, "solver": "liblinear", "word_max_features": 200_000,
                         "char_max_features": 100_000, "calibration_method": "sigmoid",
                         "calibration_folds": 5},
        thresholds={label: 0.5 for label in LABELS},
    )
```

In `test_config_carries_every_field_rubric_1_2_names`, replace `assert cfg["data_version"] == "d" * 64` with `assert len(cfg["data_version"]) == 64`. Everything else in that test stands.

Then append to `tests/unit/test_tracking.py`:

```python
def test_config_carries_all_three_provenance_fields_not_one_composite():
    """Remediation 3.12. One `data_version` string cannot answer the question anyone asks
    when a number moves: did the corpus change, did the split change, or did the environment
    change? Rubric 1.2 says 'data versions', plural."""
    bundle = _bundle()
    cfg = _config(bundle)
    assert cfg["raw_sha256"] == bundle.raw_sha256
    assert cfg["split_version"] == bundle.split_version
    assert cfg["env_version"] == bundle.env_version
    assert cfg["data_version"] == bundle.data_version
    assert len({cfg["raw_sha256"], cfg["split_version"], cfg["env_version"]}) == 3


def test_a_seed_change_moves_split_version_alone_on_the_run_page():
    a, b = _bundle(seed=42), _bundle(seed=7)
    ca, cb = _config(a), _config(b)
    assert ca["split_version"] != cb["split_version"]
    assert ca["raw_sha256"] == cb["raw_sha256"]
    assert ca["env_version"] == cb["env_version"]


def test_the_composite_is_derived_from_the_three_and_cannot_drift():
    cfg = _config()
    joined = f"{cfg['raw_sha256']}:{cfg['split_version']}:{cfg['env_version']}"
    assert cfg["data_version"] == hashlib.sha256(joined.encode()).hexdigest()


def test_build_run_config_refuses_a_bare_string_data_version():
    """Passing the composite alone is the regression this task exists to prevent."""
    with pytest.raises(TypeError, match="requires the DatasetBundle"):
        build_run_config(git_sha="x", seed=42, bundle="d" * 64, model_name="m",
                         hyperparameters={}, thresholds={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_tracking.py -v`
Expected: FAIL with `TypeError: build_run_config() got an unexpected keyword argument 'bundle'` on every case that calls `_config`.

- [ ] **Step 3: Write minimal implementation**

Replace `build_run_config` in `model/tracking.py`:
```python
def build_run_config(
    *,
    git_sha: str,
    seed: int,
    bundle,
    model_name: str,
    hyperparameters: dict[str, Any],
    thresholds: dict[str, float],
) -> dict[str, Any]:
    """Rubric 1.2 says 'data versionS'. Log all three, plus the composite for display.

    One opaque hash cannot answer 'did the corpus change, did the split change, or did the
    environment change?', which is the only question anyone asks when a metric moves
    (remediation 3.12). The bundle is required rather than a string so a caller cannot
    silently regress to the composite — that regression is exactly what happened between
    Phase 0 and Phase 1 and it was invisible because nothing typed the argument.
    """
    for field in ("raw_sha256", "split_version", "env_version", "data_version"):
        if not hasattr(bundle, field):
            raise TypeError(
                "build_run_config requires the DatasetBundle, not a bare data_version "
                f"string: {field!r} is missing"
            )
    return {
        "git_sha": git_sha,
        "seed": seed,
        "raw_sha256": bundle.raw_sha256,
        "split_version": bundle.split_version,
        "env_version": bundle.env_version,
        "data_version": bundle.data_version,
        "model_name": model_name,
        "hyperparameters": dict(hyperparameters),
        "thresholds": dict(thresholds),
    }
```

In `model/train_classical.py::main`, replace `data_version=bundle.data_version` in the `build_run_config(...)` call with `bundle=bundle`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_tracking.py -v`
Expected: `10 passed`.

- [ ] **Step 5: Commit**

```bash
git add model/tracking.py model/train_classical.py tests/unit/test_tracking.py
git commit -m "Log raw_sha256, split_version, and env_version separately to W&B"
```

**Amendments this task makes to other tasks in this phase and Phase 5:**

- Task 17 exit criteria, add: `- [ ] The W&B run page shows raw_sha256, split_version, and env_version as three distinct config keys, not one data_version.`
- Phase 5 Task 25 `docs/rubric-conformance.md`, row 1.2 evidence becomes: `git SHA, hyperparameters, metrics incl. accuracy, and raw_sha256 / split_version / env_version (three data versions)`.
- The "Corrections to the master plan" table above (the `data_version: str  # sha256 over sorted deduped ids + config` row) states the **pre-v2** single-field semantics. Phase 0 v2 Task 14 superseded it. Replace that row's right-hand cell with: `Superseded by Phase 0 v2 Task 14. The bundle carries raw_sha256, split_version, and env_version as separate fields; data_version survives only as a derived property, sha256 over the three joined by ':', for single-string display and for the once-only test-set ledger key.`
- `docs/test-set-touch-log.md`'s header (Task 11) must state: `The ledger key is the composite data_version, which includes env_version. A numpy or scikit-learn bump therefore legitimately re-opens the held-out test set on an unchanged split. That is deliberate — a different library version is a different measurement — and it is named here so it is not discovered as a surprise.`

---

### Task 15: Register, promote, and prove the Registry page is publicly visible [H11, rubric 1.3]

**Files:**
- Create: `model/registry.py`
- Test: `tests/unit/test_registry.py`

**Interfaces produced:** `register_and_promote`, `check_public_registry`, `RegistryVisibility`, `RegistryNotPublic`

Rubric 1.3 grades a **visible** promotion, and the submission checklist previously verified only
that the W&B *project* was public — a different surface from the *Registry*. Owner decision
2026-07-31: the Registry page must be publicly visible, not merely screenshotted. The verification
is an **unauthenticated** GraphQL read against `api.wandb.ai`, using `urllib` from the standard
library and sending no `Authorization` header, so it cannot accidentally pass on the strength of a
local credential. This was validated live against a known-public personal registry: a public
project returns `access: "USER_READ"` with its collections and aliases; a private or absent one
returns `null`.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_registry.py`:
```python
import pytest

from model.registry import (
    COLLECTION,
    PROMOTED_ALIAS,
    REGISTRY_PROJECT,
    RegistryNotPublic,
    check_public_registry,
    register_and_promote,
)


def _responder(body):
    calls = []

    def post(url, payload):
        calls.append((url, payload))
        return body

    return post, calls


def _public_body(collection=COLLECTION, aliases=(PROMOTED_ALIAS, "latest", "v0")):
    return {
        "data": {
            "project": {
                "name": REGISTRY_PROJECT,
                "access": "USER_READ",
                "artifactType": {
                    "artifactCollections": {
                        "edges": [
                            {
                                "node": {
                                    "name": collection,
                                    "artifacts": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "versionIndex": 0,
                                                    "aliases": [{"alias": a} for a in aliases],
                                                }
                                            }
                                        ]
                                    },
                                }
                            }
                        ]
                    }
                },
            }
        }
    }


def test_a_public_registry_with_a_promoted_alias_passes():
    post, calls = _responder(_public_body())
    result = check_public_registry("rocklambros", post=post)
    assert result.public is True and result.alias_present is True
    assert result.url.endswith(f"/{REGISTRY_PROJECT}/artifacts/model/{COLLECTION}")
    url, payload = calls[0]
    assert url == "https://api.wandb.ai/graphql"
    assert payload["variables"] == {"entity": "rocklambros", "project": REGISTRY_PROJECT}


def test_the_real_request_carries_no_authorization_header(monkeypatch):
    """A logged-in check would pass against a private registry and prove nothing."""
    import io
    import json as _json

    captured = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.headers)
        captured["url"] = request.full_url
        return _Resp(_json.dumps(_public_body()).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    check_public_registry("rocklambros")
    # urllib capitalises header keys, so compare case-insensitively
    assert not any(k.lower() == "authorization" for k in captured["headers"])
    assert captured["url"] == "https://api.wandb.ai/graphql"


def test_a_private_registry_is_refused_with_the_fix_in_the_message():
    post, _ = _responder({"data": {"project": None}})
    with pytest.raises(RegistryNotPublic, match="Project visibility"):
        check_public_registry("rocklambros", post=post)


def test_a_missing_collection_is_refused():
    post, _ = _responder(_public_body(collection="something-else"))
    with pytest.raises(RegistryNotPublic, match="has not run"):
        check_public_registry("rocklambros", post=post)


def test_a_collection_without_the_promoted_alias_is_refused():
    post, _ = _responder(_public_body(aliases=("latest", "v0")))
    with pytest.raises(RegistryNotPublic, match="rubric 1.3 requires a visible promoted stage"):
        check_public_registry("rocklambros", post=post)


def test_register_and_promote_links_the_model_with_the_production_alias():
    calls = {}

    class FakeRun:
        entity = "rocklambros"

        def link_model(self, path, registered_model_name, name=None, aliases=None):
            calls.update(
                path=path, registered_model_name=registered_model_name, name=name, aliases=aliases
            )
            return object()

    target = register_and_promote(FakeRun(), "artifacts/toxic-clf.skops")
    assert calls["registered_model_name"] == COLLECTION
    assert calls["name"] == COLLECTION
    assert PROMOTED_ALIAS in calls["aliases"]
    assert target == f"rocklambros/{REGISTRY_PROJECT}/{COLLECTION}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.registry'`

- [ ] **Step 3: Write minimal implementation**

`model/registry.py`:
```python
"""Register the classical model and verify the Registry page is publicly visible.

Rubric 1.3 grades a *visible* Staging or Production promotion. The previous submission checklist
verified only that the W&B project was public, which is a different surface from the Registry
(premortem H11). Owner decision 2026-07-31: the Registry page must be publicly visible.

The verification deliberately uses urllib from the standard library and sends no Authorization
header. A check that runs under the developer's own credentials would pass against a private
registry and prove nothing, which is the failure mode this exists to remove.
"""

import argparse
import json
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

WANDB_GRAPHQL = "https://api.wandb.ai/graphql"
REGISTRY_PROJECT = "model-registry"
COLLECTION = "toxic-clf"
PROMOTED_ALIAS = "production"

PUBLIC_REGISTRY_QUERY = (
    "query PublicRegistry($entity: String!, $project: String!) {"
    " project(name: $project, entityName: $entity) { name access"
    ' artifactType(name: "model") { artifactCollections { edges { node { name'
    " artifacts { edges { node { versionIndex aliases { alias } } } } } } } } } }"
)


class RegistryNotPublic(RuntimeError):
    """The Registry page is not anonymously readable, or carries no promoted stage."""


@dataclass(frozen=True)
class RegistryVisibility:
    entity: str
    collection: str
    alias: str
    public: bool
    alias_present: bool
    url: str


def _anonymous_post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},  # no Authorization, deliberately
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def register_and_promote(
    run,
    artifact_path: str,
    *,
    collection: str = COLLECTION,
    aliases: tuple[str, ...] = (PROMOTED_ALIAS,),
) -> str:
    """Log the model artifact and link it into the registry at a promoted stage."""
    run.link_model(
        path=artifact_path, registered_model_name=collection, name=collection, aliases=list(aliases)
    )
    return f"{run.entity}/{REGISTRY_PROJECT}/{collection}"


def check_public_registry(
    entity: str,
    *,
    collection: str = COLLECTION,
    alias: str = PROMOTED_ALIAS,
    post: Callable[[str, dict], dict] = _anonymous_post,
) -> RegistryVisibility:
    body = post(
        WANDB_GRAPHQL,
        {
            "query": PUBLIC_REGISTRY_QUERY,
            "variables": {"entity": entity, "project": REGISTRY_PROJECT},
        },
    )
    project = (body.get("data") or {}).get("project")
    url = f"https://wandb.ai/{entity}/{REGISTRY_PROJECT}/artifacts/model/{collection}"
    if project is None:
        raise RegistryNotPublic(
            f"an anonymous read of {entity}/{REGISTRY_PROJECT} returned null, so the Registry is "
            f"private. Open {url} while signed in, then Settings -> Project visibility -> Public."
        )
    edges = (
        ((project.get("artifactType") or {}).get("artifactCollections") or {}).get("edges") or []
    )
    node = next((e["node"] for e in edges if e["node"]["name"] == collection), None)
    if node is None:
        raise RegistryNotPublic(
            f"{entity}/{REGISTRY_PROJECT} is publicly readable but has no {collection!r} "
            f"collection, so register_and_promote has not run"
        )
    aliases = {
        a["alias"]
        for e in (node.get("artifacts") or {}).get("edges", [])
        for a in e["node"]["aliases"]
    }
    if alias not in aliases:
        raise RegistryNotPublic(
            f"{collection!r} is public but carries no {alias!r} alias (has: {sorted(aliases)}); "
            f"rubric 1.3 requires a visible promoted stage"
        )
    return RegistryVisibility(
        entity=entity,
        collection=collection,
        alias=alias,
        public=project.get("access") == "USER_READ",
        alias_present=True,
        url=url,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the W&B Registry page is public.")
    parser.add_argument("--entity", required=True)
    parser.add_argument("--collection", default=COLLECTION)
    parser.add_argument("--alias", default=PROMOTED_ALIAS)
    args = parser.parse_args()
    try:
        result = check_public_registry(
            args.entity, collection=args.collection, alias=args.alias
        )
    except RegistryNotPublic as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"OK: {result.url} is publicly readable and shows alias {result.alias!r}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_registry.py -v`
Expected: `6 passed`

- [ ] **Step 5: Make the Registry public and verify it logged out**

The registry is created by the first `register_and_promote` call in Task 17, so this step runs
after the training run. Exact operator steps, once:

1. Open `https://wandb.ai/<entity>/model-registry` while signed in.
2. Confirm the `toxic-clf` collection exists and its newest version carries the `production` alias.
3. Project menu -> **Settings** -> **Project visibility** -> **Public** -> Save.
4. Verify with no credentials at all:

```bash
env -u WANDB_API_KEY HOME=$(mktemp -d) .venv/bin/python -m model.registry --entity <entity>
```

Expected:
`OK: https://wandb.ai/<entity>/model-registry/artifacts/model/toxic-clf is publicly readable and shows alias 'production'`

`HOME` is redirected to an empty directory on purpose, so `~/.netrc` cannot supply a W&B
credential and turn a private page into a passing check.

5. Record that URL in `MODEL_CARD.md` and in the submission checklist.

- [ ] **Step 6: Commit**

```bash
git add model/registry.py tests/unit/test_registry.py
git commit -m "Register to the model registry and verify public visibility anonymously"
```

---

### Task 16: Guarded held-out evaluation, safe serialization, and the model card [§6.1, §6.3, H13, H14, H31]

**Files:**
- Create: `model/evaluate.py`, `model/trusted_types.py`, `model/model_card.py`
- Test: `tests/unit/test_evaluate.py`, `tests/unit/test_model_card.py`

**Interfaces produced:** `evaluate_on_test`, `dump_model`, `artifact_digest`, `TRUSTED_TYPES`, `render_model_card`

Three things land together because they are one honesty story: the test set is touched once and
the touch is recorded; the artifact is serialized with skops and its digest is written into a
**git-committed** file rather than only into W&B (H14, and the pre-mitigation for the poisoned-
artifact tail risk — recording the digest in git breaks the co-location of artifact and expected
digest inside one trust domain); and the model card discloses the fairness result and the
white-box evasion the public registry accepts (H13, H31). `SECURITY.md` already cites a
`MODEL_CARD.md` that does not exist; this task creates it.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_evaluate.py`:
```python
import hashlib

import numpy as np
import pytest
import skops.io as sio

from model.evaluate import artifact_digest, dump_model, evaluate_on_test
from model.labels import LABELS
from model.ledger import TestSetAlreadyTouched
from model.pipeline import build_classical_pipeline
from model.trusted_types import TRUSTED_TYPES
from tests.fixtures.synthetic import make_corpus


class _Bundle:
    """The Phase 0 DatasetBundle fields evaluate_on_test actually reads."""

    def __init__(self, texts, y, data_version):
        import pandas as pd

        frame = pd.DataFrame({"id": [f"c{i}" for i in range(len(texts))], "comment_text": texts})
        for j, label in enumerate(LABELS):
            frame[label] = y[:, j]
        self.test_df = frame
        self.data_version = data_version


def _fitted():
    texts, y = make_corpus(n=400)
    return build_classical_pipeline(calibration_folds=3).fit(texts, y), texts, y


def test_evaluation_returns_metrics_intervals_and_fairness(tmp_path):
    model, texts, y = _fitted()
    bundle = _Bundle(texts, y, "a" * 64)
    out = evaluate_on_test(
        bundle=bundle,
        model=model,
        thresholds={label: 0.5 for label in LABELS},
        git_sha="9f1c",
        run_id="run-1",
        ledger_path=tmp_path / "log.md",
        n_boot=100,
    )
    assert "macro_f1" in out["metrics"] and "accuracy" in out["metrics"]
    for label in LABELS:
        assert f"pr_auc/{label}" in out["cis"]
    assert "background_fpr" in out["fairness"]
    assert out["y_prob"].shape == (len(texts), len(LABELS))


def test_the_second_evaluation_of_the_same_data_version_is_refused(tmp_path):
    model, texts, y = _fitted()
    bundle = _Bundle(texts, y, "a" * 64)
    kwargs = dict(
        bundle=bundle, model=model, thresholds={label: 0.5 for label in LABELS},
        git_sha="9f1c", ledger_path=tmp_path / "log.md", n_boot=50,
    )
    evaluate_on_test(run_id="run-1", **kwargs)
    with pytest.raises(TestSetAlreadyTouched):
        evaluate_on_test(run_id="run-2", **kwargs)


def test_the_ledger_is_written_before_the_numbers_are_returned(tmp_path):
    """A crash after scoring must not leave the test set silently re-runnable."""
    from model.ledger import read_touched_versions

    model, texts, y = _fitted()
    ledger = tmp_path / "log.md"
    evaluate_on_test(
        bundle=_Bundle(texts, y, "c" * 64), model=model,
        thresholds={label: 0.5 for label in LABELS}, git_sha="9f1c", run_id="run-1",
        ledger_path=ledger, n_boot=50,
    )
    assert read_touched_versions(ledger) == {"c" * 64}


def test_the_artifact_round_trips_through_skops_with_a_static_allowlist(tmp_path):
    model, texts, _ = _fitted()
    path = tmp_path / "toxic-clf.skops"
    digest = dump_model(model, path)
    assert digest.startswith("sha256:")
    assert digest == artifact_digest(path)
    assert digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    untrusted = set(sio.get_untrusted_types(file=path))
    assert untrusted <= set(TRUSTED_TYPES), (
        f"the artifact needs types absent from the static allowlist: "
        f"{sorted(untrusted - set(TRUSTED_TYPES))}. Add them deliberately after review; never "
        f"call get_untrusted_types and trust the result at load time"
    )
    reloaded = sio.load(path, trusted=list(TRUSTED_TYPES))
    assert np.allclose(reloaded.predict_proba(texts[:20]), model.predict_proba(texts[:20]))


def test_a_tampered_artifact_changes_the_digest(tmp_path):
    model, _, _ = _fitted()
    path = tmp_path / "toxic-clf.skops"
    before = dump_model(model, path)
    path.write_bytes(path.read_bytes() + b"\x00")
    assert artifact_digest(path) != before
```

`tests/unit/test_model_card.py`:
```python
from model.labels import LABELS
from model.model_card import render_model_card


def _card():
    return render_model_card(
        model_version="toxic-clf:v1",
        model_digest="sha256:" + "e" * 64,
        data_version="d" * 64,
        git_sha="9f1c2ab",
        registry_url="https://wandb.ai/rocklambros/model-registry/artifacts/model/toxic-clf",
        metrics={
            "macro_f1": 0.7412, "macro_pr_auc": 0.6810, "accuracy": 0.9721,
            **{f"f1/{label}": 0.7 for label in LABELS},
            **{f"pr_auc/{label}": 0.6 for label in LABELS},
            **{f"accuracy/{label}": 0.97 for label in LABELS},
        },
        cis={f"pr_auc/{label}": (0.55, 0.66, 72, True) for label in LABELS},
        thresholds={label: 0.41 for label in LABELS},
        fairness_markdown="| muslim | 300 | 12 | 0.04 | 0.31 | 0.29 | [0.24, 0.34] | 4.10 |\n",
        n_train=114_000,
        n_test=20_100,
    )


def test_the_card_records_the_digest_independently_of_wandb():
    card = _card()
    assert "sha256:" + "e" * 64 in card
    assert "MODEL_DIGEST" in card


def test_the_card_carries_the_public_registry_url_and_the_promoted_stage():
    card = _card()
    assert "wandb.ai/rocklambros/model-registry/artifacts/model/toxic-clf" in card
    assert "production" in card


def test_the_card_has_a_fairness_section_with_the_slice_table():
    card = _card()
    assert "## Fairness" in card
    assert "| muslim |" in card
    assert "no identity annotations" in card


def test_the_card_discloses_the_white_box_evasion_the_public_registry_accepts():
    card = _card()
    assert "## Limitations and accepted risks" in card
    assert "white-box" in card
    assert "coefficient vector" in card
    assert "the human-review queue does not mitigate" in card


def test_the_card_states_accuracy_and_immediately_qualifies_it():
    card = _card()
    assert "0.9721" in card
    assert "not a promotion metric" in card


def test_the_card_names_every_label_and_its_threshold():
    card = _card()
    for label in LABELS:
        assert f"| {label} |" in card
    assert "0.41" in card


def test_the_card_is_deterministic_for_the_same_inputs():
    assert _card() == _card()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_evaluate.py tests/unit/test_model_card.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.evaluate'`

- [ ] **Step 3: Write minimal implementation**

`model/trusted_types.py`:
```python
"""The explicit static skops allowlist, consumed by backend/model_loader.py in Phase 2.

`skops.io.load(..., trusted=True)` was removed from the library precisely to stop the
get-untrusted-types-then-trust-all pattern, which silently voids the control. This list is
written by hand, reviewed, and asserted to be a superset of what the artifact actually needs.
When a modelling change adds a type, the Phase 1 test goes red and the addition is a deliberate
edit here rather than a runtime shrug.
"""

TRUSTED_TYPES: tuple[str, ...] = (
    "numpy.dtype",
    "numpy.int32",
    "numpy.int64",
    "numpy.float64",
    "numpy.ndarray",
    "scipy.sparse._csr.csr_matrix",
    "sklearn.calibration.CalibratedClassifierCV",
    "sklearn.calibration._CalibratedClassifier",
    "sklearn.calibration._SigmoidCalibration",
    "sklearn.feature_extraction.text.TfidfTransformer",
    "sklearn.feature_extraction.text.TfidfVectorizer",
    "sklearn.linear_model._logistic.LogisticRegression",
    "sklearn.multiclass.OneVsRestClassifier",
    "sklearn.pipeline.FeatureUnion",
    "sklearn.pipeline.Pipeline",
    "sklearn.preprocessing._label.LabelBinarizer",
    "sklearn.preprocessing._label.LabelEncoder",
)
```

`model/evaluate.py`:
```python
"""The single guarded evaluation of the held-out test set.

Discipline this enforces, from delivery spec section 6.1:

- The test set evaluates the ONE model cross-validation already chose. It never *chooses*
  between candidates: picking the better of two test numbers is selection on the test set and
  biases the winner upward.
- The ledger entry is written BEFORE the metrics are returned, so a crash downstream cannot
  leave the test set quietly re-runnable.
- The guard is a git-tracked file, not process state, because RunPod pods are ephemeral.
"""

import hashlib
from pathlib import Path

import numpy as np
import skops.io as sio
from sklearn.metrics import average_precision_score

from model.fairness import identity_fairness_report
from model.labels import LABELS
from model.ledger import LEDGER_PATH, record_touch
from model.metrics import compute_metrics, stratified_bootstrap_ci


def artifact_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump_model(model, path: Path) -> str:
    """Serialize with skops. Never pickle, never joblib."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sio.dump(model, path)
    return artifact_digest(path)


def evaluate_on_test(
    *,
    bundle,
    model,
    thresholds: dict[str, float],
    git_sha: str,
    run_id: str,
    ledger_path: Path = LEDGER_PATH,
    n_boot: int = 1000,
    seed: int = 42,
) -> dict:
    test_df = bundle.test_df
    texts = test_df["comment_text"].tolist()
    y_true = test_df[list(LABELS)].to_numpy().astype(int)
    y_prob = np.asarray(model.predict_proba(texts), dtype=float)

    metrics = compute_metrics(y_true, y_prob, thresholds)
    record_touch(
        bundle.data_version,
        git_sha=git_sha,
        run_id=run_id,
        macro_f1=metrics["macro_f1"],
        path=ledger_path,
    )

    cis = {
        f"pr_auc/{label}": stratified_bootstrap_ci(
            y_true[:, j], y_prob[:, j], average_precision_score, n_boot=n_boot, seed=seed
        )
        for j, label in enumerate(LABELS)
    }
    toxic = LABELS.index("toxic")
    fairness = identity_fairness_report(
        texts,
        y_true[:, toxic],
        (y_prob[:, toxic] >= thresholds["toxic"]).astype(int),
        y_prob[:, toxic],
        n_boot=n_boot,
        seed=seed,
    )
    return {
        "metrics": metrics,
        "cis": cis,
        "fairness": fairness,
        "y_prob": y_prob,
        "n_test": len(texts),
    }
```

`model/model_card.py`:
```python
"""MODEL_CARD.md. SECURITY.md already cites it, so it stops being a promise here."""

from model.labels import LABELS


def render_model_card(
    *,
    model_version: str,
    model_digest: str,
    data_version: str,
    git_sha: str,
    registry_url: str,
    metrics: dict,
    cis: dict,
    thresholds: dict[str, float],
    fairness_markdown: str,
    n_train: int,
    n_test: int,
) -> str:
    rows = []
    for label in LABELS:
        lo, hi, n_pos, low_power = cis[f"pr_auc/{label}"]
        note = "low power" if low_power else ""
        rows.append(
            f"| {label} | {thresholds[label]:.2f} | {metrics[f'f1/{label}']:.4f} | "
            f"{metrics[f'pr_auc/{label}']:.4f} | [{lo:.3f}, {hi:.3f}] | {n_pos} | "
            f"{metrics[f'accuracy/{label}']:.4f} | {note} |"
        )
    return f"""# Model card: toxic-clf

## Identity and provenance

| Field | Value |
|---|---|
| Model version | `{model_version}` |
| **MODEL_DIGEST** | `{model_digest}` |
| Data version | `{data_version}` |
| Code version (git) | `{git_sha}` |
| Registry (public) | {registry_url} |
| Promoted stage | `production` |
| Training rows | {n_train} |
| Held-out test rows | {n_test} |

The digest is recorded **here, in git**, not only in the registry. The artifact and its expected
digest must not live in the same trust domain under one API key, or a poisoned artifact carries
its own proof of integrity.

## Intended use and out-of-scope use

Moderation triage for English Wikipedia-style comments: score six toxicity labels, flag, and
route borderline items to a human reviewer. **Not** an automated ban system, not a decision of
record, and not validated on any language other than English.

## Architecture

TF-IDF over word 1-2 grams (`max_features=200,000`) plus `char_wb` 3-5 grams
(`max_features=100,000`), feeding `OneVsRestClassifier(CalibratedClassifierCV(
LogisticRegression(solver='liblinear', class_weight='balanced'), cv=5, method='sigmoid'))`.
Calibration is cross-fitted inside the one-vs-rest wrapper; thresholds are tuned on out-of-fold
probabilities from models that never saw those rows.

## Metrics on the held-out test set

Headline: **macro-F1 {metrics['macro_f1']:.4f}**, macro PR-AUC {metrics['macro_pr_auc']:.4f}.

| label | threshold | F1 | PR-AUC | PR-AUC 95% CI | test positives | accuracy | note |
|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Mean per-label **accuracy {metrics['accuracy']:.4f}**. Accuracy is reported because rubric 1.2
and 3.2 name it, and it is **not a promotion metric**: an all-negative predictor scores about 90%
on this corpus while catching nothing. Promotion is decided on macro-F1.

Confidence intervals come from a stratified bootstrap that preserves the positive count in every
resample. A naive bootstrap on a small stratum loses all positives sometimes and scores 0.0 with
only a warning, which drags the lower bound to the floor and hides the uncertainty it was meant
to show.

The held-out test set was evaluated **once**, on the model cross-validation had already chosen.
The touch is recorded in `docs/test-set-touch-log.md`.

## Fairness

{fairness_markdown}

Method: per-identity-term slices of the held-out test set, following `auditing-model-fairness`.
The metric that matters here is the false-positive rate among the **non-toxic** rows of each
slice, because Jigsaw's documented unintended bias is that comments which merely *mention* an
identity group are over-flagged. The original six-label Jigsaw corpus carries
**no identity annotations**, so a slice is a term match and therefore a noisy proxy: it captures
who is talked about, not who is speaking. Under-powered groups are reported with wide intervals
rather than dropped. No fair / not fair verdict is issued.

## Limitations and accepted risks

- **White-box evasion via the public registry (accepted, owner decision 2026-07-31).** The
  registry page is public and the artifact is a linear model, so the exact **coefficient vector**
  and every per-label decision boundary are downloadable. Evasion becomes an offline optimisation
  with zero queries against the service and no log entry. This is a deliberate trade of
  adversarial robustness for the graded evidence rubric 1.3 asks for. **The human-review queue
  does not mitigate it**, because a successful evasion is never flagged and therefore never
  enqueued. Compensating controls that remain: the `/predict` rate limit, the input-size cap, and
  the demo API key or source allowlist.
- **Residual obfuscation evasion.** Serving-path normalization (NFKC, homoglyph folding,
  lowercase, whitespace collapse) defeats simple tricks. Cross-script homoglyphs and heavy
  paraphrase still get through.
- **Single reviewer behind a shared secret.** Not a real authentication system. Acceptable for a
  class project, named here so nobody mistakes it for one.
- **English only.** Trained on the English six-label Jigsaw corpus.
- **Raw comment text.** Never written to W&B or to application logs. Retained in the database for
  `INPUT_TEXT_RETENTION_DAYS` (default 30), then nulled while the rest of the row survives for
  monitoring.
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_evaluate.py tests/unit/test_model_card.py -v`
Expected: `12 passed`

If `test_the_artifact_round_trips_through_skops_with_a_static_allowlist` fails, the assertion
message names the missing types. Read them, satisfy yourself each one is a scikit-learn or numpy
internal rather than something arbitrary, then add them to `TRUSTED_TYPES` by hand. Do **not**
change the test to trust whatever the artifact asks for.

- [ ] **Step 5: Commit**

```bash
git add model/evaluate.py model/trusted_types.py model/model_card.py tests/unit/test_evaluate.py tests/unit/test_model_card.py
git commit -m "Add guarded held-out evaluation, skops serialization, and the model card"
```

---

### Task 17: The training driver and the Phase 1 gate

**Files:**
- Create: `model/train_classical.py`
- Test: `tests/unit/test_train_classical.py`

**Interfaces produced:** `make train`, `make footprint`, and every artifact Phase 2 consumes

- [ ] **Step 1: Write the failing test**

`tests/unit/test_train_classical.py`:
```python
import json

import numpy as np
import pytest

from model.labels import LABELS
from model.train_classical import build_baseline_run, run_pipeline_phase, write_footprint_doc
from tests.fixtures.synthetic import make_corpus


def test_a_prior_baseline_run_is_logged_before_the_real_model():
    """Baseline-first is doctrine: without it 'macro-F1 0.74' has no reference point."""
    _texts, y = make_corpus(n=400)
    baseline = build_baseline_run(y)
    assert baseline["model_name"] == "baseline-most-frequent"
    assert baseline["metrics"]["macro_f1"] == pytest.approx(0.0)
    assert baseline["metrics"]["accuracy"] > 0.5, (
        "the all-negative baseline scores high accuracy and zero macro-F1, which is exactly why "
        "accuracy is banned as a promotion metric"
    )


def test_the_footprint_document_names_the_caps_and_the_projection(tmp_path):
    texts, _ = make_corpus(n=400)
    path = tmp_path / "feature-footprint.md"
    footprint = write_footprint_doc(texts, path, n_rows_full=135_000, max_bytes=2_000_000_000)
    body = path.read_text()
    assert "max_features" in body
    assert str(footprint.n_features) in body
    assert "projected" in body
    assert "135000" in body.replace(",", "")


def test_the_phase_produces_every_artifact_phase_two_consumes(tmp_path):
    texts, y = make_corpus(n=600)
    # Three folds of 200 rows: enough to exercise the real out-of-fold path quickly.
    idx = np.arange(600)
    folds = [(idx[idx % 3 != k], idx[idx % 3 == k]) for k in range(3)]
    out = run_pipeline_phase(
        texts=texts, y=y, fold_indices=folds, data_version="a" * 64,
        artifacts_dir=tmp_path, calibration_folds=3,
    )
    assert (tmp_path / "toxic-clf.skops").exists()
    assert (tmp_path / "thresholds.json").exists()
    thresholds = json.loads((tmp_path / "thresholds.json").read_text())
    assert list(thresholds.keys()) == list(LABELS)
    assert out["digest"].startswith("sha256:")
    assert out["oof"].y_prob.shape == (600, len(LABELS))
    assert set(out["calibration_gain"]) == set(LABELS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_train_classical.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.train_classical'`

- [ ] **Step 3: Write minimal implementation**

`model/train_classical.py`:
```python
"""Phase 1 driver: train, calibrate, tune, evaluate once, register, promote.

Order matters and is not negotiable:
  prepare -> firewall gate -> footprint budget -> baseline run -> out-of-fold fit
  -> tune thresholds on out-of-fold only -> final fit on all training rows
  -> convergence assertion -> skops dump + digest -> ONE held-out evaluation
  -> baseline flag rates -> fairness report -> model card -> W&B -> registry.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.multiclass import OneVsRestClassifier
from sklearn.pipeline import Pipeline

from model.baseline_rates import compute_baseline_flag_rates, write_baseline_flag_rates
from model.calibration import calibration_gain
from model.data.firewall_check import assert_no_leakage
from model.data.prepare import SplitConfig, prepare_dataset
from model.evaluate import dump_model, evaluate_on_test
from model.fairness import render_fairness_markdown
from model.labels import LABELS
from model.metrics import compute_metrics, select_best_run
from model.model_card import render_model_card
from model.oof import cross_val_probabilities
from model.pipeline import (
    CHAR_MAX_FEATURES,
    WORD_MAX_FEATURES,
    assert_converged,
    assert_feature_budget,
    build_classical_pipeline,
    measure_feature_footprint,
)
from model.registry import COLLECTION, PROMOTED_ALIAS, register_and_promote
from model.seeds import run_metadata, set_all_seeds
from model.thresholds import tune_thresholds, write_thresholds
from model.tracking import build_run_config, build_run_summary, log_run

MEMORY_BUDGET_BYTES = 2_000_000_000  # EC2 #1 is a 4 GB t4g.medium; the matrix gets half


def build_uncalibrated_pipeline():
    """The same features and estimator with the calibrator removed.

    Exists only so `calibration_gain` has an honest out-of-fold comparison. A bare
    `class_weight='balanced'` logistic regression is what the design would ship if calibration
    were dropped, so this is exactly the right counterfactual.
    """
    calibrated = build_classical_pipeline()
    features = calibrated.named_steps["features"]
    base = calibrated.named_steps["clf"].estimator.estimator
    return Pipeline([("features", features), ("clf", OneVsRestClassifier(base, n_jobs=1))])


def build_baseline_run(y) -> dict:
    """The trivial most-frequent baseline. Logged first so every later number has a reference."""
    y = np.asarray(y).astype(int)
    majority = (y.mean(axis=0) >= 0.5).astype(int)
    y_prob = np.tile(majority.astype(float), (len(y), 1))
    return {
        "model_name": "baseline-most-frequent",
        "metrics": compute_metrics(y, y_prob, {label: 0.5 for label in LABELS}),
    }


def write_footprint_doc(texts, path: Path, *, n_rows_full: int, max_bytes: int):
    footprint = measure_feature_footprint(texts)
    projected = assert_feature_budget(footprint, n_rows_full=n_rows_full, max_bytes=max_bytes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Measured TF-IDF feature footprint\n"
        "\n"
        "The delivery spec caps both vectorizers with `max_features` and requires the resulting\n"
        "memory to be measured rather than assumed. Uncapped, the word vectorizer reaches roughly\n"
        "4.7M features and a ~1.7 GB matrix at 135k rows, which does not fit the 4 GB EC2 #1.\n"
        "\n"
        f"| quantity | value |\n"
        f"|---|---|\n"
        f"| word `max_features` cap | {WORD_MAX_FEATURES} |\n"
        f"| char `max_features` cap | {CHAR_MAX_FEATURES} |\n"
        f"| rows measured | {footprint.n_rows} |\n"
        f"| word features realised | {footprint.n_word_features} |\n"
        f"| char features realised | {footprint.n_char_features} |\n"
        f"| total features | {footprint.n_features} |\n"
        f"| stored non-zeros | {footprint.nnz} |\n"
        f"| measured CSR bytes | {footprint.matrix_bytes} |\n"
        f"| bytes per row | {footprint.bytes_per_row:.0f} |\n"
        f"| projected bytes at {n_rows_full} rows | {projected} |\n"
        f"| budget | {max_bytes} |\n"
        "\n"
        "Bytes are the real `data`, `indices`, and `indptr` array sizes of the fitted CSR matrix,\n"
        "not a model of them.\n"
    )
    return footprint


def run_pipeline_phase(
    *, texts, y, fold_indices, data_version: str, artifacts_dir: Path, calibration_folds: int = 5
) -> dict:
    """Out-of-fold fit, threshold tuning, final fit, and serialization."""
    artifacts_dir = Path(artifacts_dir)

    def factory():
        return build_classical_pipeline(calibration_folds=calibration_folds)

    oof = cross_val_probabilities(factory, texts, y, fold_indices, data_version)
    thresholds = tune_thresholds(oof)
    write_thresholds(artifacts_dir / "thresholds.json", thresholds, data_version=data_version)

    # The calibration gain must be measured out-of-fold too, or it is measured on the rows the
    # calibrator was fitted on and always looks good. This second pass costs roughly a fifth of
    # the first, because it has no inner calibration cross-fit.
    uncal_oof = cross_val_probabilities(
        build_uncalibrated_pipeline, texts, y, fold_indices, data_version
    )
    gains = {
        label: calibration_gain(
            oof.y_true[:, j], uncal_oof.y_prob[:, j], oof.y_prob[:, j]
        )
        for j, label in enumerate(LABELS)
    }

    final = build_classical_pipeline(calibration_folds=calibration_folds).fit(texts, y)
    assert_converged(final)
    digest = dump_model(final, artifacts_dir / "toxic-clf.skops")
    return {
        "model": final,
        "oof": oof,
        "thresholds": thresholds,
        "digest": digest,
        "calibration_gain": gains,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1: train, evaluate once, register.")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--entity", default=None)
    parser.add_argument("--project", default="mlops-toxic-moderation")
    parser.add_argument("--footprint-only", action="store_true")
    parser.add_argument("--no-wandb", action="store_true")
    args = parser.parse_args()

    set_all_seeds(args.seed)
    meta = run_metadata(args.seed)
    bundle = prepare_dataset(args.csv, SplitConfig(seed=args.seed))
    assert_no_leakage(bundle)

    train_texts = bundle.train_df["comment_text"].tolist()
    y_train = bundle.train_df[list(LABELS)].to_numpy().astype(int)

    footprint = write_footprint_doc(
        train_texts,
        Path("docs/feature-footprint.md"),
        n_rows_full=len(train_texts),
        max_bytes=MEMORY_BUDGET_BYTES,
    )
    print(f"features={footprint.n_features} bytes_per_row={footprint.bytes_per_row:.0f}")
    if args.footprint_only:
        return

    baseline = build_baseline_run(y_train)
    phase = run_pipeline_phase(
        texts=train_texts,
        y=y_train,
        fold_indices=bundle.fold_indices,
        data_version=bundle.data_version,
        artifacts_dir=args.artifacts,
    )
    cv_metrics = compute_metrics(phase["oof"].y_true, phase["oof"].y_prob, phase["thresholds"])
    winner = select_best_run(
        [{"model_name": baseline["model_name"], **baseline["metrics"]},
         {"model_name": "classical-tfidf-ovr-lr", **cv_metrics}]
    )
    if winner["model_name"] != "classical-tfidf-ovr-lr":
        raise SystemExit("the trivial baseline beat the model on macro-F1; stop and investigate")

    result = evaluate_on_test(
        bundle=bundle,
        model=phase["model"],
        thresholds=phase["thresholds"],
        git_sha=meta["git_sha"],
        run_id=meta["timestamp_utc"],
    )
    model_version = f"{COLLECTION}:latest"
    rates = compute_baseline_flag_rates(
        result["y_prob"],
        data_version=bundle.data_version,
        model_version=model_version,
        model_digest=phase["digest"],
        thresholds=phase["thresholds"],
    )
    write_baseline_flag_rates(args.artifacts / "baseline_flag_rates.json", rates)

    fairness_md = render_fairness_markdown(result["fairness"])
    Path("docs/fairness-report.md").write_text(fairness_md)
    (args.artifacts / "metrics.json").write_text(
        json.dumps({"cv": cv_metrics, "test": result["metrics"], "baseline": baseline["metrics"]},
                   indent=2, sort_keys=True) + "\n"
    )

    # The model card is written before W&B, not after, so a registry outage cannot leave the
    # digest unrecorded in git. That co-location is the thing the card exists to break.
    registry_url = (
        f"https://wandb.ai/{args.entity}/model-registry/artifacts/model/{COLLECTION}"
    )
    Path("MODEL_CARD.md").write_text(
        render_model_card(
            model_version=model_version,
            model_digest=phase["digest"],
            data_version=bundle.data_version,
            git_sha=meta["git_sha"],
            registry_url=registry_url,
            metrics=result["metrics"],
            cis={k: (ci.lo, ci.hi, ci.n_pos, ci.low_power) for k, ci in result["cis"].items()},
            thresholds=phase["thresholds"],
            fairness_markdown=fairness_md,
            n_train=len(train_texts),
            n_test=result["n_test"],
        )
    )
    print(f"MODEL_DIGEST={phase['digest']}")

    if args.no_wandb:
        print("skipping W&B (--no-wandb)")
        return

    import wandb

    with wandb.init(project=args.project, entity=args.entity, job_type="train") as run:
        config = build_run_config(
            git_sha=meta["git_sha"],
            seed=args.seed,
            data_version=bundle.data_version,
            model_name="classical-tfidf-ovr-lr",
            hyperparameters={
                "solver": "liblinear",
                "C": 1.0,
                "class_weight": "balanced",
                "word_max_features": WORD_MAX_FEATURES,
                "char_max_features": CHAR_MAX_FEATURES,
                "calibration_method": "sigmoid",
                "calibration_folds": 5,
                "n_features_measured": footprint.n_features,
            },
            thresholds=phase["thresholds"],
        )
        summary = build_run_summary(result["metrics"], result["cis"])
        summary.update({f"cv/{k}": v for k, v in cv_metrics.items()})
        summary.update({f"baseline/{k}": v for k, v in baseline["metrics"].items()})
        summary.update(
            {f"calibration/{label}/{k}": v
             for label, gain in phase["calibration_gain"].items()
             for k, v in gain.items() if isinstance(v, (int, float, bool))}
        )
        summary["fairness/max_fpr_gap"] = result["fairness"]["max_fpr_gap"]
        summary["fairness/worst_term"] = result["fairness"]["worst_term"]
        log_run(run, config=config, summary=summary, corpus=train_texts[:5000])

        rates_artifact = wandb.Artifact("baseline-flag-rates", type="dataset")
        rates_artifact.add_file(str(args.artifacts / "baseline_flag_rates.json"))
        rates_artifact.add_file(str(args.artifacts / "thresholds.json"))
        run.log_artifact(rates_artifact)

        target = register_and_promote(run, str(args.artifacts / "toxic-clf.skops"))
        print(f"registered {target} with alias {PROMOTED_ALIAS!r}")
        print(f"now make {registry_url} public, then: make verify-registry")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_train_classical.py -v`
Expected: `3 passed`

- [ ] **Step 5: Run the real thing and check the gate**

```bash
make footprint                      # measure before committing two hours to a fit
cat docs/feature-footprint.md
make perf                           # the wall-clock budget check
make train
```

Exit criteria, all of which must hold before the PR:

- [ ] `docs/feature-footprint.md` exists and its projected bytes are under 2.0 GB.
- [ ] `make perf` passes: one fit at n=15,000 in under 60 s.
- [ ] `make train` completes with no `ConvergenceWarning` and `assert_converged` does not raise.
- [ ] `artifacts/toxic-clf.skops`, `thresholds.json`, `baseline_flag_rates.json`, `metrics.json` all exist.
- [ ] The classical model beats the most-frequent baseline on macro-F1.
- [ ] Per-label calibrated Brier beats uncalibrated Brier for at least four of six labels; any label where it does not is named in the model card.
- [ ] `docs/test-set-touch-log.md` has exactly one new row, and a second `make train` on the same `data_version` **fails** with `TestSetAlreadyTouched`.
- [ ] `docs/fairness-report.md` exists and every term slice is present, including the low-power ones.
- [ ] `MODEL_CARD.md` exists, carries `MODEL_DIGEST`, and its digest matches `sha256sum artifacts/toxic-clf.skops`.
- [ ] The W&B run page shows git SHA, hyperparameters, `data_version`, and **accuracy** among the metrics.
- [ ] `env -u WANDB_API_KEY HOME=$(mktemp -d) .venv/bin/python -m model.registry --entity <entity>` prints OK.
- [ ] No raw comment text appears anywhere on the W&B project.
- [ ] `make test && make lint` are green.

Deliberate second run to prove the guard, then restore:

```bash
make train ; echo "exit=$?"       # expect a TestSetAlreadyTouched traceback and a non-zero exit
```

- [ ] **Step 6: Commit and open the PR**

```bash
git add docs/feature-footprint.md docs/fairness-report.md docs/test-set-touch-log.md MODEL_CARD.md model/train_classical.py tests/unit/test_train_classical.py
git commit -m "Add the Phase 1 training driver, measured footprint, fairness report, and model card"
git push -u origin feat/phase-1-train-register
gh pr create --base main --title "Phase 1: train, calibrate, tune thresholds, evaluate, register" \
  --body "Calibrated one-vs-rest TF-IDF classifier with liblinear, capped vectorizers and a measured 
memory footprint, thresholds tuned on out-of-fold data only, stratified bootstrap intervals, a 
per-identity-term fairness slice, baseline flag rates for the drift panel, and a single guarded 
held-out evaluation recorded in a git-tracked ledger. Registry promoted to production and 
verified publicly readable without credentials."
```

---

### Task 18 (cut-line): RunPod sweep harness with the canonical lifecycle

**Cut-line.** Delivery spec section 8 puts the W&B sweep on RunPod at position 2 of the ordered
cut list and DistilBERT at position 3. Run this task **only** if the end-of-day-8 checkpoint
passed. If it did not, skip it entirely — one tracked comparison run already satisfies
"experiment tracking", and Task 17 produced two (baseline and classical).

**DistilBERT, ONNX export, and the re-scorer are not planned here.** They are Slice 3 work behind
the same checkpoint. If they are revived, `problem_type="multi_label_classification"` is
mandatory or HF Trainer silently trains softmax cross-entropy on a six-column target.

**Files:**
- Create: `infra/runpod/pods.py`, `infra/runpod/reap.py`, `.github/workflows/runpod-reaper.yml`
- Test: `tests/unit/test_runpod_reap.py`

The lifecycle rules are not negotiable, because a forgotten GPU pod is the single largest
uncontrolled cost in this project:

1. **Atomic registry write before the readiness wait.** The pod id reaches disk before anything
   blocks. A crash during the wait must never orphan a billing pod that nothing knows about.
2. **`trap EXIT` / `finally` teardown**, so the pod dies on success, failure, and interrupt.
3. **Name-guard allowlist.** Only pods whose name starts with an allowed prefix may be deleted.
4. **Orphan-safe reconcile.** Live pods absent from the registry are reported loudly and
   **never** auto-terminated. A human decides.
5. **Dry-run by default.** `--execute` is required to issue a single DELETE.
6. **Secrets from `pass` with a 5-second timeout**, never from a shell profile, and bearer tokens
   scrubbed from every exception string.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_runpod_reap.py`:
```python
import json

import pytest

from infra.runpod import reap


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(reap, "load_secret", lambda *_a, **_k: "test-key")


def _registry(tmp_path, entries):
    path = tmp_path / "pods.json"
    path.write_text(json.dumps(entries))
    return path


def test_dry_run_is_the_default_and_issues_no_delete(tmp_path, monkeypatch):
    deleted = []
    monkeypatch.setattr(reap, "_http_delete", lambda *a, **k: deleted.append(a) or _Resp(200))
    monkeypatch.setattr(
        reap, "_http_get", lambda *a, **k: _Resp(200, [{"id": "p1", "name": "toxic-sweep-a"}])
    )
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])
    summary = reap.reconcile(path, execute=False)
    assert deleted == []
    assert len(summary["live_and_ours"]) == 1


def test_the_name_guard_refuses_a_pod_outside_the_allowlist(tmp_path, monkeypatch):
    deleted = []
    monkeypatch.setattr(reap, "_http_delete", lambda *a, **k: deleted.append(a) or _Resp(200))
    path = _registry(tmp_path, [{"name": "someone-elses-pod", "pod_id": "p9"}])
    summary = reap.terminate_all_registered(path, execute=True)
    assert deleted == []
    assert summary["skipped_by_guard"] == [{"name": "someone-elses-pod", "pod_id": "p9"}]


def test_orphans_are_reported_and_never_auto_terminated(tmp_path, monkeypatch, capsys):
    deleted = []
    monkeypatch.setattr(reap, "_http_delete", lambda *a, **k: deleted.append(a) or _Resp(200))
    monkeypatch.setattr(
        reap,
        "_http_get",
        lambda *a, **k: _Resp(200, [{"id": "p1", "name": "toxic-sweep-a"},
                                    {"id": "p2", "name": "mystery-pod"}]),
    )
    path = _registry(tmp_path, [{"name": "toxic-sweep-a", "pod_id": "p1"}])
    summary = reap.reconcile(path, execute=True)
    assert [o["id"] for o in summary["orphans"]] == ["p2"]
    assert [d[0] for d in deleted] == ["https://rest.runpod.io/v1/pods/p1"]
    assert "ORPHAN" in capsys.readouterr().err


def test_a_missing_pod_is_idempotent_success(monkeypatch):
    monkeypatch.setattr(reap, "_http_delete", lambda *a, **k: _Resp(404, text="gone"))
    assert reap.terminate_pod("p1") is True


def test_a_server_error_raises_with_the_bearer_token_scrubbed(monkeypatch):
    monkeypatch.setattr(
        reap, "_http_delete", lambda *a, **k: _Resp(500, text="Bearer sk-secret-value failed")
    )
    with pytest.raises(reap.TerminateError) as excinfo:
        reap.terminate_pod("p1")
    assert "sk-secret-value" not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


def test_the_registry_is_written_before_the_readiness_wait(tmp_path, monkeypatch):
    """A crash during the wait must not orphan a pod nothing knows about."""
    from infra.runpod import pods

    path = tmp_path / "pods.json"
    monkeypatch.setattr(pods, "load_secret", lambda *_a, **_k: "test-key")
    monkeypatch.setattr(pods, "_create_pod", lambda **_k: {"id": "p1"})

    def exploding_wait(_pod_id, **_kw):
        assert json.loads(path.read_text())[0]["pod_id"] == "p1", (
            "the registry must be on disk before the readiness wait can fail"
        )
        raise TimeoutError("pod never became ready")

    monkeypatch.setattr(pods, "wait_until_ready", exploding_wait)
    with pytest.raises(TimeoutError):
        pods.launch_pod(name="toxic-sweep-a", registry_path=path)
    assert json.loads(path.read_text())[0]["pod_id"] == "p1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_runpod_reap.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'infra.runpod'`

- [ ] **Step 3: Write minimal implementation**

Create `infra/__init__.py` and `infra/runpod/__init__.py` (both empty).

`infra/runpod/reap.py`:
```python
"""Safe RunPod teardown: name-guarded, orphan-safe, dry-run by default."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

REST_BASE = "https://rest.runpod.io/v1"
DEFAULT_ALLOW: tuple[str, ...] = ("toxic-sweep-",)
DEFAULT_REGISTRY = Path("infra/runpod/pods.json")


class TerminateError(RuntimeError):
    """A non-recoverable HTTP error during termination."""


def load_secret(pass_name: str, env_var: str) -> str:
    value = os.environ.get(env_var, "")
    if value:
        return value
    try:
        result = subprocess.run(
            ["pass", "show", pass_name], capture_output=True, text=True, check=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"'pass show {pass_name}' timed out after 5s") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"'pass show {pass_name}' failed (exit {exc.returncode}); secret not loaded"
        ) from None
    return result.stdout.strip()


def _scrub(text: str) -> str:
    return re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", text)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {load_secret('runpod/api-key', 'RUNPOD_API_KEY')}",
        "Content-Type": "application/json",
    }


def _http_get(url: str, headers: dict[str, str]) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=30.0)


def _http_delete(url: str, headers: dict[str, str]) -> httpx.Response:
    return httpx.delete(url, headers=headers, timeout=30.0)


def load_registry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text().strip()
    return json.loads(text) if text else []


def list_live_pods() -> list[dict[str, Any]]:
    resp = _http_get(f"{REST_BASE}/pods", _headers())
    if resp.status_code != 200:
        raise TerminateError(_scrub(f"list_live_pods failed ({resp.status_code}): {resp.text}"))
    data = resp.json()
    return data if isinstance(data, list) else data.get("pods", [])


def terminate_pod(pod_id: str) -> bool:
    resp = _http_delete(f"{REST_BASE}/pods/{pod_id}", _headers())
    if resp.status_code == 404 or 200 <= resp.status_code < 300:
        return True  # 404 is idempotent success: already gone
    raise TerminateError(
        _scrub(f"terminate_pod({pod_id}) failed ({resp.status_code}): {resp.text}")
    )


def _allowed(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name.startswith(prefix) for prefix in prefixes)


def terminate_all_registered(
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    name_allow_prefixes: tuple[str, ...] = DEFAULT_ALLOW,
    execute: bool,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "would_terminate": [], "terminated": [], "skipped_by_guard": [], "errors": []
    }
    for entry in load_registry(registry_path):
        name, pod_id = entry.get("name", ""), entry.get("pod_id", "")
        if not _allowed(name, name_allow_prefixes):
            print(f"  SKIP (guard): {name!r} is not in the allowlist", file=sys.stderr)
            summary["skipped_by_guard"].append({"name": name, "pod_id": pod_id})
            continue
        if not execute:
            summary["would_terminate"].append({"name": name, "pod_id": pod_id})
            continue
        try:
            terminate_pod(pod_id)
            summary["terminated"].append({"name": name, "pod_id": pod_id})
        except (TerminateError, httpx.HTTPError) as exc:
            summary["errors"].append({"name": name, "pod_id": pod_id, "error": _scrub(str(exc))})
    return summary


def reconcile(registry_path: Path = DEFAULT_REGISTRY, *, execute: bool) -> dict[str, Any]:
    registered = load_registry(registry_path)
    registered_ids = {e["pod_id"] for e in registered}
    live = list_live_pods()
    live_ids = {p["id"] for p in live}

    summary: dict[str, Any] = {
        "registered_gone": [e for e in registered if e["pod_id"] not in live_ids],
        "live_and_ours": [e for e in registered if e["pod_id"] in live_ids],
        "orphans": [p for p in live if p["id"] not in registered_ids],
        "terminated": [], "skipped_by_guard": [], "errors": [],
    }
    if summary["orphans"]:
        print("\n*** ORPHAN PODS DETECTED - not auto-terminated ***", file=sys.stderr)
        for orphan in summary["orphans"]:
            print(
                f"    ORPHAN id={orphan['id']} name={orphan.get('name', '?')}", file=sys.stderr
            )
        print("*** A human must decide what to do with these ***\n", file=sys.stderr)

    if execute:
        for entry in summary["live_and_ours"]:
            name, pod_id = entry.get("name", ""), entry["pod_id"]
            if not _allowed(name, DEFAULT_ALLOW):
                summary["skipped_by_guard"].append({"name": name, "pod_id": pod_id})
                continue
            try:
                terminate_pod(pod_id)
                summary["terminated"].append({"name": name, "pod_id": pod_id})
            except (TerminateError, httpx.HTTPError) as exc:
                summary["errors"].append(
                    {"name": name, "pod_id": pod_id, "error": _scrub(str(exc))}
                )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reap RunPod pods. Default is a dry-run reconcile; no DELETE is issued."
    )
    parser.add_argument("--execute", action="store_true", help="actually terminate")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    args = parser.parse_args()
    if not args.execute:
        print("=== DRY RUN (no DELETE calls) ===")
    summary = reconcile(args.registry, execute=args.execute)
    print(
        f"  live_and_ours={len(summary['live_and_ours'])} "
        f"orphans={len(summary['orphans'])} terminated={len(summary['terminated'])} "
        f"skipped_by_guard={len(summary['skipped_by_guard'])} errors={len(summary['errors'])}"
    )
    if summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
```

`infra/runpod/pods.py`:
```python
"""Launch a sweep pod. The registry write is atomic and happens BEFORE the readiness wait."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from infra.runpod.reap import DEFAULT_REGISTRY, REST_BASE, load_secret, terminate_pod

SWEEP_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
GPU_TYPES = ("NVIDIA L4", "NVIDIA GeForce RTX 4090", "NVIDIA A40")


def _create_pod(**payload: Any) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {load_secret('runpod/api-key', 'RUNPOD_API_KEY')}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(f"{REST_BASE}/pods", headers=headers, json=payload, timeout=30.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"pod creation failed ({resp.status_code})")
    return resp.json()


def _append_registry_atomically(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else []
    existing.append(entry)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    os.replace(tmp, path)   # atomic rename: the file is never half-written


def wait_until_ready(pod_id: str, *, timeout_s: int = 900, interval_s: int = 15) -> None:
    headers = {
        "Authorization": f"Bearer {load_secret('runpod/api-key', 'RUNPOD_API_KEY')}",
        "Content-Type": "application/json",
    }
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = httpx.get(f"{REST_BASE}/pods/{pod_id}", headers=headers, timeout=30.0)
        if resp.status_code == 200 and resp.json().get("desiredStatus") == "RUNNING":
            return
        time.sleep(interval_s)
    raise TimeoutError(f"pod {pod_id} did not become ready within {timeout_s}s")


def launch_pod(
    *,
    name: str,
    registry_path: Path = DEFAULT_REGISTRY,
    gpu_type: str = GPU_TYPES[0],
    image: str = SWEEP_IMAGE,
    container_disk_gb: int = 40,
) -> str:
    """Create a pod, record it, then wait. Never the other way round."""
    pod = _create_pod(
        name=name,
        imageName=image,
        gpuTypeIds=[gpu_type],
        gpuCount=1,
        containerDiskInGb=container_disk_gb,
        volumeInGb=0,
        interruptible=True,          # sweep runs are restartable, so spot is the right price
        supportPublicIp=True,
    )
    pod_id = pod["id"]
    _append_registry_atomically(registry_path, {"name": name, "pod_id": pod_id})
    wait_until_ready(pod_id)
    return pod_id


def run_sweep(name: str, registry_path: Path = DEFAULT_REGISTRY) -> None:
    """Launch, use, and always tear down. The finally block is the whole point."""
    pod_id = None
    try:
        pod_id = launch_pod(name=name, registry_path=registry_path)
        print(f"pod {pod_id} ready; attach the W&B sweep agent to it")
    finally:
        if pod_id is not None:
            terminate_pod(pod_id)
            print(f"terminated {pod_id}")
```

`.github/workflows/runpod-reaper.yml`:
```yaml
name: runpod-reaper

on:
  schedule:
    - cron: "0 * * * *"
  workflow_dispatch:
    inputs:
      execute:
        description: "Actually terminate (default is a dry run)"
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  reap:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2
      - uses: actions/setup-python@0b93645e9fea7318ecaed2b359559ac225c90a2b  # v5.3.0
        with:
          python-version: "3.11"
      - run: pip install httpx==0.27.2
      - name: Reap
        env:
          RUNPOD_API_KEY: ${{ secrets.RUNPOD_API_KEY }}
        run: |
          if [ -z "${RUNPOD_API_KEY}" ]; then
            echo "RUNPOD_API_KEY is not set; nothing to reap" && exit 0
          fi
          python -m infra.runpod.reap ${{ inputs.execute && '--execute' || '' }}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_runpod_reap.py -v`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add infra/__init__.py infra/runpod/ .github/workflows/runpod-reaper.yml tests/unit/test_runpod_reap.py
git commit -m "Add name-guarded orphan-safe RunPod reaper and sweep pod lifecycle"
```

---

## Deferred to Slice 3, behind the day-8 cut-line

These were Phase 1 tasks in the master roadmap. The delivery spec moved them to Slice 3 and put
them on the ordered cut list, so they are named here rather than planned, and the trigger is
recorded so the decision is not re-litigated at 2 a.m.

| Item | Cut-list position | If revived |
|---|---|---|
| W&B hyperparameter sweep on RunPod | 2 | Task 18 provides the pod lifecycle; add `model/sweep.py` with the sweep config |
| DistilBERT fine-tune | 3 | `problem_type="multi_label_classification"` is mandatory, or HF Trainer trains softmax cross-entropy on a six-column target. Early stopping on validation, weight decay, 2-3 epochs, per-epoch train/val gap logged |
| ONNX int8 export and the re-scorer | 3 | Logit parity against the float model on a sample, registered with its own digest |
| The reviewer's second-opinion column | 4 | Phase 3 concern; the reviewer still labels without it |

**Trigger:** if Slice 1 is not serving end-to-end on local compose at the end of day 8, all four
are cut immediately and EC2 #3 stays a `t4g.small`, so no Terraform rework follows.

Whichever way that goes, the held-out test discipline is unchanged: the test set evaluates the
**one** model cross-validation already chose. Even if DistilBERT lands, it never gets a second
number on the test set to be compared against, because picking the better of two test numbers is
selection on the test set.

## Self-Review

**Spec coverage.**

| Normative source | Requirement | Covered by |
|---|---|---|
| delivery-spec §6.1 | Held-out test evaluates the model already chosen; never *chooses* | Tasks 11, 16; the deferred-work note |
| delivery-spec §6.2 | `OneVsRestClassifier(CalibratedClassifierCV(...))`, `method='sigmoid'`, disjoint calibration and tuning folds | Tasks 2, 4, 5, 6 |
| delivery-spec §6.2 | `max_features` caps, `solver='liblinear'`, convergence asserted | Tasks 2, 3, 7 |
| delivery-spec §6.2 | Accuracy logged and shown, never a promotion or comparison metric | Task 9, model card in Task 16 |
| delivery-spec §6.2 | Stratified bootstrap CIs handling zero-positive resamples | Task 8 |
| delivery-spec §6.2 | Single authoritative array-to-dict adapter | Task 1 |
| delivery-spec §6.3 | skops with an explicit static trusted-type allowlist | Task 16 |
| delivery-spec §6.3 | Digest recorded independently of the artifact | Task 16 (git-committed `MODEL_CARD.md`) |
| delivery-spec §6.4 | No raw user text on a public surface | Task 14 |
| delivery-spec §10 | RunPod ephemeral pods, `finally` teardown, reaper, spot | Task 18 |
| delivery-spec §11 | Rubric 1.1 baseline model | Task 17 (`build_baseline_run`) |
| delivery-spec §11 | Rubric 1.2 git SHA, hyperparameters, metrics incl. accuracy, data version | Task 14 |
| delivery-spec §11 | Rubric 1.3 promote to Production **visibly** | Task 15 |
| delivery-spec §11 | Rubric 3.2 predicted-class distribution against a stored baseline | Task 13 |
| delivery-spec §13 | White-box evasion disclosed in the model card | Task 16 |
| premortem H31 | Per-identity-term fairness slice; `MODEL_CARD.md` exists | Tasks 12, 16 |
| master plan | TF-IDF fitted inside the pipeline inside each fold | Tasks 2, 6 |

Every premortem finding assigned to this phase has an owning task with a test that fails if the
finding is unfixed. The mapping is the "Premortem finding coverage" table above; the two id
collisions are called out there rather than papered over.

**Placeholder scan.** Every step carries real code and an exact command. No TODO, no "handle edge
cases", no "similar to", no `...`. The one place this plan deliberately does not ship code is the
Slice 3 deferral above, which is a scope decision recorded with its trigger rather than a gap.

**Code that was executed before it was written down.** Every non-obvious claim in this plan was
run on this box under the available scikit-learn before being committed to paper, because the
premortem's most damaging findings were all "the plan's own code has never been run":

- `CalibratedClassifierCV(OneVsRestClassifier(...))` raising exactly
  `ValueError: y should be a 1d array, got an array of shape (96, 6) instead.`
- The inner nesting fitting a `(n, 6)` target and returning `(n, 6)` probabilities.
- The attribute path `Pipeline["clf"].estimators_[j].calibrated_classifiers_[k].estimator` and
  its `n_iter_`.
- `assert_converged` raising at `max_iter=1` and passing at `max_iter=1000`.
- Sigmoid calibration moving Brier 0.0903 to 0.0284 and ECE 0.1686 to 0.0107 on the pinned
  configuration, which is what makes the Task 5 margins safe.
- The naive bootstrap losing all positives in 38 of 2000 resamples at 4 positives in 120 rows,
  and `average_precision_score` returning `0.0` with only a `UserWarning` on those — silent, not
  loud, which is why the plan says "silently" rather than "crashes".
- The stratified resampler never dropping below the observed positive count.
- Recall-weighted F-beta lowering the `threat`, `severe_toxic`, and `identity_hate` thresholds
  while leaving the weight-1.0 labels untouched, on the seeded overlapping fixture.
- The ledger refusing a second touch **from a separate interpreter**, and the header row not
  being mistaken for a `data_version`.
- The measured CSR footprint, the cap binding both vectorizers, and the budget projection.
- The identity-term slice recovering a 4.4x background false-positive ratio and keeping the
  under-powered group in the report.
- The anonymous W&B GraphQL query returning `access: "USER_READ"` with collections and aliases
  for a public personal `model-registry`, and `null` for a private or absent one — all four
  `check_public_registry` branches exercised live with no credentials.
- The `urllib` request carrying no `Authorization` header.

**Type consistency.** `LABELS` (a tuple) is imported, never re-typed, and indexes every per-label
array by position in `pipeline`, `metrics`, `thresholds`, `baseline_rates`, `evaluate`,
`fairness`, and `model_card`. `predict_proba(texts) -> (n, 6)` matches the master plan's model
interface and is what `cross_val_probabilities`, `evaluate_on_test`, and
`compute_baseline_flag_rates` all consume. `thresholds.json` is `{label: float}` in `LABELS`
order, which is what `backend/policy.decide` expects in Phase 2. `OofPredictions` is the only
type `tune_thresholds` accepts. `CIResult` is produced by both bootstrap helpers and consumed by
`build_run_summary` and `render_model_card` through one destructuring shape. `probs_to_dict` is
the single array-to-dict seam for Phase 2 and Phase 3. `TRUSTED_TYPES` is a `tuple[str, ...]`
that `skops.io.load(..., trusted=list(TRUSTED_TYPES))` takes directly. The four master-plan
Interface Contract drifts this phase touches are corrected in the Interfaces section above.

**Known cost, stated rather than discovered.** The full run is roughly 180 liblinear fits: six
labels by five calibration folds across five outer folds, plus the uncalibrated comparison pass
and the final fit. At the measured liblinear rate that is a small number of hours, not minutes.
`make footprint` and `make perf` exist so that number is known before the run starts rather than
after it fails to finish. If `make perf` projects past three hours, the pre-committed reduction
is `calibration_folds=3`, which is a one-line change in `model/pipeline.py` and costs a small
amount of calibration quality on the rare labels — take it, and record it in the model card.

## Execution Handoff

Two options:

1. **Subagent-Driven (recommended):** a fresh subagent per task with review between tasks.
   REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL:
   `superpowers:executing-plans`.
