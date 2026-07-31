"""Orchestrate load -> dedup -> split, and compute three separate version fields.

One `data_version` string could not answer the question anyone actually asks when a number
moves: did the corpus change, did the split change, or did the environment change? The
three fields are logged to W&B separately.

  raw_sha256    the bytes of the CSV as delivered by Kaggle
  split_version the realized train/test/fold membership plus per-id label content
  env_version   the pinned libraries and the dedup/normalizer parameters
"""

import hashlib
import json
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import numpy as np
import pandas as pd

from model.data.dedup import DEDUP_JACCARD, LSH_BANDS, LSH_ROWS, dedup
from model.data.load import load_raw
from model.data.provenance import sha256_file
from model.data.shingles import NUM_PERM, SHINGLE_K
from model.data.split import make_splits
from model.labels import LABELS
from model.normalize import CORPUS_NORMALIZER_ID

_WEIGHTS = (1 << np.arange(len(LABELS) - 1, -1, -1)).astype(np.uint16)
_BIT_PATTERNS = np.array([format(i, f"0{len(LABELS)}b") for i in range(2 ** len(LABELS))])


@dataclass(frozen=True)
class SplitConfig:
    seed: int = 42
    test_size: float = 0.15
    n_folds: int = 5


@dataclass(frozen=True, eq=False)
class DatasetBundle:
    # eq=False: the generated __eq__ would compare DataFrames elementwise and then call
    # bool() on the result, raising "truth value of a DataFrame is ambiguous".
    train_df: pd.DataFrame
    test_df: pd.DataFrame
    fold_indices: list[tuple[np.ndarray, np.ndarray]]
    raw_sha256: str
    split_version: str
    env_version: str
    config: SplitConfig = field(default_factory=SplitConfig)

    @property
    def data_version(self) -> str:
        """Composite for single-string display. The three fields are the source of truth."""
        joined = f"{self.raw_sha256}:{self.split_version}:{self.env_version}"
        return hashlib.sha256(joined.encode()).hexdigest()


DEFAULT_SPLIT = SplitConfig()


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def label_fingerprint(df: pd.DataFrame) -> list[str]:
    """Vectorized (id, label-vector) fingerprint. No iterrows: it is O(n) Python calls."""
    ids = df["id"].to_numpy(dtype=str)
    packed = df[list(LABELS)].to_numpy(dtype=np.uint16) @ _WEIGHTS
    codes = _BIT_PATTERNS[packed]
    pairs = np.char.add(np.char.add(ids, ":"), codes)
    pairs.sort()
    return pairs.tolist()


def compute_env_version() -> str:
    payload = json.dumps(
        {
            "packages": {
                "numpy": np.__version__,
                "pandas": pd.__version__,
                "scikit-learn": _pkg_version("scikit-learn"),
                "iterative-stratification": _pkg_version("iterative-stratification"),
                "datasketch": _pkg_version("datasketch"),
            },
            "dedup": {
                "shingle_k": SHINGLE_K,
                "num_perm": NUM_PERM,
                "lsh_bands": LSH_BANDS,
                "lsh_rows": LSH_ROWS,
                "jaccard": DEDUP_JACCARD,
            },
            "normalizer": CORPUS_NORMALIZER_ID,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_split_version(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    folds: list[tuple[np.ndarray, np.ndarray]],
    config: SplitConfig,
) -> str:
    payload = json.dumps(
        {
            "train": label_fingerprint(train_df),
            "test": label_fingerprint(test_df),
            "folds": [sorted(int(i) for i in val_idx) for _, val_idx in folds],
            "config": {
                "seed": config.seed,
                "test_size": config.test_size,
                "n_folds": config.n_folds,
            },
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def prepare_dataset(raw_csv: Path, config: SplitConfig = DEFAULT_SPLIT) -> DatasetBundle:
    raw_sha256 = sha256_file(raw_csv)
    deduped = dedup(load_raw(raw_csv))
    train_df, test_df, folds = make_splits(
        deduped, seed=config.seed, test_size=config.test_size, n_folds=config.n_folds
    )
    return DatasetBundle(
        train_df=train_df,
        test_df=test_df,
        fold_indices=folds,
        raw_sha256=raw_sha256,
        split_version=compute_split_version(train_df, test_df, folds, config),
        env_version=compute_env_version(),
        config=config,
    )
