# Phase 0: Data Pipeline and Leakage Firewall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reproducible offline pipeline that turns raw Jigsaw into deduplicated, iteratively-stratified, locked train/test/fold splits with a `data_version` hash, plus label constants, the model output contract, seed hygiene, and an executable leakage-firewall gate.

**Architecture:** Pure Python, no cloud, no model training. Every unit runs and tests on a laptop against a small committed synthetic fixture. Dedup runs before any split. The 15% test set is locked once with a fixed seed. TF-IDF is not fit here (that lives inside the Phase 1 CV pipeline); Phase 0 only guarantees the split is clean and reproducible.

**Tech Stack:** Python 3.11, pandas, numpy, scipy, scikit-learn, iterative-stratification, datasketch (MinHash LSH), pydantic, pytest, ruff.

## Global Constraints

Inherited from the master roadmap (`2026-07-01-toxic-moderation-master-plan.md`). The ones that bind Phase 0:

- Labels ordered exactly: `toxic`, `severe_toxic`, `obscene`, `threat`, `insult`, `identity_hate`.
- Near-duplicate dedup before any split. Lock 15% held-out test with a fixed seed and iterative multi-label stratification. Every label including `threat` appears in every fold and in the test set. Determinism: same seed reproduces identical splits and `data_version`.
- Pinned dependencies (exact `==` pins; regenerate a hashed lock with pip-compile before Phase 4).
- Feature-branch + PR, human author (`rocklambros <rock@rockcyber.com>`), no AI attribution in commits or docs.

**Branch:** `feat/phase-0-data-firewall` off `main`.

## File Structure

- `pyproject.toml` — project metadata, ruff, pytest config.
- `requirements/base.txt`, `requirements/dev.txt` — pinned deps.
- `Makefile` — `lint`, `test`, `data`.
- `.env.example` — placeholder config.
- `model/__init__.py`, `model/data/__init__.py` — empty, no heavy imports.
- `model/labels.py` — `LABELS`.
- `model/data/load.py` — `load_raw`.
- `model/data/dedup.py` — `normalize`, `dedup`.
- `model/data/split.py` — `make_splits`.
- `model/seeds.py` — `set_all_seeds`, `run_metadata`.
- `model/contract.py` — `LabelScore`, `PredictionResponse`.
- `model/data/prepare.py` — `SplitConfig`, `DatasetBundle`, `prepare_dataset`.
- `model/data/firewall_check.py` — `assert_no_leakage`.
- `model/data/run.py` — CLI entrypoint for `make data`.
- `tests/fixtures/make_mini.py` — deterministic fixture builder.
- `tests/fixtures/mini_jigsaw.csv` — generated fixture (committed).
- `tests/unit/test_*.py` — one per module.

## Interfaces Produced (consumed by Phase 1+)

```python
LABELS: tuple[str, ...]
load_raw(csv_path: Path) -> pd.DataFrame
normalize(text: str) -> str
dedup(df: pd.DataFrame, threshold: float = 0.9, num_perm: int = 64) -> pd.DataFrame
make_splits(df, seed, test_size=0.15, n_folds=5) -> tuple[pd.DataFrame, pd.DataFrame, list[tuple[np.ndarray, np.ndarray]]]
set_all_seeds(seed: int) -> None
run_metadata(seed: int, data_version: str | None = None) -> dict
class LabelScore(BaseModel): prob: float; flag: bool
class PredictionResponse(BaseModel): request_id, model_version, labels, decision, max_prob, latency_ms
@dataclass(frozen=True) class SplitConfig: seed=42; test_size=0.15; n_folds=5
@dataclass(frozen=True) class DatasetBundle: train_df; test_df; fold_indices; data_version
prepare_dataset(raw_csv: Path, config: SplitConfig = SplitConfig()) -> DatasetBundle
assert_no_leakage(bundle: DatasetBundle) -> None
```

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, `Makefile`, `.env.example`, `model/__init__.py`, `model/data/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`
- Test: `tests/unit/test_imports.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_imports.py`:
```python
def test_package_imports_cleanly():
    import model  # noqa: F401
    import model.data  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_imports.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model'`

- [ ] **Step 3: Create the scaffold**

`pyproject.toml`:
```toml
[project]
name = "mlops-toxic-moderation"
version = "0.1.0"
requires-python = ">=3.11,<3.12"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
markers = ["integration: needs external services (deselect with -m 'not integration')"]
```

`requirements/base.txt` (exact pins; regenerate a hashed lock with pip-compile before Phase 4):
```
numpy==2.1.3
pandas==2.2.3
scipy==1.14.1
scikit-learn==1.5.2
skops==0.11.0
iterative-stratification==0.1.9
datasketch==1.6.5
pydantic==2.9.2
```

`requirements/dev.txt`:
```
-r base.txt
pytest==8.3.3
ruff==0.7.4
```

`Makefile` (tabs, not spaces, for recipe lines):
```makefile
.PHONY: lint test data
lint:
	ruff check .
test:
	PYTHONHASHSEED=0 pytest -m "not integration"
data:
	PYTHONHASHSEED=0 python -m model.data.run --csv tests/fixtures/mini_jigsaw.csv
```

`.env.example`:
```
# Phase 0 needs no secrets. Later phases add W&B, RunPod, AWS, and the RDS DSN here.
DATA_SEED=42
```

Create empty `model/__init__.py`, `model/data/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`.

- [ ] **Step 4: Install deps and verify test passes**

Run: `python -m pip install -r requirements/dev.txt && pytest tests/unit/test_imports.py -v && ruff check .`
Expected: test PASS, ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml requirements Makefile .env.example model tests
git commit -m "Scaffold Phase 0 package, pinned deps, and tooling config"
```

---

### Task 2: Label constants

**Files:**
- Create: `model/labels.py`
- Test: `tests/unit/test_labels.py`

**Interfaces produced:** `LABELS: tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_labels.py`:
```python
from model.labels import LABELS


def test_labels_exact_order_and_count():
    assert LABELS == (
        "toxic",
        "severe_toxic",
        "obscene",
        "threat",
        "insult",
        "identity_hate",
    )
    assert len(LABELS) == 6


def test_labels_is_immutable_tuple():
    assert isinstance(LABELS, tuple)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_labels.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.labels'`

- [ ] **Step 3: Write minimal implementation**

`model/labels.py`:
```python
"""Single source of truth for the six toxicity labels, in fixed order."""

LABELS: tuple[str, ...] = (
    "toxic",
    "severe_toxic",
    "obscene",
    "threat",
    "insult",
    "identity_hate",
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_labels.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add model/labels.py tests/unit/test_labels.py
git commit -m "Add ordered toxicity label constants"
```

---

### Task 3: Synthetic fixture builder

**Files:**
- Create: `tests/fixtures/make_mini.py`, `tests/fixtures/mini_jigsaw.csv` (generated), `tests/fixtures/__init__.py`
- Test: `tests/unit/test_fixture.py`

The fixture holds about 30 rows. Each label carries at least six positives (so five-fold stratification can place one positive per fold). It plants one exact duplicate and one near-duplicate so the dedup tests have something to collapse.

- [ ] **Step 1: Write the fixture builder**

`tests/fixtures/make_mini.py`:
```python
"""Deterministic builder for the mini Jigsaw fixture. Run to regenerate the CSV.

Usage: python tests/fixtures/make_mini.py
"""

import csv
from pathlib import Path

from model.labels import LABELS

# Each entry: (comment_text, set-of-positive-labels)
BASE: list[tuple[str, set[str]]] = [
    ("have a nice day friend", set()),
    ("thanks for the thoughtful edit", set()),
    ("i disagree but respect your point", set()),
    ("great work on the article", set()),
    ("the weather is lovely today", set()),
    ("you are an idiot", {"toxic", "insult"}),
    ("what a moron you are", {"toxic", "insult"}),
    ("shut up you fool", {"toxic", "insult"}),
    ("you clueless clown", {"toxic", "insult"}),
    ("absolute imbecile", {"toxic", "insult"}),
    ("you stupid loser", {"toxic", "insult"}),
    ("f*ck this garbage", {"toxic", "obscene"}),
    ("this is sh!t", {"toxic", "obscene"}),
    ("what the hell is this crap", {"toxic", "obscene"}),
    ("total bullsh1t", {"toxic", "obscene"}),
    ("piece of cr@p edit", {"toxic", "obscene"}),
    ("obscene filth everywhere", {"toxic", "obscene"}),
    ("i will kill you", {"toxic", "threat", "severe_toxic"}),
    ("i am going to hurt you", {"toxic", "threat"}),
    ("watch your back i am coming", {"toxic", "threat"}),
    ("you will pay with your life", {"toxic", "threat", "severe_toxic"}),
    ("i will find you and end you", {"toxic", "threat", "severe_toxic"}),
    ("death to you and yours", {"toxic", "threat", "severe_toxic"}),
    ("people of that group are subhuman", {"toxic", "identity_hate"}),
    ("i hate everyone of your race", {"toxic", "identity_hate"}),
    ("your religion makes you worthless", {"toxic", "identity_hate"}),
    ("go back to where you came from", {"toxic", "identity_hate"}),
    ("your kind does not belong here", {"toxic", "identity_hate"}),
    ("slur against your ethnicity", {"toxic", "identity_hate", "severe_toxic"}),
    ("you vile disgusting worthless scum", {"toxic", "severe_toxic", "insult"}),
]

# Planted duplicate and near-duplicate of row index 5.
PLANTS: list[tuple[str, set[str]]] = [
    ("you are an idiot", {"toxic", "insult"}),          # exact duplicate
    ("You  are an   IDIOT", {"toxic", "insult"}),       # near-duplicate (case + whitespace)
]


def build_rows() -> list[dict]:
    rows = []
    for i, (text, positives) in enumerate(BASE + PLANTS):
        row = {"id": f"c{i:03d}", "comment_text": text}
        for label in LABELS:
            row[label] = 1 if label in positives else 0
        rows.append(row)
    return rows


def main() -> None:
    out = Path(__file__).parent / "mini_jigsaw.csv"
    rows = build_rows()
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "comment_text", *LABELS])
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the fixture and write its verification test**

Run: `touch tests/fixtures/__init__.py && python tests/fixtures/make_mini.py`
Expected: `wrote 32 rows to .../mini_jigsaw.csv`

`tests/unit/test_fixture.py`:
```python
from pathlib import Path

import pandas as pd

from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_fixture_exists_and_covers_all_labels():
    df = pd.read_csv(FIXTURE)
    assert set(["id", "comment_text", *LABELS]).issubset(df.columns)
    for label in LABELS:
        assert df[label].sum() >= 6, f"{label} needs >= 6 positives for 5-fold stratification"
```

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/unit/test_fixture.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/make_mini.py tests/fixtures/mini_jigsaw.csv tests/fixtures/__init__.py tests/unit/test_fixture.py
git commit -m "Add deterministic mini Jigsaw fixture with planted duplicates"
```

---

### Task 4: Raw loader with validation

**Files:**
- Create: `model/data/load.py`
- Test: `tests/unit/test_load.py`

**Interfaces produced:** `load_raw(csv_path: Path) -> pd.DataFrame`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_load.py`:
```python
from pathlib import Path

import pandas as pd
import pytest

from model.data.load import load_raw
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_load_raw_returns_required_columns():
    df = load_raw(FIXTURE)
    assert list(df.columns) == ["id", "comment_text", *LABELS]
    assert len(df) == 32


def test_load_raw_rejects_missing_column(tmp_path):
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"id": [1], "comment_text": ["hi"]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="missing columns"):
        load_raw(bad)


def test_load_raw_rejects_label_out_of_range(tmp_path):
    bad = tmp_path / "bad.csv"
    row = {"id": [1], "comment_text": ["hi"]}
    for label in LABELS:
        row[label] = [0]
    row["toxic"] = [2]  # invalid
    pd.DataFrame(row).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="outside"):
        load_raw(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_load.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.data.load'`

- [ ] **Step 3: Write minimal implementation**

`model/data/load.py`:
```python
"""Load and validate the raw Jigsaw CSV."""

from pathlib import Path

import pandas as pd

from model.labels import LABELS

REQUIRED_COLUMNS: tuple[str, ...] = ("id", "comment_text", *LABELS)


def load_raw(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    if df["comment_text"].isna().any():
        raise ValueError("comment_text contains nulls")
    for label in LABELS:
        col = df[label]
        if col.isna().any():
            raise ValueError(f"label {label} contains nulls")
        if not col.isin((0, 1)).all():
            raise ValueError(f"label {label} has values outside {{0, 1}}")
    return df[list(REQUIRED_COLUMNS)].copy()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_load.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add model/data/load.py tests/unit/test_load.py
git commit -m "Add raw Jigsaw loader with column and range validation"
```

---

### Task 5: Near-duplicate dedup (before any split)

**Files:**
- Create: `model/data/dedup.py`
- Test: `tests/unit/test_dedup.py`

**Interfaces produced:** `normalize(text)`, `dedup(df, threshold=0.9, num_perm=64)`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_dedup.py`:
```python
from pathlib import Path

from model.data.dedup import dedup, normalize
from model.data.load import load_raw

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_normalize_collapses_case_and_whitespace():
    assert normalize("You  are an   IDIOT") == "you are an idiot"


def test_dedup_removes_exact_and_near_duplicates():
    df = load_raw(FIXTURE)
    out = dedup(df)
    # The fixture plants one exact and one near-duplicate of "you are an idiot".
    assert len(out) == len(df) - 2
    texts = [normalize(t) for t in out["comment_text"]]
    assert texts.count("you are an idiot") == 1


def test_dedup_is_idempotent():
    df = load_raw(FIXTURE)
    once = dedup(df)
    twice = dedup(once)
    assert list(once["id"]) == list(twice["id"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_dedup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.data.dedup'`

- [ ] **Step 3: Write minimal implementation**

`model/data/dedup.py`:
```python
"""Deterministic near-duplicate dedup. Runs before any split (leakage firewall)."""

import re
import unicodedata

import pandas as pd
from datasketch import MinHash, MinHashLSH

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text)).lower().strip()
    return _WS.sub(" ", text)


def _shingles(text: str, k: int = 5) -> set[str]:
    if len(text) <= k:
        return {text} if text else set()
    return {text[i : i + k] for i in range(len(text) - k + 1)}


def _minhash(text: str, num_perm: int) -> MinHash:
    m = MinHash(num_perm=num_perm)
    for sh in _shingles(text):
        m.update(sh.encode("utf-8"))
    return m


def dedup(df: pd.DataFrame, threshold: float = 0.9, num_perm: int = 64) -> pd.DataFrame:
    work = df.copy()
    work["_norm"] = work["comment_text"].map(normalize)
    # Exact-normalized dedup first; sort by id so "keep first" is deterministic.
    work = work.sort_values("id").drop_duplicates("_norm", keep="first")
    # Near-duplicate collapse via MinHash LSH over char shingles.
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    keep_ids: list[str] = []
    for row_id, norm in zip(work["id"], work["_norm"], strict=True):
        m = _minhash(norm, num_perm)
        if lsh.query(m):
            continue  # near-duplicate of a row already kept
        lsh.insert(str(row_id), m)
        keep_ids.append(row_id)
    return (
        work[work["id"].isin(keep_ids)]
        .drop(columns="_norm")
        .reset_index(drop=True)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_dedup.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add model/data/dedup.py tests/unit/test_dedup.py
git commit -m "Add deterministic near-duplicate dedup before split"
```

---

### Task 6: Iterative multi-label stratified split

**Files:**
- Create: `model/data/split.py`
- Test: `tests/unit/test_split.py`

**Interfaces produced:** `make_splits(df, seed, test_size=0.15, n_folds=5)`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_split.py`:
```python
from pathlib import Path

from model.data.dedup import dedup
from model.data.load import load_raw
from model.data.split import make_splits
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def _clean():
    return dedup(load_raw(FIXTURE))


def test_test_set_is_disjoint_from_train():
    train_df, test_df, _ = make_splits(_clean(), seed=42, n_folds=5)
    assert set(train_df["id"]).isdisjoint(set(test_df["id"]))


def test_every_label_present_in_test_and_every_fold():
    train_df, test_df, folds = make_splits(_clean(), seed=42, n_folds=5)
    for label in LABELS:
        assert test_df[label].sum() >= 1, f"{label} missing from test"
    ytr = train_df[list(LABELS)].to_numpy()
    for _, val_idx in folds:
        fold_positives = ytr[val_idx].sum(axis=0)
        assert (fold_positives >= 1).all(), "a label is missing from a validation fold"


def test_split_is_deterministic_for_fixed_seed():
    a_train, a_test, _ = make_splits(_clean(), seed=42, n_folds=5)
    b_train, b_test, _ = make_splits(_clean(), seed=42, n_folds=5)
    assert list(a_test["id"]) == list(b_test["id"])
    assert list(a_train["id"]) == list(b_train["id"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_split.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.data.split'`

- [ ] **Step 3: Write minimal implementation**

`model/data/split.py`:
```python
"""Iterative multi-label stratified split: locked test set plus CV folds."""

import numpy as np
import pandas as pd
from iterstrat.ml_stratifiers import (
    MultilabelStratifiedKFold,
    MultilabelStratifiedShuffleSplit,
)

from model.labels import LABELS


def make_splits(
    df: pd.DataFrame,
    seed: int,
    test_size: float = 0.15,
    n_folds: int = 5,
) -> tuple[pd.DataFrame, pd.DataFrame, list[tuple[np.ndarray, np.ndarray]]]:
    df = df.reset_index(drop=True)
    y = df[list(LABELS)].to_numpy()
    x = np.zeros((len(df), 1))

    msss = MultilabelStratifiedShuffleSplit(
        n_splits=1, test_size=test_size, random_state=seed
    )
    train_idx, test_idx = next(msss.split(x, y))
    train_df = df.iloc[train_idx].reset_index(drop=True)
    test_df = df.iloc[test_idx].reset_index(drop=True)

    y_train = train_df[list(LABELS)].to_numpy()
    mskf = MultilabelStratifiedKFold(
        n_splits=n_folds, shuffle=True, random_state=seed
    )
    fold_indices = [
        (tr, va)
        for tr, va in mskf.split(np.zeros((len(train_df), 1)), y_train)
    ]
    return train_df, test_df, fold_indices
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_split.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add model/data/split.py tests/unit/test_split.py
git commit -m "Add iterative multi-label stratified split with locked test set"
```

---

### Task 7: Seed hygiene and run metadata

**Files:**
- Create: `model/seeds.py`
- Test: `tests/unit/test_seeds.py`

**Interfaces produced:** `set_all_seeds(seed)`, `run_metadata(seed, data_version=None)`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_seeds.py`:
```python
import numpy as np

from model.seeds import run_metadata, set_all_seeds


def test_set_all_seeds_makes_numpy_deterministic():
    set_all_seeds(123)
    a = np.random.rand(5)
    set_all_seeds(123)
    b = np.random.rand(5)
    assert np.array_equal(a, b)


def test_run_metadata_carries_git_sha_and_seed():
    meta = run_metadata(seed=7, data_version="abc")
    assert "git_sha" in meta
    assert meta["seed"] == 7
    assert meta["data_version"] == "abc"
    assert "timestamp_utc" in meta
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_seeds.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.seeds'`

- [ ] **Step 3: Write minimal implementation**

`model/seeds.py`:
```python
"""Seed hygiene and reproducibility metadata.

Note: PYTHONHASHSEED is read by the interpreter at startup, so setting it here
records intent but does not change the current process. The Makefile sets it in
the environment for real determinism.
"""

import datetime as dt
import os
import random
import subprocess

import numpy as np


def set_all_seeds(seed: int) -> None:
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch  # guarded: torch is a build-time dependency only

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def run_metadata(seed: int, data_version: str | None = None) -> dict:
    return {
        "git_sha": _git_sha(),
        "seed": seed,
        "data_version": data_version,
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_seeds.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add model/seeds.py tests/unit/test_seeds.py
git commit -m "Add seed hygiene and reproducibility metadata"
```

---

### Task 8: Output contract types

**Files:**
- Create: `model/contract.py`
- Test: `tests/unit/test_contract.py`

**Interfaces produced:** `LabelScore`, `PredictionResponse`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_contract.py`:
```python
import pytest
from pydantic import ValidationError

from model.contract import PredictionResponse
from model.labels import LABELS


def _valid_payload():
    return {
        "request_id": "uuid",
        "model_version": "toxic-clf:v3@sha256:abcd",
        "labels": {label: {"prob": 0.1, "flag": False} for label in LABELS},
        "decision": "allow",
        "max_prob": 0.1,
        "latency_ms": 42,
    }


def test_valid_payload_parses():
    resp = PredictionResponse(**_valid_payload())
    assert set(resp.labels.keys()) == set(LABELS)


def test_rejects_unknown_decision():
    payload = _valid_payload()
    payload["decision"] = "delete"
    with pytest.raises(ValidationError):
        PredictionResponse(**payload)


def test_rejects_wrong_label_keys():
    payload = _valid_payload()
    payload["labels"].pop("threat")
    with pytest.raises(ValidationError):
        PredictionResponse(**payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.contract'`

- [ ] **Step 3: Write minimal implementation**

`model/contract.py`:
```python
"""Stable model output contract. The DB and UI never change when the model swaps."""

from typing import Literal

from pydantic import BaseModel, model_validator

from model.labels import LABELS


class LabelScore(BaseModel):
    prob: float
    flag: bool


class PredictionResponse(BaseModel):
    request_id: str
    model_version: str
    labels: dict[str, LabelScore]
    decision: Literal["allow", "review", "block"]
    max_prob: float
    latency_ms: int

    @model_validator(mode="after")
    def _labels_match_constant(self) -> "PredictionResponse":
        if set(self.labels.keys()) != set(LABELS):
            raise ValueError(f"labels keys must equal {LABELS}")
        return self
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_contract.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add model/contract.py tests/unit/test_contract.py
git commit -m "Add stable prediction output contract"
```

---

### Task 9: Prepare orchestration, data_version, and firewall gate

**Files:**
- Create: `model/data/prepare.py`, `model/data/firewall_check.py`, `model/data/run.py`
- Test: `tests/unit/test_prepare.py`, `tests/unit/test_firewall.py`

**Interfaces produced:** `SplitConfig`, `DatasetBundle`, `prepare_dataset`, `assert_no_leakage`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_prepare.py`:
```python
from pathlib import Path

from model.data.prepare import SplitConfig, prepare_dataset

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_prepare_is_deterministic():
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42, n_folds=5))
    b = prepare_dataset(FIXTURE, SplitConfig(seed=42, n_folds=5))
    assert a.data_version == b.data_version
    assert list(a.test_df["id"]) == list(b.test_df["id"])


def test_data_version_changes_with_seed():
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42, n_folds=5))
    b = prepare_dataset(FIXTURE, SplitConfig(seed=7, n_folds=5))
    assert a.data_version != b.data_version
```

`tests/unit/test_firewall.py`:
```python
from pathlib import Path

import pytest

from model.data.firewall_check import assert_no_leakage
from model.data.prepare import DatasetBundle, SplitConfig, prepare_dataset

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_clean_bundle_passes():
    bundle = prepare_dataset(FIXTURE, SplitConfig(seed=42, n_folds=5))
    assert_no_leakage(bundle)  # must not raise


def test_injected_id_overlap_is_caught():
    bundle = prepare_dataset(FIXTURE, SplitConfig(seed=42, n_folds=5))
    leaked = DatasetBundle(
        train_df=bundle.train_df,
        test_df=bundle.train_df.iloc[:1].copy(),  # a train row now also in test
        fold_indices=bundle.fold_indices,
        data_version=bundle.data_version,
    )
    with pytest.raises(AssertionError, match="overlap"):
        assert_no_leakage(leaked)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_prepare.py tests/unit/test_firewall.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'model.data.prepare'`

- [ ] **Step 3: Write minimal implementation**

`model/data/prepare.py`:
```python
"""Orchestrate load -> dedup -> split and compute a reproducible data_version."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from model.data.dedup import dedup
from model.data.load import load_raw
from model.data.split import make_splits


@dataclass(frozen=True)
class SplitConfig:
    seed: int = 42
    test_size: float = 0.15
    n_folds: int = 5


@dataclass(frozen=True)
class DatasetBundle:
    train_df: pd.DataFrame
    test_df: pd.DataFrame
    fold_indices: list[tuple[np.ndarray, np.ndarray]]
    data_version: str


def _data_version(deduped: pd.DataFrame, config: SplitConfig) -> str:
    ids = sorted(str(x) for x in deduped["id"].tolist())
    payload = json.dumps(
        {
            "ids": ids,
            "seed": config.seed,
            "test_size": config.test_size,
            "n_folds": config.n_folds,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def prepare_dataset(raw_csv: Path, config: SplitConfig = SplitConfig()) -> DatasetBundle:
    deduped = dedup(load_raw(raw_csv))
    version = _data_version(deduped, config)
    train_df, test_df, folds = make_splits(
        deduped,
        seed=config.seed,
        test_size=config.test_size,
        n_folds=config.n_folds,
    )
    return DatasetBundle(train_df, test_df, folds, version)
```

`model/data/firewall_check.py`:
```python
"""Executable leakage-firewall gate. Fails loudly on any train/test contamination."""

from model.data.dedup import normalize
from model.data.prepare import DatasetBundle


def assert_no_leakage(bundle: DatasetBundle) -> None:
    train_ids = set(bundle.train_df["id"])
    test_ids = set(bundle.test_df["id"])
    overlap = train_ids & test_ids
    if overlap:
        raise AssertionError(f"train/test id overlap: {sorted(overlap)[:5]}")

    train_norm = {normalize(t) for t in bundle.train_df["comment_text"]}
    test_norm = {normalize(t) for t in bundle.test_df["comment_text"]}
    text_leak = train_norm & test_norm
    if text_leak:
        raise AssertionError(f"normalized text leak across split: {len(text_leak)} rows")
```

`model/data/run.py`:
```python
"""CLI: prepare the dataset, run the firewall gate, print the data_version."""

import argparse
from pathlib import Path

from model.data.firewall_check import assert_no_leakage
from model.data.prepare import SplitConfig, prepare_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    bundle = prepare_dataset(args.csv, SplitConfig(seed=args.seed))
    assert_no_leakage(bundle)
    print(f"data_version={bundle.data_version}")
    print(f"train={len(bundle.train_df)} test={len(bundle.test_df)} folds={len(bundle.fold_indices)}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_prepare.py tests/unit/test_firewall.py -v`
Expected: 4 PASS

- [ ] **Step 5: Verify the `make data` gate runs end to end**

Run: `make data`
Expected: prints `data_version=<64-hex>` and `train=... test=... folds=5`, exits 0.

- [ ] **Step 6: Commit**

```bash
git add model/data/prepare.py model/data/firewall_check.py model/data/run.py tests/unit/test_prepare.py tests/unit/test_firewall.py
git commit -m "Add prepare orchestration, data_version, and firewall gate"
```

---

### Task 10: Phase 0 gate and PR

- [ ] **Step 1: Full suite and lint green**

Run: `make test && make lint`
Expected: all unit tests PASS, ruff reports no errors.

- [ ] **Step 2: Reproducibility check**

Run: `make data && make data`
Expected: identical `data_version` on both runs.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/phase-0-data-firewall
gh pr create --base main --title "Phase 0: data pipeline and leakage firewall" \
  --body "Deterministic load/dedup/split, locked 15% test, seed hygiene, output contract, executable firewall gate. All unit tests green, ruff clean, data_version reproducible."
```

## Self-Review

**Spec coverage (spec section 6, the firewall):** near-dup dedup before split (Task 5), locked 15% test with fixed seed and iterative multi-label stratification (Task 6), every label in every fold (Task 6 test), determinism / seed hygiene + git SHA (Tasks 6, 7, 9), executable firewall gate (Task 9). TF-IDF-inside-CV is a Phase 1 obligation, noted in the master roadmap. Output contract (spec section 5) is Task 8.

**Placeholder scan:** every step carries real code and an exact command. No TODO, no "handle edge cases," no "similar to."

**Type consistency:** `LABELS` (tuple) used identically across load, dedup, split, contract, prepare. `make_splits` returns `(train_df, test_df, fold_indices)`, consumed unchanged by `prepare_dataset`. `DatasetBundle` fields match the master roadmap interface block. `prepare_dataset` signature matches the interface contract.

## Execution Handoff

Two options:
1. **Subagent-Driven (recommended):** fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.
